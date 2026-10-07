@echo off
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
for %%D in ("C:\Program Files\Keysight\ADS2027" "C:\Program Files\Keysight\ADS2026" "C:\Program Files\Keysight\ADS2025" "C:\Program Files\Keysight\ADS2024") do (
  if exist "%%~D\tools\python\python.exe" set "PY=%%~D\tools\python\python.exe" & goto :eof
)

rem 2d. py 启动器 / 系统 python
where py >nul 2>nul && for /f "delims=" %%P in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do if exist "%%P" set "PY=%%P" & goto :eof
where python >nul 2>nul && for /f "delims=" %%P in ('where python') do set "PY=%%P" & goto :eof
goto :eof
