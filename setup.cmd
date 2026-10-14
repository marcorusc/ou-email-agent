@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run install.ps1 first to install the isolated Python environment.
  pause
  exit /b 1
)
:credentials
".venv\Scripts\python.exe" -m ou_email_agent credentials
if errorlevel 1 goto retry_credentials
".venv\Scripts\python.exe" -m ou_email_agent sync
if errorlevel 1 goto failed
echo.
echo Setup completed.
if /i "%~1"=="/start-monitor" (
  start "OU Email Monitor" /D "%~dp0" "%~dp0start-monitor.cmd"
  echo The monitor is opening in a separate window. Leave that window running.
) else (
  echo Start start-monitor.cmd to monitor and process mail.
)
echo You can close this window. Your password stays in Windows Credential Manager.
pause
exit /b 0
:retry_credentials
echo.
echo Login or password setup did not complete. Your saved password has not been replaced unless verification succeeded.
"%SystemRoot%\System32\choice.exe" /c RQ /n /m "Press R to enter another password, or Q to quit: "
if errorlevel 2 exit /b 1
goto credentials
:failed
echo.
echo Setup did not finish. Keep your password private; share only the error type if you need help.
pause
exit /b 1
