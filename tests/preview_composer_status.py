"""Actual composer rendering with synthetic running states, without network calls."""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))
import _harness
import ui_preview as preview
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])
preview.app = app
for name, dark, width in [('composer-status-light', False, 632),
                          ('composer-status-dark', True, 632),
                          ('composer-status-compact', False, 330)]:
    widget = preview._make(w=width, h=700, dark=dark)
    widget._turn_project = widget.projects_data['active']
    widget._set_run_state(True)
    widget._tool_name = 'run_simulation'
    widget.status.setText('执行 run_simulation … 已用 7s')
    app.processEvents()
    target = os.path.join(os.path.dirname(__file__), 'preview', name + '.png')
    widget.input_frame.grab().save(target)
    print(target)
    widget.close()
