@echo off
rem Start the ADS Agent backend (LLM agent loop). Keep this window open.
setlocal
cd /d "%~dp0"
set "PYTHON=python"
where python >nul 2>nul || set "PYTHON=E:\anaconda\python.exe"
echo [ADS Agent] 使用 Python: %PYTHON%
"%PYTHON%" backend\server.py
echo [ADS Agent] 后端已退出。日志见 logs\backend.log（运行中也可用浏览器打开
echo            http://127.0.0.1:8760/logs?n=200 查看最近 200 行）
pause
