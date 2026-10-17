"""The shared "FTCTools" panels in the Design workspace: one on the Solid tab,
one on the Sketch tab (shown while a sketch is being edited), and one on the
Sheet Metal tab."""

import adsk.core

WORKSPACE_ID = 'FusionSolidEnvironment'
TAB_ID = 'SolidTab'
PANEL_ID = 'FTCToolsPanel'
SKETCH_TAB_ID = 'SketchTab'
SKETCH_PANEL_ID = 'FTCToolsSketchPanel'
SHEET_METAL_TAB_ID = 'SheetMetalTab'
SHEET_METAL_PANEL_ID = 'FTCToolsSheetMetalPanel'
PANEL_NAME = 'FTCTools'


def _ids(sketch, sheet_metal):
    if sheet_metal:
        return SHEET_METAL_TAB_ID, SHEET_METAL_PANEL_ID
    return (SKETCH_TAB_ID, SKETCH_PANEL_ID) if sketch else (TAB_ID, PANEL_ID)


def get(sketch=False, sheet_metal=False):
    tab_id, panel_id = _ids(sketch, sheet_metal)
    ui = adsk.core.Application.get().userInterface
    tab = ui.workspaces.itemById(WORKSPACE_ID).toolbarTabs.itemById(tab_id)
    panel = tab.toolbarPanels.itemById(panel_id)
    if panel is None:
        panel = tab.toolbarPanels.add(panel_id, PANEL_NAME)
    return panel


def remove_if_empty(sketch=False, sheet_metal=False):
    tab_id, panel_id = _ids(sketch, sheet_metal)
    ui = adsk.core.Application.get().userInterface
    tab = ui.workspaces.itemById(WORKSPACE_ID).toolbarTabs.itemById(tab_id)
    panel = tab.toolbarPanels.itemById(panel_id)
    if panel is not None and panel.controls.count == 0:
        panel.deleteMe()
