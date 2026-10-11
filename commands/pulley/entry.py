"""Pulley: parametric timing pulley (GT2 2mm, GT2 3mm, HTD 3mm, HTD 5mm).

Each pulley is its own component holding a Fusion custom feature that groups
one base feature: the pulley solid (teeth, optional flanges, bore, recesses).
Every size is a custom parameter, so it shows up in Change Parameters and
accepts expressions. The center point (and optional plane) are dependencies,
so moving the point moves the pulley.
"""

import collections
import json
import os
import re
import traceback

import adsk.core
import adsk.fusion

from .. import links, panel
from ..sketch_tools import common as sketch_common
from ..lighten.geometry import body_edges
from . import body, profiles
from .profiles import PulleyError

app = adsk.core.Application.get()
ui = app.userInterface

CMD_ID = 'FTCTools_Pulley'
EDIT_CMD_ID = 'FTCTools_PulleyEdit'
FEATURE_ID = 'FTCToolsPulley'
ICONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources')
SETTINGS_PATH = os.path.join(os.getenv('APPDATA') or os.path.expanduser('~'), 'FTCTools', 'pulley.json')
SHORT_NAMES = {'gt2_2': 'GT2-2', 'gt2_3': 'GT2-3', 'htd_3': 'HTD3', 'htd_5': 'HTD5'}
OVERLAY_COLOR = (255, 210, 0)
SKETCH_CENTERS = (adsk.fusion.SketchPoint.classType(), adsk.fusion.SketchCircle.classType(),
                  adsk.fusion.SketchArc.classType())
# Picks that bring their own plane: a circular edge or arc, or a planar face
# bounded by one (the end of a hub), stand for its center and plane.
CIRCULAR = (adsk.fusion.BRepEdge.classType(), adsk.fusion.BRepFace.classType())

# Sized parameters: (id, parameter name, dialog label, units kind, default).
SIZES = [
    ('width', 'Width', 'Width', 'length', '10 mm'),
    ('clearance', 'Clearance', 'Clearance', 'length', '0.05 mm'),
    ('flange_t', 'FlangeThickness', 'Thickness', 'length', '1 mm'),
    ('flange_h', 'FlangeHeight', 'Height', 'length', '1.5 mm'),
    ('flange_angle', 'FlangeAngle', 'Angle', 'angle', '30 deg'),
    ('bore_d', 'BoreDiameter', 'Bore diameter', 'length', '8 mm'),
    ('hex_af', 'HexWidth', 'Hex width', 'length', '7.15 mm'),
    ('bolt_circle', 'BoltCircle', 'Bolt circle', 'length', '16 mm'),
    ('hole_d', 'HoleDiameter', 'Hole diameter', 'length', '4.3 mm'),
    ('cb_d', 'CounterboreDiameter', 'Head diameter', 'length', '7.5 mm'),
    ('cb_depth', 'CounterboreDepth', 'Head depth', 'length', '4 mm'),
    ('layer_h', 'BridgeLayerHeight', 'Layer height', 'length', '0.2 mm'),
    ('bearing_od', 'BearingOD', 'Bearing OD', 'length', '14 mm'),
    ('bearing_depth', 'BearingDepth', 'Bearing depth', 'length', '4 mm'),
    ('hub_d', 'HubRecessDiameter', 'Hub diameter', 'length', '22 mm'),
    ('hub_depth', 'HubRecessDepth', 'Hub depth', 'length', '2 mm'),
]
# Counts: (id, parameter name, dialog label, min, max, default).
COUNTS = [('teeth', 'Teeth', 'Teeth', 6, 400, 24), ('hole_count', 'HoleCount', 'Hole count', 1, 16, 4)]
# Choices kept in hidden unitless parameters: (id, list of (key, label)).
CHOICES = [
    ('profile', [(k, l) for k, l, _ in profiles.PROFILES]),
    ('bore', body.BORES),
    ('pattern', [(k, l) for k, l, _ in body.PATTERNS]),
]
FLAGS = [('flip', 'Flip direction', False), ('flanges', 'Flanges', True),
         ('bearing', 'Bearing recess', False), ('bearing_flip', 'Flip bearing', False),
         ('counterbore', 'Counterbores', False),
         ('bridging', 'Seq. bridging', False), ('hub_recess', 'Hub recess', False),
         ('hub_flip', 'Flip hub', False)]
DEFAULTS = dict([(s[0], s[4]) for s in SIZES] + [(c[0], c[5]) for c in COUNTS] + [(f[0], f[2]) for f in FLAGS]
                + [('profile', 'htd_5'), ('bore', 'round'), ('pattern', 'gobilda')])

_handlers = []
_feature_def = None
_edit = {}
_overlay = []
_creating = [False]


# ----------------------------------------------------------------- lifecycle

def start():
    global _feature_def
    create_def = ui.commandDefinitions.addButtonDefinition(
        CMD_ID, 'Pulley', 'Timing pulley: GT2 2mm, GT2 3mm, HTD 3mm or HTD 5mm.', ICONS)
    _on(create_def.commandCreated, adsk.core.CommandCreatedEventHandler, _create_created)

    edit_def = ui.commandDefinitions.addButtonDefinition(EDIT_CMD_ID, 'Edit Pulley', 'Edit a Pulley feature.', ICONS)
    _on(edit_def.commandCreated, adsk.core.CommandCreatedEventHandler, _edit_created)

    _feature_def = adsk.fusion.CustomFeatureDefinition.create(FEATURE_ID, 'Pulley', ICONS)
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
                ui.messageBox('Pulley failed:\n' + traceback.format_exc())
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


def _save_settings(v):
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, 'w', encoding='utf-8') as f:
        json.dump({k: v[k] for k in DEFAULTS}, f, indent=2)


# -------------------------------------------------------------------- dialog

def _design():
    return adsk.fusion.Design.cast(app.activeProduct)


def _units(kind):
    return _design().unitsManager.defaultLengthUnits if kind == 'length' else 'deg'


def _build_inputs(cmd, values):
    inputs = cmd.commandInputs

    sel = inputs.addSelectionInput('center', 'Center', 'Sketch point or circle, circular edge or face, or any point with a plane')
    for f in ('SketchPoints', 'SketchCircles', 'CircularEdges', 'PlanarFaces', 'Vertices', 'ConstructionPoints'):
        sel.addSelectionFilter(f)
    sel.setSelectionLimits(1, 1)
    sel = inputs.addSelectionInput('plane', 'Plane', 'Plane or face the pulley starts on (optional for sketch points)')
    sel.addSelectionFilter('PlanarFaces')
    sel.addSelectionFilter('ConstructionPlanes')
    sel.setSelectionLimits(0, 1)

    def dropdown(parent, input_id, label):
        dd = parent.addDropDownCommandInput(input_id, label, adsk.core.DropDownStyles.TextListDropDownStyle)
        for key, text in dict(CHOICES)[input_id]:
            dd.listItems.add(text, key == values[input_id])

    def size(parent, input_id):
        _, _, label, kind, _ = next(s for s in SIZES if s[0] == input_id)
        parent.addValueInput(input_id, label, _units(kind), adsk.core.ValueInput.createByString(values[input_id]))

    def flag(parent, input_id):
        label = next(f[1] for f in FLAGS if f[0] == input_id)
        parent.addBoolValueInput(input_id, label, True, '', bool(values[input_id]))

    def count(parent, input_id):
        _, _, label, lo, hi, _ = next(c for c in COUNTS if c[0] == input_id)
        parent.addIntegerSpinnerCommandInput(input_id, label, lo, hi, 1, int(values[input_id]))

    dropdown(inputs, 'profile', 'Profile')
    count(inputs, 'teeth')
    inputs.addTextBoxCommandInput('link', 'Link', '', 1, True).isVisible = False
    size(inputs, 'width')
    size(inputs, 'clearance')
    inputs.itemById('clearance').tooltip = 'Groove grows by this on every side, for print fit.'
    flag(inputs, 'flip')

    g = inputs.addGroupCommandInput('flange_group', 'Flanges').children
    flag(g, 'flanges')
    for i in ('flange_t', 'flange_h', 'flange_angle'):
        size(g, i)

    g = inputs.addGroupCommandInput('bore_group', 'Bore').children
    dropdown(g, 'bore', 'Bore')
    size(g, 'bore_d')
    size(g, 'hex_af')
    dropdown(g, 'pattern', 'Bolt pattern')
    inputs.itemById('pattern').tooltip = 'goBILDA: 4x M4 on a 16 mm square. REV: 4x M3 on a 16 mm circle.'
    size(g, 'bolt_circle')
    count(g, 'hole_count')
    size(g, 'hole_d')
    flag(g, 'counterbore')
    size(g, 'cb_d')
    size(g, 'cb_depth')
    flag(g, 'bridging')
    size(g, 'layer_h')
    flag(g, 'bearing')
    size(g, 'bearing_od')
    size(g, 'bearing_depth')
    flag(g, 'bearing_flip')
    flag(g, 'hub_recess')
    size(g, 'hub_d')
    size(g, 'hub_depth')
    flag(g, 'hub_flip')

    _update_visibility(inputs)


def _choice(inputs, input_id):
    label = inputs.itemById(input_id).selectedItem.name
    return next(k for k, l in dict(CHOICES)[input_id] if l == label)


def _relevant(v):
    """Which inputs matter for these choices. Drives both dialog and parameter visibility."""
    bore = v['bore']
    return {
        'flange_t': v['flanges'], 'flange_h': v['flanges'], 'flange_angle': v['flanges'],
        'bore_d': bore in ('round', 'hub'), 'hex_af': bore == 'hex',
        'pattern': bore == 'hub',
        'bolt_circle': bore == 'hub' and v['pattern'] == 'custom',
        'hole_count': bore == 'hub' and v['pattern'] == 'custom',
        'hole_d': bore == 'hub' and v['pattern'] == 'custom',
        'bearing': bore in ('round', 'hub'),
        'bearing_od': bore in ('round', 'hub') and v['bearing'],
        'bearing_depth': bore in ('round', 'hub') and v['bearing'],
        'bearing_flip': bore in ('round', 'hub') and v['bearing'],
        'hub_recess': bore != 'none',
        'hub_d': bore != 'none' and v['hub_recess'],
        'hub_depth': bore != 'none' and v['hub_recess'],
        'counterbore': bore == 'hub',
        'cb_d': bore == 'hub' and v['counterbore'],
        'cb_depth': bore == 'hub' and v['counterbore'],
        'bridging': bore == 'hub' and v['counterbore'],
        'layer_h': bore == 'hub' and v['counterbore'] and v['bridging'],
        # Which end the hub sits on: moves the hub recess, and the
        # counterbores (screw heads) go on the other end.
        'hub_flip': (bore != 'none' and v['hub_recess']) or (bore == 'hub' and v['counterbore']),
    }


def _update_visibility(inputs):
    v = {'bore': _choice(inputs, 'bore'), 'pattern': _choice(inputs, 'pattern'),
         'flanges': inputs.itemById('flanges').value, 'bearing': inputs.itemById('bearing').value,
         'hub_recess': inputs.itemById('hub_recess').value, 'counterbore': inputs.itemById('counterbore').value,
         'bridging': inputs.itemById('bridging').value}
    for input_id, shown in _relevant(v).items():
        inputs.itemById(input_id).isVisible = shown


def _selection(inputs, input_id):
    sel = inputs.itemById(input_id)
    return sel.selection(0).entity if sel.selectionCount else None


def _read_inputs(inputs):
    v = {'center': _selection(inputs, 'center'), 'plane': _selection(inputs, 'plane')}
    for input_id, _ in CHOICES:
        v[input_id] = _choice(inputs, input_id)
    for s in SIZES:
        v[s[0]] = inputs.itemById(s[0]).expression
    for c in COUNTS:
        v[c[0]] = inputs.itemById(c[0]).value
    for f in FLAGS:
        v[f[0]] = inputs.itemById(f[0]).value
    v['teeth_link'] = _teeth_link(v)
    v['follow'] = v['teeth_link'] is None and _follows(v)
    return v


def _teeth_link(v):
    """The tooth-count parameter of the Pulley & Gear Diameter circle picked as
    the center, while the dialog matches it (profile and count), else None.
    The pulley's teeth follow it; a different count stands on its own."""
    tag = sketch_common.pulley_tag(v['center'])
    if tag is None or tag['profile'] != v['profile']:
        return None
    return tag['teeth'] if links.evaluate(tag['teeth']) == v['teeth'] else None


def _follows(v):
    """Whether the pulley follows a circle from a sketch derived from another
    design, where there is no tooth parameter to link to: compute reads the
    count from the circle (_follow_circle). Same rule as a link: the dialog
    matches the circle."""
    circle, tag = sketch_common.derived_tag(v['center'])
    return (tag is not None and tag['profile'] == v['profile']
            and sketch_common.circle_teeth(circle, tag) == v['teeth'])


def _options(v):
    """Evaluated build options (cm, radians) from dialog values."""
    um = _design().unitsManager
    o = {k: v[k] for k in ('profile', 'bore', 'pattern', 'teeth', 'hole_count', 'flanges', 'bearing', 'bearing_flip', 'counterbore', 'bridging', 'hub_recess', 'hub_flip', 'flip')}
    for input_id, _, _, kind, _ in SIZES:
        value = um.evaluateExpression(v[input_id], _units(kind))
        o[input_id] = value  # angles evaluate to radians
    return o


def _input_changed(args):
    if args.input.id in ('bore', 'pattern', 'flanges', 'bearing', 'hub_recess', 'counterbore', 'bridging'):
        # args.inputs is only the changed input's group; look up from the command.
        _update_visibility(args.input.parentCommand.commandInputs)


def _validate(args):
    inputs = args.inputs
    center = _selection(inputs, 'center')
    ok = center is not None
    if ok and center.objectType not in SKETCH_CENTERS + CIRCULAR:
        ok = inputs.itemById('plane').selectionCount == 1
    for input_id, _, _, _, _ in SIZES:
        i = inputs.itemById(input_id)
        if not i.isVisible and input_id not in ('width', 'clearance'):
            continue
        minimum_ok = i.value >= 0 if input_id in ('clearance', 'flange_angle') else i.value > 0
        ok = ok and i.isValidExpression and minimum_ok
    args.areInputsValid = ok
    if not ok:
        _clear_overlay()


def _on_select(args):
    if args.activeInput.id != 'center' or _edit.get('populating'):
        return
    inputs = args.activeInput.parentCommand.commandInputs
    entity = args.selection.entity
    # A Pulley & Gear Diameter circle: take its profile and tooth count (from
    # the geometry for a circle in a derived sketch).
    tag = sketch_common.pulley_tag(entity)
    if tag is not None and tag['profile'] != sketch_common.GEAR:
        teeth = int(round(links.evaluate(tag['teeth'])))
    else:
        circle, tag = sketch_common.derived_tag(entity)
        teeth = sketch_common.circle_teeth(circle, tag) if tag else None
    if teeth is not None:
        label = next(l for k, l in dict(CHOICES)['profile'] if k == tag['profile'])
        for item in inputs.itemById('profile').listItems:
            item.isSelected = item.name == label
        inputs.itemById('teeth').value = teeth
    # After the center is picked, a plane is only needed for a bare point.
    if entity.objectType not in SKETCH_CENTERS + CIRCULAR:
        inputs.itemById('plane').hasFocus = True


def _pre_select(args):
    # Planar faces are only centers when their boundary is circular.
    if args.activeInput.id == 'center' and args.selection.entity.objectType == adsk.fusion.BRepFace.classType():
        args.isSelectable = circle_of(_native(args.selection.entity)) is not None


def _connect_dialog(cmd, preview, execute, destroy):
    _on(cmd.inputChanged, adsk.core.InputChangedEventHandler, _input_changed)
    _on(cmd.validateInputs, adsk.core.ValidateInputsEventHandler, _validate)
    _on(cmd.preSelect, adsk.core.SelectionEventHandler, _pre_select)
    _on(cmd.select, adsk.core.SelectionEventHandler, _on_select)
    _on(cmd.executePreview, adsk.core.CommandEventHandler, preview)
    _on(cmd.execute, adsk.core.CommandEventHandler, execute)
    _on(cmd.destroy, adsk.core.CommandEventHandler, destroy)


def _preview(args):
    _draw_preview(args.command.commandInputs)


def _draw_preview(inputs):
    """Outline the pulley solid's edges. Inputs that can't make a pulley show nothing."""
    _clear_overlay()
    v = _read_inputs(inputs)
    link = inputs.itemById('link')
    link.isVisible = v['teeth_link'] is not None or v['follow']
    link.text = v['teeth_link'] or ('Derived circle' if v['follow'] else '')
    try:
        comp, frame = _placement(v['center'], v['plane'], _edit.get('feature'))
        solid = _solid(_options(v), frame)
    except PulleyError:
        return
    points = []
    indices = []
    for edge in body_edges(solid):
        _, t0, t1 = edge.evaluator.getParameterExtents()
        _, strokes = edge.evaluator.getStrokes(t0, t1, 0.002)
        start = len(points) // 3
        for p in strokes:
            points += [p.x, p.y, p.z]
        for i in range(len(strokes) - 1):
            indices += [start + i, start + i + 1]
    # In the component the pulley goes in: its space, and not ghosted when active.
    group = comp.customGraphicsGroups.add()
    lines = group.addLines(adsk.fusion.CustomGraphicsCoordinates.create(points), indices, False)
    lines.color = adsk.fusion.CustomGraphicsSolidColorEffect.create(adsk.core.Color.create(*OVERLAY_COLOR, 255))
    lines.weight = 1
    _overlay.append(group)


def _clear_overlay():
    while _overlay:
        group = _overlay.pop()
        if group.isValid:
            group.deleteMe()


# -------------------------------------------------------------------- create

def _create_created(args):
    cmd = args.command
    _build_inputs(cmd, _load_settings())
    _connect_dialog(cmd, _preview, _create_execute, lambda a: _clear_overlay())


def _create_execute(args):
    _clear_overlay()
    v = _read_inputs(args.command.commandInputs)
    try:
        parent, frame = _placement(v['center'], v['plane'])
        solid = _solid(_options(v), frame)
    except PulleyError as e:
        ui.messageBox(str(e), 'Pulley')
        return
    _create_feature(v, parent, solid)
    _save_settings(v)


def _pulley_name(key, teeth):
    return '%s %dT' % (SHORT_NAMES[key], teeth)


# Names the tools give components, bodies and features (a Pulley's or a
# Belt's), with the " (1)" Fusion adds to a duplicate name; and Fusion's own
# default feature names.
AUTO_NAME = re.compile(r'^(?:%s) \d+T(?: Belt)?(?: \(\d+\))?$' % '|'.join(map(re.escape, SHORT_NAMES.values())))
DEFAULT_FEATURE_NAME = re.compile(r'^(?:Pulley|Belt)\d*$')


def solid_body(feature):
    """The body a Pulley or Belt feature makes (outside its compute: there the
    base feature's bodies are only reachable between startEdit and finishEdit)."""
    base = next(f for f in feature.features if f.objectType == adsk.fusion.BaseFeature.classType())
    return base.bodies.item(0)


def free_body_name(comp, name, body=None):
    """name, or name (2), (3), ... if another body in comp has it: Fusion
    silently keeps the old name when a body is given a taken one."""
    taken = {b.name for b in comp.bRepBodies if b != body}
    candidate, n = name, 2
    while candidate in taken:
        candidate = '%s (%d)' % (name, n)
        n += 1
    return candidate


def follow_name(feature, body, name):
    """Name a feature's component, body and timeline item for what it now is
    (linked counts and profiles change without an edit), unless the user
    named them."""
    def stale(current):
        return AUTO_NAME.match(current) and re.sub(r' \(\d+\)$', '', current) != name
    if stale(body.name):
        body.name = free_body_name(body.parentComponent, name, body)
    comp = feature.parentComponent
    if comp != _design().rootComponent and stale(comp.name):
        comp.name = name
    if stale(feature.name) or (DEFAULT_FEATURE_NAME.match(feature.name) and feature.name != name):
        feature.name = name


def new_component(parent, name):
    """A new component under parent for one Pulley or Belt. Its occurrence
    has an identity transform, so its space is parent's space."""
    occ = parent.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    occ.component.name = name
    return occ



def _create_feature(v, parent, solid):
    """A new component under the placement's component, holding the pulley
    feature; the solid is already in that space."""
    title = _pulley_name(v['profile'], v['teeth'])
    occ = new_component(parent, title)
    comp = occ.component
    base = comp.features.baseFeatures.add()
    base.startEdit()
    comp.bRepBodies.add(solid, base).name = free_body_name(comp, title)
    base.finishEdit()
    base.name = 'Pulley Body'

    relevant = _relevant(v)
    cf_input = comp.features.customFeatures.createInput(_feature_def)
    for input_id, name, _, kind, _ in SIZES:
        cf_input.addCustomParameter(input_id, name, adsk.core.ValueInput.createByString(v[input_id]),
                                    _units(kind), relevant.get(input_id, True))
    for input_id, name, _, _, _, _ in COUNTS:
        link = v.get('teeth_link') if input_id == 'teeth' else None
        value = adsk.core.ValueInput.createByString(link) if link else adsk.core.ValueInput.createByReal(v[input_id])
        cf_input.addCustomParameter(input_id, name, value, '', relevant.get(input_id, True))
    # Choices and on/off switches live in hidden parameters so compute can read
    # them even during customFeatures.add, before any other state could be stored.
    for input_id, items in CHOICES:
        index = [k for k, _ in items].index(v[input_id])
        cf_input.addCustomParameter(input_id, input_id, adsk.core.ValueInput.createByReal(index), '', False)
    for input_id, _, _ in FLAGS:
        cf_input.addCustomParameter(input_id, input_id, adsk.core.ValueInput.createByReal(1 if v[input_id] else 0), '', False)
    cf_input.addCustomParameter('follow', 'follow', adsk.core.ValueInput.createByReal(1 if v.get('follow') else 0), '', False)
    _add_dependencies(cf_input.addDependency, v)
    cf_input.setStartAndEndFeatures(base, base)
    # Fusion computes the feature once inside add. The base feature already
    # holds exactly this solid, so that compute is skipped.
    _creating[0] = True
    try:
        feature = comp.features.customFeatures.add(cf_input)
    finally:
        _creating[0] = False
    feature.name = title
    return feature


def _add_dependencies(add, v):
    add('center', v['center'])
    if v['plane'] is not None:
        add('plane', v['plane'])


# ---------------------------------------------------------------------- edit

# What a pulley made by an older version has for the parameters it lacks
# (anything not listed falls back to DEFAULTS). Custom parameters can only be
# added when a feature is created.
LEGACY = {'flanges': False, 'bore': 'none', 'bearing': False, 'hub_recess': False}


def _feature_values(feature):
    """Dialog values (expressions, counts, keys, flags) from a feature's parameters."""
    p = feature.parameters
    v = {}
    for input_id, _, _, _, default in SIZES:
        param = p.itemById(input_id)
        v[input_id] = param.expression if param else default
    for input_id, _, _, _, _, default in COUNTS:
        param = p.itemById(input_id)
        v[input_id] = int(round(param.value)) if param else default
    for input_id, items in CHOICES:
        param = p.itemById(input_id)
        v[input_id] = items[int(round(param.value))][0] if param else LEGACY.get(input_id, DEFAULTS[input_id])
    for input_id, _, _ in FLAGS:
        param = p.itemById(input_id)
        v[input_id] = param.value > 0.5 if param else LEGACY.get(input_id, DEFAULTS[input_id])
    return v


def _edit_created(args):
    feature = adsk.fusion.CustomFeature.cast(ui.activeSelections.item(0).entity)
    _edit.clear()
    _edit.update({'feature': feature, 'restore': None, 'rolled': False, 'populated': False})
    timeline = _design().timeline
    if timeline.markerPosition > 0:
        _edit['restore'] = timeline.item(timeline.markerPosition - 1)
    # Roll back before the dialog records changes, so previews (whose changes
    # Fusion undoes) can't undo the roll with them.
    feature.timelineObject.rollTo(True)
    _edit['rolled'] = True

    cmd = args.command
    _build_inputs(cmd, _feature_values(feature))
    _connect_dialog(cmd, _edit_preview, _edit_execute, _edit_destroy)
    _on(cmd.activate, adsk.core.CommandEventHandler, _edit_activate)


def _edit_preview(args):
    if _edit.get('populating'):
        return
    _edit['feature'].timelineObject.rollTo(True)
    _draw_preview(args.command.commandInputs)


def _edit_activate(args):
    if _edit.get('populated'):
        return
    _edit['populated'] = True
    feature = _edit['feature']
    feature.timelineObject.rollTo(True)
    inputs = args.command.commandInputs
    center, plane = _dependencies(feature, skip_lost=True)
    _edit['populating'] = True
    try:
        if center is not None:
            inputs.itemById('center').addSelection(center)
        if plane is not None:
            inputs.itemById('plane').addSelection(plane)
    finally:
        _edit['populating'] = False
    _draw_preview(inputs)


def _edit_execute(args):
    feature = _edit['feature']
    _clear_overlay()
    feature.timelineObject.rollTo(True)
    v = _read_inputs(args.command.commandInputs)
    # Values that can't make a pulley: say why, as create does, and change nothing.
    try:
        _solid(_options(v), _placement(v['center'], v['plane'], feature)[1])
    except PulleyError as e:
        ui.messageBox(str(e), 'Pulley')
        return

    # Rolled back to before this feature: one recompute when it rolls forward.
    feature.dependencies.deleteAll()
    _add_dependencies(feature.dependencies.add, v)
    p = feature.parameters
    relevant = _relevant(v)
    missing = []
    for input_id, _, _, _, _ in SIZES:
        param = p.itemById(input_id)
        if param is None:
            missing.append(input_id)
            continue
        param.expression = v[input_id]
        param.isVisible = relevant.get(input_id, True)
    for input_id, _, _, _, _, _ in COUNTS:
        param = p.itemById(input_id)
        if param is None:
            missing.append(input_id)
            continue
        if input_id == 'teeth':
            param.expression = v['teeth_link'] or str(int(v['teeth']))
        else:
            param.value = v[input_id]
        param.isVisible = relevant.get(input_id, True)
    for input_id, items in CHOICES:
        param = p.itemById(input_id)
        if param is None:
            missing.append(input_id)
            continue
        param.value = [k for k, _ in items].index(v[input_id])
    for input_id, _, _ in FLAGS:
        param = p.itemById(input_id)
        if param is None:
            missing.append(input_id)
            continue
        param.value = 1 if v[input_id] else 0
    if missing:
        ui.messageBox('This pulley was made with an older version of the tool, so these settings were '
                      'not saved: %s. Delete it and create it again to use them.' % ', '.join(missing), 'Pulley')
    follow = links.param(feature, 'follow')
    if follow is not None:  # pulleys from before v0.2.1 don't have it
        follow.value = 1 if v['follow'] else 0
    links.push_profile(p.itemById('teeth').name, v['profile'])  # belts following this pulley

    _restore_timeline()
    _save_settings(v)


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
    if _creating[0]:
        return
    try:
        base = next(f for f in feature.features if f.objectType == adsk.fusion.BaseFeature.classType())
        center, plane = _dependencies(feature)
        _, frame = _placement(center, plane, feature)
        _follow_circle(feature, center)
        o = _feature_options(feature)
        solid = _solid(o, frame)
        base.startEdit()
        try:
            body_ = base.bodies.item(0)
            updated = base.updateBody(body_, solid)
            if updated:
                follow_name(feature, body_, _pulley_name(o['profile'], o['teeth']))
        finally:
            base.finishEdit()
        if not updated:
            raise PulleyError('Could not update the pulley body.')
    except PulleyError as e:
        # Custom feature compute errors can't carry custom text, so the reason
        # goes to the Text Commands log and the feature gets Fusion's message.
        app.log('Pulley (%s): %s' % (feature.name, e))
        args.computeStatus.statusMessages.addError('API_COMPUTE_ERROR', '')
    except Exception:
        # A modal dialog here would block Fusion mid-recompute.
        app.log('Pulley (%s) failed:\n%s' % (feature.name, traceback.format_exc()))
        args.computeStatus.statusMessages.addError('API_COMPUTE_ERROR', '')


def _follow_circle(feature, center):
    """A pulley following a circle in a derived sketch (_follows) takes the
    tooth count and profile it now has. Writing a feature's own parameters
    in its compute doesn't start another compute."""
    follow = links.param(feature, 'follow')
    if follow is None or follow.value < 0.5:
        return
    circle, tag = sketch_common.derived_tag(center)
    if tag is None:
        return
    p = feature.parameters
    teeth = sketch_common.circle_teeth(circle, tag)
    if int(round(p.itemById('teeth').value)) != teeth:
        p.itemById('teeth').value = teeth
    index = [k for k, _ in dict(CHOICES)['profile']].index(tag['profile'])
    if int(round(p.itemById('profile').value)) != index:
        p.itemById('profile').value = index


def _feature_options(feature):
    """Build options (cm, radians) straight from the feature's parameter values."""
    v = _feature_values(feature)
    o = {k: v[k] for k in ('profile', 'bore', 'pattern', 'teeth', 'hole_count', 'flanges', 'bearing', 'bearing_flip', 'counterbore', 'bridging', 'hub_recess', 'hub_flip', 'flip')}
    um = _design().unitsManager
    for input_id, _, _, kind, default in SIZES:
        param = feature.parameters.itemById(input_id)
        # internal units: cm, radians
        o[input_id] = param.value if param else um.evaluateExpression(default, _units(kind))
    return o


def _dependencies(feature, skip_lost=False):
    """Center and plane. A reference to deleted geometry is an error for the
    recompute; edit passes skip_lost, since editing is how it gets fixed."""
    center = plane = None
    for i in range(feature.dependencies.count):
        dep = feature.dependencies.item(i)
        if dep.entity is None:
            if skip_lost:
                continue
            raise PulleyError('Lost the reference "%s". Edit the feature and reselect it.' % dep.id)
        if dep.id == 'center':
            center = dep.entity
        elif dep.id == 'plane':
            plane = dep.entity
    if center is None and not skip_lost:
        raise PulleyError('The pulley has no center.')
    return center, plane


# ------------------------------------------------------------------ geometry

def _native(entity):
    if entity.assemblyContext:
        return entity.nativeObject
    return entity


def _other_copy(a, b):
    """Occurrences a and b (None: no assembly context) are different copies
    of the same component."""
    return bool(a and b and a.fullPathName != b.fullPathName)


def _owner(native):
    t = native.objectType
    if t in SKETCH_CENTERS:
        return native.parentSketch.parentComponent
    if t in (adsk.fusion.BRepVertex.classType(), adsk.fusion.BRepEdge.classType(), adsk.fusion.BRepFace.classType()):
        return native.body.parentComponent
    return native.component  # construction point or plane


def circle_of(native):
    """(center, normal) of a circular edge or arc, or of a planar face whose
    outer boundary is arcs around one center, in its component's space; else None."""
    arcs = (adsk.core.Circle3D.classType(), adsk.core.Arc3D.classType())
    t = native.objectType
    if t == adsk.fusion.BRepEdge.classType():
        g = native.geometry
        return (g.center, g.normal) if g.objectType in arcs else None
    if t != adsk.fusion.BRepFace.classType() or native.geometry.objectType != adsk.core.Plane.classType():
        return None
    loop = next(l for l in native.loops if l.isOuter)
    edges = [e.geometry for e in loop.edges]
    if any(g.objectType not in arcs for g in edges):
        return None
    c = edges[0].center
    if any(g.center.distanceTo(c) > 1e-6 for g in edges):
        return None
    return c, native.geometry.normal


def center_point(native):
    """A center pick's point in its component's space."""
    t = native.objectType
    if t == adsk.fusion.SketchPoint.classType():
        return native.worldGeometry
    if t in SKETCH_CENTERS:
        return native.centerSketchPoint.worldGeometry
    if t in CIRCULAR:
        return circle_of(native)[0]
    return native.geometry


def _outward(native):
    """A circular pick's plane normal, pointing out of the body: a face's own
    outward normal, or for an edge that of a planar face it bounds."""
    faces = [native] if native.objectType == adsk.fusion.BRepFace.classType() else list(native.faces)
    normal = circle_of(native)[1]
    normal.normalize()
    for face in faces:
        if face.geometry.objectType == adsk.core.Plane.classType():
            _, n = face.evaluator.getNormalAtPoint(face.pointOnFace)
            n.normalize()
            if abs(abs(n.dotProduct(normal)) - 1) < 1e-6:
                return n
    return normal


def _space(entity, feature=None, other=None):
    """Where a pulley picked on entity goes: (component, that component's
    occurrence in the pick's assembly path or None, transforms taking the
    entity's own component space to that component's space).

    Normally the component that owns the entity. Nothing can be added inside
    a component linked from another design (an imported goBILDA part), so a
    pick inside one goes in the component holding the outermost linked
    occurrence. A pick on a part's edge or face goes beside that part, in the
    component holding it, not inside it. A belt (`other` is its second pick)
    goes no deeper than the component that holds both picks.

    `feature` is the existing Pulley or Belt being recomputed or edited: it
    stays in the component it is in, whatever rule put it there."""
    native = _native(entity)
    path = []   # the pick's occurrences, its own first, top level last
    occ = entity.assemblyContext
    while occ is not None:
        path.append(occ)
        occ = occ.assemblyContext
    leaf = path[0] if path else None
    if not path or _inside(feature, _owner(native)):
        return _owner(native), leaf, []
    outer = next((o for o in path if _inside(feature, _holder(o))), None)
    if outer is None:
        outer = next((o for o in reversed(path) if o.isReferencedComponent), None)
        if outer is None and hasattr(native, 'body'):
            outer = leaf
        if other is not None:
            shared = set()
            occ = other.assemblyContext
            while occ is not None:
                shared.add(occ.fullPathName)
                occ = occ.assemblyContext
            apart = next((o for o in reversed(path) if o.fullPathName not in shared), None)
            if apart is not None and (outer is None or path.index(apart) > path.index(outer)):
                outer = apart
    if outer is None:
        return _owner(native), leaf, []
    parent = outer.assemblyContext
    steps = [leaf.transform2]   # to root
    if parent is not None:
        to_parent = parent.transform2.copy()
        to_parent.invert()
        steps.append(to_parent)
    return (parent.component if parent is not None else _design().rootComponent), parent, steps


def _inside(feature, comp):
    """feature's component is directly under comp."""
    return feature is not None and any(o.component == feature.parentComponent for o in comp.occurrences)


def _holder(occ):
    """The component an occurrence is in."""
    return occ.assemblyContext.component if occ.assemblyContext else _design().rootComponent


def _moved(geometry, steps):
    geometry = geometry.copy()
    for m in steps:
        geometry.transformBy(m)
    return geometry


def _placement(center, plane, feature=None, other=None):
    """(component, (origin, x, y, z)) with the frame in that component's space.

    The pulley goes in a new component under that component (see _space).
    Without a plane, a sketch center uses the sketch's frame and a circular
    edge or face its own plane; otherwise the plane's frame, with the center
    projected onto it.
    """
    if center is None:
        raise PulleyError('Pick a center.')
    native = _native(center)
    comp, comp_occ, steps = _space(center, feature, other)
    t = native.objectType
    origin = _moved(center_point(native), steps)

    if plane is None:
        if t in SKETCH_CENTERS:
            _, x, y, z = native.parentSketch.transform.getAsCoordinateSystem()
        elif t in CIRCULAR:
            z = _outward(native)
            # Any x in the plane does; take world x (or y) flattened onto it.
            ref = adsk.core.Vector3D.create(1, 0, 0) if abs(z.x) < 0.9 else adsk.core.Vector3D.create(0, 1, 0)
            along = z.copy()
            along.scaleBy(ref.dotProduct(z))
            x = ref
            x.subtract(along)
            x.normalize()
            y = z.crossProduct(x)
        else:
            raise PulleyError('Pick a plane for a point that is not in a sketch or on a circular edge.')
        return comp, (origin, _moved(x, steps), _moved(y, steps), _moved(z, steps))

    geom = _plane_in(plane, comp, comp_occ)
    z = geom.normal.copy()
    z.normalize()
    dist = geom.origin.vectorTo(origin).dotProduct(z)
    along = z.copy()
    along.scaleBy(-dist)
    origin = origin.copy()
    origin.translateBy(along)
    x = geom.uDirection.copy()
    x.normalize()
    y = z.crossProduct(x)
    return comp, (origin, x, y, z)


def _plane_in(plane, comp, comp_occ):
    """Plane geometry in the space of comp (whose occurrence in the center's
    assembly path is comp_occ). A face's normal points out of its body, so the
    pulley builds outward from a face."""
    native = _native(plane)
    geom = native.geometry.copy()
    if native.objectType == adsk.fusion.BRepFace.classType():
        _, n = native.evaluator.getNormalAtPoint(native.pointOnFace)
        geom.normal = n
    owner = _owner(native)
    if owner == comp:
        if _other_copy(plane.assemblyContext, comp_occ):
            raise PulleyError('The plane is in another copy of %s than the center. The pulley goes in %s, '
                              'so every copy gets one: pick both in the same copy.' % (comp.name, comp.name))
        return geom
    # Different components: go through root space.
    if plane.assemblyContext:
        geom.transformBy(plane.assemblyContext.transform2)
    elif owner != _design().rootComponent:
        raise PulleyError('Pick the plane in the assembly, not inside another component.')
    if comp_occ is not None:
        to_comp = comp_occ.transform2.copy()
        to_comp.invert()
        geom.transformBy(to_comp)
    return geom


def _solid(o, frame):
    """The pulley solid placed on the frame; Flip builds it the other way."""
    origin, x, y, z = frame
    if o['flip']:
        z = z.copy()
        z.scaleBy(-1)
        y = y.copy()
        y.scaleBy(-1)  # keep the frame right-handed
    solid = _cached(tuple(sorted(o.items())), lambda: body.build(o))
    m = adsk.core.Matrix3D.create()
    m.setWithCoordinateSystem(origin, x, y, z)
    adsk.fusion.TemporaryBRepManager.get().transform(solid, m)
    return solid


# Built solids by their build options, in the tool's own frame. A solid only
# depends on its options, and rolling the timeline, editing, or moving a
# center recomputes features with options they were already built with.
_built = collections.OrderedDict()
BUILT_KEEP = 8


def _cached(key, build):
    """build() remembered under key; a copy, so the remembered solid never changes."""
    solid = _built.get(key)
    if solid is None:
        solid = build()
        _built[key] = solid
        if len(_built) > BUILT_KEEP:
            _built.popitem(last=False)
    else:
        _built.move_to_end(key)
    return adsk.fusion.TemporaryBRepManager.get().copy(solid)
