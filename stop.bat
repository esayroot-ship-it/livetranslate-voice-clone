@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\manage.ps1" -Action stop
if errorlevel 1 pause
endlocal
