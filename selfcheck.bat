@echo off
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
