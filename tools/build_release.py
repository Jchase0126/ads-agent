"""打包发布：产出带版本号的 ZIP。**可重复执行**，结果稳定。

    python tools/build_release.py                 # 生成 dist\\ADSAgent-<版本>.zip
    python tools/build_release.py --check         # 只做完整性检查，不打包
    python tools/build_release.py --out <目录>     # 指定输出目录

## 为什么按白名单而不是压缩整个目录

仓库里放着真东西：``config.ini`` 是**真实配置**（含 API Key 和回环令牌）、
``projects.json`` 是真实会话、``logs/`` 是运行日志、``tests/`` 里有一堆实机
探针。整目录压缩等于把密钥和开发现场一起发出去。

白名单在 ``release_manifest.py``（安装时的 deploy 步骤也用它 —— 两处共用一份，
不会出现"打好的包"和"装进去的东西"不一致）。

## 打包前会验什么

1. 白名单里的文件都在，且都不在排除规则里；
2. 每个进包的 Python 文件能编译通过；
3. **不含任何真实密钥** —— 先看配置文件字段，再扫疑似凭据串，命中就**拒绝打包**；
4. 包里没有 config.ini / projects.json / design_jobs / logs / tests / __pycache__；
5. 三级 --ads-dir 无关合格证：包内不含绝对路径硬编码（ADS / Anaconda）。

任何一项不过就退出非零，**不产出半吊子 ZIP**。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import pkgutil
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "backend"))

import release_manifest as manifest  # noqa: E402
import paths  # noqa: E402

ZIP_NAME_PREFIX = "ADSAgent"

#: 硬编码的绝对安装路径 —— 出现即视为"本机依赖泄漏"
#: 只匹配**路径形态**的硬编码。说明文字里提到 "Anaconda" 不算问题，
#: 写成盘符路径才是（那意味着换台机器就废）。
HARDCODED_PATTERNS = [
    re.compile(r"[A-Za-z]:[\\/]+ADS\b", re.IGNORECASE),
    re.compile(r"[A-Za-z]:[\\/]+[Aa]naconda\b", re.IGNORECASE),
    re.compile(r"[A-Za-z]:[\\/]+Antenna\b", re.IGNORECASE),
    re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+\w+", re.IGNORECASE),
]

#: 疑似真实凭据：命中即拒绝打包（模板里的占位值不会命中）
SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9]{16,}"),                    # OpenAI/DeepSeek 风格
    re.compile(r"^\s*(api_key|token)\s*=\s*\S{12,}\s*$"),     # 配置里写死了值
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{16,}"),
]


class BuildError(RuntimeError):
    pass


def _rel(path: str) -> str:
    return os.path.relpath(path, ROOT).replace("\\", "/")


# ---------------------------------------------------------------------------
# 检查
# ---------------------------------------------------------------------------

def check_manifest(app_root: str) -> list:
    files, missing, violations = manifest.collect(app_root)
    problems = []
    if missing:
        problems.append("白名单里的文件缺失（发布清单与代码不同步？）："
                        + ", ".join(missing))
    if violations:
        problems.append("白名单登记命中了排除规则：" + ", ".join(violations))
    return problems


def check_compiles(files: list) -> list:
    problems = []
    for rel, abs_path in files:
        if not rel.endswith(".py"):
            continue
        try:
            py_compile.compile(abs_path, cfile=os.path.join(
                tempfile.gettempdir(), "_pkgcheck_%d.pyc" % abs(hash(rel))),
                doraise=True)
        except py_compile.PyCompileError as e:
            problems.append(f"语法错误 {rel}: {e}")
    return problems


def check_secrets(files: list) -> list:
    problems = []
    for rel, abs_path in files:
        if not (rel.endswith(".py") or rel.endswith(".ini") or rel.endswith(".md")):
            continue
        try:
            body = open(abs_path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for pattern in SECRET_PATTERNS:
            hit = pattern.search(body)
            if hit:
                problems.append(
                    f"疑似真实凭据 {rel}: 命中 {pattern.pattern}（内容已隐藏）"
                )
    return problems


def check_hardcoded_paths(files: list) -> list:
    problems = []
    for rel, abs_path in files:
        if not rel.endswith((".py", ".bat")):
            continue
        try:
            body = open(abs_path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for pattern in HARDCODED_PATTERNS:
            hit = pattern.search(body)
            if hit:
                problems.append(
                    f"硬编码的本机安装路径 {rel}: {hit.group(0)!r} "
                    f"（安装目录必须由安装器自动探测）"
                )
    return problems


def check_excluded_absent(files: list) -> list:
    problems = []
    _packed_names = {os.path.normcase(rel) for rel, _ in files}
    for rel, _abs in files:
        name = os.path.basename(rel)
        if name in {"config.ini", "projects.json", "projects.json.bak"}:
            problems.append(f"包里出现了用户数据文件：{rel}")
        if rel.split("/")[0] in {"logs", "design_jobs", "tests", "tools",
                                 "attic", "__pycache__"}:
            problems.append(f"包里出现了不该出现的目录：{rel}")
        if name.endswith((".pyc", ".log", ".bak")):
            problems.append(f"包里出现了缓存/日志产物：{rel}")
    return problems


def _collect_bat_templates() -> dict:
    """四个入口 bat 的内容。

    与本机环境无关：ADS 目录先问环境变量、再问注册表（由 Python 侧探测），
    实在没有才退到 Keysight 的默认安装路径。这里**不写任何本机专属路径**。
    """
    return {
        "install_addon.bat": BODY_INSTALL,
        "uninstall_addon.bat": BODY_UNINSTALL,
        "selfcheck.bat": BODY_SELFCHECK,
        "start_backend.bat": BODY_STARTBACKEND,
    }


BODY_INSTALL = r"""@echo off
rem ============================================================
rem  ADS Agent 安装 —— 自动识别 ADS 2027，注册为原生插件
rem  不需要预先安装 Python / Anaconda：会优先使用 ADS 自带的解释器
rem ============================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title ADS Agent 安装

echo.
echo   ADS Agent 安装程序
echo   --------------------------------------------------------
echo   程序目录: %CD%
echo.

rem ---- 1. 挑一个能跑安装脚本的解释器（ADS 自带优先） ----
set "PY="
call :find_python
if not defined PY (
  echo.
  echo   [失败] 没有找到可用的 Python 解释器。
  echo      正常情况下 ADS 2027 自带 tools\python\python.exe。
  echo      如果已经装了 ADS，可以用下面的命令指定它的位置再重试：
  echo.
  echo          install_addon.py --ads-dir "你的 ADS 安装目录"
  echo.
  echo      或者手工执行（把路径换成你的 Python）：
  echo          "C:\path\to\python.exe" install_addon.py
  echo.
  pause
  exit /b 1
)

echo   解释器: !PY!
echo.

rem ---- 2. 安装 ----
"!PY!" "%~dp0install_addon.py" %*
set "RC=!ERRORLEVEL!"

echo.
if not "!RC!"=="0" (
  echo   [失败] 安装退出码 !RC! —— 请先看上面的错误信息。
  echo      常见问题：ADS 装在 Program Files 里需要"以管理员身份运行"本脚本。
) else (
  echo   [完成] 请重启 ADS 2027，菜单 Tools ^> ADS Agent 即可使用。
)
echo.
pause
exit /b !RC!

rem ============================================================
rem  找一个能用的解释器。优先 ADS 自带的，因为它必然存在。
rem ============================================================
:find_python
rem 2a. 用户显式指定
if defined ADS_AGENT_PYTHON if exist "%ADS_AGENT_PYTHON%" set "PY=%ADS_AGENT_PYTHON%" & goto :eof

rem 2b. HPEESOF_DIR 指到哪就在哪找 ADS 自带解释器
if defined HPEESOF_DIR if exist "%HPEESOF_DIR%\tools\python\python.exe" (
  set "PY=%HPEESOF_DIR%\tools\python\python.exe" & goto :eof
)

rem 2c. 常见的默认安装位置
for %%D in ("C:\Program Files\Keysight\ADS2027") do (
  if exist "%%~D\tools\python\python.exe" set "PY=%%~D\tools\python\python.exe" & goto :eof
)

rem 2d. py 启动器 / 系统 python
where py >nul 2>nul && for /f "delims=" %%P in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do if exist "%%P" set "PY=%%P" & goto :eof
where python >nul 2>nul && for /f "delims=" %%P in ('where python') do set "PY=%%P" & goto :eof
goto :eof
"""

BODY_STARTBACKEND = r"""@echo off
rem ============================================================
rem  手动启动 ADS Agent 后端（平时不用 —— 面板会自动拉起它）
rem  日志: %LOCALAPPDATA%\ADSAgent\logs\backend.log
rem  运行中也可用浏览器打开 http://127.0.0.1:8760/logs?n=200 看最近 200 行
rem ============================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title ADS Agent 后端

set "PY="
call :find_python
if not defined PY (
  echo   [失败] 没有找到可用的 Python 解释器。
  pause
  exit /b 1
)
echo   解释器: !PY!
echo   后端日志: %LOCALAPPDATA%\ADSAgent\logs\backend.log
echo   按 Ctrl+C 停止。
echo.

"!PY!" "%~dp0backend\server.py"
echo.
echo   后端已退出。
pause
exit /b 0

:find_python
if defined ADS_AGENT_PYTHON if exist "%ADS_AGENT_PYTHON%" set "PY=%ADS_AGENT_PYTHON%" & goto :eof
if defined HPEESOF_DIR if exist "%HPEESOF_DIR%\tools\python\python.exe" (
  set "PY=%HPEESOF_DIR%\tools\python\python.exe" & goto :eof
)
for %%D in ("C:\Program Files\Keysight\ADS2027") do (
  if exist "%%~D\tools\python\python.exe" set "PY=%%~D\tools\python\python.exe" & goto :eof
)
where py >nul 2>nul && for /f "delims=" %%P in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do if exist "%%P" set "PY=%%P" & goto :eof
where python >nul 2>nul && for /f "delims=" %%P in ('where python') do set "PY=%%P" & goto :eof
goto :eof
"""

BODY_UNINSTALL = r"""@echo off
rem ============================================================
rem  ADS Agent 卸载 —— 只移除本插件的注册与程序文件
rem  用户数据（配置 / 会话 / 设计任务）默认保留在
rem  %LOCALAPPDATA%\ADSAgent
rem ============================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title ADS Agent 卸载

echo.
echo   ADS Agent 卸载
echo   --------------------------------------------------------
echo.
echo   说明：默认只移除 ADS 里的注册记录，不动你的配置、会话和设计任务。
echo         要连程序文件一起删，请按提示选是；要连用户数据一起删，
echo         请改用命令行：install_addon.py --remove --purge-data
echo.

set "PY="
call :find_python
if not defined PY (
  echo   [失败] 没有找到可用的 Python 解释器。
  pause
  exit /b 1
)
echo   解释器: !PY!
echo.

set /p "REMOVE_FILES=是否同时删除程序文件目录？(y/N) "
set "EXTRA="
if /i "!REMOVE_FILES!"=="y" set "EXTRA=--remove-files"

"!PY!" "%~dp0install_addon.py" --remove !EXTRA!
set "RC=!ERRORLEVEL!"
echo.
if "!RC!"=="0" (
  echo   [完成] 请重启 ADS 2027 让卸载生效。
  echo      你的数据仍在 %LOCALAPPDATA%\ADSAgent
) else (
  echo   [失败] 退出码 !RC! —— 请看上面的错误信息。
)
echo.
pause
exit /b !RC!

:find_python
if defined ADS_AGENT_PYTHON if exist "%ADS_AGENT_PYTHON%" set "PY=%ADS_AGENT_PYTHON%" & goto :eof
if defined HPEESOF_DIR if exist "%HPEESOF_DIR%\tools\python\python.exe" (
  set "PY=%HPEESOF_DIR%\tools\python\python.exe" & goto :eof
)
for %%D in ("C:\Program Files\Keysight\ADS2027") do (
  if exist "%%~D\tools\python\python.exe" set "PY=%%~D\tools\python\python.exe" & goto :eof
)
where py >nul 2>nul && for /f "delims=" %%P in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do if exist "%%P" set "PY=%%P" & goto :eof
where python >nul 2>nul && for /f "delims=" %%P in ('where python') do set "PY=%%P" & goto :eof
goto :eof
"""

BODY_SELFCHECK = r"""@echo off
rem ============================================================
rem  ADS Agent 环境自检
rem  解释器 / ADS 目录 / 数据与程序目录 / 令牌 / 注册状态 / 实例身份
rem ============================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title ADS Agent 环境自检

set "PY="
call :find_python
if not defined PY (
  echo   [失败] 没有找到可用的 Python 解释器。
  pause
  exit /b 1
)
echo   解释器: !PY!
echo.

"!PY!" "%~dp0check_env.py" %*
set "RC=!ERRORLEVEL!"
echo.
echo   退出码 !RC!（0 = 全部通过）
echo.
pause
exit /b !RC!

:find_python
if defined ADS_AGENT_PYTHON if exist "%ADS_AGENT_PYTHON%" set "PY=%ADS_AGENT_PYTHON%" & goto :eof
if defined HPEESOF_DIR if exist "%HPEESOF_DIR%\tools\python\python.exe" (
  set "PY=%HPEESOF_DIR%\tools\python\python.exe" & goto :eof
)
for %%D in ("C:\Program Files\Keysight\ADS2027") do (
  if exist "%%~D\tools\python\python.exe" set "PY=%%~D\tools\python\python.exe" & goto :eof
)
where py >nul 2>nul && for /f "delims=" %%P in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do if exist "%%P" set "PY=%%P" & goto :eof
where python >nul 2>nul && for /f "delims=" %%P in ('where python') do set "PY=%%P" & goto :eof
goto :eof
"""


# ---------------------------------------------------------------------------
# 打包
# ---------------------------------------------------------------------------

def sync_repo_entries(app_root: str, log: list | None = None) -> list:
    """把入口 bat 写回仓库根 —— 保证"仓库里的"和"打进包的"是同一份。

    这四个 bat 同时也在白名单里（``BAT_FILES``），所以不同步就会把旧版本
    （还指向作者机器的 Python）打进包里。
    """
    written = []
    for name, body in _collect_bat_templates().items():
        target = os.path.join(app_root, name)
        before = None
        if os.path.isfile(target):
            before = open(target, "rb").read()
        new_bytes = body.encode("utf-8")
        if before == new_bytes:
            continue
        with open(target, "wb") as f:
            f.write(new_bytes)
        written.append(name)
        if log is not None:
            log.append(f"入口 {name} 已同步")
    return written


def build_staging(app_root: str, staging: str) -> tuple[list, dict]:
    """把要进包的文件复制到 staging 目录，返回 [(rel, abs_in_staging)]。"""
    files, _missing, _violations = manifest.collect(app_root)
    for rel, src in files:
        dst = os.path.join(staging, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)

    # 三个 bat 入口（版本号写进去，便于用户核对拿到的是哪一版）
    for name, body in _collect_bat_templates().items():
        dst = os.path.join(staging, name)
        with open(dst, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(body)

    packed = []
    for rel, _src in files:
        packed.append((rel.replace("\\", "/"), os.path.join(staging, rel)))
    for name in _collect_bat_templates():
        packed.append((name, os.path.join(staging, name)))
    packed.sort()
    return packed, {"version": paths.PLUGIN_VERSION}


def write_manifest_txt(staging: str, packed: list, version: str) -> str:
    """包内清单（含 sha256），便于用户/验收者核对包里有什么。"""
    lines = [
        f"ADS Agent {version}  发布清单",
        f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"文件数: {len(packed)}",
        "",
        f"{'sha256前16位':<18} {'字节':>10}  文件",
        "-" * 72,
    ]
    for rel, abs_path in packed:
        digest = hashlib.sha256(open(abs_path, "rb").read()).hexdigest()[:16]
        size = os.path.getsize(abs_path)
        lines.append(f"{digest:<18} {size:>10}  {rel}")
    lines += [
        "",
        "说明：",
        "  * 本包不含任何 API 密钥 —— config.ini 由首次运行时的干净模板生成，",
        "    请在 ADS 面板 ⚙设置 里填写。",
        "  * 用户数据（配置/会话/设计任务/日志）在 %LOCALAPPDATA%\\ADSAgent，",
        "    安装包里没有，卸载默认也不会删。",
    ]
    target = os.path.join(staging, "发布清单.txt")
    with open(target, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(lines) + "\n")
    return target


def make_zip(staging: str, out_dir: str, version: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    folder = f"{ZIP_NAME_PREFIX}-{version}"
    zip_path = os.path.join(out_dir, f"{folder}.zip")
    if os.path.exists(zip_path):
        os.unlink(zip_path)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for dirpath, dirnames, filenames in os.walk(staging):
            dirnames[:] = [d for d in dirnames
                           if d.lower() not in {"__pycache__", ".git"}]
            for fn in filenames:
                if fn.lower().endswith((".pyc", ".tmp")):
                    continue
                abs_path = os.path.join(dirpath, fn)
                arc = os.path.relpath(abs_path, staging).replace("\\", "/")
                zf.write(abs_path, f"{folder}/{arc}")
    return zip_path


def verify_zip(zip_path: str) -> list:
    """打开成品包再验一遍 —— 检查 staging 与实际产物是否一致。"""
    problems = []
    expected = {os.path.join("ADS Agent 文档")}  # 占位，实际按 namelist 判断
    del expected
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        bad = [n for n in names
               if n.endswith((".pyc", ".log", ".tmp"))
               or n.split("/")[-1] in {"config.ini", "projects.json"}
               or "/__pycache__/" in n]
        if bad:
            problems.append("成品包里出现了不该有的文件：" + ", ".join(bad[:10]))
        must = ["addon/ads_agent/__init__.py", "backend/server.py",
                "backend/paths.py", "config.example.ini", "install_addon.py",
                "release_manifest.py", "check_env.py", "安装说明.md"]
        root = names[0].split("/")[0] if names else ""
        for item in must:
            if f"{root}/{item}" not in names:
                problems.append(f"成品包缺少必需文件：{item}")
        # 包内的 install_addon.py 必须还能被解析成合法 Python
        body = zf.read(f"{root}/install_addon.py").decode("utf-8", errors="replace")
        try:
            compile(body, "install_addon.py", "exec")
        except SyntaxError as e:
            problems.append(f"成品包里的 install_addon.py 有语法错误：{e}")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="打包 ADS Agent 发布 ZIP")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"), help="输出目录")
    ap.add_argument("--check", action="store_true", help="只检查，不打包")
    ap.add_argument("--keep-staging", action="store_true", help="保留临时中间目录")
    ap.add_argument("--sync-bats", action="store_true",
                    help="配合 --check：把入口 bat 写回仓库根但不打包")
    args = ap.parse_args(argv)

    print(f"项目根目录: {ROOT}")
    print(f"插件版本  : {paths.PLUGIN_VERSION}   数据版本: {paths.DATA_VERSION}")
    print("-" * 72)

    # 入口 bat 先同步到仓库根：否则检查的是"仓库里那份旧的"、打包的却是
    # 模板里那份新的 —— 两边不一致，检查就失去意义。
    synced = sync_repo_entries(ROOT, [])
    if synced:
        print("入口 bat 已同步到仓库根：" + ", ".join(synced))

    files, missing, violations = manifest.collect(ROOT)
    print(f"白名单命中 {len(files)} 个文件")

    problems = []
    problems += check_manifest(ROOT)
    problems += check_compiles(files)
    problems += check_secrets(files)
    problems += check_hardcoded_paths(files)
    problems += check_excluded_absent(files)

    leaks = manifest.scan_for_leaks(ROOT)
    if leaks:
        print("提醒：仓库里还有这些白名单之外的文件（不会被打包）：")
        for item in leaks[:20]:
            print(f"    {item}")
        if len(leaks) > 20:
            print(f"    ... 另有 {len(leaks) - 20} 个")

    if problems:
        print("=" * 72)
        print(f"检查未通过（{len(problems)} 项）:")
        for item in problems:
            print(f"  x {item}")
        return 1

    print("检查通过：清单完整 / 全部能编译 / 无密钥 / 无本机硬编码路径")

    if args.check:
        print("--check 模式，不打包。")
        return 0

    staging = os.path.join(tempfile.mkdtemp(prefix="adsagent_release_"), "staging")
    try:
        packed, meta = build_staging(ROOT, staging)
        # 中文安装说明：ZIP 内置文档
        doc = os.path.join(staging, "安装说明.md")
        if not os.path.isfile(doc):
            src = os.path.join(ROOT, "docs", "安装说明.md")
            if os.path.isfile(src):
                shutil.copy2(src, doc)
        packed.sort()
        write_manifest_txt(staging, packed, meta["version"])
        zip_path = make_zip(staging, args.out, meta["version"])
    finally:
        if not args.keep_staging:
            shutil.rmtree(os.path.dirname(staging), ignore_errors=True)

    problems = verify_zip(zip_path)
    if problems:
        print("成品包复核失败:")
        for item in problems:
            print(f"  x {item}")
        return 1

    size_kb = os.path.getsize(zip_path) / 1024
    print("-" * 72)
    print(f"已生成: {zip_path}   ({size_kb:.0f} KB)")
    print(f"包内顶层目录: ADSAgent-{paths.PLUGIN_VERSION}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
