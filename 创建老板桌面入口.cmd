@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0DouK-Web\setup-shortcut.ps1"
if errorlevel 1 pause
