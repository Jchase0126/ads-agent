import importlib
import json
import panel
w = panel._panel.widget()
if (w._worker and w._worker.isRunning()) or any(t.isRunning() for t in w._cfg_workers):
    raise RuntimeError('Panel busy')
dock, handle, tab = panel._panel, panel._handle, panel._handle_tab
old_type = type(w)
importlib.reload(panel)
panel._panel, panel._handle, panel._handle_tab = dock, handle, tab
for name in ('_input_model_groups', '_input_available_models', '_build_input_model_menu'):
    setattr(old_type, name, getattr(panel.AgentPanelWidget, name))
menu = w._build_input_model_menu()
actual = [a.data() for a in menu.actions() if a.data()]
expected = [m for g in w._provider_groups if g.get('enabled', True) for m in g['models']]
assert actual == expected
print(json.dumps({'updated':True, 'enabled_models':len(actual), 'provider_groups':len(w._input_model_groups()),
                  'active_preserved':w._active_model in actual}))
menu.deleteLater()
