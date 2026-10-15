@echo off
cd /d "%~dp0"
title OU Email - request indexing
echo Requesting background indexing with the small embedding model.
".venv\Scripts\python.exe" -u index-cached.py
echo.
echo Check email_status through your assistant for progress. You may close this window.
pause
