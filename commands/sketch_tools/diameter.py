"""Pulley & Gear Diameter: dimension a sketch circle as a timing pulley's or a
spur gear's pitch, outside or root diameter, driven by a tooth-count user
parameter.

Click a circle, pick the profile (a belt profile, or Gear with its module),
tooth count and which diameter. The tooth count becomes a user parameter
named for the profile and count (HTD5_24T, or M1_5_24T for a module 1.5 gear;
a second one of the same gets a _2 suffix), and the circle's diameter
dimension is an expression on it, e.g.

    pitch    HTD5_24T * 5 mm / PI
    outside  HTD5_24T * 5 mm / PI - 2 * 0.5715 mm      (2 * PLD)
    root     outside - 2 * 2.08 mm                       (2 * tooth depth)

Gears are standard spur gears (addendum 1 m, dedendum 1.25 m):

    pitch    M1_5_24T * 1.5 mm
    outside  (M1_5_24T + 2) * 1.5 mm
    root     (M1_5_24T - 2.5) * 1.5 mm

To edit one, double-click its dimension (or right-click it or the circle,
Edit Pulley & Gear Diameter), or run the tool on the circle again. The
parameter is renamed to match (Fusion updates every expression that uses it),
and changing a pulley's profile rebuilds the center distances on it.
"""

import math
import os

import adsk.core
import adsk.fusion

from .. import links, panel
from ..pulley import profiles
from . import center, common
from .common import SketchToolError

CMD_ID = 'FTCTools_PulleyDiameter'
EDIT_CMD_ID = 'FTCTools_PulleyDiameterEdit'
TITLE = 'Pulley & Gear Diameter'
ICONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', 'diameter')
KINDS = [('pitch', 'Pitch'), ('outside', 'Outside'), ('root', 'Root')]
SHORT = {'gt2_2': 'GT2_2', 'gt2_3': 'GT2_3', 'htd_3': 'HTD3', 'htd_5': 'HTD5'}  # parameter-name safe
DEFAULTS = {'profile': 'htd_5', 'teeth': 24, 'kind': 'pitch', 'module': '1 mm'}

_handlers = []
_ui_handlers = []   # (UI event, handler): these outlive commands and must come off in stop
_edit_event = []
_editing = {}


def start():
    cmd_def = common.ui.commandDefinitions.addButtonDefinition(
        CMD_ID, TITLE, 'Dimension a circle as a timing pulley\'s or a spur gear\'s pitch, outside or root diameter.', ICONS)
    common.on(_handlers, cmd_def.commandCreated, adsk.core.CommandCreatedEventHandler, _created, TITLE)
    control = panel.get(sketch=True).controls.addCommand(cmd_def)
    control.isPromotedByDefault = True
    control.isPromoted = True
    edit_def = common.ui.commandDefinitions.addButtonDefinition(
        EDIT_CMD_ID, 'Edit ' + TITLE, 'Reopen the %s dialog for this circle.' % TITLE, ICONS)
    common.on(_handlers, edit_def.commandCreated, adsk.core.CommandCreatedEventHandler, _edit_created, TITLE)
    _edit_event.append(common.install_editing(_ui_handlers, _handlers, TITLE, EDIT_CMD_ID, _match))


def stop():
    # UI hooks first, so a failure below can't leave them attached.
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


def _match(entity):
    """The tagged circle behind a diameter/radius dimension or the circle itself."""
    if entity.objectType in (adsk.fusion.SketchDiameterDimension.classType(),
                             adsk.fusion.SketchRadialDimension.classType()):
        entity = entity.entity
    return entity if common.pulley_tag(entity) else None


def _edit_created(args):
    _editing['circle'] = common.editing_target(EDIT_CMD_ID, _match)
    _created(args)
    common.on(_handlers, args.command.activate, adsk.core.CommandEventHandler, _edit_activate, TITLE)


def _edit_activate(args):
    # Selections can only be added once the dialog is up.
    circle = _editing.pop('circle', None)
    if circle is None:
        return
    inputs = args.command.commandInputs
    inputs.itemById('circle').addSelection(circle)
    _load_tag(inputs, circle)


def expression(key, teeth_param, kind, module=None):
    """module (cm) is for gears only."""
    if key == common.GEAR:
        m = common.mm(module)
        return {'pitch': '%s * %s' % (teeth_param, m),
                'outside': '(%s + 2) * %s' % (teeth_param, m),
                'root': '(%s - 2.5) * %s' % (teeth_param, m)}[kind]
    spec = profiles.profile(key)
    expr = '%s * %s / PI' % (teeth_param, common.mm(spec['pitch']))
    if kind in ('outside', 'root'):
        expr += ' - 2 * %s' % common.mm(spec['pld'])
    if kind == 'root':
        expr += ' - 2 * %s' % common.mm(spec['depth'])
    return expr


def diameter(key, teeth, kind, module=None):
    if key == common.GEAR:
        return {'pitch': teeth, 'outside': teeth + 2, 'root': teeth - 2.5}[kind] * module
    spec = profiles.profile(key)
    d = teeth * spec['pitch'] / math.pi
    if kind in ('outside', 'root'):
        d -= 2 * spec['pld']
    if kind == 'root':
        d -= 2 * spec['depth']
    return d


# -------------------------------------------------------------------- dialog

def _created(args):
    cmd = args.command
    inputs = cmd.commandInputs
    sel = inputs.addSelectionInput('circle', 'Circle', 'Sketch circle to dimension')
    sel.addSelectionFilter('SketchCircles')
    sel.setSelectionLimits(1, 1)
    common.add_profile_dropdown(inputs, DEFAULTS['profile'], gear=True)
    inputs.addValueInput('module', 'Module', 'mm', adsk.core.ValueInput.createByString(DEFAULTS['module']))
    inputs.addIntegerSpinnerCommandInput('teeth', 'Teeth', 6, 400, 1, DEFAULTS['teeth'])
    dd = inputs.addDropDownCommandInput('kind', 'Diameter', adsk.core.DropDownStyles.TextListDropDownStyle)
    for k, label in KINDS:
        dd.listItems.add(label, k == DEFAULTS['kind'])
    inputs.addTextBoxCommandInput('result', 'Result', '', 1, True)
    _show_result(inputs)

    common.on(_handlers, cmd.select, adsk.core.SelectionEventHandler, _on_select, TITLE)
    common.on(_handlers, cmd.inputChanged, adsk.core.InputChangedEventHandler,
              lambda a: _show_result(a.input.parentCommand.commandInputs), TITLE)
    common.on(_handlers, cmd.validateInputs, adsk.core.ValidateInputsEventHandler, _validate, TITLE)
    common.on(_handlers, cmd.execute, adsk.core.CommandEventHandler, _execute, TITLE)


def _read(inputs):
    sel = inputs.itemById('circle')
    return {'circle': sel.selection(0).entity if sel.selectionCount else None,
            'profile': common.profile_key(inputs.itemById('profile').selectedItem.name),
            'module': inputs.itemById('module').value,
            'teeth': inputs.itemById('teeth').value,
            'kind': next(k for k, l in KINDS if l == inputs.itemById('kind').selectedItem.name)}


def _module_ok(inputs):
    m = inputs.itemById('module')
    return m.isValidExpression and m.value > 0


def _show_result(inputs):
    v = _read(inputs)
    inputs.itemById('module').isVisible = v['profile'] == common.GEAR
    if v['profile'] == common.GEAR and not _module_ok(inputs):
        inputs.itemById('result').text = 'Module must be above 0.'
        return
    um = common.design().unitsManager
    d = diameter(v['profile'], v['teeth'], v['kind'], v['module'])
    inputs.itemById('result').text = um.formatInternalValue(d, um.defaultLengthUnits, True)


def _on_select(args):
    _load_tag(args.activeInput.parentCommand.commandInputs, args.selection.entity)


def _load_tag(inputs, circle):
    # A circle this tool already dimensioned: load what it was made with.
    tag = common.pulley_tag(circle)
    if tag is None:
        return
    common.select_item(inputs.itemById('profile'), common.profile_label(tag['profile']))
    if tag['profile'] == common.GEAR:
        inputs.itemById('module').expression = common.mm(tag['module'])
    common.select_item(inputs.itemById('kind'), dict(KINDS)[tag['kind']])
    teeth = common.design().userParameters.itemByName(tag['teeth'])
    inputs.itemById('teeth').value = int(round(teeth.value))
    _show_result(inputs)


def _validate(args):
    v = _read(args.inputs)
    args.areInputsValid = v['circle'] is not None and (v['profile'] != common.GEAR or _module_ok(args.inputs))


def _execute(args):
    v = _read(args.command.commandInputs)
    try:
        apply(v['circle'], v['profile'], v['teeth'], v['kind'], v['module'])
    except SketchToolError as e:
        args.executeFailed = True  # roll back what apply already changed
        common.ui.messageBox(str(e), TITLE)


def param_base(key, teeth, module=None):
    if key == common.GEAR:
        # Module 1.5 mm -> M1_5
        return 'M%s_%dT' % (common.mm(module)[:-3].replace('.', '_'), int(teeth))
    return '%s_%dT' % (SHORT[key], int(teeth))


def apply(circle, key, teeth, kind, module=None, chain=True):
    """module (cm) is for gears only. chain=False leaves the circles joined to
    this one by center distances alone (the caller is switching them)."""
    tag = common.pulley_tag(circle)
    if key == common.GEAR and center.users(circle):
        raise SketchToolError('Center distance %s runs a belt on this circle, so it can\'t be a gear.'
                              % center.users(circle)[0])
    params = common.design().userParameters
    current = params.itemByName(tag['teeth']) if tag is not None else None
    teeth_param = common.free_name(param_base(key, teeth, module), current)
    if current is None:
        common.set_param(teeth_param, str(int(teeth)), '', 'Tooth count (FTCTools)')
    else:
        if current.name != teeth_param:
            current.name = teeth_param  # Fusion updates the expressions using it
        current.expression = str(int(teeth))
    expr = expression(key, teeth_param, kind, module)
    circle = common.native(circle)
    dim = _existing_dimension(circle)
    try:
        if dim is None:
            c = circle.centerSketchPoint.geometry
            r = circle.radius
            text = adsk.core.Point3D.create(c.x + r * 0.9, c.y + r * 0.9, 0)
            dim = circle.parentSketch.sketchDimensions.addDiameterDimension(circle, text)
            dim.parameter.expression = expr
        elif dim.objectType == adsk.fusion.SketchRadialDimension.classType():
            dim.parameter.expression = '(%s) / 2' % expr
        else:
            dim.parameter.expression = expr
    except RuntimeError as e:
        raise SketchToolError('Could not dimension the circle (is it already fully constrained?):\n%s' % e)
    new_tag = {'profile': key, 'teeth': teeth_param, 'kind': kind}
    if key == common.GEAR:
        new_tag['module'] = module
    common.set_pulley_tag(circle, new_tag)
    # Pulleys made on this circle follow its tooth parameter (and Fusion
    # renamed their expressions with it); bring their profile along too.
    if key == common.GEAR:
        links.unlink(teeth_param)  # a gear drives no pulley
    elif tag is None or tag['profile'] != key:
        links.push_profile(teeth_param, key)
    if chain and (tag is None or tag['profile'] != key) and center.users(circle):
        # One belt profile runs through every circle joined by center
        # distances: switch them all, then rebuild the center distances.
        joined = center.chain(circle)
        for other in joined:
            other_tag = common.pulley_tag(other)
            if other != circle and other_tag['profile'] != key:
                count = params.itemByName(other_tag['teeth']).value
                apply(other, key, int(round(count)), other_tag['kind'], chain=False)
        for c in joined:
            center.follow_profile(c, key)
    return dim


def _existing_dimension(circle):
    """The circle's driving diameter or radius dimension, if it has one."""
    for dim in circle.parentSketch.sketchDimensions:
        if dim.objectType in (adsk.fusion.SketchDiameterDimension.classType(),
                              adsk.fusion.SketchRadialDimension.classType()):
            if dim.entity == circle and dim.isDriving:
                return dim
    return None
