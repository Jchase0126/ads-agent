@echo off
rem Install the ADS Agent addon into ADS 2027 (writes eesof_addons.xml, with backup)
setlocal
set "PYTHON=python"
where python >nul 2>nul || set "PYTHON=E:\anaconda\python.exe"
"%PYTHON%" "%~dp0install_addon.py" %*
pause
