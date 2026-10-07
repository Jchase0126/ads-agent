# ADS Agent

在 Keysight PathWave ADS 2027 内运行的 AI 助手插件：聊天、原理图自动建图、仿真、射频审查和设计指标评估。

[下载安装包](https://github.com/Jchase0126/ads-agent/releases) · [安装说明](docs/安装说明.md) · [更新记录](CHANGELOG.md) · [开发与发布](docs/版本迭代与发布.md)

> 当前支持 Windows + ADS 2027。仓库为私有，访问源码和附件需要权限。
> 当前提供 ZIP + BAT 安装入口；EXE/MSI 图形安装程序和插件内自动更新尚未实现。

## 安装

1. 从 Releases 下载 `ADSAgent-1.0.1.zip` 并完整解压。
2. 运行 `安装 ADS Agent.bat`。ADS 位于需要提升权限的目录时，按安装说明操作。
3. 重启 ADS，在右侧面板的设置中填写 API 地址、密钥并选择模型。

需要自行安装并授权的 ADS 2027。插件使用 ADS 自带的 Python、PySide6 和 Keysight API，不捆绑 ADS 软件；正常运行不要求另装这些组件。
GitHub 自动提供的 `Source code` ZIP 是开发源码，安装请使用 `ADSAgent-<版本>.zip`。

升级时关闭 ADS，解压新版本并运行安装入口。用户配置、会话、设计任务与日志保存在 `%LOCALAPPDATA%\ADSAgent`，升级和默认卸载保留这些数据。

## 功能

- 在 ADS 内聊天，配置 OpenAI 兼容 API 和模型。
- 读取工作区、设计、变量，修改参数并检查保存结果。
- 构建原理图、自动布局及网表连接等价检查。
- 运行仿真、读取真实数据集、导出曲线。
- 从真实数据确定性计算设计指标，生成可恢复的结果页。
- 射频及 Layout 审查，检测几何或连接问题。
- 保存项目会话；本机服务采用随机令牌和实例身份校验。

例如：读取当前设计的 VAR 变量；修改偏置电阻并仿真 S 参数；评估 2.3–2.5 GHz 内增益和 S11。
LLM 提供设计与指标定义，实测值和判定由数据评估代码计算。

## 架构

```text
ADS 2027 进程
  ├─ PySide6 聊天面板
  └─ 本机工具服务 → ADS 数据库 / 建图 / 仿真 / 数据集
           ↑
  本机 Python 后端 → LLM API、工具派发、设计指标评估
```

```text
addon/ads_agent/       ADS 插件入口、面板、工具服务和设计操作
backend/              对话循环、接口、路径、实例和指标评估
tests/                离线回归测试与电路用例
release_manifest.py   运行文件打包白名单
install_addon.py      安装、部署、注册、升级和卸载
config.example.ini    不带密钥的配置模板
tools/build_release.py 可重复构建 ZIP
.github/workflows/    离线 CI 和版本标签发布
docs/                 使用、技术和版本发布说明
```

## 开发与验证

```powershell
python -m pip install PySide6==6.8.3
python tests/run_tests.py
python tools/build_release.py
```

Python 3.12 用于 GitHub 离线 CI，UI 测试使用 offscreen 模式。
测试构建不需要 ADS，但真实建图、仿真和插件生命周期需要本地 ADS 实机验收。
新增运行模块需同步 `release_manifest.py`；不要提交真实配置、会话、日志或仿真产物。

## 版本发布

修改 `backend/paths.py` 中的 `PLUGIN_VERSION`，更新 `CHANGELOG.md` 和 `docs/releases/<版本>.md`。
合入 `master` 后推送对应 `v<版本>` 标签，Actions 自动测试并构建 ZIP、SHA-256 校验文件和草稿 Release。
完成实机验收、核对附件后发布草稿。详见 [版本迭代与发布](docs/版本迭代与发布.md)。

工程发布流程已经提供；自动更新插件需要后续另行实现。

## 支持范围与许可

首版目标为 Windows + ADS 2027。跨机器安装、其他 ADS 版本和多个 ADS 实例并行操作尚未完成验收。
历史交付记录报告本机 ADS 实机验收通过；本次 GitHub 发布准备未重新运行 ADS 实机验收。

尚未选择开源许可证，源码可见性不代表额外复制或再分发授权。
第三方运行组件说明见 [THIRD-PARTY-NOTICES.txt](THIRD-PARTY-NOTICES.txt)。
