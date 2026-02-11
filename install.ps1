#Requires -Version 5.1
<#
.SYNOPSIS
    VAF - Veyllo Agentic Framework - Windows Auto-Installer
    
.DESCRIPTION
    Complete installation script for Windows that handles:
    - Python 3.10+ detection/installation guidance
    - Virtual environment creation
    - All dependencies installation
    - Docker Desktop detection for Memory System (pgvector)
    - GPU detection for local LLM acceleration
    - Desktop shortcuts creation
    
.EXAMPLE
    .\install.ps1
    .\install.ps1 -SkipDocker
    .\install.ps1 -Verbose
#>

[CmdletBinding()]
param(
    [switch]$SkipDocker,
    [switch]$SkipShortcuts,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# ============================================================================
# CONFIGURATION
# ============================================================================
$MIN_PYTHON_VERSION = [version]"3.10"
$PROJECT_ROOT = $PSScriptRoot
if (-not $PROJECT_ROOT) { $PROJECT_ROOT = Get-Location }

# Colors - ASCII-safe output functions
function Write-Step { 
    param($msg) 
    Write-Host "`n>> $msg" -ForegroundColor Cyan 
}
function Write-Success { 
    param($msg) 
    Write-Host "  [OK] $msg" -ForegroundColor Green 
}
function Write-Warn { 
    param($msg) 
    Write-Host "  [!] $msg" -ForegroundColor Yellow 
}
function Write-Err { 
    param($msg) 
    Write-Host "  [X] $msg" -ForegroundColor Red 
}
function Write-Info { 
    param($msg) 
    Write-Host "  [i] $msg" -ForegroundColor Gray 
}

# Spinner animation for long-running commands
function Invoke-WithSpinner {
    param(
        [string]$Message,
        [scriptblock]$ScriptBlock
    )
    
    $spinChars = @('|', '/', '-', '\')
    $spinIndex = 0
    $startTime = Get-Date
    
    # Start the job
    $job = Start-Job -ScriptBlock $ScriptBlock
    
    # Show spinner while job is running
    Write-Host -NoNewline "  [" -ForegroundColor Gray
    while ($job.State -eq 'Running') {
        Write-Host -NoNewline "`b$($spinChars[$spinIndex])" -ForegroundColor Yellow
        $spinIndex = ($spinIndex + 1) % 4
        Start-Sleep -Milliseconds 150
    }
    
    # Get elapsed time
    $elapsed = [math]::Round(((Get-Date) - $startTime).TotalSeconds, 1)
    
    # Get result and clean up
    $result = Receive-Job -Job $job
    $exitCode = $job.ChildJobs[0].JobStateInfo.Reason
    Remove-Job -Job $job -Force
    
    # Show result
    if ($job.State -eq 'Completed') {
        Write-Host "`b] $Message (${elapsed}s)" -ForegroundColor Green
        return $true
    } else {
        Write-Host "`b] $Message - failed (${elapsed}s)" -ForegroundColor Red
        return $false
    }
}

# ============================================================================
# BANNER
# ============================================================================
Write-Host ""
Write-Host "=====================================================================" -ForegroundColor Magenta
Write-Host "   __      __     ___     _____     ___   _   _   ____   _____      " -ForegroundColor Magenta
Write-Host "   \ \    / /    / _ \   |  ___|   |_ _| | \ | | / ___| |_   _|     " -ForegroundColor Magenta
Write-Host "    \ \  / /    | |_| |  | |_       | |  |  \| | \___ \   | |       " -ForegroundColor Magenta
Write-Host "     \ \/ /     |  _  |  |  _|      | |  | |\  |  ___) |  | |       " -ForegroundColor Magenta
Write-Host "      \__/      |_| |_|  |_|       |___| |_| \_| |____/   |_|       " -ForegroundColor Magenta
Write-Host "                                                                    " -ForegroundColor Magenta
Write-Host "   Veyllo Agentic Framework - Windows Installer                     " -ForegroundColor Cyan
Write-Host "   Python + FastAPI + Next.js + pgvector + Local LLM                " -ForegroundColor Gray
Write-Host "=====================================================================" -ForegroundColor Magenta
Write-Host ""

# ============================================================================
# SYSTEM DETECTION
# ============================================================================
Write-Step "Detecting System Configuration..."

# OS Info
$osInfo = Get-CimInstance Win32_OperatingSystem
Write-Info "OS: $($osInfo.Caption) ($($osInfo.OSArchitecture))"

# ============================================================================
# 1. PYTHON CHECK
# ============================================================================
Write-Step "Checking Python Installation..."

$pythonCmd = $null
$pythonVersion = $null

# Try python3 first, then python
foreach ($cmd in @("python3", "python")) {
    try {
        $versionOutput = & $cmd --version 2>&1
        if ($versionOutput -match "Python (\d+\.\d+\.\d+)") {
            $version = [version]$Matches[1]
            if ($version -ge $MIN_PYTHON_VERSION) {
                $pythonCmd = $cmd
                $pythonVersion = $version
                break
            }
        }
    } catch { }
}

if ($pythonCmd) {
    Write-Success "Python $pythonVersion found ($pythonCmd)"
} else {
    Write-Err "Python $MIN_PYTHON_VERSION or higher not found!"
    Write-Host ""
    Write-Host "  Please install Python from: https://www.python.org/downloads/" -ForegroundColor Yellow
    Write-Host "  Or use winget: winget install Python.Python.3.12" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Make sure to check 'Add Python to PATH' during installation!" -ForegroundColor Yellow
    exit 1
}

# ============================================================================
# 2. GPU DETECTION
# ============================================================================
Write-Step "Detecting GPU for LLM Acceleration..."

$gpuInfo = @{
    HasNvidia = $false
    HasAMD = $false
    HasIntel = $false
    CudaAvailable = $false
    GpuName = "None"
    Recommendation = "CPU"
}

try {
    $gpus = Get-CimInstance Win32_VideoController | Where-Object { $_.Status -eq "OK" }
    foreach ($gpu in $gpus) {
        $name = $gpu.Name.ToLower()
        if ($name -match "nvidia") {
            $gpuInfo.HasNvidia = $true
            $gpuInfo.GpuName = $gpu.Name
            # Check for CUDA
            if (Get-Command "nvidia-smi" -ErrorAction SilentlyContinue) {
                $gpuInfo.CudaAvailable = $true
                $gpuInfo.Recommendation = "CUDA"
            }
        } elseif ($name -match "amd" -or $name -match "radeon") {
            $gpuInfo.HasAMD = $true
            $gpuInfo.GpuName = $gpu.Name
            $gpuInfo.Recommendation = "Vulkan/ROCm"
        } elseif ($name -match "intel" -and $name -match "arc") {
            $gpuInfo.HasIntel = $true
            $gpuInfo.GpuName = $gpu.Name
            $gpuInfo.Recommendation = "OpenCL"
        }
    }
} catch {
    Write-Warn "Could not detect GPU"
}

if ($gpuInfo.HasNvidia -and $gpuInfo.CudaAvailable) {
    Write-Success "NVIDIA GPU detected: $($gpuInfo.GpuName)"
    Write-Success "CUDA available - LLM will use GPU acceleration"
} elseif ($gpuInfo.HasNvidia) {
    Write-Warn "NVIDIA GPU detected but CUDA not found"
    Write-Info "Install CUDA Toolkit for GPU acceleration: https://developer.nvidia.com/cuda-downloads"
} elseif ($gpuInfo.HasAMD) {
    Write-Success "AMD GPU detected: $($gpuInfo.GpuName)"
    Write-Info "Vulkan acceleration may be available"
} else {
    Write-Info "No dedicated GPU detected - will use CPU for LLM"
}

# ============================================================================
# 3. DOCKER DETECTION
# ============================================================================
Write-Step "Checking Docker Installation (for Memory System)..."

$dockerInfo = @{
    Installed = $false
    Running = $false
    Version = $null
    ComposeAvailable = $false
}

if (-not $SkipDocker) {
    try {
        $dockerVersion = & docker --version 2>&1
        if ($dockerVersion -match "Docker version (\d+\.\d+)") {
            $dockerInfo.Installed = $true
            $dockerInfo.Version = $Matches[1]
            Write-Success "Docker $($dockerInfo.Version) installed"
            
            # Check if running
            try {
                $null = & docker info 2>&1
                $dockerInfo.Running = $true
                Write-Success "Docker daemon is running"
            } catch {
                Write-Warn "Docker is installed but not running"
                Write-Info "Please start Docker Desktop"
            }
            
            # Check Docker Compose
            try {
                $null = & docker compose version 2>&1
                $dockerInfo.ComposeAvailable = $true
                Write-Success "Docker Compose available"
            } catch {
                try {
                    $null = & docker-compose --version 2>&1
                    $dockerInfo.ComposeAvailable = $true
                    Write-Success "Docker Compose (standalone) available"
                } catch {
                    Write-Warn "Docker Compose not found"
                }
            }
        }
    } catch {
        Write-Warn "Docker not found"
    }

    if (-not $dockerInfo.Installed) {
        Write-Host ""
        Write-Warn "Docker is NOT installed!"
        Write-Host "  The Memory System requires Docker (pgvector database)." -ForegroundColor Yellow
        Write-Host ""
        
        # Try to install Docker automatically using winget
        $wingetAvailable = Get-Command winget -ErrorAction SilentlyContinue
        if ($wingetAvailable) {
            Write-Info "Attempting to install Docker Desktop via winget..."
            Write-Host "  (This may take several minutes and require a restart)" -ForegroundColor DarkGray
            Write-Host ""
            
            try {
                # Install Docker Desktop
                Write-Host -NoNewline "  [" -ForegroundColor Gray
                $dockerInstallStart = Get-Date
                
                $installJob = Start-Job -ScriptBlock {
                    winget install Docker.DockerDesktop --accept-source-agreements --accept-package-agreements 2>&1
                }
                
                $spinChars = @('|', '/', '-', '\')
                $spinIndex = 0
                $lastUpdate = Get-Date
                while ($installJob.State -eq 'Running') {
                    Write-Host -NoNewline "`b$($spinChars[$spinIndex])" -ForegroundColor Yellow
                    $spinIndex = ($spinIndex + 1) % 4
                    
                    $elapsed = [math]::Round(((Get-Date) - $dockerInstallStart).TotalSeconds, 0)
                    if ($elapsed -gt 0 -and $elapsed % 30 -eq 0 -and ((Get-Date) - $lastUpdate).TotalSeconds -ge 29) {
                        Write-Host -NoNewline "`b] ${elapsed}s... [" -ForegroundColor DarkGray
                        $lastUpdate = Get-Date
                    }
                    Start-Sleep -Milliseconds 200
                }
                
                $installResult = Receive-Job -Job $installJob
                $installExitCode = $installJob.ChildJobs[0].JobStateInfo.Reason
                Remove-Job -Job $installJob -Force
                $dockerInstallTime = [math]::Round(((Get-Date) - $dockerInstallStart).TotalSeconds, 1)
                
                # Check if installation succeeded
                if ($installResult -match "Successfully installed" -or (Get-Command docker -ErrorAction SilentlyContinue)) {
                    Write-Host "`b] Docker Desktop installed (${dockerInstallTime}s)" -ForegroundColor Green
                    Write-Host ""
                    Write-Success "Docker Desktop was installed successfully!"
                    Write-Host ""
                    Write-Host "  ============================================================" -ForegroundColor Yellow
                    Write-Host "  IMPORTANT: You need to:" -ForegroundColor Yellow
                    Write-Host "    1. RESTART your computer (required for WSL2/Hyper-V)" -ForegroundColor White
                    Write-Host "    2. After restart, run the installer again:" -ForegroundColor White
                    Write-Host "       .\install.bat" -ForegroundColor Cyan
                    Write-Host "    (Docker will start automatically and set up the database)" -ForegroundColor DarkGray
                    Write-Host "  ============================================================" -ForegroundColor Yellow
                    Write-Host ""
                    $dockerInfo.Installed = $true
                } else {
                    Write-Host "`b] Installation may have failed (${dockerInstallTime}s)" -ForegroundColor Yellow
                    Write-Host ""
                    Write-Warn "Docker installation might not have completed successfully"
                    Write-Host "  You can try installing manually:" -ForegroundColor Gray
                    Write-Host "  winget install Docker.DockerDesktop" -ForegroundColor Cyan
                    Write-Host "  Or download from: https://www.docker.com/products/docker-desktop/" -ForegroundColor Cyan
                }
            } catch {
                Write-Host "`b] Failed" -ForegroundColor Red
                Write-Warn "Automatic Docker installation failed: $_"
                Write-Host "  Please install Docker manually:" -ForegroundColor Gray
                Write-Host "  winget install Docker.DockerDesktop" -ForegroundColor Cyan
            }
        } else {
            Write-Host "  Install Docker Desktop from: https://www.docker.com/products/docker-desktop/" -ForegroundColor Cyan
            Write-Host "  Or install winget first, then: winget install Docker.DockerDesktop" -ForegroundColor Cyan
            Write-Host ""
            Write-Warn "Continuing installation - Memory System will be unavailable until Docker is installed"
            Write-Host "  After installing Docker, run the installer again: .\install.bat" -ForegroundColor Gray
        }
        Write-Host ""
    } elseif (-not $dockerInfo.Running) {
        Write-Warn "Docker is installed but NOT running!"
        Write-Host "  Starting Docker Desktop..." -ForegroundColor Yellow
        
        # Try to start Docker Desktop
        try {
            $dockerPath = "${env:ProgramFiles}\Docker\Docker\Docker Desktop.exe"
            if (Test-Path $dockerPath) {
                Start-Process $dockerPath
                Write-Info "Docker Desktop is starting... (this may take 30-60 seconds)"
                
                # Wait for Docker to start (max 60 seconds)
                $maxWait = 60
                $waited = 0
                Write-Host -NoNewline "  [" -ForegroundColor Gray
                while ($waited -lt $maxWait) {
                    try {
                        $null = & docker info 2>&1
                        $dockerInfo.Running = $true
                        break
                    } catch {
                        Write-Host -NoNewline "`b$($spinChars[$waited % 4])" -ForegroundColor Yellow
                        Start-Sleep -Seconds 2
                        $waited += 2
                    }
                }
                
                if ($dockerInfo.Running) {
                    Write-Host "`b] Docker is now running!" -ForegroundColor Green
                    $dockerInfo.ComposeAvailable = $true
                } else {
                    Write-Host "`b] Docker is still starting..." -ForegroundColor Yellow
                    Write-Info "Docker Desktop is starting in the background"
                    Write-Host "  After it's ready, run the installer again: .\install.bat" -ForegroundColor Gray
                }
            } else {
                Write-Warn "Could not find Docker Desktop executable"
                Write-Host "  Please start Docker Desktop manually" -ForegroundColor Gray
            }
        } catch {
            Write-Warn "Could not start Docker Desktop automatically"
            Write-Host "  Please start Docker Desktop manually, then run the installer again:" -ForegroundColor Gray
            Write-Host "  .\install.bat" -ForegroundColor Cyan
        }
    }
} else {
    Write-Info "Docker check skipped (--SkipDocker flag)"
}

# ============================================================================
# 4. GIT CHECK
# ============================================================================
Write-Step "Checking Git Installation..."

try {
    $gitVersion = & git --version 2>&1
    if ($gitVersion -match "git version") {
        Write-Success "Git installed: $gitVersion"
    }
} catch {
    Write-Warn "Git not found - some features may be limited"
    Write-Info "Install with: winget install Git.Git"
}

# ============================================================================
# 5. NODE.JS CHECK (for Web UI)
# ============================================================================
Write-Step "Checking Node.js Installation (for Web UI)..."

$nodeInstalled = $false
try {
    $nodeVersion = & node --version 2>&1
    if ($nodeVersion -match "v(\d+)") {
        $majorVersion = [int]$Matches[1]
        if ($majorVersion -ge 18) {
            Write-Success "Node.js $nodeVersion installed"
            $nodeInstalled = $true
        } else {
            Write-Warn "Node.js $nodeVersion is outdated (need v18+)"
        }
    }
} catch {
    Write-Warn "Node.js not found"
}

if (-not $nodeInstalled) {
    Write-Info "Install Node.js 18+ from: https://nodejs.org/"
    Write-Info "Or use winget: winget install OpenJS.NodeJS.LTS"
    Write-Warn "Web UI will not be available without Node.js"
}

# ============================================================================
# 6. VIRTUAL ENVIRONMENT
# ============================================================================
Write-Step "Setting up Python Virtual Environment..."

$venvPath = Join-Path $PROJECT_ROOT "venv"

if ((Test-Path $venvPath) -and -not $Force) {
    Write-Success "Virtual environment already exists"
    $response = Read-Host "  Recreate virtual environment? (y/N)"
    if ($response -eq "y" -or $response -eq "Y") {
        Remove-Item -Recurse -Force $venvPath
        & $pythonCmd -m venv $venvPath
        Write-Success "Virtual environment recreated"
    }
} else {
    if (Test-Path $venvPath) { Remove-Item -Recurse -Force $venvPath }
    & $pythonCmd -m venv $venvPath
    Write-Success "Virtual environment created"
}

# Activate venv
$env:VIRTUAL_ENV = $venvPath
$env:Path = "$venvPath\Scripts;$env:Path"

# ============================================================================
# 7. PYTHON DEPENDENCIES
# ============================================================================
Write-Step "Installing Python Dependencies..."
Write-Host "  (This may take 2-5 minutes depending on your internet connection)" -ForegroundColor DarkGray

# Upgrade pip
Write-Info "Upgrading pip..."
$pipStart = Get-Date
& python -m pip install --upgrade pip --quiet 2>$null
$pipTime = [math]::Round(((Get-Date) - $pipStart).TotalSeconds, 1)
Write-Success "pip upgraded (${pipTime}s)"

# Install pywin32 first (Windows-specific)
Write-Info "Installing Windows-specific packages..."
& python -m pip install pywin32 --quiet 2>$null
Write-Success "Windows packages installed"

# Install core dependencies with progress
Write-Host -NoNewline "  [" -ForegroundColor Gray
$coreStart = Get-Date
$job = Start-Job -ScriptBlock { 
    Set-Location $using:PROJECT_ROOT
    $env:VIRTUAL_ENV = $using:venvPath
    $env:Path = "$using:venvPath\Scripts;$env:Path"
    & "$using:venvPath\Scripts\python.exe" -m pip install -e . --quiet 2>&1
}
$spinChars = @('|', '/', '-', '\')
$spinIndex = 0
while ($job.State -eq 'Running') {
    Write-Host -NoNewline "`b$($spinChars[$spinIndex])" -ForegroundColor Yellow
    $spinIndex = ($spinIndex + 1) % 4
    Start-Sleep -Milliseconds 200
}
$coreResult = Receive-Job -Job $job
Remove-Job -Job $job -Force
$coreTime = [math]::Round(((Get-Date) - $coreStart).TotalSeconds, 1)
Write-Host "`b] Core dependencies installed (${coreTime}s)" -ForegroundColor Green

# Install all requirements with progress
Write-Host ""
Write-Host "  [i] Installing all requirements..." -ForegroundColor Gray
Write-Host "      (sentence-transformers, cryptography, etc. - please wait)" -ForegroundColor DarkGray
Write-Host -NoNewline "  [" -ForegroundColor Gray
$reqStart = Get-Date
$job = Start-Job -ScriptBlock {
    Set-Location $using:PROJECT_ROOT
    $env:VIRTUAL_ENV = $using:venvPath
    $env:Path = "$using:venvPath\Scripts;$env:Path"
    & "$using:venvPath\Scripts\python.exe" -m pip install -r requirements.txt --quiet 2>&1
}
$spinIndex = 0
$lastUpdate = Get-Date
while ($job.State -eq 'Running') {
    Write-Host -NoNewline "`b$($spinChars[$spinIndex])" -ForegroundColor Yellow
    $spinIndex = ($spinIndex + 1) % 4
    
    # Show elapsed time every 10 seconds
    $elapsed = [math]::Round(((Get-Date) - $reqStart).TotalSeconds, 0)
    if ($elapsed -gt 0 -and $elapsed % 15 -eq 0 -and ((Get-Date) - $lastUpdate).TotalSeconds -ge 14) {
        Write-Host -NoNewline "`b] ${elapsed}s... [" -ForegroundColor DarkGray
        $lastUpdate = Get-Date
    }
    Start-Sleep -Milliseconds 200
}
$reqResult = Receive-Job -Job $job
$jobSuccess = $job.State -eq 'Completed'
Remove-Job -Job $job -Force
$reqTime = [math]::Round(((Get-Date) - $reqStart).TotalSeconds, 1)

if ($jobSuccess) {
    Write-Host "`b] All dependencies installed (${reqTime}s)" -ForegroundColor Green
} else {
    Write-Host "`b] Some dependencies may have failed (${reqTime}s)" -ForegroundColor Yellow
    Write-Warn "Core functionality should still work"
}

# Fix pywin32 COM registration
Write-Info "Registering Windows COM components..."
try {
    & python scripts\fix_venv.py 2>$null
} catch { }

# ============================================================================
# 8. WEB UI SETUP (Node.js)
# ============================================================================
if ($nodeInstalled) {
    Write-Step "Setting up Web UI (Next.js)..."
    
    $webPath = Join-Path $PROJECT_ROOT "web"
    if (Test-Path $webPath) {
        Write-Info "Installing/updating npm packages (Web UI dependencies from web/package.json)..."
        Write-Host -NoNewline "  [" -ForegroundColor Gray
        $npmStart = Get-Date
        $job = Start-Job -ScriptBlock {
            Set-Location $using:webPath
            & npm install --silent 2>&1
        }
        $spinChars = @('|', '/', '-', '\')
        $spinIndex = 0
        while ($job.State -eq 'Running') {
            Write-Host -NoNewline "`b$($spinChars[$spinIndex])" -ForegroundColor Yellow
            $spinIndex = ($spinIndex + 1) % 4
            Start-Sleep -Milliseconds 200
        }
        $npmResult = Receive-Job -Job $job
        Remove-Job -Job $job -Force
        $npmTime = [math]::Round(((Get-Date) - $npmStart).TotalSeconds, 1)
        Write-Host "`b] Web UI dependencies installed (${npmTime}s)" -ForegroundColor Green
    }
}

# ============================================================================
# 9. DOCKER SETUP (Memory System)
# ============================================================================
if ($dockerInfo.Installed -and $dockerInfo.Running -and $dockerInfo.ComposeAvailable) {
    Write-Step "Setting up Memory System Database (pgvector)..."
    
    $composeFile = Join-Path $PROJECT_ROOT "docker-compose.memory.yml"
    if (Test-Path $composeFile) {
        Write-Info "Starting PostgreSQL with pgvector (this may take a minute on first run)..."
        Write-Host -NoNewline "  [" -ForegroundColor Gray
        $dockerStart = Get-Date
        
        try {
            # Run docker compose in background and show spinner
            $job = Start-Job -ScriptBlock {
                param($composeFile)
                & docker compose -f $composeFile up -d 2>&1
            } -ArgumentList $composeFile
            
            $spinChars = @('|', '/', '-', '\')
            $spinIndex = 0
            while ($job.State -eq 'Running') {
                Write-Host -NoNewline "`b$($spinChars[$spinIndex])" -ForegroundColor Yellow
                $spinIndex = ($spinIndex + 1) % 4
                Start-Sleep -Milliseconds 200
            }
            
            $dockerResult = Receive-Job -Job $job
            Remove-Job -Job $job -Force
            $dockerTime = [math]::Round(((Get-Date) - $dockerStart).TotalSeconds, 1)
            
            # Check if container is running
            Start-Sleep -Seconds 2
            $containerStatus = & docker ps --filter "name=vaf-memory-db" --format "{{.Status}}" 2>$null
            if ($containerStatus -match "Up") {
                Write-Host "`b] Memory System database started (${dockerTime}s)" -ForegroundColor Green
                Write-Success "Database URL: postgresql://vaf:vaf_dev_secret@localhost:5432/vaf_memory"
            } else {
                Write-Host "`b] Container starting... (${dockerTime}s)" -ForegroundColor Yellow
                Write-Info "Check status with: docker ps"
            }
        } catch {
            Write-Host "`b] Failed" -ForegroundColor Red
            Write-Warn "Failed to start Memory System - run the installer again later:"
            Write-Info ".\install.bat"
        }
    }
} elseif ($dockerInfo.Installed -and -not $dockerInfo.Running) {
    Write-Step "Memory System Database (pgvector) - Skipped"
    Write-Warn "Docker is not running. Start Docker Desktop and run the installer again:"
    Write-Info ".\install.bat"
}

# ============================================================================
# 10. CREATE SHORTCUTS
# ============================================================================
if (-not $SkipShortcuts) {
    Write-Step "Creating Desktop Shortcuts..."
    
    try {
        & python scripts\create_app_shortcut.py 2>$null
        Write-Success "Shortcuts created"
    } catch {
        Write-Warn "Could not create shortcuts"
    }
}

# ============================================================================
# 11. VERIFICATION
# ============================================================================
Write-Step "Verifying Installation..."

$checks = @(
    @{ Name = "VAF Module"; Test = { python -c "import vaf" 2>$null; $LASTEXITCODE -eq 0 } },
    @{ Name = "FastAPI"; Test = { python -c "import fastapi" 2>$null; $LASTEXITCODE -eq 0 } },
    @{ Name = "TTS Engine"; Test = { python -c "import pyttsx3" 2>$null; $LASTEXITCODE -eq 0 } },
    @{ Name = "Speech Recognition"; Test = { python -c "import speech_recognition" 2>$null; $LASTEXITCODE -eq 0 } }
)

foreach ($check in $checks) {
    if (& $check.Test) {
        Write-Success "$($check.Name)"
    } else {
        Write-Warn "$($check.Name) - not available"
    }
}

# ============================================================================
# SUMMARY
# ============================================================================
Write-Host ""
Write-Host "=====================================================================" -ForegroundColor Green
Write-Host "               INSTALLATION COMPLETE!                               " -ForegroundColor Green
Write-Host "=====================================================================" -ForegroundColor Green
Write-Host ""

Write-Host "  Quick Start:" -ForegroundColor Cyan
Write-Host "    - Double-click the Desktop shortcut, OR" -ForegroundColor White
Write-Host "    - Run: .\run_vaf.bat" -ForegroundColor White
Write-Host ""

if ($dockerInfo.Running) {
    Write-Host "  Memory System:" -ForegroundColor Cyan
    Write-Host "    - Database: postgresql://localhost:5432/vaf_memory" -ForegroundColor White
    Write-Host "    - Stop: docker compose -f docker-compose.memory.yml down" -ForegroundColor White
    Write-Host ""
}

Write-Host "  GPU Acceleration: $($gpuInfo.Recommendation)" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Documentation: https://github.com/Veyllo/VAF" -ForegroundColor Gray
Write-Host ""
