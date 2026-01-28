@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
set "PROJECT_ROOT=%SCRIPT_DIR%"

if exist "%PROJECT_ROOT%venv\Scripts\activate.bat" (
    call "%PROJECT_ROOT%venv\Scripts\activate.bat"
    set "PYTHONPATH=%PROJECT_ROOT%;%PYTHONPATH%"
    echo 🚀 Starting VAF...
    python -m vaf.main %*
) else (
    echo ❌ Virtual environment not found.
    echo Please run: powershell -ExecutionPolicy Bypass -File scripts\setup_win.ps1
    pause
)
endlocal
