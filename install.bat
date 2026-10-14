@echo off
REM FX Workspace - first-time setup. Safe to run again (it keeps what's already installed).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
echo.
pause
