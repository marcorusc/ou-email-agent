@echo off
cd /d "%~dp0"
title OU Email - one-time GPU indexing
echo Indexing cached emails with the small embedding model on the GPU.
echo Press Ctrl+C to stop. Saved chunks will be resumed next time.
".venv\Scripts\python.exe" -u index-cached.py
echo.
echo Indexing process ended. Progress is saved in data\indexing-progress.json.
pause
