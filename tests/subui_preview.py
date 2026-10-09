"""Render native result cards and child dialogs with synthetic preview data."""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import _harness  # isolate user data
import ui_preview as preview
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QListWidgetItem
import panel as P
import result_page as R
import uiscale as U


def sample_job():
    xs = [2 + i / 100 for i in range(101)]
    traces = {}
    for i, name in enumerate(('dB(S(2,1))', 'dB(S(1,1))', 'dB(S(2,2))')):
        traces[name] = dict(x=xs, y=[12 - i * 14 + 2 * math.sin(x * 5) for x in xs],
                            x_name='freq', x_unit='GHz', y_name=name, y_unit='dB',
                            source='D:/示例工作区/simulation/Amplifier.ds',
                            n_points=101, n_display=101, display_method='none')
    return dict(job_id='preview_only', title='2.4 GHz 放大器 · 示例预览',
                verdict='partial', stage='evaluated', stage_label='评估完成',
                requirement='检查增益与输入匹配，比较偏置调整后的表现。',
                band={'label': '2.0–3.0 GHz'}, summary={'n_passed': 1, 'n_metrics': 2},
                design_ref='RF_Lib:Amplifier:schematic',
                design={'workspace': 'D:/示例工作区', 'library': 'RF_Lib', 'cell': 'Amplifier'},
                metrics=[dict(label='带内增益', actual=11.8, target=10, unit='dB',
                              at='2.4 GHz', **{'pass': True}),
                         dict(label='输入回波损耗', actual=-12, target=-15, unit='dB',
                              at='2.4 GHz', note='输入匹配尚未满足目标，需要继续调整。', **{'pass': False})],
                sim={'status': '完成'}, artifacts={'traces': traces,
                'output_dir': 'D:/示例工作区/simulation',
                'dataset_path': 'D:/示例工作区/simulation/Amplifier.ds'})


def main():
    app = QApplication.instance() or QApplication([])
    preview.app = app
    job = sample_job()
    for name, width, dark, scale in (
            ('13-result-narrow', 330, False, 1),
            ('14-result-wide', 880, False, 1),
            ('15-result-dark', 380, True, 1),
            ('16-result-large-font', 440, False, 1.35)):
        U.refresh()
        U._cache['scale'] = scale
        row = R.ResultPageRow(job, U.PALETTES['dark' if dark else 'light'])
        item = QListWidgetItem()
        row.reflow(width, item)
        row.resize(width, item.sizeHint().height())
        row.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        row.show()
        app.processEvents()
        row.reflow(width, item)
        row.resize(width, item.sizeHint().height())
        app.processEvents()
        preview._shot(row, name)
        row.close()
    U.refresh()
    for name, dark, width in (('17-points-light', False, 620), ('18-points-dark', True, 380)):
        dialog = R.PointsDialog(job, on_export_full=lambda *_: None,
                                pal=U.PALETTES['dark' if dark else 'light'])
        dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        dialog.resize(width, 680)
        dialog.show()
        app.processEvents()
        preview._shot(dialog, name)
        dialog.close()
    widget = preview._make(w=330, h=640)
    widget.side_btn.click()
    app.processEvents()
    preview._shot(widget, '19-projects-narrow')
    widget.close()
    widget = preview._make(w=430, h=800, entries=preview.TALK)
    activity = next(e for e in widget.entries if e['kind'] == 'activity')
    activity.update(expanded=True, reasoning=['比较两次仿真的增益和匹配变化。\n检查频段覆盖与电路变量后，再判断是否继续优化。'])
    widget._rebuild()
    app.processEvents()
    preview._shot(widget, '20-thinking-expanded')
    dialog = widget._project_name_dialog()
    dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    dialog.show()
    app.processEvents()
    preview._shot(dialog, '21-new-project')
    dialog.close()
    widget.close()
    dialog = U.message_dialog('ADS Agent 工具服务', '工具服务正在运行，可以连接 ADS。',
                              '示例预览：服务详细信息', pal=U.PALETTES['dark'])
    dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    dialog.show()
    app.processEvents()
    preview._shot(dialog, '22-status-dialog-dark')
    dialog.close()


if __name__ == '__main__':
    main()
