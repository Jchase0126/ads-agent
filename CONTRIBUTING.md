# 开发说明

本项目当前为私有仓库，尚未选择开源许可证。

## 开发环境

- Windows、Python 3.12 或 ADS 2027 自带 Python。
- UI 离线测试需要 PySide6；实际运行使用 ADS 自带组件。
- ADS 数据库、建图和仿真验收需要已授权的 ADS 2027。

```powershell
python -m pip install PySide6==6.8.3
python tests/run_tests.py
python tools/build_release.py
```

不要提交 API 密钥、config.ini、会话、日志、仿真数据或 dist 产物。
新增运行模块时同步更新 release_manifest.py。
使用功能分支和 Pull Request 合入 master，避免把未完成修改混入版本发布。

完整发布步骤见 [版本迭代与发布](docs/版本迭代与发布.md)。
