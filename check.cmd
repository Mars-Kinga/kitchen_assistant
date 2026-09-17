@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Please run install.cmd first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" scripts\portable.py check
set "TASK_EXIT=%errorlevel%"
pause
exit /b %TASK_EXIT%
