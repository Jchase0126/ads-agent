"""Offline settings layout previews using synthetic connection data."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import _harness
import ui_preview as preview
from PySide6.QtWidgets import QApplication


def main():
    preview.app = QApplication.instance() or QApplication([])
    for name, width, dark, scale in (
        ('settings-models-light', 485, False, 1),
        ('settings-models-wide', 760, False, 1),
        ('settings-models-dark', 485, True, 1),
        ('settings-models-compact', 330, False, 1),
        ('settings-models-scaled', 485, False, 1.35),
    ):
        widget = preview._make(w=width, h=850, dark=dark, scale=scale, settings=True)
        widget._spawn_cfg_worker = lambda payload, path, callback: callback(dict(
            model=payload['model'], base_url='https://api.deepseek.com', api_key='sk-preview-fake-key',
            provider_name='我的 DeepSeek 服务', models=['deepseek-flash', 'deepseek-chat', 'deepseek-reasoner']))
        widget._on_config_loaded(dict(
            base_url='https://api.deepseek.com', model='deepseek-flash',
            models=['deepseek-flash', 'deepseek-chat', 'deepseek-reasoner'],
            provider_groups=[dict(model='deepseek-flash', provider_name='我的 DeepSeek 服务',
                                 models=['deepseek-flash', 'deepseek-chat', 'deepseek-reasoner'])],
            profile_labels={n: '我的 DeepSeek 服务' for n in ['deepseek-flash', 'deepseek-chat', 'deepseek-reasoner']},
            has_key=True, api_key_hint='sk-d****demo'))
        widget._adapt_responsive()
        preview.app.processEvents()
        preview._shot(widget, name)
        if name == 'settings-models-light':
            widget.add_model_btn.click()
            preview.app.processEvents()
            preview._shot(widget, 'settings-models-new')
        widget.close()


if __name__ == '__main__':
    main()
