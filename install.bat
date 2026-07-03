@echo off
:: VAF - Veyllo Agentic Framework - Windows Installer
:: This is a wrapper that runs install.ps1 with appropriate permissions

title VAF Installer

echo.
echo =====================================================
echo    VAF - Veyllo Agentic Framework Installer
echo =====================================================
echo.

:: Check if PowerShell is available
where powershell >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo ERROR: PowerShell not found. Please install PowerShell.
    pause
    exit /b 1
)

:: Get the directory where this script is located
set "SCRIPT_DIR=%~dp0"

:: Run the PowerShell installer
echo Starting installation...
echo.
powershell -ExecutionPolicy Bypass -File "%SCRIPT_DIR%install.ps1" %*

:: 3010 = reboot required (planned pause, not a failure)
if %ERRORLEVEL% equ 3010 (
    echo.
    echo Installation PAUSED - not an error. Windows must restart to finish a step.
    echo After the restart, run the same install commands from the guide again.
    echo 'git clone' will say the folder already exists - that is expected;
    echo keep going with the remaining commands. Or simply run:
    echo     cd "%SCRIPT_DIR%"
    echo     .\install.bat
    pause
    exit /b 3010
)

if %ERRORLEVEL% neq 0 (
    echo.
    echo Installation encountered an error. Please check the output above.
    pause
    exit /b 1
)

echo.
echo Press any key to exit...
pause >nul
