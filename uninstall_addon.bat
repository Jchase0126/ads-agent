@echo off
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
for %%D in ("C:\Program Files\Keysight\ADS2027" "C:\Program Files\Keysight\ADS2026" "C:\Program Files\Keysight\ADS2025" "C:\Program Files\Keysight\ADS2024") do (
  if exist "%%~D\tools\python\python.exe" set "PY=%%~D\tools\python\python.exe" & goto :eof
)
where py >nul 2>nul && for /f "delims=" %%P in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do if exist "%%P" set "PY=%%P" & goto :eof
where python >nul 2>nul && for /f "delims=" %%P in ('where python') do set "PY=%%P" & goto :eof
goto :eof
