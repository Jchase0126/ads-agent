import importlib
import json
import panel
from PySide6.QtWidgets import QDockWidget
old_dock, handle, tab = panel._panel, panel._handle, panel._handle_tab
old = old_dock.widget() if isinstance(old_dock, QDockWidget) else old_dock.centralWidget()
if (old._worker and old._worker.isRunning()) or any(t.isRunning() for t in old._cfg_workers):
    raise RuntimeError('Panel busy; refresh deferred')
state = dict(input=old.input.toPlainText(), dark=old.dark, settings=old.settings_btn.isChecked(),
             permission=old.allow_python.isChecked(), active=old._active_model,
             editor=old._editor_model, groups=old._provider_groups, names=old._provider_names,
             saved=old._saved_models, selected=list(old._editing_provider_models),
             provider=old.provider_name_edit.text(), address=old.base_url_edit.text(),
             key=old.api_key_edit.text(), model=old.model_combo.currentText(),
             choices=[old.model_list.item(i).text() for i in range(old.model_list.count())],
             hint=old.cfg_hint.text(), projects_file=old._PROJECTS_FILE)
old._save_projects()
importlib.reload(panel)
panel._panel, panel._handle, panel._handle_tab = old_dock, handle, tab
panel.AgentPanelWidget._PROJECTS_FILE = state['projects_file']
new = panel.AgentPanelWidget()
# Invalidate the initial config request so it cannot replace this unsaved editor.
new._profile_request_id += 1
new._active_model, new._editor_model = state['active'], state['editor']
new._provider_groups, new._provider_names = state['groups'], state['names']
new._editing_provider_models = state['selected']
new.dark = state['dark']
new._apply_theme()
new.input.setPlainText(state['input'])
new.allow_python.setChecked(state['permission'])
new._set_model_combo_text(state['model'])
new._refresh_saved_models(state['saved'], state['names'])
new._set_editor_provider(state['address'])
new.provider_name_edit.setText(state['provider'])
new.base_url_edit.setText(state['address'])
new.api_key_edit.setText(state['key'])
new._populate_model_list(state['choices'])
new.provider_enabled.setChecked(next((g.get('enabled', True) for g in state['groups']
                                     if state['editor'] in g['models']), True))
new.cfg_hint.setText(state['hint'])
new.settings_btn.setChecked(state['settings'])
new._update_model_shortcut()
if isinstance(old_dock, QDockWidget):
    old_dock.setWidget(new)
    panel._install_dock_title_bar(old_dock, new)
else:
    old_dock.setCentralWidget(new)
old.hide()
old.deleteLater()
new.show()
print(json.dumps({'switch_loaded': True, 'draft_preserved': new.api_key_edit.text()==state['key']
                  and new.base_url_edit.text()==state['address'], 'status_text_visible': False}))
