import importlib
import json
import panel
import backend_launcher
w = panel._panel.widget()
if (w._worker and w._worker.isRunning()) or any(t.isRunning() for t in w._cfg_workers):
    raise RuntimeError('Panel busy')
# Update only connection handlers on the existing widget, preserving the draft.
original_type = type(w)
dock, handle, tab = panel._panel, panel._handle, panel._handle_tab
importlib.reload(panel)
panel._panel, panel._handle, panel._handle_tab = dock, handle, tab
original_type._test_connection = panel.AgentPanelWidget._test_connection
original_type._on_test_result = panel.AgentPanelWidget._on_test_result
ok, message = backend_launcher.ensure_backend()
if not ok:
    raise RuntimeError(message)
w._test_connection()
print(json.dumps({'backend_ready': ok, 'sync_started': True, 'draft_preserved': True}))
