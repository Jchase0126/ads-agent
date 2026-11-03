"""Execute inside ADS via tests/probe_via_ads.py to refresh this settings UI."""
import importlib
import json
import panel
from PySide6.QtWidgets import QDockWidget

dock = panel._panel
if dock is None:
    raise RuntimeError('ADS Agent 面板尚未打开')
old = dock.widget() if isinstance(dock, QDockWidget) else dock.centralWidget()
if old._worker is not None and old._worker.isRunning():
    raise RuntimeError('对话正在运行，暂不刷新面板')
if any(worker.isRunning() for worker in old._cfg_workers):
    raise RuntimeError('配置请求正在运行，暂不刷新面板')
draft = old.input.toPlainText()
dark = old.dark
settings = old.settings_btn.isChecked()
permission = old.allow_python.isChecked()
projects_file = old._PROJECTS_FILE
old._save_projects()
handle, tab = panel._handle, panel._handle_tab
importlib.reload(panel.U)
importlib.reload(panel)
panel._panel, panel._handle, panel._handle_tab = dock, handle, tab
panel.AgentPanelWidget._PROJECTS_FILE = projects_file
new = panel.AgentPanelWidget()
new.dark = dark
new._apply_theme()
new.input.setPlainText(draft)
new.allow_python.setChecked(permission)
new.settings_btn.setChecked(settings)
if isinstance(dock, QDockWidget):
    dock.setWidget(new)
    panel._install_dock_title_bar(dock, new)
else:
    dock.setCentralWidget(new)
old.hide()
old.deleteLater()
new.show()
print(json.dumps({'panel_refreshed': True, 'provider_field': hasattr(new, 'provider_name_edit'),
                  'draft_preserved': new.input.toPlainText() == draft}))
