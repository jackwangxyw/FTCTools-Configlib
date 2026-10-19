"""Sequential Bridging: bridge layers above counterbore floors, so a
counterbored hole prints cleanly facing the bed.

Above each picked counterbore floor, the first layer cuts a slot as wide as
the hole across the counterbore (leaving two bridges), the second a square
(bridges the other way), then an octagon and a 16-gon. The round hole then
starts on solid layers.

The feature is a Fusion custom feature grouping two native features:
    1. a base feature holding one solid: the bridge layers of every hole
    2. a combine that cuts that solid from the holes' body
Layer height and levels are custom parameters, the counterbore floors are
dependencies. The compute rebuilds the solid in place with
BaseFeature.updateBody, which keeps the body's identity so the combine never
loses its tool.
"""

import json
import math
import os
import traceback

import adsk.core
import adsk.fusion

from .. import links, panel
from ..lighten import geometry as lighten_geometry

app = adsk.core.Application.get()
ui = app.userInterface

CMD_ID = 'FTCTools_Bridging'
EDIT_CMD_ID = 'FTCTools_BridgingEdit'
FEATURE_ID = 'FTCToolsBridging'
NAME = 'Sequential Bridging'
ICONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources')

SETTINGS_PATH = os.path.join(os.getenv('APPDATA') or os.path.expanduser('~'), 'FTCTools', 'bridging.json')
DEFAULTS = {'layer': '0.2 mm', 'levels': 2}
MAX_LEVELS = 4

# The solid starts this far above the floor so the cut has no coplanar faces.
LIFT = 0.001  # cm
TOL = 1e-4  # cm, for matching circle centers and radii
OVERLAY_COLOR = (255, 255, 255)  # preview outline, as Lighten
OVERLAY_WEIGHT = 2  # a bit thicker than Lighten's: these outlines sit down in small counterbores
OVERLAY_LIFT = 0.01  # cm above the floor, as Lighten lifts its outline

_handlers = []
_feature_def = None
_edit = {}
_overlay = []


class BridgingError(Exception):
    """An input problem the user can fix. message_id is the Fusion status message to show on the feature."""

    def __init__(self, message, message_id='API_COMPUTE_ERROR'):
        super().__init__(message)
        self.message_id = message_id


# ----------------------------------------------------------------- lifecycle

def start():
    global _feature_def
    create_def = ui.commandDefinitions.addButtonDefinition(
        CMD_ID, NAME, 'Bridge layers above counterbore floors, so the holes print facing the bed without support.', ICONS)
    _on(create_def.commandCreated, adsk.core.CommandCreatedEventHandler, _create_created)

    edit_def = ui.commandDefinitions.addButtonDefinition(EDIT_CMD_ID, 'Edit ' + NAME, 'Edit a Sequential Bridging feature.', ICONS)
    _on(edit_def.commandCreated, adsk.core.CommandCreatedEventHandler, _edit_created)

    _feature_def = adsk.fusion.CustomFeatureDefinition.create(FEATURE_ID, NAME, ICONS)
    _feature_def.editCommandId = EDIT_CMD_ID
    _on(_feature_def.customFeatureCompute, adsk.fusion.CustomFeatureEventHandler, _compute)

    control = panel.get().controls.addCommand(create_def)
    control.isPromotedByDefault = True
    control.isPromoted = True


def stop():
    _clear_overlay()
    control = panel.get().controls.itemById(CMD_ID)
    if control:
        control.deleteMe()
    panel.remove_if_empty()
    for cmd_id in (CMD_ID, EDIT_CMD_ID):
        cmd_def = ui.commandDefinitions.itemById(cmd_id)
        if cmd_def:
            cmd_def.deleteMe()
    _handlers.clear()


def _on(event, handler_base, fn):
    """Subscribe fn to a Fusion event. Unhandled errors are shown, never dropped."""
    class Handler(handler_base):
        def notify(self, args):
            try:
                fn(args)
            except Exception:
                ui.messageBox(NAME + ' failed:\n' + traceback.format_exc())
    handler = Handler()
    event.add(handler)
    _handlers.append(handler)


# ------------------------------------------------------------------ settings

def _load_settings():
    values = dict(DEFAULTS)
    if os.path.exists(SETTINGS_PATH):
        with open(SETTINGS_PATH, 'r', encoding='utf-8') as f:
            values.update(json.load(f))
    return values


def _save_settings(inputs):
    values = {'layer': inputs.itemById('layer').expression, 'levels': inputs.itemById('levels').value}
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, 'w', encoding='utf-8') as f:
        json.dump(values, f, indent=2)


# -------------------------------------------------------------------- dialog

def _design():
    return adsk.fusion.Design.cast(app.activeProduct)


def _build_inputs(cmd, values):
    inputs = cmd.commandInputs
    units = _design().unitsManager.defaultLengthUnits
    sel = inputs.addSelectionInput('holes', 'Holes', 'Counterbored holes: the floor or a wall')
    sel.addSelectionFilter('Faces')
    sel.setSelectionLimits(1, 0)
    inputs.addIntegerSpinnerCommandInput('levels', 'Levels', 1, MAX_LEVELS, 1, int(values['levels']))
    inputs.itemById('levels').tooltip = '1 slot, 2 adds a square, 3 an octagon, 4 a 16-gon.'
    inputs.addValueInput('layer', 'Layer height', units, adsk.core.ValueInput.createByString(values['layer']))


def _native(entity):
    """The entity in its owning component's space (see lighten.entry._native)."""
    if entity.assemblyContext:
        return entity.nativeObject
    return entity


def _floor_for(entity):
    """The counterbore floor for a picked face (the floor itself, a
    counterbore wall or the hole's wall), in the pick's context, or None if
    it isn't a counterbored hole."""
    native = _native(entity)
    if _hole(native) is not None:
        return entity
    for loop in native.loops:
        for edge, _ in lighten_geometry.loop_coedges(loop):
            for face in edge.faces:
                if face != native and _hole(face) is not None:
                    return face.createForAssemblyContext(entity.assemblyContext) if entity.assemblyContext else face
    return None


def _floors(inputs):
    sel = inputs.itemById('holes')
    return _unique([_floor_for(sel.selection(i).entity) for i in range(sel.selectionCount)])


def _unique(entities):
    """Each floor once. Picks in two occurrences of one part are the same
    floor, and cutting it twice would only do the same cut again."""
    seen = set()
    out = []
    for e in entities:
        token = _native(e).entityToken
        if token not in seen:
            seen.add(token)
            out.append(e)
    return out


def _linked(entity):
    """Whether the entity is inside a part inserted from another design,
    which Fusion can't add features to."""
    occ = entity.assemblyContext
    while occ is not None:
        if occ.isReferencedComponent:
            return True
        occ = occ.assemblyContext
    return False


def _read_inputs(inputs):
    return {'floors': _floors(inputs), 'layer': inputs.itemById('layer').expression,
            'levels': inputs.itemById('levels').value}


def _pre_select(args):
    if args.activeInput.id != 'holes':
        return
    floor = _floor_for(args.selection.entity)
    if floor is None or _linked(floor):
        args.isSelectable = False
        return
    # One feature cuts one body.
    picked = _floors(args.activeInput.parentCommand.commandInputs)
    if picked and _native(picked[0]).body != _native(floor).body:
        args.isSelectable = False


def _validate(args):
    inputs = args.inputs
    layer = inputs.itemById('layer')
    args.areInputsValid = (inputs.itemById('holes').selectionCount > 0 and layer.isValidExpression and layer.value > 0
                           and 1 <= inputs.itemById('levels').value <= MAX_LEVELS)
    if not args.areInputsValid:
        _clear_overlay()


def _connect_dialog(cmd, preview, execute, destroy):
    _on(cmd.validateInputs, adsk.core.ValidateInputsEventHandler, _validate)
    _on(cmd.preSelect, adsk.core.SelectionEventHandler, _pre_select)
    if preview is not None:
        _on(cmd.executePreview, adsk.core.CommandEventHandler, preview)
    _on(cmd.execute, adsk.core.CommandEventHandler, execute)
    _on(cmd.destroy, adsk.core.CommandEventHandler, destroy)


def _preview(args):
    _draw_preview(args.command.commandInputs)


def _draw_preview(inputs):
    """Outline the bridge layers for the current inputs. Inputs that can't be bridged show nothing."""
    _clear_overlay()
    v = _read_inputs(inputs)
    layer = _evaluate(v['layer'])
    try:
        _solid(v['floors'], layer, v['levels'])  # the same checks as OK
    except BridgingError:
        return
    holes = [_hole(_native(floor)) for floor in v['floors']]
    _show_overlay(_native(v['floors'][0]).body.parentComponent,
                  [(hole, _hole_layers(hole, layer, v['levels'])) for hole in holes])


def _show_overlay(comp, layers):
    """Draw each hole's layer outlines flat on its floor, just above it, as
    Lighten draws its pockets on the face: the layers themselves are inside
    the part, where the part hides their edges."""
    points = []
    indices = []
    for (center, normal, _, _), solid in layers:
        for edge in lighten_geometry.body_edges(solid):
            _, t0, t1 = edge.evaluator.getParameterExtents()
            _, strokes = edge.evaluator.getStrokes(t0, t1, 0.002)
            flat = []
            for p in strokes:
                lift = normal.copy()
                lift.scaleBy(OVERLAY_LIFT - center.vectorTo(p).dotProduct(normal))
                p.translateBy(lift)
                flat.append(p)
            if flat[0].distanceTo(flat[-1]) < TOL and len(flat) == 2:
                continue  # an edge straight down, which lands on a point
            start = len(points) // 3
            for p in flat:
                points += [p.x, p.y, p.z]
            for i in range(len(flat) - 1):
                indices += [start + i, start + i + 1]
    group = comp.customGraphicsGroups.add()
    lines = group.addLines(adsk.fusion.CustomGraphicsCoordinates.create(points), indices, False)
    lines.color = adsk.fusion.CustomGraphicsSolidColorEffect.create(adsk.core.Color.create(*OVERLAY_COLOR, 255))
    lines.weight = OVERLAY_WEIGHT
    lines.depthPriority = 1  # above the face's selection highlight
    _overlay.append(group)


def _clear_overlay():
    while _overlay:
        group = _overlay.pop()
        if group.isValid:
            group.deleteMe()


def _evaluate(expression):
    um = _design().unitsManager
    return um.evaluateExpression(expression, um.defaultLengthUnits)


# -------------------------------------------------------------------- create

def _create_created(args):
    cmd = args.command
    _build_inputs(cmd, _load_settings())
    _connect_dialog(cmd, _preview, _create_execute, lambda a: _clear_overlay())


def _create_execute(args):
    inputs = args.command.commandInputs
    _clear_overlay()
    v = _read_inputs(inputs)
    try:
        solid = _solid(v['floors'], _evaluate(v['layer']), v['levels'])
    except BridgingError as e:
        ui.messageBox(str(e), NAME)
        return
    _create_feature(v, solid)
    _save_settings(inputs)


def _create_feature(v, solid):
    body = _native(v['floors'][0]).body
    comp = body.parentComponent
    units = _design().unitsManager.defaultLengthUnits

    base = comp.features.baseFeatures.add()
    base.startEdit()
    comp.bRepBodies.add(solid, base)
    base.finishEdit()
    base.name = 'Bridge Layers'

    # The cut splits each floor in two, after which the picked floor no longer
    # exists and Fusion rejects it as a dependency. So the feature is made
    # around the base feature alone while the floors are whole, and takes in
    # the cut afterwards.
    cf_input = comp.features.customFeatures.createInput(_feature_def)
    cf_input.addCustomParameter('layer', 'LayerHeight', adsk.core.ValueInput.createByString(v['layer']), units, True)
    cf_input.addCustomParameter('levels', 'Levels', adsk.core.ValueInput.createByReal(v['levels']), '', True)
    _add_dependencies(cf_input.addDependency, v['floors'])
    cf_input.setStartAndEndFeatures(base, base)
    feature = comp.features.customFeatures.add(cf_input)

    tools = adsk.core.ObjectCollection.create()
    tools.add(base.bodies.item(0))
    combine_input = comp.features.combineFeatures.createInput(body, tools)
    combine_input.operation = adsk.fusion.FeatureOperations.CutFeatureOperation
    combine_input.isKeepToolBodies = False
    combine = comp.features.combineFeatures.add(combine_input)
    combine.name = 'Bridge Cut'
    # Fusion only changes a feature's range with the timeline rolled back before it.
    feature.timelineObject.rollTo(True)
    feature.setStartAndEndFeatures(base, combine)
    feature.timelineObject.rollTo(False)
    return feature


# ---------------------------------------------------------------------- edit

def _edit_created(args):
    # Edited from an assembly, the selection is an assembly copy of the
    # feature, which Fusion can invalidate while the dialog is open.
    feature = _native(adsk.fusion.CustomFeature.cast(ui.activeSelections.item(0).entity))
    values = {'layer': feature.parameters.itemById('layer').expression,
              'levels': int(round(feature.parameters.itemById('levels').value))}
    values['levels'] = min(max(values['levels'], 1), MAX_LEVELS)  # the spinner's range

    _edit.clear()
    _edit.update({'feature': feature, 'restore': None, 'populated': False})
    timeline = _design().timeline
    if timeline.markerPosition > 0:
        _edit['restore'] = timeline.item(timeline.markerPosition - 1)
    # Roll back now, before the dialog starts recording changes, so previews
    # (whose changes Fusion undoes) can't undo the roll with them.
    feature.timelineObject.rollTo(True)
    _edit['rolled'] = True

    cmd = args.command
    _build_inputs(cmd, values)
    _connect_dialog(cmd, None, _edit_execute, _edit_destroy)
    _on(cmd.activate, adsk.core.CommandEventHandler, _edit_activate)


def _edit_activate(args):
    # activate can fire more than once per command; the setup is one-time.
    if _edit.get('populated'):
        return
    _edit['populated'] = True
    feature = _edit['feature']
    feature.timelineObject.rollTo(True)
    inputs = args.command.commandInputs
    for floor in _dependencies(feature, skip_lost=True):
        inputs.itemById('holes').addSelection(floor)
    _on(args.command.executePreview, adsk.core.CommandEventHandler, _edit_preview)
    _draw_preview(inputs)


def _edit_preview(args):
    _edit['feature'].timelineObject.rollTo(True)
    _draw_preview(args.command.commandInputs)


def _edit_execute(args):
    feature = _edit['feature']
    inputs = args.command.commandInputs
    _clear_overlay()
    feature.timelineObject.rollTo(True)
    v = _read_inputs(inputs)
    _, combine = _children(feature)
    old = _dependencies(feature, skip_lost=True)

    # Dependencies: ids are only for ordering, so rewrite them all.
    deps = feature.dependencies
    for dep in [deps.item(i) for i in range(deps.count)]:
        dep.deleteMe()
    _add_dependencies(deps.add, v['floors'])
    layer = feature.parameters.itemById('layer')
    if layer.expression != v['layer']:
        layer.expression = v['layer']
    levels = feature.parameters.itemById('levels')
    if levels.value != v['levels']:
        levels.value = v['levels']

    new_body = _native(v['floors'][0]).body
    if not old or new_body != _native(old[0]).body:
        combine.timelineObject.rollTo(True)
        combine.targetBody = new_body

    _restore_timeline()
    _save_settings(inputs)


def _edit_destroy(args):
    # Cancel leaves the timeline rolled back; put it where the user had it.
    _clear_overlay()
    _restore_timeline()
    _edit.clear()


def _restore_timeline():
    if not _edit.get('rolled'):
        return
    restore = _edit.get('restore')
    if restore is not None:
        restore.rollTo(False)
    else:
        _design().timeline.moveToEnd()
    _edit['rolled'] = False


# ------------------------------------------------------------------- compute

def _compute(args):
    feature = args.customFeature
    try:
        # Only the base feature: at creation the cut joins after the first compute.
        base = next((f for f in feature.features if f.objectType == adsk.fusion.BaseFeature.classType()), None)
        if base is None:
            raise BridgingError('The base feature is missing.', 'FEATURE_MISSING_INPUTS')
        levels = feature.parameters.itemById('levels').value
        solid = _solid(_dependencies(feature), feature.parameters.itemById('layer').value, levels)
        base.startEdit()
        try:
            updated = base.updateBody(base.bodies.item(0), solid)
        finally:
            base.finishEdit()
        if not updated:
            raise BridgingError('Could not update the bridge layer body.')
    except BridgingError as e:
        # Custom feature compute errors can't carry custom text, so the reason
        # goes to the Text Commands log and the feature gets Fusion's message.
        app.log('%s (%s): %s' % (NAME, feature.name, e))
        args.computeStatus.statusMessages.addError(e.message_id, '')
    except Exception:
        # A modal dialog here would block Fusion mid-recompute.
        app.log('%s (%s) failed:\n%s' % (NAME, feature.name, traceback.format_exc()))
        args.computeStatus.statusMessages.addError('API_COMPUTE_ERROR', '')


def _children(feature):
    base = combine = None
    for f in feature.features:
        if f.objectType == adsk.fusion.BaseFeature.classType():
            base = f
        elif f.objectType == adsk.fusion.CombineFeature.classType():
            combine = f
    if base is None or combine is None:
        raise BridgingError('The base feature or combine is missing.', 'FEATURE_MISSING_INPUTS')
    return base, combine


def _add_dependencies(add, floors):
    """Each floor as 'hole<i>', and the faces around it (the hole's wall and
    the counterbore's walls) as 'wall<i>_<j>'. Fusion only reruns the feature
    when a dependency's own surface changes: widening the hole leaves the
    floor's plane where it was, but not the hole's wall."""
    for i, floor in enumerate(floors):
        add('hole%d' % i, floor)
        for j, wall in enumerate(_walls(floor)):
            add('wall%d_%d' % (i, j), wall)


def _walls(floor):
    """The faces bordering a floor, in the floor's context."""
    native = _native(floor)
    seen = set()
    out = []
    for loop in native.loops:
        for edge, _ in lighten_geometry.loop_coedges(loop):
            for face in edge.faces:
                if face != native and face.tempId not in seen:
                    seen.add(face.tempId)
                    out.append(face.createForAssemblyContext(floor.assemblyContext) if floor.assemblyContext else face)
    return out


def _dependencies(feature, skip_lost=False):
    """The counterbore floors, in order (the walls only trigger recomputes).
    A reference to deleted geometry is an error for the recompute; edit
    passes skip_lost, since editing is how it gets fixed."""
    floors = []
    for i in range(feature.dependencies.count):
        dep = feature.dependencies.item(i)
        if not dep.id.startswith('hole'):
            continue
        if dep.entity is None:
            if skip_lost:
                continue
            raise BridgingError('Lost the reference "%s". Edit the feature and reselect it.' % dep.id,
                                'FEATURE_REFERENCE_LOST')
        floors.append((int(dep.id[4:]), dep.entity))
    return [e for _, e in sorted(floors)]


# ------------------------------------------------------------------ geometry

def _circle_of(loop):
    """(center, radius) if every edge of the loop is on one circle, else None."""
    center = radius = None
    for edge, _ in lighten_geometry.loop_coedges(loop):
        g = edge.geometry
        if g.objectType not in (adsk.core.Circle3D.classType(), adsk.core.Arc3D.classType()):
            return None
        if center is None:
            center, radius = g.center, g.radius
        elif g.center.distanceTo(center) > TOL or abs(g.radius - radius) > TOL:
            return None
    return center, radius


def _hole(face):
    """(hole center, floor's outward normal, hole radius, counterbore loop) if
    the face is a counterbore floor, else None.

    A floor is a planar face with one hole in it: a round hole inside a
    counterbore outline of any shape (round, hex, square...), with the
    counterbore wall on its open side and the hole on the other. That rules
    out the top face of a washer-like part, whose inner circle is the
    counterbore going up from it."""
    if face.geometry.objectType != adsk.core.Plane.classType() or face.loops.count != 2:
        return None
    outer = next(loop for loop in face.loops if loop.isOuter)
    inner = next((loop for loop in face.loops if not loop.isOuter), None)
    circle = _circle_of(inner) if inner is not None else None
    if circle is None:
        return None
    center, small = circle
    _, normal = face.evaluator.getNormalAtPoint(face.pointOnFace)
    if not (_side(face, outer, center, normal) > 0 > _side(face, inner, center, normal)):
        return None
    return center, normal, small, outer


def _side(floor, loop, center, normal):
    """Which side of the floor the other face along the loop is on (+ is the floor's outward side)."""
    edge = loop.coEdges.item(0).edge
    other = next((f for f in edge.faces if f != floor), None)
    if other is None:
        return 0
    return center.vectorTo(other.pointOnFace).dotProduct(normal)


def _axes(normal):
    """In-plane axes for the slot: the component's x axis projected onto the
    floor (y when the floor faces along x), so every hole's bridges run the same way."""
    for axis in (adsk.core.Vector3D.create(1, 0, 0), adsk.core.Vector3D.create(0, 1, 0)):
        u = axis.copy()
        n = normal.copy()
        n.scaleBy(axis.dotProduct(normal))
        u.subtract(n)
        if u.length > 0.1:
            u.normalize()
            v = normal.crossProduct(u)
            v.normalize()
            return u, v


def _rotated(u, v, angle):
    a, b = u.copy(), v.copy()
    a.scaleBy(math.cos(angle))
    b.scaleBy(math.sin(angle))
    a.add(b)
    return a


def _solid(floors, layer, levels):
    """One solid with the bridge layers of every floor, in component space."""
    if not floors:
        raise BridgingError('No counterbored holes picked.', 'FEATURE_MISSING_INPUTS')
    levels = int(round(levels))
    if not 1 <= levels <= MAX_LEVELS:
        raise BridgingError('Levels must be 1 to %d.' % MAX_LEVELS)
    if layer <= 0:
        raise BridgingError('Layer height must be more than 0.')
    tb = adsk.fusion.TemporaryBRepManager.get()
    solid = None
    for floor in floors:
        hole = _hole(_native(floor))
        if hole is None:
            raise BridgingError('A picked face is no longer a counterbore floor.')
        layers = _hole_layers(hole, layer, levels)
        if solid is None:
            solid = layers
        elif not tb.booleanOperation(solid, layers, adsk.fusion.BooleanTypes.UnionBooleanType):
            raise BridgingError('Could not combine the bridge layers.')
    return solid


def _hole_layers(hole, layer, levels):
    """The bridge layers of one hole. Level k (from 0) goes from just above
    the floor down k + 1 layers, so together they step down: a slot as wide
    as the hole across the counterbore, then a square, an octagon and a
    16-gon around the hole (squares turned by 90 / 2^(k-1) degrees,
    intersected). All of it stays inside the counterbore's outline."""
    tb = adsk.fusion.TemporaryBRepManager.get()
    center, normal, small, outer = hole
    if levels * layer >= _hole_depth(hole):
        raise BridgingError('The bridge layers are deeper than the hole.')
    u, v = _axes(normal)
    # Long enough to cross the counterbore from the hole, whatever its shape.
    reach = max(center.distanceTo(edge.startVertex.geometry) for edge, _ in lighten_geometry.loop_coedges(outer))
    reach = max(reach, max(center.distanceTo(edge.pointOnEdge) for edge, _ in lighten_geometry.loop_coedges(outer)))
    solid = None
    for k in range(levels):
        top, bottom = LIFT, -(k + 1) * layer
        mid = center.copy()
        shift = normal.copy()
        shift.scaleBy((top + bottom) / 2)
        mid.translateBy(shift)
        height = top - bottom

        def box(length, width, angle=0.0):
            return tb.createBox(adsk.core.OrientedBoundingBox3D.create(
                mid, _rotated(u, v, angle), _rotated(u, v, angle + math.pi / 2), length, width, height))

        if k == 0:
            shape = box(4 * reach, 2 * small)
        else:
            count = 2 ** (k - 1)
            shape = box(2 * small, 2 * small)
            for j in range(1, count):
                if not tb.booleanOperation(shape, box(2 * small, 2 * small, j * (math.pi / 2) / count),
                                           adsk.fusion.BooleanTypes.IntersectionBooleanType):
                    raise BridgingError('Could not build the bridge layers.')
        if solid is None:
            solid = shape
        elif not tb.booleanOperation(solid, shape, adsk.fusion.BooleanTypes.UnionBooleanType):
            raise BridgingError('Could not build the bridge layers.')
    if not tb.booleanOperation(solid, _outline_prism(outer, normal, LIFT, -levels * layer),
                               adsk.fusion.BooleanTypes.IntersectionBooleanType):
        raise BridgingError('Could not build the bridge layers.')
    return solid


def _hole_depth(hole):
    """How far the hole's wall goes below the floor, so the layers can't cut
    out through the far side of the part."""
    center, normal, _, _ = hole
    floor = hole[3].face
    inner = next(loop for loop in floor.loops if not loop.isOuter)
    wall = next(f for f in inner.coEdges.item(0).edge.faces if f != floor)
    depth = 0.0
    for loop in wall.loops:
        for edge, _ in lighten_geometry.loop_coedges(loop):
            for p in (edge.startVertex.geometry, edge.pointOnEdge):
                depth = max(depth, -center.vectorTo(p).dotProduct(normal))
    return depth


def _outline_prism(loop, normal, top, bottom):
    """The counterbore's outline swept from top to bottom along normal (cm
    from the floor). Splines and ellipses in the outline become straight
    pieces within lighten's CHORD, which the sweep needs."""
    curves = []
    for edge, _ in lighten_geometry.loop_coedges(loop):
        g = edge.geometry
        if g.objectType in (adsk.core.Line3D.classType(), adsk.core.Arc3D.classType(), adsk.core.Circle3D.classType()):
            curves.append(g)
            continue
        ev = edge.evaluator
        _, t0, t1 = ev.getParameterExtents()
        _, points = ev.getStrokes(t0, t1, lighten_geometry.CHORD)
        curves += [adsk.core.Line3D.create(a, b) for a, b in zip(points, points[1:]) if a.distanceTo(b) > TOL]
    tb = adsk.fusion.TemporaryBRepManager.get()
    try:
        sheet = lighten_geometry._face(curves)
        up = normal.copy()
        up.scaleBy(top)
        move = adsk.core.Matrix3D.create()
        move.translation = up
        tb.transform(sheet, move)
        down = normal.copy()
        down.scaleBy(bottom - top)
        return lighten_geometry.prism(sheet, down)
    except lighten_geometry.LightenError as e:
        raise BridgingError('Could not follow the counterbore outline: %s' % e)
