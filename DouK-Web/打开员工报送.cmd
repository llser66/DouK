@echo off
powershell.exe -NoProfile -File "%~dp0start-report.ps1"
if errorlevel 1 pause
