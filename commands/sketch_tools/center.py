"""Center Distance: dimension two sketch entities to a belt's center distance.

Click two points or circles (their centers), or two parallel lines. Pick the
profile, both pulley tooth counts, and the belt's tooth count or length. A
user parameter named CC1, CC2, ... (<Name> below) solves the center distance from the
tooth-count and belt parameters, and the dimension is just "<Name>", so its
edit box stays short:

    C0 = (b + sqrt(b^2 - 8 d^2)) / 4,   b = L - pi (r1 + r2),  d = r2 - r1
    C  = one Newton step from C0 on the exact belt length
         L(C) = 2 C cos(phi) + pi (r1 + r2) + 2 phi d,  sin(phi) = d / C

C0 is inlined, so the parameters added are <Name>, the belt's (<Name>_BeltTeeth
or <Name>_BeltLength) and tooth counts for picks that aren't Pulley & Gear Diameter
circles. One step is within 0.002 mm of exact even for a 5:1 ratio packed
tight, and ~1e-9 mm for normal layouts. <Name>_Tolerance is added on top, to
set the pulleys apart by more (or less) than the belt's exact fit.

To edit one, double-click its dimension (or right-click, Edit Center
Distance): the dialog reopens with its two entities and values.

Circles dimensioned with Pulley & Gear Diameter fill in the profile and tooth count,
and the expressions use their <Pulley>_Teeth parameters directly, so a pulley
tooth change resizes the circle and moves the centers together.
"""

import json
import os

import adsk.core
import adsk.fusion

from .. import links, panel
from ..belt import body as belt_body
from ..pulley import profiles
from ..pulley.profiles import PulleyError
from . import common
from .common import SketchToolError

CMD_ID = 'FTCTools_CenterDistance'
EDIT_CMD_ID = 'FTCTools_CenterDistanceEdit'
ICONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', 'center')
MODES = [('teeth', 'Belt teeth'), ('length', 'Belt length')]
CC_ATTR = 'center:'   # design attribute per dimension name: profile and belt mode
DEFAULTS = {'profile': 'htd_5', 'teeth1': 24, 'teeth2': 36, 'mode': 'teeth', 'belt_teeth': 90, 'belt_length': '450 mm',
            'tolerance': '0 mm'}

_handlers = []
_ui_handlers = []   # (UI event, handler): these outlive commands and must come off in stop
_editing = {}
_edit_event = []


def start():
    cmd_def = common.ui.commandDefinitions.addButtonDefinition(
        CMD_ID, 'Center Distance', 'Dimension two points, circles or lines to a belt\'s center distance.', ICONS)
    common.on(_handlers, cmd_def.commandCreated, adsk.core.CommandCreatedEventHandler, _created, 'Center Distance')
    control = panel.get(sketch=True).controls.addCommand(cmd_def)
    control.isPromotedByDefault = True
    control.isPromoted = True
    edit_def = common.ui.commandDefinitions.addButtonDefinition(
        EDIT_CMD_ID, 'Edit Center Distance', 'Reopen the Center Distance dialog for this dimension.', ICONS)
    common.on(_handlers, edit_def.commandCreated, adsk.core.CommandCreatedEventHandler, _edit_created, 'Center Distance')
    _edit_event.append(common.install_editing(_ui_handlers, _handlers, 'Center Distance', EDIT_CMD_ID, _match))


def stop():
    # Editing hooks are on the UI, not a command; take them off first, so a
    # failure below can't leave them attached.
    if _edit_event:
        common.remove_editing(_ui_handlers, _edit_event.pop())
    control = panel.get(sketch=True).controls.itemById(CMD_ID)
    if control:
        control.deleteMe()
    panel.remove_if_empty(sketch=True)
    for cmd_id in (CMD_ID, EDIT_CMD_ID):
        cmd_def = common.ui.commandDefinitions.itemById(cmd_id)
        if cmd_def:
            cmd_def.deleteMe()
    _handlers.clear()


def _our_name(dim):
    """The CC name of a dimension this tool made, or None."""
    if dim is None or not hasattr(dim, 'parameter') or dim.parameter is None:
        return None
    name = dim.parameter.expression.strip()
    if not common.NAME_RE.match(name):
        return None
    return name if common.design().attributes.itemByName(common.ATTR_GROUP, CC_ATTR + name) else None


def _match(entity):
    """A dimension this tool made, for editing."""
    return entity if _our_name(entity) else None


def expression(key, t1, t2, length, tolerance=None):
    """The center distance from tooth-count expressions t1, t2 (parameter
    names or numbers) and a belt pitch-length expression, plus a tolerance
    expression if given."""
    p = common.mm(profiles.profile(key)['pitch'])
    d = '((%s - %s) * %s / (2 * PI))' % (t2, t1, p)
    wrap = '(%s + %s) * %s / 2' % (t1, t2, p)             # pi (r1 + r2)
    b = '(%s - %s)' % (length, wrap)
    c0 = '(%s + sqrt(%s ^ 2 - 8 * %s ^ 2)) / 4' % (b, b, d)

    def newton(prev):
        phi = 'asin(%s / %s)' % (d, prev)
        return '%s - (2 * %s * cos(%s) + %s + 2 * (%s / 1 rad) * %s - %s) / (2 * cos(%s))' % (
            prev, prev, phi, wrap, phi, d, length, phi)
    exact = newton('(%s)' % c0)
    return exact if tolerance is None else '%s + %s' % (exact, tolerance)


def tolerance(name):
    """The tolerance parameter of center distance `name`, or None: ones made
    before there was a tolerance have none."""
    return common.design().userParameters.itemByName(name + '_Tolerance')


# -------------------------------------------------------------------- dialog

def _created(args):
    cmd = args.command
    inputs = cmd.commandInputs
    for input_id, label in (('one', 'Pulley 1'), ('two', 'Pulley 2')):
        sel = inputs.addSelectionInput(input_id, label, 'Point, circle (its center) or line')
        for f in ('SketchPoints', 'SketchCircles', 'SketchLines'):
            sel.addSelectionFilter(f)
        sel.setSelectionLimits(1, 1)
    common.add_profile_dropdown(inputs, DEFAULTS['profile'])
    inputs.addIntegerSpinnerCommandInput('teeth1', 'Pulley 1 teeth', 6, 400, 1, DEFAULTS['teeth1'])
    inputs.addIntegerSpinnerCommandInput('teeth2', 'Pulley 2 teeth', 6, 400, 1, DEFAULTS['teeth2'])
    dd = inputs.addDropDownCommandInput('mode', 'Belt', adsk.core.DropDownStyles.TextListDropDownStyle)
    for k, label in MODES:
        dd.listItems.add(label, k == DEFAULTS['mode'])
    inputs.addIntegerSpinnerCommandInput('belt_teeth', 'Belt teeth', 20, 2000, 1, DEFAULTS['belt_teeth'])
    inputs.addValueInput('belt_length', 'Belt length', common.design().unitsManager.defaultLengthUnits,
                         adsk.core.ValueInput.createByString(DEFAULTS['belt_length']))
    inputs.itemById('belt_length').tooltip = 'Pitch length (circumference at the pitch line).'
    inputs.addValueInput('tolerance', 'Tolerance', common.design().unitsManager.defaultLengthUnits,
                         adsk.core.ValueInput.createByString(DEFAULTS['tolerance']))
    inputs.itemById('tolerance').tooltip = 'Added to the center distance, e.g. for belt stretch.'
    inputs.addTextBoxCommandInput('result', 'Center distance', '', 1, True)
    _refresh(inputs)

    common.on(_handlers, cmd.select, adsk.core.SelectionEventHandler, _on_select, 'Center Distance')
    common.on(_handlers, cmd.inputChanged, adsk.core.InputChangedEventHandler,
              lambda a: _refresh(a.input.parentCommand.commandInputs), 'Center Distance')
    common.on(_handlers, cmd.validateInputs, adsk.core.ValidateInputsEventHandler, _validate, 'Center Distance')
    common.on(_handlers, cmd.execute, adsk.core.CommandEventHandler, _execute, 'Center Distance')


def _edit_created(args):
    dim = common.editing_target(EDIT_CMD_ID, _match)
    _editing.clear()
    _editing['entities'] = _dimension_entities(dim)
    _created(args)
    common.on(_handlers, args.command.activate, adsk.core.CommandEventHandler, _edit_activate, 'Center Distance')


def _edit_activate(args):
    # Selections can only be added once the dialog is up.
    if not _editing:
        return
    one, two = _editing.pop('entities')
    if one is None:
        return  # started without a dimension (e.g. Repeat): an empty dialog
    inputs = args.command.commandInputs
    inputs.itemById('one').addSelection(one)
    inputs.itemById('two').addSelection(two)
    _load_existing(inputs, _read(inputs))  # profile, tooth counts and belt
    _refresh(inputs)


def _selection(inputs, input_id):
    sel = inputs.itemById(input_id)
    return sel.selection(0).entity if sel.selectionCount else None


def _read(inputs):
    return {'one': _selection(inputs, 'one'), 'two': _selection(inputs, 'two'),
            'profile': common.profile_key(inputs.itemById('profile').selectedItem.name),
            'teeth1': inputs.itemById('teeth1').value, 'teeth2': inputs.itemById('teeth2').value,
            'mode': next(k for k, l in MODES if l == inputs.itemById('mode').selectedItem.name),
            'belt_teeth': inputs.itemById('belt_teeth').value,
            'belt_length': inputs.itemById('belt_length').expression,
            'tolerance': inputs.itemById('tolerance').expression}


def _refresh(inputs):
    v = _read(inputs)
    inputs.itemById('belt_teeth').isVisible = v['mode'] == 'teeth'
    inputs.itemById('belt_length').isVisible = v['mode'] == 'length'
    um = common.design().unitsManager
    try:
        C = center_distance(v)
        inputs.itemById('result').text = um.formatInternalValue(C, um.defaultLengthUnits, True)
    except (PulleyError, RuntimeError) as e:
        inputs.itemById('result').text = str(e)


def center_distance(v):
    pitch = profiles.profile(v['profile'])['pitch']
    um = common.design().unitsManager
    if v['mode'] == 'teeth':
        belt_teeth = v['belt_teeth']
    else:
        belt_teeth = um.evaluateExpression(v['belt_length'], um.defaultLengthUnits) / pitch
    return (belt_body.center_distance(v['profile'], belt_teeth, v['teeth1'], v['teeth2'])
            + um.evaluateExpression(v['tolerance'], um.defaultLengthUnits))


def _on_select(args):
    inputs = args.activeInput.parentCommand.commandInputs
    entity = args.selection.entity
    # A Pulley & Gear Diameter circle: take its profile and tooth count.
    tag = _belt_tag(entity)
    if tag is not None:
        common.select_item(inputs.itemById('profile'), common.profile_label(tag['profile']))
        teeth = common.design().userParameters.itemByName(tag['teeth'])
        inputs.itemById('teeth1' if args.activeInput.id == 'one' else 'teeth2').value = int(round(teeth.value))
    if args.activeInput.id == 'one':
        inputs.itemById('two').hasFocus = True
    # A pair this tool already dimensioned: load what it was made with.
    v = _read(inputs)
    if v['one'] is not None and v['two'] is not None:
        _load_existing(inputs, v)
    _refresh(inputs)


def _load_existing(inputs, v):
    try:
        dim = _existing_dimension(*_targets(v['one'], v['two']))
    except SketchToolError:
        return
    name = _our_name(dim)
    if name is None:
        return
    made = json.loads(common.design().attributes.itemByName(common.ATTR_GROUP, CC_ATTR + name).value)
    params = common.design().userParameters
    common.select_item(inputs.itemById('profile'), common.profile_label(made['profile']))
    common.select_item(inputs.itemById('mode'), dict(MODES)[made['mode']])
    # Tooth counts: a Pulley & Gear Diameter circle's parameter, else this one's own.
    for index, input_id, entity in ((1, 'teeth1', v['one']), (2, 'teeth2', v['two'])):
        tag = _belt_tag(entity)
        param = params.itemByName(tag['teeth'] if tag else '%s_Teeth%d' % (name, index))
        if param is not None:
            inputs.itemById(input_id).value = int(round(param.value))
    belt = params.itemByName(name + ('_BeltTeeth' if made['mode'] == 'teeth' else '_BeltLength'))
    if belt is not None and made['mode'] == 'teeth':
        inputs.itemById('belt_teeth').value = int(round(belt.value))
    elif belt is not None:
        inputs.itemById('belt_length').expression = belt.expression
    tol = tolerance(name)
    if tol is not None:
        inputs.itemById('tolerance').expression = tol.expression


def _validate(args):
    v = _read(args.inputs)
    ok = v['one'] is not None and v['two'] is not None
    if ok and v['mode'] == 'length':
        ok = args.inputs.itemById('belt_length').isValidExpression
    ok = ok and args.inputs.itemById('tolerance').isValidExpression
    args.areInputsValid = ok


def _execute(args):
    v = _read(args.command.commandInputs)
    try:
        center_distance(v)
        apply(v)
    except (SketchToolError, PulleyError) as e:
        args.executeFailed = True  # roll back what apply already changed
        common.ui.messageBox(str(e), 'Center Distance')


def apply(v):
    one, two = _targets(v['one'], v['two'])
    dim = _existing_dimension(one, two)
    # Editing keeps the dimension's name; a new one gets the next free CCn.
    name = _our_name(dim) or common.unique_name('CC')
    params = common.design().userParameters

    # Tooth counts: a Pulley & Gear Diameter circle's own parameter, else ours.
    teeth = []
    for index, entity, count in ((1, v['one'], v['teeth1']), (2, v['two'], v['teeth2'])):
        tag = common.pulley_tag(entity)
        if tag is not None:
            if tag['profile'] == common.GEAR:
                raise SketchToolError('Pulley %d is a gear circle; a belt needs pulleys.' % index)
            if tag['profile'] != v['profile']:
                raise SketchToolError('Pulley %d is %s, not %s. Change one of them to match.' % (
                    index, common.profile_label(tag['profile']), common.profile_label(v['profile'])))
            common.set_param(tag['teeth'], str(int(count)), '')
            teeth.append(tag['teeth'])
        else:
            own = '%s_Teeth%d' % (name, index)
            common.set_param(own, str(int(count)), '', 'Pulley tooth count (FTC Tools)')
            teeth.append(own)

    if v['mode'] == 'teeth':
        common.set_param(name + '_BeltTeeth', str(int(v['belt_teeth'])), '', 'Belt tooth count (FTC Tools)')
        unused = name + '_BeltLength'
    else:
        common.set_param(name + '_BeltLength', v['belt_length'], 'mm', 'Belt pitch length (FTC Tools)')
        unused = name + '_BeltTeeth'

    length = _length(name, v['profile'], v['mode'])
    tol = common.set_param(name + '_Tolerance', v['tolerance'], 'mm', 'Added to the center distance (FTC Tools)')
    common.set_param(name, expression(v['profile'], teeth[0], teeth[1], length, tol.name), 'mm',
                     'Center distance (FTC Tools)')
    try:
        if dim is None:
            dim = _add_dimension(one, two)
        dim.parameter.expression = name
    except RuntimeError as e:
        raise SketchToolError('Could not dimension them (are they already constrained?):\n%s' % e)
    # Belts made on this center distance follow its belt parameter: move them
    # to the new mode or pitch before the old parameter goes (Fusion won't
    # delete a parameter something references).
    made = common.design().attributes.itemByName(common.ATTR_GROUP, CC_ATTR + name)
    if made is not None:
        made = json.loads(made.value)
        links.relink(belt_link(name, made['profile'], made['mode']), belt_link(name, v['profile'], v['mode']))
    links.push_profile(belt_link(name, v['profile'], v['mode']), v['profile'])
    # Switching between belt teeth and length leaves the other one unused.
    old = params.itemByName(unused)
    if old is not None:
        old.deleteMe()
    common.design().attributes.add(common.ATTR_GROUP, CC_ATTR + name, json.dumps(
        {'profile': v['profile'], 'mode': v['mode']}))
    return dim


def _belt_tag(entity):
    """A Pulley & Gear Diameter circle's tag for the dialog to load; gears take no belt."""
    tag = common.pulley_tag(entity)
    return tag if tag is None or tag['profile'] != common.GEAR else None


def _length(name, key, mode):
    """The belt pitch-length expression of center distance `name`."""
    if mode == 'teeth':
        return '%s_BeltTeeth * %s' % (name, common.mm(profiles.profile(key)['pitch']))
    return name + '_BeltLength'


def belt_link(name, key, mode):
    """The belt tooth-count expression a Belt on center distance `name`
    follows. A belt length becomes the nearest whole number of teeth."""
    if mode == 'teeth':
        return name + '_BeltTeeth'
    return 'round(%s_BeltLength / %s)' % (name, common.mm(profiles.profile(key)['pitch']))


def between(one, two):
    """The center distance dimensioned between two picks (points or circles),
    as (name, profile, mode, [tooth-count expression of each pick]), or None."""
    kinds = (adsk.fusion.SketchPoint.classType(), adsk.fusion.SketchCircle.classType())
    if one is None or two is None or one.objectType not in kinds or two.objectType not in kinds:
        return None
    try:
        a, b = _targets(one, two)
    except SketchToolError:
        return None
    dim = _existing_dimension(a, b)
    name = _our_name(dim)
    if name is None:
        return None
    made = json.loads(common.design().attributes.itemByName(common.ATTR_GROUP, CC_ATTR + name).value)
    first = _dimension_entities(dim)[0]
    teeth = []
    for point in (a, b):
        tag = common.pulley_tag(point)
        teeth.append(tag['teeth'] if tag else '%s_Teeth%d' % (name, 1 if point == first else 2))
    return name, made['profile'], made['mode'], teeth


def users(circle):
    """Names of the center distances dimensioned to a circle's center."""
    point = common.native(circle).centerSketchPoint
    return [_our_name(dim) for dim in point.parentSketch.sketchDimensions
            if _our_name(dim) and point in _dimension_entities(dim)]


def chain(circle):
    """The Pulley & Gear Diameter circles joined to `circle` through center
    distances, directly or through other circles, and `circle` itself."""
    circle = common.native(circle)
    sketch = circle.parentSketch
    dims = [_dimension_entities(dim) for dim in sketch.sketchDimensions if _our_name(dim)]
    joined, todo = [circle], [circle]
    while todo:
        point = todo.pop().centerSketchPoint
        for ends in dims:
            if point not in ends:
                continue
            other = ends[1] if ends[0] == point else ends[0]
            found = next((c for c in sketch.sketchCurves.sketchCircles
                          if c.centerSketchPoint == other and common.pulley_tag(c)), None)
            if found is not None and found not in joined:
                joined.append(found)
                todo.append(found)
    return joined


def follow_profile(circle, key):
    """Rebuild the center distances on a circle whose Pulley & Gear Diameter tag
    changed to profile `key` or was just added, so they use its pitch and its
    tooth-count parameter. Tooth and belt counts stay."""
    point = common.native(circle).centerSketchPoint
    params = common.design().userParameters
    for dim in point.parentSketch.sketchDimensions:
        name = _our_name(dim)
        ends = _dimension_entities(dim)
        if name is None or point not in ends:
            continue
        attr = common.design().attributes.itemByName(common.ATTR_GROUP, CC_ATTR + name)
        made = json.loads(attr.value)
        teeth = []
        replaced = []   # this dimension's own tooth counts, now a tagged circle's
        for index, entity in enumerate(ends, 1):
            tag = common.pulley_tag(entity)
            own = '%s_Teeth%d' % (name, index)
            teeth.append(tag['teeth'] if tag else own)
            if tag:
                replaced.append((own, tag['teeth']))
        tol = tolerance(name)
        common.set_param(name, expression(key, teeth[0], teeth[1], _length(name, key, made['mode']),
                                          tol.name if tol else None), 'mm')
        # Belts on it follow the new pitch and tooth parameters; then the
        # replaced ones are unreferenced and can go.
        links.relink(belt_link(name, made['profile'], made['mode']), belt_link(name, key, made['mode']))
        for own, tagged in replaced:
            links.relink(own, tagged)
            old = params.itemByName(own)
            if old is not None:
                old.deleteMe()
        for t in teeth:
            links.push_profile(t, key)
        made['profile'] = key
        attr.value = json.dumps(made)


def _targets(one, two):
    """What the dimension goes between: two sketch points (a circle means its
    center) or two lines, in the same sketch."""
    def point_or_line(entity):
        entity = common.native(entity)
        if entity.objectType == adsk.fusion.SketchCircle.classType():
            return entity.centerSketchPoint
        return entity
    a, b = point_or_line(one), point_or_line(two)
    if a.parentSketch != b.parentSketch:
        raise SketchToolError('Pick both in the same sketch.')
    lines = [e.objectType == adsk.fusion.SketchLine.classType() for e in (a, b)]
    if any(lines) and not all(lines):
        raise SketchToolError('Pick two points/circles, or two parallel lines.')
    if a == b:
        raise SketchToolError('Pick two different entities.')
    return a, b


def _dimension_entities(dim):
    for first, second in (('entityOne', 'entityTwo'), ('line', 'entityTwo')):
        try:
            return getattr(dim, first), getattr(dim, second)
        except AttributeError:
            continue
    return None, None


def _existing_dimension(a, b):
    """A driving distance dimension already between a and b, if any."""
    for dim in a.parentSketch.sketchDimensions:
        if not dim.isDriving or dim.objectType not in (adsk.fusion.SketchLinearDimension.classType(),
                                                  adsk.fusion.SketchOffsetDimension.classType()):
            continue
        e1, e2 = _dimension_entities(dim)
        if (e1 == a and e2 == b) or (e1 == b and e2 == a):
            return dim
    return None


def _add_dimension(a, b):
    dims = a.parentSketch.sketchDimensions
    if a.objectType == adsk.fusion.SketchLine.classType():
        m = a.startSketchPoint.geometry
        n = b.startSketchPoint.geometry
        return dims.addOffsetDimension(a, b, adsk.core.Point3D.create((m.x + n.x) / 2 + 0.5, (m.y + n.y) / 2, 0))
    m, n = a.geometry, b.geometry
    text = adsk.core.Point3D.create((m.x + n.x) / 2, (m.y + n.y) / 2 + 0.5, 0)
    return dims.addDistanceDimension(a, b, adsk.fusion.DimensionOrientations.AlignedDimensionOrientation, text)
