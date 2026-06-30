# VAF Windows Setup Script
$ErrorActionPreference = "Stop"

Write-Host "VAF Windows Setup..."

$ProjectRoot = Get-Location

# 1. Check Python
if (-not (Get-Command "python" -ErrorAction SilentlyContinue)) {
    Write-Error "Python not found. Please install Python 3.10+ and add it to PATH."
    exit 1
}

# 2. Virtual Environment
if (-not (Test-Path "venv")) {
    Write-Host "Creating virtual environment..."
    python -m venv venv
}

# 3. Install Dependencies
if ($env:VAF_SKIP_PIP_INSTALL -eq "1") {
    Write-Host "Skipping dependency installation (already running via setup.py)..."
} else {
    Write-Host "Installing Dependencies..."
    $env:VIRTUAL_ENV = "$ProjectRoot\venv"
    $env:Path = "$ProjectRoot\venv\Scripts;$env:Path"

    python -m pip install --upgrade pip
    # pyttsx3 removed - caused 1-4GB RAM explosion via Windows SAPI/comtypes. TTS is via Docker (Piper).
    python -m pip install pywin32 requests beautifulsoup4 rich typer prompt_toolkit SpeechRecognition pyaudio
    python -m pip install -e .

    try {
        python -m pip install -r requirements.txt
    } catch {
        Write-Host "Some optional requirements failed, but core should work."
    }
}

# 4. Create Shortcuts
Write-Host "Creating Shortcuts..."
python scripts\create_app_shortcut.py

Write-Host "Setup Finished!"
Write-Host "You can run VAF via the Desktop Shortcut or run_vaf.bat"