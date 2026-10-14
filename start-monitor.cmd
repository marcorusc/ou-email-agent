@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" -m ou_email_agent monitor
if errorlevel 1 pause
