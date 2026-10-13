"""Lighten: parametric pocketing of a planar face, driven by sketch struts.

The feature is a Fusion custom feature grouping two native features:
    1. a base feature holding one solid: every pocket swept to the cut depth
    2. a combine that cuts that solid from the face's body
Wall, strut, fillet radius, min pocket width and depth are custom parameters,
so they show up in Change Parameters and accept expressions. The face, strut curves, excluded
profiles and to-object target are dependencies. When any of them change, the
compute handler rebuilds the pocket solid in place with BaseFeature.updateBody,
which keeps the body's identity so the combine never loses its tool.

While the dialog is open, the preview is a custom graphics overlay of the
pockets rather than real features, so each click only pays for the 2D pocket
math. OK reuses those pockets.
"""

import json
import os
import traceback

import adsk.core
import adsk.fusion

from .. import panel
from . import geometry
from .geometry import LightenError

app = adsk.core.Application.get()
ui = app.userInterface

CMD_ID = 'FTCTools_Lighten'
EDIT_CMD_ID = 'FTCTools_LightenEdit'
FOCUS_EVENT_ID = 'FTCTools_LightenFocusStruts'
FEATURE_ID = 'FTCToolsLighten'
ICONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources')

SETTINGS_PATH = os.path.join(os.getenv('APPDATA') or os.path.expanduser('~'), 'FTCTools', 'lighten.json')
DEFAULTS = {'wall': '4 mm', 'strut': '4 mm', 'radius': '2 mm', 'minwidth': '0 mm', 'extent': 'all', 'depth': '3 mm'}

# Stored on the feature by index, so only append.
EXTENTS = [('all', 'All'), ('distance', 'Distance'), ('object', 'To object')]
# (id, parameter label, dialog label)
LENGTHS = [('wall', 'WallThickness', 'Wall thickness'),
           ('strut', 'StrutThickness', 'Strut thickness'),
           ('radius', 'FilletRadius', 'Fillet radius'),
           ('minwidth', 'MinPocketWidth', 'Min width'),
           ('depth', 'Depth', 'Depth')]
POSITIVE = ('wall', 'strut', 'radius')  # minwidth may be 0, meaning off

FACE_ATTR_GROUP = 'FTCTools'
FACE_ATTR = 'lighten_face_point'  # a point of the picked face, see _picked_face

# The pocket solid starts this far above the face so the cut has no coplanar faces.
LIFT = 0.001  # cm
OVERLAY_COLOR = (255, 255, 255)  # preview outline

_handlers = []
_feature_def = None
_edit = {}
_cache = {'key': None, 'pockets': None}
_overlay = []



# ----------------------------------------------------------------- lifecycle

def start():
    global _feature_def
    create_def = ui.commandDefinitions.addButtonDefinition(
        CMD_ID, 'Lighten',
        'Pocket a planar face, leaving walls around its edges and struts along sketch curves.',
        ICONS)
    _on(create_def.commandCreated, adsk.core.CommandCreatedEventHandler, _create_created)

    edit_def = ui.commandDefinitions.addButtonDefinition(EDIT_CMD_ID, 'Edit Lighten', 'Edit a Lighten feature.', ICONS)
    _on(edit_def.commandCreated, adsk.core.CommandCreatedEventHandler, _edit_created)

    _feature_def = adsk.fusion.CustomFeatureDefinition.create(FEATURE_ID, 'Lighten', ICONS)
    _feature_def.editCommandId = EDIT_CMD_ID
    _on(_feature_def.customFeatureCompute, adsk.fusion.CustomFeatureEventHandler, _compute)
    _on(app.registerCustomEvent(FOCUS_EVENT_ID), adsk.core.CustomEventHandler, _deferred_focus)

    # Sheet metal parts get pocketed too, so the button is on that tab as well.
    for sheet_metal in (False, True):
        control = panel.get(sheet_metal=sheet_metal).controls.addCommand(create_def)
        control.isPromotedByDefault = True
        control.isPromoted = True


def stop():
    _clear_overlay()
    app.unregisterCustomEvent(FOCUS_EVENT_ID)
    for sheet_metal in (False, True):
        control = panel.get(sheet_metal=sheet_metal).controls.itemById(CMD_ID)
        if control:
            control.deleteMe()
        panel.remove_if_empty(sheet_metal=sheet_metal)
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
                ui.messageBox('Lighten failed:\n' + traceback.format_exc())
    handler = Handler()
    event.add(handler)
    _handlers.append(handler)


# ------------------------------------------------------------------ settings

def _load_settings():
    values = dict(DEFAULTS)
    if os.path.exists(SETTINGS_PATH):
        with open(SETTINGS_PATH, 'r', encoding='utf-8') as f:
            values.update(json.load(f))
    if values['extent'] not in [k for k, _ in EXTENTS]:
        values['extent'] = DEFAULTS['extent']  # saved by an older version
    return values


def _save_settings(inputs):
    values = {key: inputs.itemById(key).expression for key, _, _ in LENGTHS}
    values['extent'] = _extent_key(inputs)
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, 'w', encoding='utf-8') as f:
        json.dump(values, f, indent=2)


# -------------------------------------------------------------------- dialog

def _design():
    return adsk.fusion.Design.cast(app.activeProduct)


def _build_inputs(cmd, values):
    inputs = cmd.commandInputs
    units = _design().unitsManager.defaultLengthUnits

    sel = inputs.addSelectionInput('face', 'Face', 'Planar face to pocket')
    sel.addSelectionFilter('PlanarFaces')
    sel.setSelectionLimits(1, 1)

    sel = inputs.addSelectionInput('struts', 'Struts', 'Sketch lines, arcs and circles to leave struts along')
    sel.addSelectionFilter('SketchCurves')
    sel.setSelectionLimits(0, 0)

    sel = inputs.addSelectionInput('exclude', 'Exclusions', 'Sketch profiles to leave solid, with a wall around them')
    sel.addSelectionFilter('Profiles')
    sel.setSelectionLimits(0, 0)

    for key, _, label in LENGTHS[:4]:
        inputs.addValueInput(key, label, units, adsk.core.ValueInput.createByString(values[key]))
    inputs.itemById('minwidth').tooltip = 'Cut off pocket fingers and channels narrower than this. 0 turns it off.'

    dd = inputs.addDropDownCommandInput('extent', 'Extent', adsk.core.DropDownStyles.TextListDropDownStyle)
    for key, label in EXTENTS:
        dd.listItems.add(label, key == values['extent'])
    inputs.addValueInput('depth', 'Distance', units, adsk.core.ValueInput.createByString(values['depth']))

    sel = inputs.addSelectionInput('object', 'Object', 'Face, plane, vertex or point to cut to')
    for f in ('PlanarFaces', 'ConstructionPlanes', 'Vertices', 'SketchPoints', 'ConstructionPoints'):
        sel.addSelectionFilter(f)
    sel.setSelectionLimits(0, 1)  # required only in To object mode, see _validate

    _update_visibility(inputs)


def _extent_key(inputs):
    label = inputs.itemById('extent').selectedItem.name
    return next(k for k, l in EXTENTS if l == label)


def _update_visibility(inputs):
    mode = _extent_key(inputs)
    inputs.itemById('depth').isVisible = mode == 'distance'
    inputs.itemById('object').isVisible = mode == 'object'


def _selections(inputs, input_id):
    sel = inputs.itemById(input_id)
    return [sel.selection(i).entity for i in range(sel.selectionCount)]


def _native(entity):
    """The entity in its owning component's space.

    Selections and dependencies stay as picked (assembly proxies when the body
    is in a sub-component; Fusion rejects the native object as a dependency
    there). Geometry work happens on the native object, in component space.
    """
    if entity.assemblyContext:
        return entity.nativeObject
    return entity


def _read_inputs(inputs):
    mode = _extent_key(inputs)
    values = {key: inputs.itemById(key).expression for key, _, _ in LENGTHS}
    values.update({
        'face': _selections(inputs, 'face')[0],
        'struts': _selections(inputs, 'struts'),
        'exclude': _selections(inputs, 'exclude'),
        'extent': mode,
        'upto': _selections(inputs, 'object')[0] if mode == 'object' else None,
    })
    return values


STRUT_TYPES = (adsk.fusion.SketchLine.classType(), adsk.fusion.SketchArc.classType(), adsk.fusion.SketchCircle.classType())


def _pre_select(args):
    if args.activeInput.id == 'struts' and args.selection.entity.objectType not in STRUT_TYPES:
        args.isSelectable = False


_retoggling = [False]


def _input_changed(args):
    inputs = args.inputs
    if args.input.id == 'extent':
        _update_visibility(inputs)
        # Picking To object means the next click is the object.
        if _extent_key(inputs) == 'object':
            inputs.itemById('object').hasFocus = True
        else:
            _focus_struts(inputs)
    elif args.input.id in ('struts', 'exclude') and not _retoggling[0] and not _edit.get('populating'):
        _toggle_duplicates(inputs.itemById(args.input.id))
    elif args.input.id == 'face':
        _focus_struts(inputs)


def _focus_struts(inputs):
    """Struts is the input to be picking into whenever a face is chosen."""
    if inputs.itemById('face').selectionCount == 1:
        inputs.itemById('struts').hasFocus = True


def _deferred_focus(args):
    inputs = _edit.get('inputs')
    if inputs is not None:
        _focus_struts(inputs)


def _on_select(args):
    # Fires as the pick lands, before inputChanged; focus changes stick here.
    if args.activeInput.id in ('face', 'object'):
        # After the face (or the to-object target) is picked, go back to struts.
        args.activeInput.parentCommand.commandInputs.itemById('struts').hasFocus = True


def _toggle_duplicates(sel):
    """Treat clicking an already-selected entity as deselecting it.

    Fusion only toggles selections the user made by clicking. For ones the
    add-in put back with addSelection (edit reselects the feature's inputs),
    a click adds a second copy of the same entity instead. So any entity that
    now appears twice is removed entirely."""
    entities = [sel.selection(i).entity for i in range(sel.selectionCount)]
    tokens = [e.entityToken for e in entities]
    if len(set(tokens)) == len(tokens):
        return
    keep = [e for e, t in zip(entities, tokens) if tokens.count(t) == 1]
    _retoggling[0] = True
    try:
        sel.clearSelection()
        for e in keep:
            sel.addSelection(e)
    finally:
        _retoggling[0] = False


def _validate(args):
    inputs = args.inputs
    ok = inputs.itemById('face').selectionCount == 1
    for key, _, _ in LENGTHS[:4]:
        v = inputs.itemById(key)
        ok = ok and v.isValidExpression and (v.value > 0 if key in POSITIVE else v.value >= 0)
    mode = _extent_key(inputs)
    if mode == 'distance':
        v = inputs.itemById('depth')
        ok = ok and v.isValidExpression and v.value > 0
    elif mode == 'object':
        ok = ok and inputs.itemById('object').selectionCount == 1
    args.areInputsValid = ok
    if not ok:
        _clear_overlay()


def _connect_dialog(cmd, preview, execute, destroy):
    _on(cmd.inputChanged, adsk.core.InputChangedEventHandler, _input_changed)
    _on(cmd.validateInputs, adsk.core.ValidateInputsEventHandler, _validate)
    _on(cmd.preSelect, adsk.core.SelectionEventHandler, _pre_select)
    _on(cmd.select, adsk.core.SelectionEventHandler, _on_select)
    if preview is not None:
        _on(cmd.executePreview, adsk.core.CommandEventHandler, preview)
    _on(cmd.execute, adsk.core.CommandEventHandler, execute)
    _on(cmd.destroy, adsk.core.CommandEventHandler, destroy)


def _preview(args):
    _draw_preview(args.command.commandInputs)


def _draw_preview(inputs):
    """Overlay the pockets for the current inputs. Inputs Lighten can't pocket show nothing."""
    _clear_overlay()
    v = _read_inputs(inputs)
    try:
        dims = _evaluate(v)
        pockets = _cached_pockets(v, dims)
        _depth_for(v['face'], v['extent'], dims['depth'], v['upto'])
    except LightenError:
        return
    _show_overlay(v['face'], pockets)


def _show_overlay(face, pockets):
    """Draw the pocket outlines as thin highlighted lines just outside the face."""
    native = _native(face)
    _, n = native.evaluator.getNormalAtPoint(native.pointOnFace)
    lift = n.copy()
    lift.scaleBy(0.01)

    points = []
    indices = []
    for edge in geometry.body_edges(pockets):
        _, t0, t1 = edge.evaluator.getParameterExtents()
        _, strokes = edge.evaluator.getStrokes(t0, t1, 0.002)  # cm chord error; arcs stay round
        start = len(points) // 3
        for p in strokes:
            p.translateBy(lift)
            points += [p.x, p.y, p.z]
        for i in range(len(strokes) - 1):
            indices += [start + i, start + i + 1]

    # Graphics live in the face's component: component space needs no
    # transform, and Fusion ghosts anything outside an activated component.
    group = native.body.parentComponent.customGraphicsGroups.add()
    lines = group.addLines(adsk.fusion.CustomGraphicsCoordinates.create(points), indices, False)
    lines.color = adsk.fusion.CustomGraphicsSolidColorEffect.create(adsk.core.Color.create(*OVERLAY_COLOR, 255))
    lines.weight = 1
    lines.depthPriority = 1  # above the face's selection highlight
    _overlay.append(group)


def _clear_overlay():
    while _overlay:
        group = _overlay.pop()
        if group.isValid:
            group.deleteMe()


def _cached_pockets(v, dims):
    """Pockets for these inputs, reusing the last result when nothing that affects them changed.

    The dialog's preview fills the cache and the recompute after OK reads it,
    so OK doesn't redo the pocket math. Returns a copy, since callers
    transform what they get."""
    key = (v['face'].entityToken,
           tuple(e.entityToken for e in v['struts']),
           tuple(e.entityToken for e in v['exclude']),
           dims['wall'], dims['strut'], dims['radius'], dims['minwidth'])
    if _cache['key'] != key:
        _cache['pockets'] = _pockets_for(v['face'], v['struts'], v['exclude'], dims)
        _cache['key'] = key
    return adsk.fusion.TemporaryBRepManager.get().copy(_cache['pockets'])


def _end_dialog():
    _clear_overlay()
    _cache['key'] = _cache['pockets'] = None


# -------------------------------------------------------------------- create

def _create_created(args):
    cmd = args.command
    _build_inputs(cmd, _load_settings())
    _connect_dialog(cmd, _preview, _create_execute, lambda a: _end_dialog())


def _create_execute(args):
    inputs = args.command.commandInputs
    _clear_overlay()
    v = _read_inputs(inputs)
    try:
        dims = _evaluate(v)
        solid = _sweep(v['face'], _cached_pockets(v, dims), v['extent'], dims['depth'], v['upto'])
    except LightenError as e:
        ui.messageBox(str(e), 'Lighten')
        return
    _create_feature(v, solid)
    _save_settings(inputs)


def _create_feature(v, solid):
    body = _native(v['face']).body
    comp = body.parentComponent
    units = _design().unitsManager.defaultLengthUnits

    base = comp.features.baseFeatures.add()
    base.startEdit()
    comp.bRepBodies.add(solid, base)
    base.finishEdit()
    base.name = 'Lighten Pockets'

    tools = adsk.core.ObjectCollection.create()
    tools.add(base.bodies.item(0))
    combine_input = comp.features.combineFeatures.createInput(body, tools)
    combine_input.operation = adsk.fusion.FeatureOperations.CutFeatureOperation
    combine_input.isKeepToolBodies = False
    combine = comp.features.combineFeatures.add(combine_input)
    combine.name = 'Lighten Cut'

    cf_input = comp.features.customFeatures.createInput(_feature_def)
    for key, name, _ in LENGTHS:
        cf_input.addCustomParameter(key, name, adsk.core.ValueInput.createByString(v[key]), units,
                                    key != 'depth' or v['extent'] == 'distance')
    # The extent mode lives in a hidden parameter so compute can read it even
    # during customFeatures.add, before any other state could be stored.
    cf_input.addCustomParameter('extent', 'ExtentMode', adsk.core.ValueInput.createByReal(_extent_index(v['extent'])), '', False)
    _add_dependencies(cf_input.addDependency, v)
    cf_input.setStartAndEndFeatures(base, combine)
    # Fusion computes the feature once inside add, and that compute is not
    # skipped although the base feature already holds this solid: Fusion
    # matches the faces of a body placed with bRepBodies.add wrongly to the
    # next updateBody, so an Offset Face on a pocket wall jumped to other
    # walls on the first recompute. From one updateBody to the next it keeps
    # them. The pockets are cached, so this compute is only the sweep.
    feature = comp.features.customFeatures.add(cf_input)
    _remember_face(feature, v['face'])
    return feature


def _extent_index(mode):
    return [k for k, _ in EXTENTS].index(mode)


def _extent_of(feature):
    # Features made before 'To object' merged face and vertex stored 3 for vertex.
    return EXTENTS[min(int(round(feature.parameters.itemById('extent').value)), len(EXTENTS) - 1)][0]


def _unique(entities):
    seen = set()
    out = []
    for e in entities:
        if e.entityToken not in seen:
            seen.add(e.entityToken)
            out.append(e)
    return out


def _add_dependencies(add, v):
    add('face', v['face'])
    for i, curve in enumerate(_unique(v['struts'])):
        add('strut%d' % i, curve)
    for i, profile in enumerate(_unique(v['exclude'])):
        add('exclude%d' % i, profile)
    if v['upto'] is not None:
        add('upto', v['upto'])


def _sync_dependencies(feature, v):
    """Make a live feature's dependencies match v, touching only what changed.

    Every add or delete on a live feature costs about 150 ms, so rewriting all
    of them made OK take 20 s on a plate with 130 struts. Strut and exclusion
    ids can end up with gaps; _dependencies only uses them for ordering.
    """
    deps = feature.dependencies
    existing = [deps.item(i) for i in range(deps.count)]
    for dep_id, entity in (('face', v['face']), ('upto', v['upto'])):
        dep = deps.itemById(dep_id)
        if entity is None:
            if dep is not None:
                dep.deleteMe()
        elif dep is None:
            deps.add(dep_id, entity)
        elif dep.entity is None or dep.entity.entityToken != entity.entityToken:
            dep.entity = entity
    for prefix, key in (('strut', 'struts'), ('exclude', 'exclude')):
        wanted = {e.entityToken: e for e in _unique(v[key])}
        kept = set()
        top = -1
        for dep in existing:
            if not dep.id.startswith(prefix):
                continue
            top = max(top, int(dep.id[len(prefix):]))
            token = dep.entity.entityToken if dep.entity is not None else None
            if token in wanted and token not in kept:
                kept.add(token)
            else:
                dep.deleteMe()
        for token, entity in wanted.items():
            if token not in kept:
                top += 1
                deps.add('%s%d' % (prefix, top), entity)


def _evaluate(v):
    um = _design().unitsManager
    units = um.defaultLengthUnits
    return {k: um.evaluateExpression(v[k], units) for k, _, _ in LENGTHS}


# ---------------------------------------------------------------------- edit

def _edit_created(args):
    # Edited from an assembly, the selection is an assembly copy of the
    # feature, which Fusion can invalidate while the dialog is open.
    feature = _native(adsk.fusion.CustomFeature.cast(ui.activeSelections.item(0).entity))
    values = _load_settings()
    for key, _, _ in LENGTHS:
        param = feature.parameters.itemById(key)
        values[key] = param.expression if param else '0 mm'  # made before this parameter existed
    values['extent'] = _extent_of(feature)

    _edit.clear()
    _edit.update({'feature': feature, 'restore': None, 'rolled': False, 'populated': False})
    timeline = _design().timeline
    if timeline.markerPosition > 0:
        _edit['restore'] = timeline.item(timeline.markerPosition - 1)
    # Roll back now, before the dialog starts recording changes, so previews
    # (whose changes Fusion undoes) can't undo the roll with them.
    _roll_before(feature)
    _edit['rolled'] = True
    _edit['shown'] = _show_sketches(feature)

    cmd = args.command
    _build_inputs(cmd, values)
    # The preview is subscribed in activate, once the inputs are reselected.
    _connect_dialog(cmd, None, _edit_execute, _edit_destroy)
    _on(cmd.activate, adsk.core.CommandEventHandler, _edit_activate)


def _show_sketches(feature):
    """Turn on the sketches the feature's struts and exclusions are in, as
    Fusion does for an extrude's profile sketch. Returns (object, property)
    pairs to turn back off when the dialog closes."""
    _, struts, exclude, _ = _dependencies(feature, skip_lost=True)
    sketches = {}
    for e in struts + exclude:
        sketch = _native(e).parentSketch
        sketches[sketch.entityToken] = sketch
    shown = []
    for sketch in sketches.values():
        comp = sketch.parentComponent
        if not comp.isSketchFolderLightBulbOn:
            comp.isSketchFolderLightBulbOn = True
            shown.append((comp, 'isSketchFolderLightBulbOn'))
        if not sketch.isLightBulbOn:
            sketch.isLightBulbOn = True
            shown.append((sketch, 'isLightBulbOn'))
    return shown


def _roll_before(feature):
    """Put the marker just before the feature, so its inputs are read and
    edited as they were before it. Fusion can undo an earlier roll between
    events (a preview's changes are rolled back), so every event that needs
    it does it itself."""
    feature.timelineObject.rollTo(True)


def _edit_preview(args):
    _roll_before(_edit['feature'])
    if _edit.pop('focus_pending', False):
        app.fireCustomEvent(FOCUS_EVENT_ID)
    _draw_preview(args.command.commandInputs)


def _edit_activate(args):
    # activate can fire more than once per command; the setup is one-time.
    if _edit.get('populated'):
        return
    _edit['populated'] = True
    feature = _edit['feature']
    _roll_before(feature)

    inputs = args.command.commandInputs
    face, struts, exclude, upto = _dependencies(feature, skip_lost=True)
    # Every addSelection fires inputChanged and validate, and a preview when one
    # is subscribed. Fusion's own work for each preview cost about 30 ms per
    # selection even with a handler that returned at once (4.6 s for 130 struts),
    # so the preview is only subscribed after the reselect and drawn once.
    _edit['populating'] = True
    try:
        for input_id, entities in (('face', [face] if face is not None else []),
                                   ('struts', _unique(struts)), ('exclude', _unique(exclude)),
                                   ('object', [upto] if upto is not None else [])):
            for entity in entities:
                inputs.itemById(input_id).addSelection(entity)
    finally:
        _edit['populating'] = False
    _on(args.command.executePreview, adsk.core.CommandEventHandler, _edit_preview)
    # Fusion assigns focus itself after activate returns, and a focus change
    # made inside a preview is undone with the preview. So the first preview
    # queues a custom event, which runs after the preview has finished.
    _edit['inputs'] = inputs
    _edit['focus_pending'] = True
    _draw_preview(inputs)


def _edit_execute(args):
    feature = _edit['feature']
    inputs = args.command.commandInputs
    _clear_overlay()
    _roll_before(feature)
    v = _read_inputs(inputs)
    _, combine = _children(feature)
    old_face = _dependencies(feature, skip_lost=True)[0]

    # The timeline is still rolled back to before this feature, so these
    # changes cost one recompute when it rolls forward, not one each.
    _sync_dependencies(feature, v)
    _remember_face(feature, v['face'])
    # Each write costs time on a live feature even when the value is the same.
    for key, _, _ in LENGTHS:
        param = feature.parameters.itemById(key)
        if param:
            if param.expression != v[key]:
                param.expression = v[key]
        elif _evaluate(v)[key] != 0:
            # Custom parameters can only be added when a feature is created.
            ui.messageBox('This Lighten was made before %s existed. Delete it and create it again to use that.'
                          % key, 'Lighten')
    depth = feature.parameters.itemById('depth')
    if depth.isVisible != (v['extent'] == 'distance'):
        depth.isVisible = v['extent'] == 'distance'
    extent = feature.parameters.itemById('extent')
    if extent.value != _extent_index(v['extent']):
        extent.value = _extent_index(v['extent'])

    new_body = _native(v['face']).body
    if old_face is None or new_body != _native(old_face).body:
        combine.timelineObject.rollTo(True)
        combine.targetBody = new_body

    _restore_timeline()
    _save_settings(inputs)


def _edit_destroy(args):
    # Cancel leaves the timeline rolled back; put it where the user had it.
    _end_dialog()
    _restore_timeline()
    for obj, prop in _edit.get('shown', []):
        setattr(obj, prop, False)
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
        base, _ = _children(feature)
        face, struts, exclude, upto = _dependencies(feature)
        dims = _feature_dims(feature)
        pockets = _cached_pockets({'face': face, 'struts': struts, 'exclude': exclude}, dims)
        solid = _sweep(face, pockets, _extent_of(feature), dims['depth'], upto)
        base.startEdit()
        try:
            updated = base.updateBody(base.bodies.item(0), solid)
        finally:
            base.finishEdit()
        if not updated:
            raise LightenError('Could not update the pocket body.')
    except LightenError as e:
        # Custom feature compute errors can't carry custom text, so the reason
        # goes to the Text Commands log and the feature gets Fusion's message.
        app.log('Lighten (%s): %s' % (feature.name, e))
        args.computeStatus.statusMessages.addError(e.message_id, '')
    except Exception:
        # A modal dialog here would block Fusion mid-recompute.
        app.log('Lighten (%s) failed:\n%s' % (feature.name, traceback.format_exc()))
        args.computeStatus.statusMessages.addError('API_COMPUTE_ERROR', '')


def _feature_dims(feature):
    dims = {}
    for key, _, _ in LENGTHS:
        param = feature.parameters.itemById(key)
        dims[key] = param.value if param else 0.0  # made before this parameter existed
    return dims


def _children(feature):
    base = combine = None
    for f in feature.features:
        if f.objectType == adsk.fusion.BaseFeature.classType():
            base = f
        elif f.objectType == adsk.fusion.CombineFeature.classType():
            combine = f
    if base is None or combine is None:
        raise LightenError('The Lighten base feature or combine is missing.', 'FEATURE_MISSING_INPUTS')
    return base, combine


def _dependencies(feature, skip_lost=False):
    """The feature's inputs. A reference to deleted geometry is an error for
    the recompute; edit passes skip_lost, since editing is how it gets fixed."""
    face = upto = None
    struts = []
    exclude = []
    for i in range(feature.dependencies.count):
        dep = feature.dependencies.item(i)
        if dep.entity is None:
            if skip_lost:
                continue
            raise LightenError('Lost the reference "%s". Edit the feature and reselect it.' % dep.id,
                               'FEATURE_REFERENCE_LOST')
        if dep.id == 'face':
            face = _picked_face(feature, dep.entity)
        elif dep.id == 'upto':
            upto = dep.entity
        elif dep.id.startswith('strut'):
            struts.append((int(dep.id[5:]), dep.entity))
        elif dep.id.startswith('exclude'):
            exclude.append((int(dep.id[7:]), dep.entity))
    return face, [e for _, e in sorted(struts)], [e for _, e in sorted(exclude)], upto


def _remember_face(feature, face):
    """Store a point of the picked face on the feature (see _picked_face)."""
    p = _native(face).pointOnFace
    _native(feature).attributes.add(FACE_ATTR_GROUP, FACE_ATTR, json.dumps([p.x, p.y, p.z]))


def _picked_face(feature, face):
    """The face the user picked, given the one the dependency resolved to.

    When an earlier feature split a face into coplanar pieces (text joined
    onto a plate leaves an island inside each closed letter), Fusion tracks
    the pieces as siblings, and once Lighten has cut one the dependency can
    come back as another. The point stored at pick time says which one it
    was. Features made before the point was stored keep what Fusion gives.
    The point is on the feature itself: edited from an assembly, `feature` is
    an assembly copy without it.
    """
    attr = _native(feature).attributes.itemByName(FACE_ATTR_GROUP, FACE_ATTR)
    if attr is None:
        return face
    point = adsk.core.Point3D.create(*json.loads(attr.value))
    native = _native(face)
    if native.geometry.objectType != adsk.core.Plane.classType() or _has_point(native, point):
        return face
    plane = native.geometry
    for other in native.body.faces:
        if other == native or other.geometry.objectType != adsk.core.Plane.classType():
            continue
        g = other.geometry
        if not g.normal.isParallelTo(plane.normal) or abs(plane.origin.vectorTo(g.origin).dotProduct(plane.normal)) > PLANE_TOL:
            continue
        if _has_point(other, point):
            return other.createForAssemblyContext(face.assemblyContext) if face.assemblyContext else other
    return face


def _has_point(face, point):
    ev = face.evaluator
    ok, param = ev.getParameterAtPoint(point)
    if not ok:
        return False
    ok, on_surface = ev.getPointAtParameter(param)
    return ok and on_surface.distanceTo(point) < PLANE_TOL and ev.isParameterOnFace(param)


# ------------------------------------------------------------------ geometry

PLANE_TOL = 1e-6  # cm


def _sweep(face, pockets, mode, distance, upto):
    """The solid to cut, in the face's component space: the pockets swept from
    just above the face to the extent. face and upto are as selected."""
    depth = _depth_for(face, mode, distance, upto)
    native = _native(face)
    _, n = native.evaluator.getNormalAtPoint(native.pointOnFace)  # outward from the material
    lift = n.copy()
    lift.scaleBy(LIFT)
    m = adsk.core.Matrix3D.create()
    m.translation = lift
    adsk.fusion.TemporaryBRepManager.get().transform(pockets, m)
    sweep = n.copy()
    sweep.scaleBy(-(depth + LIFT))
    return geometry.prism(pockets, sweep)


def _comp_space_geometry(entity, face):
    """Plane or point of a to-object target, in the lightened face's component space.

    A proxy's geometry is in root space, as is anything in the root component.
    Either way it is brought into the face's component through the inverse of
    the face's occurrence transform.
    """
    is_sketch_point = entity.objectType == adsk.fusion.SketchPoint.classType()
    geom = (entity.worldGeometry if is_sketch_point else entity.geometry).copy()
    native = _native(entity)
    owner = native.parentSketch.parentComponent if is_sketch_point else (
        native.body.parentComponent if hasattr(native, 'body') else native.component)
    face_comp = _native(face).body.parentComponent
    if not entity.assemblyContext and owner == face_comp:
        return geom  # already in the face's component space
    if not entity.assemblyContext and owner != _design().rootComponent:
        raise LightenError('Pick the target object in the assembly, not inside another component.')
    if face.assemblyContext:
        to_comp = face.assemblyContext.transform2.copy()
        to_comp.invert()
        geom.transformBy(to_comp)
    return geom


def _depth_for(face, mode, distance, upto):
    """Cut depth into the material, measured from the face along its inward normal."""
    native = _native(face)
    _, n = native.evaluator.getNormalAtPoint(native.pointOnFace)
    origin = native.pointOnFace
    into = n.copy()
    into.scaleBy(-1)
    if mode == 'distance':
        return distance
    if mode == 'all':
        bb = native.body.boundingBox
        lo, hi = bb.minPoint, bb.maxPoint
        corners = [adsk.core.Point3D.create(x, y, z) for x in (lo.x, hi.x) for y in (lo.y, hi.y) for z in (lo.z, hi.z)]
        return max(origin.vectorTo(c).dotProduct(into) for c in corners) + LIFT
    target = _comp_space_geometry(upto, face)
    if target.objectType == adsk.core.Plane.classType():
        if not target.normal.isParallelTo(n):
            raise LightenError('The target face or plane must be parallel to the lightened face.')
        depth = origin.vectorTo(target.origin).dotProduct(into)
    else:
        depth = origin.vectorTo(target).dotProduct(into)
    if depth <= 0:
        raise LightenError('The target object must be inside the material, below the face.')
    return depth


def _pockets_for(face, struts, exclude, dims):
    """Pocket sheet on the face plane, in component space. Inputs are as selected."""
    face = _native(face)
    struts = [_native(c) for c in struts]
    exclude = [_native(p) for p in exclude]
    plane = face.geometry
    comp = face.body.parentComponent
    for entity in struts + exclude:
        if entity.parentSketch.parentComponent != comp:
            raise LightenError('Struts and excluded regions must be in sketches in the same component as the face.')

    strut_curves = [_onto_plane(_to_model(c.geometry, c.parentSketch), plane) for c in struts]
    loops = []
    for profile in exclude:
        outer = next(l for l in profile.profileLoops if l.isOuter)
        loops.append([_onto_plane(_to_model(pc.geometry, profile.parentSketch), plane) for pc in outer.profileCurves])

    pockets = geometry.compute_pockets(face, plane.normal, strut_curves, loops,
                                       dims['wall'], dims['strut'], dims['radius'], dims['minwidth'])
    if pockets is None:
        raise LightenError('Nothing left to pocket. Reduce the wall, strut or fillet size.')
    return pockets


def _to_model(curve, sketch):
    curve = curve.copy()
    curve.transformBy(sketch.transform)
    return curve


def _onto_plane(curve, plane):
    """Move a curve from a parallel sketch plane onto the face plane."""
    ev = curve.evaluator
    _, t0, t1 = ev.getParameterExtents()
    _, points = ev.getPointsAtParameters([t0, (t0 + t1) / 2, t1])
    n = plane.normal
    dists = [plane.origin.vectorTo(p).dotProduct(n) for p in points]
    if max(dists) - min(dists) > PLANE_TOL:
        raise LightenError('Strut and excluded-region sketches must be parallel to the face.')
    d = dists[0]
    if abs(d) > PLANE_TOL:
        shift = n.copy()
        shift.scaleBy(-d)
        m = adsk.core.Matrix3D.create()
        m.translation = shift
        curve.transformBy(m)
    return curve
