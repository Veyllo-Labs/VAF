@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
set "PROJECT_ROOT=%SCRIPT_DIR%"

if not exist "%PROJECT_ROOT%venv\Scripts\activate.bat" (
    echo [ERROR] Virtual environment not found.
    echo Please run install.bat first.
    pause
    exit /b 1
)

call "%PROJECT_ROOT%venv\Scripts\activate.bat"
set "PYTHONPATH=%PROJECT_ROOT%;%PYTHONPATH%"

:: Default to the desktop app (tray + web UI) when no command is given,
:: so a double-click actually starts VAF instead of doing nothing.
set "VAF_ARGS=%*"
if "%VAF_ARGS%"=="" set "VAF_ARGS=tray"

echo Starting VAF (%VAF_ARGS%)...
python -m vaf.main %VAF_ARGS%
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
    echo.
    echo [ERROR] VAF exited with code %EXITCODE%.
    echo See logs\ for details ^(e.g. tray_startup_*.txt^).
    pause
)
endlocal
