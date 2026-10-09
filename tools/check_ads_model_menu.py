"""Execute inside ADS: inspect the provider menu without exposing credentials."""
import json
import panel
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QDockWidget, QLineEdit

dock = panel._panel
widget = dock.widget() if isinstance(dock, QDockWidget) else dock.centralWidget()
previous_page = widget.settings_btn.isChecked()
try:
    widget.settings_btn.setChecked(False)
    QApplication.processEvents()
    widget._show_input_model_menu()
    QApplication.processEvents()
    menu = widget._input_model_menu
    models = [action.data() for action in menu.actions() if action.data()]
    anchor = widget.model_shortcut.mapToGlobal(QPoint(0, 0))
    upward = menu.geometry().bottom() <= anchor.y()
    assert upward, 'Model menu must open above the input control'
    assert models == widget._provider_models_for(widget._active_model)
    menu.grab().save('D:/Antenna/ADS Agent/tests/preview/input-model-menu-live.png')
    menu.close()
    widget.settings_btn.setChecked(True)
    QApplication.processEvents()
    assert widget.api_key_edit.echoMode() == QLineEdit.EchoMode.Password
    assert widget.model_editor_hint.isHidden()
    assert widget.model_list_hint.isHidden()
    assert widget.settings_save_hint.isHidden()
    button = widget.add_provider_model_btn
    assert button.width() >= button.sizeHint().width(), 'Add button text must fit'
    scroll_position = widget.settings_scroll.verticalScrollBar().value()
    widget.settings_scroll.ensureWidgetVisible(button)
    QApplication.processEvents()
    widget.grab().save('D:/Antenna/ADS Agent/tests/preview/model-settings-live.png')
    widget.settings_scroll.verticalScrollBar().setValue(scroll_position)
    print(json.dumps({'models': models, 'opens_upward': upward,
                      'provider_count': len(widget._provider_groups),
                      'key_masked': True, 'add_button_fits': True,
                      'repeated_hints_hidden': True}, ensure_ascii=False))
finally:
    widget.settings_btn.setChecked(previous_page)
