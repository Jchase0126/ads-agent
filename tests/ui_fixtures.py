"""Synthetic design data for offline UI checks."""
import math


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
