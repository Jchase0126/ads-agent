"""Native UI navigation checks; no ADS or network required."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _harness import add_path, eq, ok, run

add_path("addon", "ads_agent")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDockWidget, QMainWindow
import panel
import project_store
import result_page
from ui_fixtures import sample_job

APP = QApplication.instance() or QApplication([])
panel.AgentPanelWidget.reload_config = lambda self: None


def make(w=330, h=520):
    widget = panel.AgentPanelWidget()
    widget.resize(w, h)
    widget.show()
    APP.processEvents()
    return widget


def test_settings_return_preserves_draft_and_conversation():
    widget = make()
    widget.input.setPlainText("保留这份草稿")
    widget._add_entry("assistant", "已有回复")
    history = list(widget.entries)
    widget.settings_btn.click()
    eq(widget.pages.currentWidget(), widget.settings_page)
    ok(not widget.input.isVisible())
    widget.settings_close.click()
    eq(widget.pages.currentWidget(), widget.conversation_page)
    eq(widget.input.toPlainText(), "保留这份草稿")
    eq(widget.entries, history)
    widget.close()


def test_escape_from_form_returns_to_chat():
    widget = make()
    widget.settings_btn.click()
    widget.api_key_edit.setFocus()
    QTest.keyClick(widget.api_key_edit, Qt.Key.Key_Escape)
    APP.processEvents()
    eq(widget.pages.currentWidget(), widget.conversation_page)
    ok(not widget.settings_btn.isChecked())
    widget.close()


def test_short_settings_scroll_with_pinned_save():
    widget = make(h=400)
    widget.settings_btn.click()
    APP.processEvents()
    ok(widget.settings_scroll.verticalScrollBar().maximum() > 0)
    ok(widget.save_btn.isVisible())
    ok(widget.save_btn.geometry().bottom() < widget.settings_page.height())
    eq(widget.settings_scroll.horizontalScrollBar().maximum(), 0)
    widget.close()


def test_compact_project_navigation_and_resize_restore():
    widget = make(w=600)
    ok(widget._side_wanted)
    widget.resize(330, 520)
    APP.processEvents()
    ok(widget._side_wanted, "automatic collapse must preserve user preference")
    widget.side_btn.click()
    ok(widget.sidebar.isVisible())
    ok(not widget.chat_column.isVisible())
    widget.side_btn.click()
    ok(widget.chat_column.isVisible())
    widget.resize(600, 520)
    APP.processEvents()
    ok(widget.sidebar.isVisible())
    widget.close()


def test_custom_collapse_and_edge_tab_restore_docked_and_floating_panel():
    from PySide6.QtWidgets import QWidget
    host = QMainWindow()
    dock = QDockWidget(host)
    widget = panel.AgentPanelWidget()
    dock.setWidget(widget)
    bar = panel._install_dock_title_bar(dock, widget)
    eq(dock.titleBarWidget(), bar)
    eq(len(bar.findChildren(panel.QToolButton)), 1)
    host.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
    handle = QDockWidget(host)
    handle.setTitleBarWidget(QWidget(handle))
    handle.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
    tab = panel._VerticalTabButton('ADS Agent')
    handle.setWidget(tab)
    host.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, handle)
    saved = panel._panel, panel._handle, panel._handle_tab
    try:
        panel._panel, panel._handle, panel._handle_tab = dock, handle, tab
        dock.visibilityChanged.connect(panel._sync_handle)
        tab.clicked.connect(panel._expand_panel)
        host.show()
        APP.processEvents()
        widget.input.setPlainText('收起后保留草稿')
        widget.settings_btn.click()
        for floating in (False, True):
            dock.setFloating(floating)
            APP.processEvents()
            bar.collapse_btn.click()
            APP.processEvents()
            ok(host.isVisible())
            ok(not dock.isVisible())
            ok(handle.isVisible())
            tab.click()
            APP.processEvents()
            ok(dock.isVisible())
            ok(not handle.isVisible())
            eq(dock.isFloating(), floating)
            eq(dock.widget(), widget)
            eq(widget.input.toPlainText(), '收起后保留草稿')
            eq(widget.pages.currentWidget(), widget.settings_page)
    finally:
        host.close()
        panel._panel, panel._handle, panel._handle_tab = saved


def test_custom_drag_bar_double_click_floats_and_redocks():
    from PySide6.QtCore import QPoint
    host = QMainWindow()
    host.resize(700, 600)
    dock = QDockWidget(host)
    widget = panel.AgentPanelWidget()
    dock.setWidget(widget)
    bar = panel._install_dock_title_bar(dock, widget)
    host.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
    host.show()
    APP.processEvents()
    try:
        outside = QPoint(bar.width() // 2, bar.height() // 2)
        QTest.mouseDClick(bar, Qt.MouseButton.LeftButton, pos=outside)
        APP.processEvents()
        ok(not dock.isFloating(), 'blank title area must not toggle floating')
        for _ in range(2):
            position = QPoint(panel.U.px(33), bar.height() // 2)
            before = dock.isFloating()
            QTest.mouseDClick(bar, Qt.MouseButton.LeftButton, pos=position)
            APP.processEvents()
            eq(dock.isFloating(), not before)
        dock.setFloating(True)
        APP.processEvents()
        before_position = dock.pos()
        QTest.mousePress(bar, Qt.MouseButton.LeftButton, pos=outside)
        QTest.mouseMove(bar, outside + QPoint(30, 20))
        QTest.mouseRelease(bar, Qt.MouseButton.LeftButton, pos=outside + QPoint(30, 20))
        APP.processEvents()
        eq(dock.pos(), before_position, 'blank title drag must not move the floating dock')
        widget.dark = True
        widget._apply_theme()
        eq(bar._pal, panel.PALETTES['dark'])
        eq(widget.grab().toImage().pixelColor(2, 2).name(), panel.PALETTES['dark']['header_bg'])
    finally:
        host.close()


def test_drag_requires_grip_start_and_continues_outside_grip():
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent
    dock = QDockWidget()
    bar = panel._DockTitleBar(dock, panel.PALETTES['light'])
    bar.resize(330, panel.U.px(32))
    grip = QPointF(panel.U.px(33), bar.height() / 2)
    outside = QPointF(150, bar.height() / 2)

    def event(kind, point, button=Qt.MouseButton.LeftButton):
        return QMouseEvent(kind, point, point, button, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier)

    try:
        for start, allowed in ((outside, False), (grip, True)):
            press = event(QEvent.Type.MouseButtonPress, start)
            bar.mousePressEvent(press)
            eq(press.isAccepted(), not allowed)
            # Moving into/out of the grip never changes gesture ownership.
            move = event(QEvent.Type.MouseMove, grip if not allowed else outside,
                         Qt.MouseButton.NoButton)
            bar.mouseMoveEvent(move)
            eq(move.isAccepted(), not allowed)
            release = event(QEvent.Type.MouseButtonRelease, outside)
            bar.mouseReleaseEvent(release)
            eq(release.isAccepted(), not allowed)
            eq(bar.cursor().shape(), Qt.CursorShape.ArrowCursor)
    finally:
        dock.close()


def test_header_sidebar_and_chat_have_distinct_rendered_colors():
    for dark in (False, True):
        widget = make(900, 700)
        widget.dark = dark
        widget._apply_theme()
        APP.processEvents()
        pal = widget._pal()
        colors = [widget.grab().toImage().pixelColor(2, 2).name(),
                  widget.sidebar.grab().toImage().pixelColor(3, widget.sidebar.height() // 2).name(),
                  widget.chat_column.grab().toImage().pixelColor(3, widget.chat_column.height() // 2).name()]
        eq(colors, [pal['header_bg'], pal['sidebar_bg'], pal['chat_bg']])
        eq(len(set(colors)), 3)
        widget.close()


def test_custom_title_bar_fits_narrow_and_scaled_panels():
    try:
        for scale in (1, 1.35, 1.75):
            panel.U._cache['scale'] = scale
            host = QMainWindow()
            dock = QDockWidget(host)
            widget = panel.AgentPanelWidget()
            dock.setWidget(widget)
            bar = panel._install_dock_title_bar(dock, widget)
            host.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
            host.resize(panel.U.px(330), panel.U.px(520))
            host.show()
            APP.processEvents()
            ok(bar.collapse_btn.geometry().right() < bar.width())
            ok(bar.collapse_btn.geometry().bottom() < bar.height())
            ok(bar.collapse_btn.geometry().left() > panel.U.px(60))
            host.close()
    finally:
        panel.U.refresh()


def test_delete_undo_restores_project_and_saved_history():
    widget = make()
    widget.projects_data = {'active': 'A', 'projects': {
        'A': {'entries': [{'kind': 'user', 'text': 'keep'}], 'history': [{'role': 'user', 'content': 'keep'}]},
        'B': {'entries': [], 'history': []}}}
    widget._apply_project('A')
    widget._refresh_project_list()
    widget._delete_project()
    ok('A' not in widget.projects_data['projects'])
    widget.project_undo.click()
    eq(widget.history[0]['content'], 'keep')
    data, _ = project_store.load(widget._PROJECTS_FILE)
    eq(data['projects']['A']['history'][0]['content'], 'keep')
    widget.close()


def test_project_search_and_selection_return_to_chat():
    widget = make()
    widget.projects_data = {'active': 'Alpha', 'projects': {
        'Alpha': {'entries': [], 'history': []}, 'Beta': {'entries': [], 'history': []}}}
    widget._apply_project('Alpha')
    widget._refresh_project_list()
    widget.side_btn.click()
    widget.project_search.setText('BETA')
    ok(widget.project_list.item(0).isHidden())
    ok(not widget.project_list.item(1).isHidden())
    ok(not widget.proj_del_btn.isEnabled(), 'hidden selected project must not be deleted accidentally')
    widget.project_list.setCurrentRow(1)
    eq(widget.projects_data['active'], 'Beta')
    ok(widget.chat_column.isVisible())
    widget.close()


def test_result_reflows_without_clipping_actions():
    from PySide6.QtWidgets import QListWidgetItem
    for scale in (1, 1.35, 1.75):
        panel.U._cache['scale'] = scale
        row = result_page.ResultPageRow(sample_job(), panel.PALETTES['light'])
        item = QListWidgetItem()
        for width in (330, 900, 330):
            row.reflow(width, item)
            row.resize(width, item.sizeHint().height())
            row.show()
            APP.processEvents()
            row.reflow(width, item)
            row.resize(width, item.sizeHint().height())
            APP.processEvents()
            ok(row.resim_btn.geometry().bottom() < row.card.height())
            ok(row.open_btn.geometry().right() < row.card.width())
            ok(row.chart.geometry().right() < row.card.width())
            eq(row._metric_columns, 1 if width - panel.U.P('xl') * 2 < panel.U.px(520) else 2)
        row.close()
    panel.U.refresh()


def test_points_inherit_theme_and_remain_read_only():
    from PySide6.QtWidgets import QAbstractItemView
    row = result_page.ResultPageRow(sample_job(), panel.PALETTES['dark'])
    dialog = result_page.PointsDialog(sample_job(), parent=row, on_export_full=lambda *_: None)
    dialog.resize(330, 640)
    dialog.show()
    APP.processEvents()
    eq(dialog._pal, panel.PALETTES['dark'])
    eq(dialog.table.editTriggers(), QAbstractItemView.EditTrigger.NoEditTriggers)
    eq(dialog.actions.rowCount(), 3)
    dialog._copy()
    ok('完整数据' in QApplication.clipboard().text())
    dialog.close()
    row.close()


def test_python_setting_is_accessible_in_compact_settings():
    widget = make()
    widget.settings_btn.click()
    widget.python_setting.setChecked(False)
    ok(not widget.allow_python.isChecked())
    widget.allow_python.setChecked(True)
    ok(widget.python_setting.isChecked())
    widget.close()


def test_send_arrow_is_centered_at_multiple_ui_scales():
    try:
        for scale in (1, 1.35, 1.75):
            panel.U._cache['scale'] = scale
            widget = make(panel.U.px(520), panel.U.px(700))
            widget.dark = False
            widget._apply_theme()
            APP.processEvents()
            image = widget.send.grab().toImage()
            points = []
            # Only inspect the middle: rounded corners expose the white page.
            for y in range(image.height() // 5, image.height() * 4 // 5):
                for x in range(image.width() // 5, image.width() * 4 // 5):
                    color = image.pixelColor(x, y)
                    if min(color.red(), color.green(), color.blue()) > 180:
                        points.append((x, y))
            ok(points, 'send arrow must be visible')
            for axis, extent in ((0, image.width()), (1, image.height())):
                center = (min(p[axis] for p in points) + max(p[axis] for p in points) + 1) / 2
                ok(abs(center - extent / 2) <= image.devicePixelRatio(),
                   f'arrow axis {axis} is off center at scale {scale}: {center} / {extent}')
            widget.close()
    finally:
        panel.U.refresh()


def test_header_settings_label_and_actions_fit_narrow_panel():
    from PySide6.QtCore import QPoint
    for width in (330, 400, 440, 520, 600):
        widget = make(width, 700)
        eq(widget.settings_btn.text(), '模型设置')
        eq(widget.settings_btn.toolButtonStyle(), Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        previous_right = -1
        ok(not hasattr(widget, 'collapse_btn'))
        ok(not hasattr(widget, 'close_btn'))
        for button in (widget.theme_btn, widget.settings_btn):
            left = button.mapTo(widget, QPoint(0, 0)).x()
            ok(left > previous_right, f'header actions overlap at {width}px')
            previous_right = left + button.width() - 1
            ok(previous_right < widget.width(), f'header action is clipped at {width}px')
        widget.close()


def test_model_editor_keeps_active_model_until_saved_and_adds_manual_name():
    widget = make(485, 800)
    widget._spawn_cfg_worker = lambda payload, path, callback: callback(
        dict(base_url='https://api.deepseek.com', api_key=''))
    widget._on_config_loaded(dict(base_url='https://api.deepseek.com',
                                 model='deepseek-chat', models=['deepseek-chat', 'deepseek-reasoner']))
    widget.settings_btn.click()
    widget.saved_model_list.setCurrentRow(1)
    eq(widget.model_combo.currentText(), 'deepseek-reasoner')
    eq(widget._active_model, 'deepseek-chat')
    widget.add_model_btn.click()
    eq(widget.model_combo.currentText(), '')
    eq(widget._active_model, 'deepseek-chat')
    requests = []
    widget._spawn_cfg_worker = lambda payload, path, callback: requests.append((payload, path, callback))
    widget._save_settings()
    eq(len(requests), 0)
    widget._set_model_combo_text('my-custom-model')
    widget.base_url_edit.setText('https://api.deepseek.com')
    widget._save_settings()
    payload, path, callback = requests.pop()
    eq(path, '/config')
    eq(payload['models'], ['deepseek-chat', 'deepseek-reasoner', 'my-custom-model'])
    eq(payload['api_key'], '', 'new model must never inherit the previous credential')
    callback(dict(model='my-custom-model', models=payload['models'], has_key=True))
    eq(widget._active_model, 'my-custom-model')
    eq(widget.saved_model_list.currentItem().data(Qt.ItemDataRole.UserRole), 'my-custom-model')
    widget.close()


def test_connection_refresh_preserves_manual_model_and_saved_sidebar():
    widget = make(485, 800)
    widget._refresh_saved_models(['saved-model'])
    widget._set_model_combo_text('manual-model')
    widget._on_test_result(dict(ok=True, models=['api-model'], latency_ms=10))
    eq(widget.model_combo.currentText(), 'manual-model')
    eq(widget.saved_model_list.item(0).data(Qt.ItemDataRole.UserRole), 'saved-model')
    widget.close()


def test_model_settings_columns_stack_without_horizontal_clipping():
    from PySide6.QtWidgets import QBoxLayout
    for width in (330, 485, 760):
        widget = make(width, 650)
        widget.settings_btn.click()
        APP.processEvents()
        eq(widget.settings_columns.direction(), QBoxLayout.Direction.TopToBottom
           if width < panel.U.px(440) else QBoxLayout.Direction.LeftToRight)
        eq(widget.settings_scroll.horizontalScrollBar().maximum(), 0)
        ok(widget.base_url_edit.width() >= panel.U.px(120))
        ok(widget.save_btn.isVisible())
        widget.close()


def test_saved_models_load_separate_connections_and_mask_keys():
    widget = make(485, 800)
    profiles = {
        'model-a': dict(base_url='https://api.deepseek.com', api_key='fake-key-a'),
        'model-b': dict(base_url='https://open.bigmodel.cn/api/paas/v4', api_key='fake-key-b'),
    }
    widget._spawn_cfg_worker = lambda payload, path, callback: callback(profiles[payload['model']])
    widget._refresh_saved_models(list(profiles))
    widget._edit_saved_model('model-a')
    eq(widget.base_url_edit.text(), profiles['model-a']['base_url'])
    eq(widget.api_key_edit.text(), 'fake-key-a')
    eq(widget.api_key_edit.echoMode(), panel.QLineEdit.EchoMode.Password)
    widget.key_eye.click()
    eq(widget.api_key_edit.echoMode(), panel.QLineEdit.EchoMode.Normal)
    widget._edit_saved_model('model-b')
    eq(widget.base_url_edit.text(), profiles['model-b']['base_url'])
    eq(widget.preset_combo.currentText(), '智谱 GLM')
    eq(widget.model_combo.currentText(), 'model-b', 'provider load must not replace model name')
    eq(widget.api_key_edit.text(), 'fake-key-b')
    eq(widget.api_key_edit.echoMode(), panel.QLineEdit.EchoMode.Password)
    ok(not widget.key_eye.isChecked())
    widget.preset_combo.setCurrentText('DeepSeek')
    eq(widget.model_combo.currentText(), 'model-b')
    eq(widget.api_key_edit.text(), '', 'changing service must clear the prior service key')
    widget._edit_saved_model('model-b')
    widget.key_eye.click()
    widget._new_model()
    eq(widget.api_key_edit.text(), '')
    eq(widget.base_url_edit.text(), '')
    ok('已保存' not in widget.api_key_edit.placeholderText())
    eq(widget.api_key_edit.echoMode(), panel.QLineEdit.EchoMode.Password)
    ok(not widget.key_eye.isChecked())
    widget.close()


def test_late_profile_results_cannot_fill_new_or_other_model():
    widget = make(485, 800)
    pending = []
    widget._spawn_cfg_worker = lambda payload, path, callback: pending.append(callback)
    widget._edit_saved_model('a')
    widget._edit_saved_model('b')
    pending[0](dict(base_url='https://a.invalid', api_key='fake-a'))
    eq(widget.api_key_edit.text(), '')
    ok(not widget.save_btn.isEnabled())
    pending[1](dict(base_url='https://b.invalid', api_key='fake-b'))
    eq(widget.api_key_edit.text(), 'fake-b')
    widget._edit_saved_model('a')
    widget._new_model()
    pending[2](dict(base_url='https://a.invalid', api_key='fake-a'))
    eq(widget.api_key_edit.text(), '')
    eq(widget.base_url_edit.text(), '')
    eq(widget.model_combo.currentText(), '')
    ok(widget.save_btn.isEnabled())
    widget.close()


def test_save_response_does_not_change_new_editor_and_empty_probe_key_is_explicit():
    widget = make(485, 800)
    pending = []
    widget._spawn_cfg_worker = lambda payload, path, callback: pending.append((payload, callback))
    widget._set_model_combo_text('saved-a')
    widget.base_url_edit.setText('https://a.invalid')
    widget.api_key_edit.setText('fake-save-key')
    widget._save_settings()
    _, saved = pending.pop()
    widget._new_model()
    saved(dict(model='saved-a', models=['saved-a']))
    eq(widget.model_editor_heading.text(), '新增供应商')
    eq(widget.api_key_edit.text(), '')
    eq(widget.model_combo.currentText(), '')
    widget.base_url_edit.setText('https://new.invalid')
    widget._test_connection()
    payload, _ = pending.pop()
    eq(payload['api_key'], '', 'empty probe key must not fall back to active credential')
    widget.close()


def test_discovered_saved_model_switch_preserves_credentials_and_new_draft():
    widget = make(485, 800)
    widget._refresh_saved_models(['a', 'b'])
    widget.base_url_edit.setText('https://a.invalid')
    widget.api_key_edit.setText('fake-a')
    pending = []
    widget._spawn_cfg_worker = lambda payload, path, callback: pending.append((payload, callback))
    widget._apply_model_selection('b', ['b'])
    payload, switched = pending.pop()
    ok('api_key' not in payload and 'base_url' not in payload,
       'saved model selection must preserve its own connection')
    widget._new_model()
    switched(dict(model='b', models=['a', 'b']))
    eq(widget.model_editor_heading.text(), '新增供应商')
    eq(widget.api_key_edit.text(), '')
    eq(widget.model_combo.currentText(), '')
    widget.close()


def test_late_initial_config_cannot_restore_key_in_new_editor():
    widget = make(485, 800)
    request_id = widget._profile_request_id
    widget._new_model()
    widget._on_config_loaded(dict(model='old-model', models=['old-model'],
                                 base_url='https://old.invalid', has_key=True), request_id)
    eq(widget.model_editor_heading.text(), '新增供应商')
    eq(widget.api_key_edit.text(), '')
    eq(widget.base_url_edit.text(), '')
    eq(widget.model_combo.currentText(), '')
    widget.close()


def test_custom_provider_name_roundtrip_and_sidebar_identity():
    widget = make(760, 850)
    name = '我的自定义供应商名称'
    widget._spawn_cfg_worker = lambda payload, path, callback: callback(dict(
        base_url='https://proxy.invalid/v1', api_key='fake-custom-key', provider_name=name))
    widget._refresh_saved_models(['custom-model'], {'custom-model': name})
    widget.saved_model_list.setCurrentRow(0)
    widget.settings_btn.click()
    APP.processEvents()
    eq(widget.provider_name_edit.text(), name)
    eq(widget.base_url_edit.text(), 'https://proxy.invalid/v1')
    eq(widget.api_key_edit.text(), 'fake-custom-key')
    eq(widget.saved_model_selector.currentData(), 'custom-model')
    row = widget.saved_model_list.itemWidget(widget.saved_model_list.item(0))
    eq(row.findChild(panel.QLabel, 'providerLabel').text(), name)
    widget.provider_name_edit.setText('修改后的供应商')
    requests = []
    widget._spawn_cfg_worker = lambda payload, path, callback: requests.append(payload)
    widget._save_settings()
    eq(requests[0]['provider_name'], '修改后的供应商')
    widget._new_model()
    eq(widget.provider_name_edit.text(), '')
    widget.close()


def test_profile_load_failure_explains_error_and_selected_row_can_retry():
    widget = make(760, 850)
    widget._refresh_saved_models(['retry-model'])
    widget._spawn_cfg_worker = lambda payload, path, callback: callback({'error': '后端版本过旧'})
    widget._edit_saved_model('retry-model')
    eq(widget.cfg_hint.text(), '后端版本过旧')
    ok(not widget.retry_profile_btn.isHidden())
    widget._spawn_cfg_worker = lambda payload, path, callback: callback(dict(
        base_url='https://retry.invalid', api_key='fake-retry', provider_name='重试供应商'))
    widget.retry_profile_btn.click()
    eq(widget.api_key_edit.text(), 'fake-retry')
    eq(widget.provider_name_edit.text(), '重试供应商')
    ok(widget.retry_profile_btn.isHidden())
    widget.close()


def test_provider_search_matches_names_and_models_and_preserves_selection():
    widget = make(760, 850)
    widget._refresh_saved_models(['model-a', 'model-b'], {'model-a': '我的供应商', 'model-b': 'Second'})
    widget.saved_model_search.setText('我的')
    ok(not widget.saved_model_list.item(0).isHidden())
    ok(widget.saved_model_list.item(1).isHidden())
    widget.saved_model_search.setText('MODEL-B')
    ok(widget.saved_model_list.item(0).isHidden())
    ok(not widget.saved_model_list.item(1).isHidden())
    widget.saved_model_search.clear()
    ok(not widget.saved_model_list.item(0).isHidden())
    ok(not widget.saved_model_list.item(1).isHidden())
    widget.close()


def test_grouped_input_menu_lists_all_enabled_providers_and_switches_between_them():
    widget = make(760, 850)
    widget._active_model = 'a'
    widget._ingest_provider_groups({'provider_groups': [
        dict(model='a', models=['a', 'b'], provider_name='同一供应商'),
        dict(model='c', models=['c'], provider_name='另一个供应商'),
        dict(model='d', models=['d'], provider_name='停用供应商', enabled=False)]})
    widget._refresh_saved_models(['a', 'b', 'c', 'd'], {'a': '同一供应商', 'b': '同一供应商', 'c': '另一个供应商'})
    eq(widget.saved_model_list.count(), 3)
    menu = widget._build_input_model_menu()
    eq([a.data() for a in menu.actions() if a.data()], ['a', 'b', 'c'])
    eq([a.data() for a in menu.actions() if a.isChecked()], ['a'])
    requests = []
    widget._spawn_cfg_worker = lambda payload, path, callback: requests.append((payload, callback))
    widget._switch_input_model('d')
    eq(len(requests), 0)
    widget._switch_input_model('c')
    payload, callback = requests.pop()
    eq(payload, {'model': 'c'})
    callback(dict(model='c'))
    eq(widget._active_model, 'c')
    widget._switch_input_model('b')
    payload, callback = requests.pop()
    eq(payload, {'model': 'b'})
    callback(dict(model='b'))
    eq(widget._active_model, 'b')
    menu.close()
    widget.close()


def test_model_multiselect_preserves_active_model_until_save_and_sync():
    widget = make(760, 850)
    widget._active_model = 'a'
    widget._editing_provider_models = ['a', 'b']
    widget._populate_model_list(['a', 'b', 'c'])
    widget.model_list.item(2).setCheckState(Qt.CheckState.Checked)
    eq(widget._checked_models(), ['a', 'b', 'c'])
    eq(widget._active_model, 'a')
    widget._on_test_result(dict(ok=True, models=['b', 'd']))
    eq(widget._checked_models(), ['a', 'b', 'c'])
    eq(widget._active_model, 'a')
    widget.close()


def test_composer_status_tracks_phases_stops_and_preserves_errors():
    widget = make()
    project = widget.projects_data['active']
    widget._turn_project = project
    widget._set_run_state(True)
    widget._on_event(dict(type='status', text='正在思考…（第 1/30 步）'), project)
    ok(widget.run_status_row.isVisible())
    eq(widget.run_status_text._full_text, '正在分析问题…')
    ok(widget.run_stop_btn.isVisible())
    widget._on_event(dict(type='content_delta', text='回复内容'), project)
    eq(widget.run_status_text._full_text, '正在生成回复…')
    stopped = []
    class Worker:
        def isRunning(self):
            return True
        def stop(self):
            stopped.append(True)
    widget._worker = Worker()
    widget.run_stop_btn.click()
    eq(stopped, [True])
    ok(not widget.run_stop_btn.isEnabled())
    widget._on_event(dict(type='done'), project)
    ok(widget.run_status_row.isHidden())
    widget._on_event(dict(type='error', message='连接中断'), project)
    widget._on_worker_done(project)
    ok(widget.run_status_row.isVisible())
    eq(widget.run_status_text._full_text, '执行失败：连接中断')
    ok(widget.run_stop_btn.isHidden())
    widget.close()


def test_connection_normalized_address_preserves_credentials_and_new_draft():
    widget = make()
    root = 'https://api.stepfun.com'
    effective = root + '/v1'
    widget.base_url_edit.setText(root)
    widget.api_key_edit.setText('test-key')
    widget._on_test_result(dict(ok=True, base_url=effective, models=['test-model']),
                           widget._profile_request_id, root)
    eq(widget.base_url_edit.text(), effective)
    eq(widget.api_key_edit.text(), 'test-key')
    widget.base_url_edit.setText('https://custom.invalid/v2')
    widget._on_test_result(dict(ok=False, reachable=True, base_url=effective,
                               error='HTTP 404'), widget._profile_request_id, root)
    eq(widget.base_url_edit.text(), 'https://custom.invalid/v2')
    eq(widget.cfg_hint.text(), 'HTTP 404')
    widget._on_test_result(dict(ok=True, base_url=effective, models=['other']),
                           widget._profile_request_id - 1, 'https://custom.invalid/v2')
    eq(widget.base_url_edit.text(), 'https://custom.invalid/v2')
    widget.close()


def test_supplier_switch_hides_models_and_preserves_editor_credentials():
    widget = make()
    widget._active_model = 'off-model'
    widget._ingest_provider_groups(dict(provider_groups=[
        dict(models=['off-model'], enabled=False), dict(models=['on-model'], enabled=True)]))
    widget._on_profile_loaded(dict(base_url='https://test.invalid/v1', api_key='test-key',
                                  enabled=False, models=['off-model']),
                              widget._editor_model, widget._profile_request_id)
    ok(not widget.provider_enabled.isChecked())
    eq(widget._input_available_models(), ['on-model'])
    eq([a.data() for a in widget._build_input_model_menu().actions() if a.data()], ['on-model'])
    widget.input.setPlainText('hello')
    widget._on_send()
    eq(widget.input.toPlainText(), 'hello')
    ok('已停用' in widget.status.text())
    widget.provider_enabled.setFocus()
    QTest.keyClick(widget.provider_enabled, Qt.Key.Key_Space)
    ok(widget.provider_enabled.isChecked())
    eq(widget.api_key_edit.text(), 'test-key')
    widget._new_model()
    ok(widget.provider_enabled.isChecked())
    widget.close()


if __name__ == "__main__":
    raise SystemExit(run(globals()))
