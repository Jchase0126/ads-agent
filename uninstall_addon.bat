@echo off
rem Uninstall the ADS Agent addon
setlocal
set "PYTHON=python"
where python >nul 2>nul || set "PYTHON=E:\anaconda\python.exe"
"%PYTHON%" "%~dp0install_addon.py" --remove
pause
