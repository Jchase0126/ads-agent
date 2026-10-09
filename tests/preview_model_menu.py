"""Render the actual Qt popup together with its composer in both themes."""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))
import _harness
import ui_preview as preview
from PySide6.QtCore import QPoint
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])
preview.app = app
for dark in (False, True):
    widget = preview._make(w=632, h=780, dark=dark)
    widget._active_model = 'deepseek-v4-pro'
    widget._provider_names = {'deepseek-v4-pro': 'DeepSeek'}
    widget._provider_groups = [dict(provider_name='DeepSeek', model='deepseek-flash',
                                   models=['deepseek-flash', 'deepseek-v4-pro'])]
    widget._update_model_shortcut()
    app.processEvents()
    widget._show_input_model_menu()
    app.processEvents()
    menu = widget._input_model_menu
    canvas = QPixmap(widget.size())
    widget.render(canvas)
    painter = QPainter(canvas)
    location = menu.mapToGlobal(QPoint()) - widget.mapToGlobal(QPoint())
    painter.drawPixmap(location, menu.grab())
    painter.end()
    name = 'model-menu-dark' if dark else 'model-menu-light'
    target = os.path.join(os.path.dirname(__file__), 'preview', name + '.png')
    canvas.copy(0, 380, canvas.width(), canvas.height() - 380).save(target)
    print(target)
    menu.close()
    widget.close()
