@echo off
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
