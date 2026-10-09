"""Execute inside ADS: verify refreshed settings without printing credentials."""
import json
import panel
from PySide6.QtWidgets import QDockWidget, QLineEdit

dock = panel._panel
widget = dock.widget() if isinstance(dock, QDockWidget) else dock.centralWidget()
masked = widget.api_key_edit.echoMode() == QLineEdit.EchoMode.Password
if masked:
    widget.grab().save('D:/Antenna/ADS Agent/tests/preview/model-settings-live.png')
print(json.dumps({'provider_name': widget.provider_name_edit.text(),
                  'has_address': bool(widget.base_url_edit.text()),
                  'has_key': bool(widget.api_key_edit.text()), 'key_masked': masked,
                  'loading': widget._profile_loading, 'feedback': widget.cfg_hint.text()},
                 ensure_ascii=False))
