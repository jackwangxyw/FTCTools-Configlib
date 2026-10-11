"""Belt: closed timing belt around two pulleys (GT2 2mm, GT2 3mm, HTD 3mm, HTD 5mm).

The belt is its own component holding a Fusion custom feature that groups one
base feature: the belt solid. Belt teeth, both pulley tooth counts, width and
offset are custom parameters. The center distance follows from them and is
shown in the dialog. Placement: pulley 1 (a Pulley, or its center point and
optional plane, as for Pulley) and optionally pulley 2 (a Pulley or a point)
for the direction. Without pulley 2 the belt runs along the plane's x axis
(the sketch's x axis for a sketch point).
"""

import collections
import json
import math
import os
import traceback

import adsk.core
import adsk.fusion

from .. import links, panel
from ..pulley import entry as pulley_entry
from ..pulley import profiles
from ..pulley.profiles import PulleyError
from ..sketch_tools import center as center_distance
from ..sketch_tools import common as sketch_common
from ..lighten.geometry import body_edges
from . import body

app = adsk.core.Application.get()
ui = app.userInterface

CMD_ID = 'FTCTools_Belt'
EDIT_CMD_ID = 'FTCTools_BeltEdit'
FEATURE_ID = 'FTCToolsBelt'
ICONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources')
SETTINGS_PATH = os.path.join(os.getenv('APPDATA') or os.path.expanduser('~'), 'FTCTools', 'belt.json')
OVERLAY_COLOR = (255, 210, 0)
# Rubber - Weathered in the Fusion Appearance Library, by id so it's found in
# any UI language.
APPEARANCE_LIBRARY = 'BA5EE55E-9982-449B-9D66-9F036540E140'
APPEARANCE_ID = 'Prism-132'
SKETCH_CENTERS = pulley_entry.SKETCH_CENTERS
CIRCULAR = pulley_entry.CIRCULAR
FIT_TOLERANCE = 0.01  # cm; a belt further than this off pulley 2's center gets a warning
LABELS = {'teeth1': 'P1', 'teeth2': 'P2', 'belt_teeth': 'Belt'}
# Pulley 1/2 pick faces and edges. No body filter while picking: with one on,
# Fusion takes the body under the cursor before its edges, so other bodies'
# edges couldn't be picked. A picked Pulley face is swapped for the Pulley's
# body right after the click (from a custom event, not inside the select).
PICK_FILTERS = ('SketchPoints', 'SketchCircles', 'CircularEdges', 'Faces', 'Vertices', 'ConstructionPoints')
SWAP_EVENT = 'FTCTools_BeltPulleyBody'

SIZES = [('width', 'Width', 'Width', '6 mm'), ('offset', 'Offset', 'Offset', '0 mm')]
COUNTS = [('belt_teeth', 'BeltTeeth', 'Belt teeth', 20, 2000, 150),
          ('teeth1', 'Pulley1Teeth', 'Pulley 1 teeth', 6, 400, 20),
          ('teeth2', 'Pulley2Teeth', 'Pulley 2 teeth', 6, 400, 40)]
PROFILE_KEYS = [k for k, _, _ in profiles.PROFILES]
DEFAULTS = dict([(s[0], s[3]) for s in SIZES] + [(c[0], c[5]) for c in COUNTS] + [('profile', 'gt2_2'), ('flip', False)])

_handlers = []
_feature_def = None
_edit = {}
_overlay = []
_creating = [False]
_auto = [None]   # the belt teeth last filled in as the nearest fit
_swap = {}       # the Pulley body a picked face is swapped for


# ----------------------------------------------------------------- lifecycle

def start():
    global _feature_def
    create_def = ui.commandDefinitions.addButtonDefinition(
        CMD_ID, 'Belt', 'Timing belt around two pulleys: GT2 2mm, GT2 3mm, HTD 3mm or HTD 5mm.', ICONS)
    _on(create_def.commandCreated, adsk.core.CommandCreatedEventHandler, _create_created)

    edit_def = ui.commandDefinitions.addButtonDefinition(EDIT_CMD_ID, 'Edit Belt', 'Edit a Belt feature.', ICONS)
    _on(edit_def.commandCreated, adsk.core.CommandCreatedEventHandler, _edit_created)

    _feature_def = adsk.fusion.CustomFeatureDefinition.create(FEATURE_ID, 'Belt', ICONS)
    _feature_def.editCommandId = EDIT_CMD_ID
    _on(_feature_def.customFeatureCompute, adsk.fusion.CustomFeatureEventHandler, _compute)

    _on(app.registerCustomEvent(SWAP_EVENT), adsk.core.CustomEventHandler, _swap_to_body)

    control = panel.get().controls.addCommand(create_def)
    control.isPromotedByDefault = True
    control.isPromoted = True


def stop():
    _clear_overlay()
    app.unregisterCustomEvent(SWAP_EVENT)
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
                ui.messageBox('Belt failed:\n' + traceback.format_exc())
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


def _build_inputs(cmd, values):
    inputs = cmd.commandInputs
    units = _design().unitsManager.defaultLengthUnits

    sel = inputs.addSelectionInput('center', 'Pulley 1', 'A Pulley, or its center: sketch point or circle, circular edge or face, or any point with a plane')
    _set_pick_filters(sel, False)
    sel.setSelectionLimits(1, 1)
    sel = inputs.addSelectionInput('toward', 'Pulley 2', 'Optional: a Pulley or a point toward it. Without it the belt runs along x.')
    _set_pick_filters(sel, False)
    sel.setSelectionLimits(0, 1)
    sel = inputs.addSelectionInput('plane', 'Plane', 'Plane or face the belt starts on (optional for sketch points)')
    sel.addSelectionFilter('PlanarFaces')
    sel.addSelectionFilter('ConstructionPlanes')
    sel.setSelectionLimits(0, 1)

    dd = inputs.addDropDownCommandInput('profile', 'Profile', adsk.core.DropDownStyles.TextListDropDownStyle)
    for key, label, _ in profiles.PROFILES:
        dd.listItems.add(label, key == values['profile'])
    for input_id, _, label, lo, hi, _ in COUNTS:
        inputs.addIntegerSpinnerCommandInput(input_id, label, lo, hi, 1, int(values[input_id]))
    for input_id, _, label, _ in SIZES:
        inputs.addValueInput(input_id, label, units, adsk.core.ValueInput.createByString(values[input_id]))
    inputs.itemById('offset').tooltip = 'Distance from the plane to the belt, e.g. a pulley flange thickness.'
    inputs.addBoolValueInput('flip', 'Flip direction', True, '', bool(values['flip']))
    inputs.addTextBoxCommandInput('cc', 'Center distance', '', 1, True)
    inputs.addTextBoxCommandInput('status', 'Status', '', 4, True).isVisible = False


def _set_pick_filters(sel, bodies):
    sel.clearSelectionFilter()
    for f in PICK_FILTERS + (('SolidBodies',) if bodies else ()):
        sel.addSelectionFilter(f)


def _select_body(sel, body):
    """Make a Pulley body the input's selection: the body filter is on only
    for this."""
    _set_pick_filters(sel, True)
    try:
        sel.clearSelection()
        sel.addSelection(body)
    finally:
        _set_pick_filters(sel, False)


def _swap_to_body(args):
    sel = _swap.pop('input', None)
    body = _swap.pop('body', None)
    if sel is None or not sel.isValid:
        return  # the dialog closed first
    _edit['populating'] = True   # the dialog already filled in from this Pulley
    try:
        _select_body(sel, body)
    finally:
        _edit['populating'] = False


def _selection(inputs, input_id):
    sel = inputs.itemById(input_id)
    return sel.selection(0).entity if sel.selectionCount else None


def _pick(inputs, input_id):
    """A center pick as the belt uses it: a Pulley's face or edge is its body."""
    return _pulley_body(_selection(inputs, input_id))


def _read_inputs(inputs):
    label = inputs.itemById('profile').selectedItem.name
    v = {'center': _pick(inputs, 'center'), 'toward': _pick(inputs, 'toward'),
         'plane': _selection(inputs, 'plane'),
         'profile': next(k for k, l, _ in profiles.PROFILES if l == label),
         'flip': inputs.itemById('flip').value}
    for s in SIZES:
        v[s[0]] = inputs.itemById(s[0]).expression
    for c in COUNTS:
        v[c[0]] = inputs.itemById(c[0]).value
    v['links'] = _links(v)
    v['follow'] = _follow_ends(v)
    return v


def _follow_ends(v):
    """Pulley teeth that follow a circle from a sketch derived from another
    design, where there is no tooth parameter to link to: bit 1 for pulley 1,
    2 for pulley 2. Compute reads them from the circles (_follow_circles).
    Same rule as a link: the dialog matches the circle."""
    bits = 0
    for bit, input_id, entity in ((1, 'teeth1', v['center']), (2, 'teeth2', v['toward'])):
        circle, tag = sketch_common.derived_tag(entity)
        if (tag is not None and tag['profile'] == v['profile'] and input_id not in v['links']
                and sketch_common.circle_teeth(circle, tag) == v[input_id]):
            bits |= bit
    return bits


def _sources(v):
    """What each count can follow, from the picks: {count id: (expression,
    profile)}. A center distance between the two picks gives all three;
    otherwise a Pulley gives its teeth parameter and a Pulley & Gear Diameter
    circle its tooth-count parameter."""
    found = center_distance.between(v['center'], v['toward'])
    if found is not None:
        name, key, mode, teeth = found
        return {'teeth1': (teeth[0], key), 'teeth2': (teeth[1], key),
                'belt_teeth': (center_distance.belt_link(name, key, mode), key)}
    out = {}
    for input_id, entity in (('teeth1', v['center']), ('teeth2', v['toward'])):
        pulley = _pulley_feature(entity)
        if pulley is not None:
            p = pulley.parameters
            out[input_id] = (p.itemById('teeth').name, PROFILE_KEYS[int(round(p.itemById('profile').value))])
            continue
        tag = sketch_common.pulley_tag(entity)
        if tag is not None and tag['profile'] != sketch_common.GEAR:
            out[input_id] = (tag['teeth'], tag['profile'])
    return out


def _links(v):
    """{count id: expression} for the counts that follow their source: the
    dialog has the source's profile and count. A different count stands on its own."""
    return {i: expr for i, (expr, key) in _sources(v).items()
            if key == v['profile'] and links.evaluate(expr) == v[i]}


def _nearest(v):
    """The nearest whole belt for the distance between the two picks."""
    _, _, _, dist = _placement(v['center'], v['plane'], v['toward'], v['flip'])
    if dist is None:
        raise PulleyError('Pick pulley 2.')
    return body.nearest_teeth(v['profile'], dist - _tolerance(v['center'], v['toward']), v['teeth1'], v['teeth2'])


def _auto_solve(v):
    """Whether the belt re-solves its teeth as the pulleys move: it is on two
    picks, follows no center distance, and has the nearest fit's count."""
    if v['toward'] is None or 'belt_teeth' in v['links']:
        return False
    try:
        return _nearest(v) == v['belt_teeth']
    except PulleyError:
        return False


def _validate(args):
    inputs = args.inputs
    center = _pick(inputs, 'center')
    ok = center is not None
    if ok and center.objectType not in SKETCH_CENTERS + CIRCULAR and _pulley_feature(center) is None:
        ok = inputs.itemById('plane').selectionCount == 1
    w = inputs.itemById('width')
    off = inputs.itemById('offset')
    ok = ok and w.isValidExpression and w.value > 0 and off.isValidExpression
    args.areInputsValid = ok
    if not ok:
        _clear_overlay()


def _pre_select(args):
    # Any face of a Pulley picks that Pulley; other faces only when planar
    # with a circular boundary.
    if args.activeInput.id == 'plane':
        return
    entity = args.selection.entity
    if entity.objectType == adsk.fusion.BRepFace.classType() and _pulley_feature(entity) is None:
        args.isSelectable = pulley_entry.circle_of(pulley_entry._native(entity)) is not None


def _on_select(args):
    if _edit.get('populating'):
        return  # reopening an edit: keep the feature's own values
    inputs = args.activeInput.parentCommand.commandInputs
    entity = args.selection.entity
    pulley = _pulley_feature(entity)
    tag = sketch_common.pulley_tag(entity)
    if tag is not None and tag['profile'] == sketch_common.GEAR:
        tag = None
    if tag is not None:
        teeth = int(round(links.evaluate(tag['teeth'])))
    else:  # a circle in a derived sketch: its tooth count is in its geometry
        circle, tag = sketch_common.derived_tag(entity)
        teeth = sketch_common.circle_teeth(circle, tag) if tag else None
    if args.activeInput.id == 'center':
        if pulley is not None:
            _fill_from_pulley(inputs, pulley)
            inputs.itemById('toward').hasFocus = True
        else:
            if tag is not None:
                _select_profile(inputs, tag['profile'])
                inputs.itemById('teeth1').value = teeth
            if entity.objectType not in SKETCH_CENTERS + CIRCULAR:
                # A plane is only needed for a bare point.
                inputs.itemById('plane').hasFocus = True
            else:
                inputs.itemById('toward').hasFocus = True
    elif args.activeInput.id == 'toward':
        if pulley is not None:
            inputs.itemById('teeth2').value = int(round(pulley.parameters.itemById('teeth').value))
        elif tag is not None:
            inputs.itemById('teeth2').value = teeth
    if args.activeInput.id in ('center', 'toward'):
        _fill_belt(inputs)
        if pulley is not None and entity.objectType in CIRCULAR:
            _swap.update({'input': args.activeInput, 'body': entity.body})
            app.fireCustomEvent(SWAP_EVENT)


def _select_profile(inputs, key):
    label = next(l for k, l, _ in profiles.PROFILES if k == key)
    for item in inputs.itemById('profile').listItems:
        item.isSelected = item.name == label


def _fill_belt(inputs):
    """With both pulleys picked: a center distance between them fills in
    everything it was made with; otherwise the belt teeth become the nearest
    fit for the distance between them."""
    v = _read_inputs(inputs)
    if v['center'] is None or v['toward'] is None:
        return
    sources = _sources(v)
    if 'belt_teeth' in sources:
        _select_profile(inputs, sources['belt_teeth'][1])
        for input_id in ('teeth1', 'teeth2', 'belt_teeth'):
            value = links.evaluate(sources[input_id][0])
            if value is not None:
                inputs.itemById(input_id).value = int(round(value))
        return
    _fill_nearest(inputs, v)


def _fill_nearest(inputs, v):
    try:
        n = _nearest(v)
    except PulleyError:
        return
    inputs.itemById('belt_teeth').value = n
    _auto[0] = n


def _input_changed(args):
    if _edit.get('populating'):
        return
    inputs = args.input.parentCommand.commandInputs
    if args.input.id in ('profile', 'teeth1', 'teeth2', 'flip'):
        # A nearest-fit belt count stays the nearest fit.
        v = _read_inputs(inputs)
        if v['belt_teeth'] == _auto[0] and 'belt_teeth' not in _sources(v):
            _fill_nearest(inputs, v)
    elif args.input.id == 'width':
        pulley = _pulley_feature(_selection(inputs, 'center'))
        if pulley is not None and inputs.itemById('width').isValidExpression:
            inputs.itemById('offset').expression = _centered_offset(inputs, pulley)


def _fill_from_pulley(inputs, pulley):
    """Match the belt to a picked pulley 1: its profile, teeth, side, and an
    offset that centers the belt on its teeth. The teeth then follow the
    pulley (see _links); side and offset are copied once."""
    p = pulley.parameters
    _select_profile(inputs, PROFILE_KEYS[int(round(p.itemById('profile').value))])
    inputs.itemById('teeth1').value = int(round(p.itemById('teeth').value))
    inputs.itemById('flip').value = p.itemById('flip').value > 0.5
    inputs.itemById('offset').expression = _centered_offset(inputs, pulley)


def _pulley_span(pulley):
    """(start, width) of a Pulley's teeth from its plane, cm."""
    p = pulley.parameters
    flanges = p.itemById('flanges')
    start = p.itemById('flange_t').value if flanges is not None and flanges.value > 0.5 else 0.0
    return start, p.itemById('width').value


def _centered_offset(inputs, pulley):
    start, width = _pulley_span(pulley)
    return sketch_common.mm(start + (width - inputs.itemById('width').value) / 2)


def _connect_dialog(cmd, preview, execute, destroy):
    _on(cmd.inputChanged, adsk.core.InputChangedEventHandler, _input_changed)
    _on(cmd.validateInputs, adsk.core.ValidateInputsEventHandler, _validate)
    _on(cmd.preSelect, adsk.core.SelectionEventHandler, _pre_select)
    _on(cmd.select, adsk.core.SelectionEventHandler, _on_select)
    _on(cmd.executePreview, adsk.core.CommandEventHandler, preview)
    _on(cmd.execute, adsk.core.CommandEventHandler, execute)
    _on(cmd.destroy, adsk.core.CommandEventHandler, destroy)


def _show_center_distance(inputs, v):
    um = _design().unitsManager
    try:
        C = body.center_distance(v['profile'], v['belt_teeth'], v['teeth1'], v['teeth2'])
        inputs.itemById('cc').text = um.formatInternalValue(C, um.defaultLengthUnits, True)
    except PulleyError as e:
        inputs.itemById('cc').text = str(e)


def _show_status(inputs, v):
    """What the counts follow, how far the belt is off pulley 2, and
    mismatches with picked pulleys."""
    um = _design().unitsManager
    lines = []   # problems first, then what re-solves, then what follows what
    for i, (expr, key) in sorted(_sources(v).items()):
        if key != v['profile']:
            lines.append('%s is %s' % (LABELS[i], pulley_entry.SHORT_NAMES[key]))
    pulley = _pulley_feature(v['center'])
    if pulley is not None and inputs.itemById('width').value > _pulley_span(pulley)[1] + 1e-6:
        lines.append('Wider than P1')
    try:
        dist = _placement(v['center'], v['plane'], v['toward'], v['flip'])[3]
        if dist is not None:
            gap = (body.center_distance(v['profile'], v['belt_teeth'], v['teeth1'], v['teeth2'])
                   + _tolerance(v['center'], v['toward']) - dist)
            if abs(gap) > FIT_TOLERANCE:
                lines.append('%s off P2' % um.formatInternalValue(abs(gap), um.defaultLengthUnits, True))
            if _auto_solve(v):
                feature = _edit.get('feature')
                if feature is not None and links.param(feature, 'auto') is None:
                    lines.append('Older belt: teeth fixed')
                else:
                    lines.append('Re-solves on move')
    except PulleyError:
        pass
    lines += ['%s: %s' % (LABELS[i], v['links'][i]) for i in ('teeth1', 'teeth2', 'belt_teeth') if i in v['links']]
    lines += ['%s: derived circle' % LABELS[i] for bit, i in ((1, 'teeth1'), (2, 'teeth2')) if v['follow'] & bit]
    status = inputs.itemById('status')
    status.text = '\n'.join(lines)
    status.numRows = max(1, len(lines))   # nothing hidden below the box
    status.isVisible = bool(lines)


def _preview(args):
    _draw_preview(args.command.commandInputs)


def _draw_preview(inputs):
    """Outline the belt solid's edges. Inputs that can't make a belt show nothing."""
    _clear_overlay()
    v = _read_inputs(inputs)
    _show_center_distance(inputs, v)
    _show_status(inputs, v)
    try:
        comp, frame, phase, _ = _placement(v['center'], v['plane'], v['toward'], v['flip'], _edit.get('feature'))
        solid = _solid(_options(v, phase), frame)
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
    # In the component the belt goes in: its space, and not ghosted when active.
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


def _options(v, phase):
    um = _design().unitsManager
    units = um.defaultLengthUnits
    return {'profile': v['profile'], 'belt_teeth': v['belt_teeth'], 'teeth1': v['teeth1'], 'teeth2': v['teeth2'],
            'width': um.evaluateExpression(v['width'], units), 'offset': um.evaluateExpression(v['offset'], units),
            'phase': phase}


# -------------------------------------------------------------------- create

def _create_created(args):
    cmd = args.command
    _build_inputs(cmd, _load_settings())
    _connect_dialog(cmd, _preview, _create_execute, lambda a: _clear_overlay())


def _create_execute(args):
    _clear_overlay()
    v = _read_inputs(args.command.commandInputs)
    try:
        parent, frame, phase, _ = _placement(v['center'], v['plane'], v['toward'], v['flip'])
        solid = _solid(_options(v, phase), frame)
        look = _belt_appearance()
    except PulleyError as e:
        ui.messageBox(str(e), 'Belt')
        return
    _create_feature(v, parent, solid, look)
    _save_settings(v)


def _belt_appearance():
    """Rubber - Weathered, copied into the design from the library the first time."""
    library = app.materialLibraries.itemById(APPEARANCE_LIBRARY)
    source = library.appearances.itemById(APPEARANCE_ID) if library else None
    if source is None:
        raise PulleyError('Could not find the Rubber - Weathered appearance in the Fusion Appearance Library.')
    appearances = _design().appearances
    return appearances.itemByName(source.name) or appearances.addByCopy(source, source.name)


def _belt_name(key, teeth):
    return '%s %dT Belt' % (pulley_entry.SHORT_NAMES[key], teeth)


def _create_feature(v, parent, solid, look):
    """A new component under the placement's component, holding the belt
    feature; the solid is already in that space."""
    title = _belt_name(v['profile'], v['belt_teeth'])
    units = _design().unitsManager.defaultLengthUnits
    occ = pulley_entry.new_component(parent, title)
    comp = occ.component

    base = comp.features.baseFeatures.add()
    base.startEdit()
    body = comp.bRepBodies.add(solid, base)
    body.name = pulley_entry.free_body_name(comp, title, body)
    base.finishEdit()
    base.name = 'Belt Body'
    body.appearance = look

    cf_input = comp.features.customFeatures.createInput(_feature_def)
    for input_id, name, _, _ in SIZES:
        cf_input.addCustomParameter(input_id, name, adsk.core.ValueInput.createByString(v[input_id]), units, True)
    followed = v.get('links', {})
    for input_id, name, _, _, _, _ in COUNTS:
        value = (adsk.core.ValueInput.createByString(followed[input_id]) if input_id in followed
                 else adsk.core.ValueInput.createByReal(v[input_id]))
        cf_input.addCustomParameter(input_id, name, value, '', True)
    # Choices live in hidden parameters so compute can read them during add.
    cf_input.addCustomParameter('profile', 'profile', adsk.core.ValueInput.createByReal(PROFILE_KEYS.index(v['profile'])), '', False)
    cf_input.addCustomParameter('flip', 'flip', adsk.core.ValueInput.createByReal(1 if v['flip'] else 0), '', False)
    cf_input.addCustomParameter('auto', 'auto', adsk.core.ValueInput.createByReal(1 if _auto_solve(v) else 0), '', False)
    cf_input.addCustomParameter('follow', 'follow', adsk.core.ValueInput.createByReal(v.get('follow', 0)), '', False)
    _add_dependencies(cf_input.addDependency, v)
    cf_input.setStartAndEndFeatures(base, base)
    # Fusion computes once inside add; the base feature already holds this solid.
    _creating[0] = True
    try:
        feature = comp.features.customFeatures.add(cf_input)
    finally:
        _creating[0] = False
    feature.name = title
    return feature


def _add_dependencies(add, v):
    # A Pulley is stored as its body. That needs the belt and the Pulley in
    # different components (each has its own): a dependency on a Pulley's
    # body in the same component stops Fusion computing that Pulley at all.
    add('center', v['center'])
    if v['toward'] is not None:
        add('toward', v['toward'])
    if v['plane'] is not None:
        add('plane', v['plane'])


# ---------------------------------------------------------------------- edit

def _feature_values(feature):
    p = feature.parameters
    v = {'profile': PROFILE_KEYS[int(round(p.itemById('profile').value))], 'flip': p.itemById('flip').value > 0.5}
    for input_id, _, _, _ in SIZES:
        v[input_id] = p.itemById(input_id).expression
    for input_id, _, _, _, _, _ in COUNTS:
        v[input_id] = int(round(p.itemById(input_id).value))
    return v


def _edit_created(args):
    feature = adsk.fusion.CustomFeature.cast(ui.activeSelections.item(0).entity)
    _edit.clear()
    _edit.update({'feature': feature, 'restore': None, 'rolled': False, 'populated': False})
    auto = links.param(feature, 'auto')
    _auto[0] = int(round(feature.parameters.itemById('belt_teeth').value)) if auto and auto.value > 0.5 else None
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
    deps = _dependencies(feature, skip_lost=True)
    _edit['populating'] = True
    try:
        for input_id in ('center', 'toward', 'plane'):
            entity = deps.get(input_id)
            if entity is not None and entity.objectType == adsk.fusion.CustomFeature.classType():
                entity = pulley_entry.solid_body(entity)   # belts from an interim build stored the Pulley feature
            if entity is not None and entity.objectType == adsk.fusion.BRepBody.classType():
                _select_body(inputs.itemById(input_id), entity)
            elif entity is not None:
                inputs.itemById(input_id).addSelection(entity)
    finally:
        _edit['populating'] = False
    _draw_preview(inputs)


def _edit_execute(args):
    feature = _edit['feature']
    _clear_overlay()
    feature.timelineObject.rollTo(True)
    v = _read_inputs(args.command.commandInputs)
    # Values that can't make a belt: say why, as create does, and change nothing.
    try:
        _, frame, phase, _ = _placement(v['center'], v['plane'], v['toward'], v['flip'], feature)
        _solid(_options(v, phase), frame)
    except PulleyError as e:
        ui.messageBox(str(e), 'Belt')
        return

    # Rolled back to before this feature: one recompute when it rolls forward.
    feature.dependencies.deleteAll()
    _add_dependencies(feature.dependencies.add, v)
    p = feature.parameters
    for input_id, _, _, _ in SIZES:
        p.itemById(input_id).expression = v[input_id]
    for input_id, _, _, _, _, _ in COUNTS:
        p.itemById(input_id).expression = v['links'].get(input_id, str(int(v[input_id])))
    p.itemById('profile').value = PROFILE_KEYS.index(v['profile'])
    p.itemById('flip').value = 1 if v['flip'] else 0
    auto = links.param(feature, 'auto')
    if auto is not None:  # belts from 0.1 don't have it and stay fixed
        auto.value = 1 if _auto_solve(v) else 0
    follow = links.param(feature, 'follow')
    if follow is not None:  # nor belts from before v0.2.1 this
        follow.value = v['follow']

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
        deps = _dependencies(feature)
        _follow_circles(feature, deps)
        v = _feature_values(feature)
        _, frame, phase, dist = _placement(deps['center'], deps.get('plane'), deps.get('toward'), v['flip'], feature)
        p = feature.parameters
        o = {'profile': v['profile'], 'belt_teeth': v['belt_teeth'], 'teeth1': v['teeth1'], 'teeth2': v['teeth2'],
             'width': p.itemById('width').value, 'offset': p.itemById('offset').value, 'phase': phase}
        auto = links.param(feature, 'auto')
        # Pulley 2 is meant to sit the center distance's tolerance off the fit.
        fit = dist - _tolerance(deps['center'], deps.get('toward')) if dist is not None else None
        if auto is not None and auto.value > 0.5 and dist is not None:
            o['belt_teeth'] = body.nearest_teeth(v['profile'], fit, v['teeth1'], v['teeth2'])
            if o['belt_teeth'] != v['belt_teeth']:
                # Keep the parameter showing the re-solved count. Writing it
                # here doesn't trigger another compute.
                p.itemById('belt_teeth').value = o['belt_teeth']
        solid = _solid(o, frame)
        base.startEdit()
        try:
            belt_body = base.bodies.item(0)
            updated = base.updateBody(belt_body, solid)
            if updated:
                pulley_entry.follow_name(feature, belt_body, _belt_name(v['profile'], o['belt_teeth']))
        finally:
            base.finishEdit()
        if not updated:
            raise PulleyError('Could not update the belt body.')
        if dist is not None:
            gap = body.center_distance(v['profile'], o['belt_teeth'], v['teeth1'], v['teeth2']) - fit
            if abs(gap) > FIT_TOLERANCE:
                # Yellow in the timeline. Custom features can't show their own
                # text, so the reason goes to the Text Commands log. The empty
                # id matters: 'API_COMPUTE_WARNING' marks the feature failed
                # (red) and makes every later API edit in the design throw.
                app.log('Belt (%s): %dT fits pulley 2 %.3f mm %s.' % (
                    feature.name, o['belt_teeth'], abs(gap) * 10, 'farther out' if gap > 0 else 'closer in'))
                args.computeStatus.statusMessages.addWarning('', '')
    except PulleyError as e:
        # Custom feature compute errors can't carry custom text, so the reason
        # goes to the Text Commands log and the feature gets Fusion's message.
        app.log('Belt (%s): %s' % (feature.name, e))
        args.computeStatus.statusMessages.addError('API_COMPUTE_ERROR', '')
    except Exception:
        # A modal dialog here would block Fusion mid-recompute.
        app.log('Belt (%s) failed:\n%s' % (feature.name, traceback.format_exc()))
        args.computeStatus.statusMessages.addError('API_COMPUTE_ERROR', '')


def _follow_circles(feature, deps):
    """Pulley teeth following circles in a derived sketch (_follow_ends) take
    the counts and profile those circles now have. Writing a feature's own
    parameters in its compute doesn't start another compute."""
    follow = links.param(feature, 'follow')
    bits = int(round(follow.value)) if follow is not None else 0
    p = feature.parameters
    for bit, input_id, dep in ((1, 'teeth1', 'center'), (2, 'teeth2', 'toward')):
        if not bits & bit:
            continue
        circle, tag = sketch_common.derived_tag(deps.get(dep))
        if tag is None:
            continue
        teeth = sketch_common.circle_teeth(circle, tag)
        if int(round(p.itemById(input_id).value)) != teeth:
            p.itemById(input_id).value = teeth
        index = PROFILE_KEYS.index(tag['profile'])
        if int(round(p.itemById('profile').value)) != index:
            p.itemById('profile').value = index


def _dependencies(feature, skip_lost=False):
    """center, toward and plane by id. A reference to deleted geometry is an
    error for the recompute; edit passes skip_lost, since editing fixes it."""
    deps = {}
    for i in range(feature.dependencies.count):
        dep = feature.dependencies.item(i)
        if dep.entity is None:
            if skip_lost:
                continue
            raise PulleyError('Lost the reference "%s". Edit the feature and reselect it.' % dep.id)
        deps[dep.id] = dep.entity
    if not skip_lost and 'center' not in deps:
        raise PulleyError('The belt needs pulley 1\'s center.')
    return deps


# ------------------------------------------------------------------ geometry

def _pulley_body(entity):
    """A face or edge of a Pulley stands for the Pulley's body: that is what
    gets stored, since a face can be replaced when the pulley recomputes."""
    if entity is not None and entity.objectType in CIRCULAR and _pulley_feature(entity) is not None:
        return entity.body
    return entity


def _pulley_feature(entity):
    """The Pulley feature that made a body (or the body an edge or face is
    on), or None. A stored Pulley feature is itself."""
    if entity is not None and entity.objectType == adsk.fusion.CustomFeature.classType():
        return entity if entity.definition.id == pulley_entry.FEATURE_ID else None
    if entity is not None and entity.objectType in CIRCULAR:
        entity = entity.body
    if entity is None or entity.objectType != adsk.fusion.BRepBody.classType():
        return None
    native = entity.nativeObject if entity.assemblyContext else entity
    # A component can hold several pulleys and other bodies: find the one
    # whose base feature made this body.
    for feature in native.parentComponent.features.customFeatures:
        if feature.definition.id == pulley_entry.FEATURE_ID and pulley_entry.solid_body(feature) == native:
            return feature
    return None


def _center_and_plane(center, plane):
    """A picked Pulley stands for its own center and plane."""
    pulley = _pulley_feature(center)
    if pulley is not None:
        return pulley_entry._dependencies(pulley)
    return center, plane


def _tolerance(center, toward):
    """The tolerance of the center distance dimensioned between the two
    pulleys, cm: pulley 2 is meant to be that far off where the belt fits."""
    found = center_distance.between(_center_and_plane(center, None)[0], _center_and_plane(toward, None)[0])
    tol = center_distance.tolerance(found[0]) if found else None
    return tol.value if tol else 0.0


def _point_in(entity, comp, comp_occ):
    """A center pick's position in comp's space (comp_occ: comp's occurrence
    in pulley 1's assembly path, or None)."""
    native = pulley_entry._native(entity)
    p = pulley_entry.center_point(native)
    owner = pulley_entry._owner(native)
    if owner == comp:
        if pulley_entry._other_copy(entity.assemblyContext, comp_occ):
            raise PulleyError('Pulley 2 is in another copy of %s than pulley 1. The belt goes in %s, '
                              'so every copy gets one: pick both in the same copy.' % (comp.name, comp.name))
        return p
    p = p.copy()
    if entity.assemblyContext:
        p.transformBy(entity.assemblyContext.transform2)
    elif owner != _design().rootComponent:
        raise PulleyError('Pick pulley 2 in the assembly, not inside another component.')
    if comp_occ is not None:
        to_comp = comp_occ.transform2.copy()
        to_comp.invert()
        p.transformBy(to_comp)
    return p


def _placement(center, plane, toward, flip, feature=None):
    """(component, (origin, x, y, z), phase, distance). x points at pulley 2
    (the plane's x axis without one); phase is the angle, in the belt's frame,
    of pulley 1's grooves, so a Pulley on the same center meshes with the belt;
    distance is pulley 2's center from pulley 1's in the belt plane (None
    without pulley 2)."""
    center, plane = _center_and_plane(center, plane)
    comp, (origin, fx, fy, z) = pulley_entry._placement(center, plane, feature)
    d = fx.copy()
    dist = None
    if toward is not None:
        target, _ = _center_and_plane(toward, None)
        d = origin.vectorTo(_point_in(target, comp, pulley_entry._space(center, feature)[1]))
        along = z.copy()
        along.scaleBy(d.dotProduct(z))
        d.subtract(along)
        if d.length < 1e-6:
            raise PulleyError('Pulley 2 is on pulley 1\'s center.')
        dist = d.length
        d.normalize()
    beta = math.atan2(d.dotProduct(fy), d.dotProduct(fx))
    z = z.copy()
    if flip:
        z.scaleBy(-1)
    y = z.crossProduct(d)
    # Pulley grooves sit at k * step from the pulley frame's x axis (either
    # flip). Seen from the belt frame that is -beta + k * step, or +beta when
    # the belt's frame is mirrored by the flip.
    return comp, (origin, d, y, z), (beta if flip else -beta), dist


def _solid(o, frame):
    origin, x, y, z = frame
    solid = _cached(tuple(sorted(o.items())), lambda: body.build(o)[0])
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
