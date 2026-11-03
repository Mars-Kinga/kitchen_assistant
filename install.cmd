@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
for %%V in (3.13 3.12 3.11 3.14) do (
    py -%%V -c "import struct; raise SystemExit(0 if struct.calcsize('P') == 8 else 1)" >nul 2>&1
    if not errorlevel 1 (
        set "TASK_PYTHON=py -%%V"
        goto install
    )
)
python -c "import sys, struct; raise SystemExit(0 if (3,11) <= sys.version_info[:2] <= (3,14) and struct.calcsize('P') == 8 else 1)" >nul 2>&1
if not errorlevel 1 (
    set "TASK_PYTHON=python"
    goto install
)
echo Python 3.11-3.14 64-bit is required. Recommended: Python 3.13 x64.
echo Install from https://www.python.org/downloads/windows/
echo Select "Add python.exe to PATH", then run this file again.
pause
exit /b 1

:install
%TASK_PYTHON% scripts\portable.py install
set "TASK_EXIT=%errorlevel%"
if not "%TASK_EXIT%"=="0" echo Installation failed. Keep this window's error message for troubleshooting.
pause
exit /b %TASK_EXIT%
