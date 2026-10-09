import importlib
import json
import panel
w = panel._panel.widget()
dock, handle, tab = panel._panel, panel._handle, panel._handle_tab
original_type = type(w)
importlib.reload(panel)
panel._panel, panel._handle, panel._handle_tab = dock, handle, tab
original_type._update_project_context = panel.AgentPanelWidget._update_project_context
original_type._apply_theme = panel.AgentPanelWidget._apply_theme
context = w.chat_column.layout().itemAt(0).layout()
if w.chat_heading is not w.active_project_label:
    context.removeWidget(w.chat_heading)
    w.chat_heading.hide()
    w.chat_heading.deleteLater()
    context.removeWidget(w.active_project_label)
    context.insertWidget(0, w.active_project_label)
    w.chat_heading = w.active_project_label
w.active_project_label.setFont(panel.U.qfont('small', bold=True))
w.active_project_label.setMinimumWidth(0)
w._apply_theme()
w._update_project_context()
print(json.dumps({'project_heading_updated': True, 'single_heading':w.chat_heading is w.active_project_label,
                  'left_position':context.itemAt(0).widget() is w.active_project_label}))
