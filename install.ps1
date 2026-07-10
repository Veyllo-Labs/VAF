#Requires -Version 5.1
<#
.SYNOPSIS
    VAF - Veyllo Agentic Framework - Windows Auto-Installer
    
.DESCRIPTION
    Complete installation script for Windows that handles:
    - Python 3.10-3.13 detection (out-of-range Pythons fall back to uv provisioning)
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

# Disable console QuickEdit mode. With QuickEdit on (the Windows default) a stray mouse
# click puts the console into "select" mode and PAUSES the whole installer until a key is
# pressed -- which repeatedly looked like a hang. Turn it off so a click can never freeze it.
try {
    if (-not ("Win32.VafConsole" -as [type])) {
        Add-Type -Namespace Win32 -Name VafConsole -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError=true)] public static extern System.IntPtr GetStdHandle(int nStdHandle);
[DllImport("kernel32.dll", SetLastError=true)] public static extern bool GetConsoleMode(System.IntPtr h, out uint mode);
[DllImport("kernel32.dll", SetLastError=true)] public static extern bool SetConsoleMode(System.IntPtr h, uint mode);
'@
    }
    $_qeh = [Win32.VafConsole]::GetStdHandle(-10)  # STD_INPUT_HANDLE
    [uint32]$_qem = 0
    if ([Win32.VafConsole]::GetConsoleMode($_qeh, [ref]$_qem)) {
        # clear ENABLE_QUICK_EDIT_MODE (0x40), set ENABLE_EXTENDED_FLAGS (0x80)
        $_qem = ($_qem -band (-bnot [uint32]0x40)) -bor [uint32]0x80
        [void][Win32.VafConsole]::SetConsoleMode($_qeh, $_qem)
    }
} catch { }

# ============================================================================
# INSTALL LOG (written OUTSIDE the project folder so it survives re-extracting /
# deleting the VAF folder, and is easy to find + share when something goes wrong)
# ============================================================================
$VAF_INSTALL_LOG = $null
try {
    $logDir = Join-Path $env:LOCALAPPDATA "Veyllo\logs"
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $VAF_INSTALL_LOG = Join-Path $logDir ("install-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
    Start-Transcript -Path $VAF_INSTALL_LOG -Force | Out-Null
    Write-Host "  [i] Full install log: $VAF_INSTALL_LOG" -ForegroundColor DarkGray
} catch { $VAF_INSTALL_LOG = $null }

# ============================================================================
# CONFIGURATION
# ============================================================================
$MIN_PYTHON_VERSION = [version]"3.10"
# Highest SUPPORTED minor - must match the CI matrix (.github/workflows/ci.yml /
# ci-nightly.yml); tests/test_installer_python_gate.py fails when they drift.
# A NEWER Python is rejected on purpose: brand-new releases lack prebuilt wheels
# for key packages (pip then tries source builds and fails, e.g. pyaudio on 3.14),
# so the installer provisions a supported interpreter via uv instead.
$MAX_PYTHON_VERSION = [version]"3.13"
$PROJECT_ROOT = $PSScriptRoot
if (-not $PROJECT_ROOT) { $PROJECT_ROOT = Get-Location }

# Shared spinner charset (used by several wait loops below). Defined at script scope so
# every spinner site resolves it - the Docker-start wait referenced it before it existed.
$spinChars = @('|', '/', '-', '\')

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
# 0a. HARDWARE VIRTUALIZATION / HYPER-V CHECK (the very first gate)
# Everything container-related (WSL2 -> Rancher Desktop -> Docker images)
# sits on the Windows hypervisor platform, which needs hardware
# virtualization (Intel VT-x / AMD-V). When it is disabled in the BIOS/UEFI,
# 'wsl --install' still SUCCEEDS and the failure only surfaces after a
# reboot, deep in the container-runtime setup (WSL error 0x80370102 /
# HCS_E_HYPERV_NOT_INSTALLED) - minutes of installer work too late. So gate
# on it here, before anything else.
#
# HOW we check (deliberate):
# - The usual feature queries (Get-WindowsOptionalFeature -Online, dism)
#   require ADMIN. The CIM reads below do NOT - no UAC prompt needed for a
#   pure status check - and return locale-independent booleans (this
#   installer never parses localized command output).
# - Order matters (official caveat): a RUNNING hypervisor masks the CPU
#   virtualization flags, so Win32_Processor can read False on a machine
#   where everything is fine. Win32_ComputerSystem.HypervisorPresent is
#   therefore checked FIRST; the firmware flag is only consulted when no
#   hypervisor is active yet.
# - We check the hypervisor PLATFORM, not the full Hyper-V role: the role is
#   Pro/Enterprise-only, but WSL2 (all VAF needs) also runs on Home - a
#   role/feature check would wrongly fail Home machines.
# ============================================================================
function Test-VirtualizationReady {
    # Returns 'hypervisor' (already running), 'firmware' (enabled in BIOS,
    # platform not active yet), 'disabled' (off in BIOS/UEFI) or 'unknown'.
    try {
        $cs = Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop
        if ($cs.HypervisorPresent -eq $true) { return 'hypervisor' }
    } catch { }
    try {
        $cpu = Get-CimInstance -ClassName Win32_Processor -ErrorAction Stop | Select-Object -First 1
        if ($cpu.VirtualizationFirmwareEnabled -eq $true) { return 'firmware' }
        if ($cpu.VirtualizationFirmwareEnabled -eq $false) { return 'disabled' }
    } catch { }
    return 'unknown'
}

if (-not $SkipDocker) {
    Write-Step "Checking hardware virtualization (required for WSL2/containers)..."
    switch (Test-VirtualizationReady) {
        'hypervisor' {
            Write-Success "A hypervisor is already running (Hyper-V platform active)."
        }
        'firmware' {
            Write-Success "Virtualization is enabled in the firmware (VT-x/AMD-V)."
        }
        'disabled' {
            Write-Host ""
            Write-Warn "ACTION NEEDED: Hardware virtualization is DISABLED on this machine."
            Write-Info "WSL2 and the container runtime cannot work without it. To fix:"
            Write-Info "1. Reboot into the BIOS/UEFI setup (usually F2/F10/DEL at boot, or"
            Write-Info "   Windows: Settings > Recovery > Advanced startup > UEFI Firmware Settings)."
            Write-Info "2. Enable the virtualization option. Common names:"
            Write-Info "     Intel: 'Intel Virtualization Technology' / 'Intel VT-x'"
            Write-Info "     AMD:   'SVM Mode' / 'AMD-V'"
            Write-Info "   (often under Advanced / CPU Configuration / Security)"
            Write-Info "3. Save, boot back into Windows, and run the same install commands again."
            Write-Info "   Finished steps are skipped; the installer continues where it stopped."
            if ($VAF_INSTALL_LOG) { try { Stop-Transcript | Out-Null } catch { } }
            exit 1
        }
        default {
            # WMI gave no readable answer (rare OEM firmware quirks). Do not block a
            # machine that may be fine - the WSL2 step right below still gates hard.
            Write-Warn "Could not determine the virtualization state (WMI gave no answer) - continuing."
            Write-Info "If WSL2 setup fails later with error 0x80370102, enable VT-x/AMD-V in the BIOS/UEFI."
        }
    }
}

# ============================================================================
# 0b. WSL2 CHECK (before anything heavy)
# The container runtime (Rancher Desktop) runs on a WSL2 engine. Without WSL2
# its installer fails late and cryptically - after minutes of Python/Node
# work - so verify/enable WSL2 up front. Status checks need no admin; only
# enabling the Windows features does (one targeted UAC prompt, usually
# followed by a reboot).
# ============================================================================
function Test-Wsl2Ready {
    # Returns 'ready' or 'missing'. Locale-independent: wsl.exe output is
    # localized (and UTF-16), so we never parse it - only exit codes count.
    # Setting the default version to 2 needs NO admin, is idempotent, and
    # doubles as the capability probe: it fails while the WSL2 components
    # (Virtual Machine Platform / kernel) are absent, and permanently sets
    # the default for the user when they are present.
    try {
        if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) { return 'missing' }
        & wsl.exe --status *> $null
        if ($LASTEXITCODE -ne 0) { return 'missing' }
        & wsl.exe --set-default-version 2 *> $null
        if ($LASTEXITCODE -eq 0) { return 'ready' }
        return 'missing'
    } catch { return 'missing' }
}

if (-not $SkipDocker) {
    Write-Step "Checking WSL2 (required by the container runtime)..."
    # Skip only when a LINUX container engine is already serving: Docker Desktop's
    # legacy Hyper-V backend and remote engines (DOCKER_HOST) legitimately run
    # without WSL - but an engine in WINDOWS-containers mode also answers
    # 'docker info' and cannot run VAF's Linux images, so the OSType must say so.
    $engineUp = $false
    try {
        $osType = (& docker info --format '{{.OSType}}' 2>$null | Out-String).Trim()
        $engineUp = ($LASTEXITCODE -eq 0 -and $osType -eq 'linux')
    } catch { $engineUp = $false }
    if ($engineUp) {
        Write-Success "A Linux container engine is already running - skipping the WSL2 check."
    } elseif ((Test-Wsl2Ready) -eq 'ready') {
        Write-Success "WSL2 is available (default version set to 2)."
    } else {
        Write-Warn "WSL2 is not set up on this machine - Rancher Desktop cannot run without it."
        Write-Info "Enabling it now (approve the UAC prompt; this installs no Linux distribution)..."
        # Elevated once: 'wsl --install --no-distribution' on current builds; the dism
        # feature-enable pair covers older Windows 10 builds without that flag.
        $wslFix = 'wsl.exe --install --no-distribution; if ($LASTEXITCODE -ne 0) { ' +
                  'dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart; ' +
                  'dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart }'
        $elevated = $false
        try {
            Start-Process powershell.exe -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-Command",$wslFix -Verb RunAs -Wait | Out-Null
            $elevated = $true
        } catch {
            Write-Warn "Elevation was declined - WSL2 must be enabled manually."
        }
        if ($elevated -and (Test-Wsl2Ready) -eq 'ready') {
            # Features were already present and only the kernel/default needed fixing -
            # no reboot required, carry on.
            Write-Success "WSL2 enabled and set as the default version."
        } elseif ($elevated) {
            Write-Host ""
            Write-Warn "ACTION NEEDED: WSL2 was installed but Windows must RESTART to finish it."
            Write-Info "1. Restart Windows now."
            Write-Info "2. After the restart, run the same install commands from the guide again."
            Write-Info "   'git clone' will say the folder already exists - that is expected;"
            Write-Info "   keep going with the remaining commands. Finished steps are skipped,"
            Write-Info "   the installer continues where it stopped."
            Write-Info "   Shortcut: open PowerShell and run just:"
            Write-Info "       cd `"$PROJECT_ROOT`""
            Write-Info "       .\install.bat"
            if ($VAF_INSTALL_LOG) { try { Stop-Transcript | Out-Null } catch { } }
            # 3010 = ERROR_SUCCESS_REBOOT_REQUIRED: a planned pause, not a failure -
            # install.bat prints resume instructions instead of the error message.
            exit 3010
        } else {
            Write-Info "Run these commands in an ADMINISTRATOR PowerShell, then restart Windows:"
            Write-Info "    wsl --install --no-distribution"
            Write-Info "    wsl --set-default-version 2"
            Write-Info "After the restart, run the same install commands from the guide again."
            Write-Info "'git clone' will say the folder already exists - that is expected;"
            Write-Info "keep going with the remaining commands."
            if ($VAF_INSTALL_LOG) { try { Stop-Transcript | Out-Null } catch { } }
            exit 3010
        }
    }
}

# ============================================================================
# 1. PYTHON CHECK
# ============================================================================
Write-Step "Checking Python Installation..."

$pythonCmd = $null
$pythonVersion = $null
$useUv = $false

# Try python3 first, then python. Accept ONLY the supported range (MIN..MAX): a too-NEW
# Python is as unusable as a too-old one (no prebuilt wheels yet -> pip source builds fail,
# e.g. pyaudio's portaudio.h on 3.14). An out-of-range find falls through to the uv path,
# which provisions a supported interpreter.
$unsupportedPython = $null
foreach ($cmd in @("python3", "python")) {
    try {
        $versionOutput = & $cmd --version 2>&1
        if ($versionOutput -match "Python (\d+\.\d+\.\d+)") {
            $version = [version]$Matches[1]
            $majorMinor = [version]("$($version.Major).$($version.Minor)")
            if ($majorMinor -ge $MIN_PYTHON_VERSION -and $majorMinor -le $MAX_PYTHON_VERSION) {
                $pythonCmd = $cmd
                $pythonVersion = $version
                break
            } elseif (-not $unsupportedPython) {
                $unsupportedPython = $version
            }
        }
    } catch { }
}
if ($unsupportedPython -and -not $pythonCmd) {
    Write-Warn "Python $unsupportedPython found, but VAF supports $MIN_PYTHON_VERSION-$MAX_PYTHON_VERSION (the CI-tested range)."
    Write-Info "Out-of-range interpreters are not covered by CI or prebuilt wheels - provisioning a supported Python via uv instead."
}

# Prefer uv: it provisions Python without admin rights, so a bare machine needs
# nothing pre-installed. Auto-install uv when neither a suitable Python nor uv exists.
$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if (-not $pythonCmd -and -not $uvCmd) {
    Write-Warn "No suitable Python found - installing uv (provisions Python, no admin)..."
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
        $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
        $uvCmd = Get-Command uv -ErrorAction SilentlyContinue
    } catch { Write-Warn "uv install failed: $_" }
}

if ($uvCmd) {
    $useUv = $true
    Write-Success "Using uv to manage Python ($($uvCmd.Source))"
} elseif ($pythonCmd) {
    Write-Success "Python $pythonVersion found ($pythonCmd)"
} else {
    Write-Err "No supported Python ($MIN_PYTHON_VERSION-$MAX_PYTHON_VERSION) found and uv could not be installed!"
    Write-Host ""
    Write-Host "  Install Python from: https://www.python.org/downloads/ (check 'Add Python to PATH')" -ForegroundColor Yellow
    Write-Host "  Or install uv:       irm https://astral.sh/uv/install.ps1 | iex" -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

# ============================================================================
# 1b. VISUAL C++ RUNTIME (required by native wheels such as greenlet)
# ============================================================================
# uv's standalone Python does not ship vcruntime140_1.dll, which some compiled
# wheels link against - notably greenlet, used by SQLAlchemy's async engine. On a
# bare machine that DLL is missing and the import fails at runtime ("DLL load
# failed while importing _greenlet"), which breaks the database / auth / setup
# layer. Ensure the Microsoft VC++ runtime is present.
Write-Step "Checking Visual C++ Runtime (required by the database layer)..."

$vcRuntimePresent = $false
foreach ($vcDir in @("$env:SystemRoot\System32", "$env:SystemRoot\SysWOW64")) {
    if (Test-Path (Join-Path $vcDir "vcruntime140_1.dll")) { $vcRuntimePresent = $true; break }
}

if ($vcRuntimePresent) {
    Write-Success "Visual C++ runtime present"
} else {
    Write-Warn "Visual C++ runtime (vcruntime140_1.dll) missing - installing Microsoft VC++ Redistributable..."
    Write-Info "A Windows UAC prompt will appear - approve it to install the VC++ runtime."
    try {
        $vcExe = Join-Path $env:TEMP "vc_redist.x64.exe"
        Invoke-WebRequest -Uri "https://aka.ms/vs/17/release/vc_redist.x64.exe" -OutFile $vcExe -TimeoutSec 300
        # /install needs elevation; -Verb RunAs raises the UAC prompt for this step only.
        $vcProc = Start-Process -FilePath $vcExe -ArgumentList "/install","/passive","/norestart" -Verb RunAs -Wait -PassThru
        # Re-probe the DLL: a /passive install can no-op or report a non-fatal code while the
        # runtime is still absent. Gate success on the file actually being present now.
        $vcNowPresent = $false
        foreach ($vcDir in @("$env:SystemRoot\System32", "$env:SystemRoot\SysWOW64")) {
            if (Test-Path (Join-Path $vcDir "vcruntime140_1.dll")) { $vcNowPresent = $true; break }
        }
        if ($vcNowPresent) {
            Write-Success "Visual C++ runtime installed"
        } else {
            Write-Err "VC++ installer finished (exit $($vcProc.ExitCode)) but vcruntime140_1.dll is still missing."
            Write-Host "  greenlet/SQLAlchemy (database/auth/setup) cannot load without it. Install it manually, then re-run:" -ForegroundColor Yellow
            Write-Host "  https://aka.ms/vs/17/release/vc_redist.x64.exe" -ForegroundColor Yellow
            exit 1
        }
    } catch {
        Write-Err "Could not install the VC++ runtime automatically: $_"
        Write-Host "  Install it manually from: https://aka.ms/vs/17/release/vc_redist.x64.exe" -ForegroundColor Yellow
        Write-Host "  (Required - greenlet/SQLAlchemy will not load without it.) Then re-run the installer." -ForegroundColor Yellow
        exit 1
    }
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

function Install-RancherDesktop {
    # Rancher Desktop is open-source (Apache-2.0) and free for ANY use - unlike
    # Docker Desktop, which requires a paid license for larger orgs - so we may
    # auto-install it. It provides a Windows `docker`/`docker compose` CLI backed
    # by a WSL2 engine, which is exactly what VAF drives.
    try {
        Write-Info "Resolving latest Rancher Desktop release..."
        $rel = Invoke-RestMethod "https://api.github.com/repos/rancher-sandbox/rancher-desktop/releases/latest" -Headers @{ "User-Agent" = "vaf-installer" }
        $asset = $rel.assets | Where-Object { $_.name -match '\.msi$' } | Select-Object -First 1
        if (-not $asset) { Write-Warn "No Rancher Desktop .msi in the latest release."; return $false }
        $msi = Join-Path $env:TEMP $asset.name
        Write-Info "Downloading $($asset.name) (large, ~600 MB)..."
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $msi
        Write-Info "Installing Rancher Desktop (approve the UAC prompt)..."
        $p = Start-Process msiexec.exe -ArgumentList "/i","`"$msi`"","/qb","/norestart" -Verb RunAs -Wait -PassThru
        return ($p.ExitCode -eq 0 -or $p.ExitCode -eq 3010)
    } catch {
        Write-Warn "Rancher Desktop install failed: $_"
        return $false
    }
}

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
        Write-Warn "No container runtime found - VAF needs one (PostgreSQL/pgvector runs in a container)."
        Write-Info "Installing Rancher Desktop - free & open-source, no Docker Desktop license required..."
        if (Install-RancherDesktop) {
            Write-Success "Rancher Desktop installed."
            # Start Rancher's GUI NOW (right after install) so its first-run WSL2 provisioning runs
            # in the BACKGROUND while we install Python deps + build the Web UI (several minutes).
            # By the time we reach the container step at the END, Rancher is already up - the wait
            # there is then short or zero. The welcome modal appears regardless, so we embrace it
            # early (in parallel) instead of fighting it.
            $rdExeEarly = @(
                (Join-Path ${env:ProgramFiles} "Rancher Desktop\Rancher Desktop.exe"),
                (Join-Path ${env:LOCALAPPDATA} "Programs\Rancher Desktop\Rancher Desktop.exe")
            ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
            if ($rdExeEarly) {
                Write-Host ""
                Write-Warn "ACTION NEEDED: Rancher Desktop is opening. In its welcome screen:"
                Write-Info  "  1) set the Container Engine to 'dockerd (moby)',"
                Write-Info  "  2) click OK (Kubernetes can stay on or off - VAF only needs moby)."
                Write-Info  "Then just LEAVE IT RUNNING - it sets up in the background while this"
                Write-Info  "installer keeps going. We use it at the very end to start the containers."
                Write-Host ""
                try { Start-Process $rdExeEarly } catch { Write-Warn "Could not launch Rancher Desktop: $_" }
            } else {
                Write-Warn "Rancher Desktop exe not found yet - it will be started at the container step."
            }
        } else {
            Write-Warn "Automatic install did not complete - install a free runtime manually, then start VAF:"
            Write-Info "  - Rancher Desktop: https://rancherdesktop.io  (set engine to 'dockerd (moby)')"
            Write-Info "  - or Docker Engine in WSL2, or Podman"
        }
        Write-Host ""
        # We auto-install Rancher Desktop (not Docker Desktop) because Rancher is
        # Apache-2.0 / free for any use. If the user already has ANY docker CLI
        # (Docker Desktop, Engine, Rancher, Podman-with-alias) the detection above
        # picks it up and we just use it.
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
    Write-Info "Node.js not found - downloading a portable Node (user-scoped, no admin)..."
    try {
        $arch = if ([Environment]::Is64BitOperatingSystem) { "x64" } else { "x86" }
        # Resolve the latest v22 LTS filename from the official dist index (not bundled - fetched).
        $shasums = Invoke-RestMethod "https://nodejs.org/dist/latest-v22.x/SHASUMS256.txt" -TimeoutSec 30
        $zipName = ([regex]::Matches($shasums, "node-v[\d.]+-win-$arch\.zip") | Select-Object -First 1).Value
        if (-not $zipName) { throw "could not resolve Node win-$arch zip name" }
        $nodeDir = Join-Path $env:LOCALAPPDATA "Veyllo\node"
        $zip = Join-Path $env:TEMP $zipName
        Invoke-WebRequest -Uri "https://nodejs.org/dist/latest-v22.x/$zipName" -OutFile $zip -TimeoutSec 300
        if (Test-Path $nodeDir) { Remove-Item -Recurse -Force $nodeDir }
        Expand-Archive -Path $zip -DestinationPath $nodeDir -Force
        $nodeBin = (Get-ChildItem $nodeDir -Directory | Select-Object -First 1).FullName
        $env:Path = "$nodeBin;$env:Path"
        [Environment]::SetEnvironmentVariable("Path", "$nodeBin;" + [Environment]::GetEnvironmentVariable("Path", "User"), "User")
        $nodeVersion = & node --version 2>&1
        if ($nodeVersion -match "v\d+") { $nodeInstalled = $true; Write-Success "Portable Node.js $nodeVersion installed (user-scoped)" }
    } catch {
        Write-Warn "Portable Node download failed: $_"
        Write-Info "Install Node.js 18+ from https://nodejs.org/ (or: winget install OpenJS.NodeJS.LTS) for the Web UI"
    }
}

# ============================================================================
# 6. VIRTUAL ENVIRONMENT
# ============================================================================
Write-Step "Setting up Python Virtual Environment..."

$venvPath = Join-Path $PROJECT_ROOT "venv"

# Verifies a venv interpreter actually exists; aborts loudly if creation failed silently
# (a native non-zero exit from uv/python is NOT a terminating PowerShell error, so without
# this a broken venv is reported as success and pip then fails confusingly).
function Assert-Venv {
    param($exitCode, $what)
    if ($exitCode -ne 0 -or -not (Test-Path "$venvPath\Scripts\python.exe")) {
        Write-Err "$what failed (exit $exitCode) - usually a network/disk/AV issue while provisioning Python."
        Write-Info "Fix it, then re-run install.ps1 (uv can pre-provision: uv python install 3.12)."
        if (Test-Path $venvPath) { Remove-Item -Recurse -Force $venvPath }
        exit 1
    }
}

# Version of the interpreter inside an existing venv (or $null). Used to detect a venv
# that was created with a Python outside the supported range (e.g. a 3.14 venv from an
# earlier failed run) - reusing it would just reproduce the wheel-build failures.
function Get-VenvPythonVersion {
    $vpy = Join-Path $venvPath "Scripts\python.exe"
    if (Test-Path $vpy) {
        try {
            $out = & $vpy --version 2>&1
            if ("$out" -match "Python (\d+\.\d+\.\d+)") { return [version]$Matches[1] }
        } catch { }
    }
    return $null
}

$venvVersion = Get-VenvPythonVersion
if ($venvVersion) {
    $venvMajorMinor = [version]("$($venvVersion.Major).$($venvVersion.Minor)")
    if ($venvMajorMinor -lt $MIN_PYTHON_VERSION -or $venvMajorMinor -gt $MAX_PYTHON_VERSION) {
        Write-Warn "Existing venv uses Python $venvVersion (supported: $MIN_PYTHON_VERSION-$MAX_PYTHON_VERSION) - recreating it..."
        Remove-Item -Recurse -Force $venvPath
    }
}

if ($useUv) {
    # uv creates the venv (and downloads Python 3.12 if needed). --seed adds pip so the
    # existing `python -m pip install` steps below keep working inside a uv venv.
    if ((Test-Path $venvPath) -and $Force) { Remove-Item -Recurse -Force $venvPath }
    if (Test-Path $venvPath) {
        Write-Success "Virtual environment already exists"
    } else {
        & uv venv $venvPath --python 3.12 --seed
        Assert-Venv $LASTEXITCODE "uv venv (download CPython 3.12 + create venv)"
        Write-Success "Virtual environment created (uv, Python 3.12)"
    }
} elseif ((Test-Path $venvPath) -and -not $Force) {
    Write-Success "Virtual environment already exists"
    $response = Read-Host "  Recreate virtual environment? (y/N)"
    if ($response -eq "y" -or $response -eq "Y") {
        Remove-Item -Recurse -Force $venvPath
        & $pythonCmd -m venv $venvPath
        Assert-Venv $LASTEXITCODE "python -m venv (recreate)"
        Write-Success "Virtual environment recreated"
    }
} else {
    if (Test-Path $venvPath) { Remove-Item -Recurse -Force $venvPath }
    & $pythonCmd -m venv $venvPath
    Assert-Venv $LASTEXITCODE "python -m venv (create)"
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

# Don't let `pip install -e .` re-trigger setup.py's platform post-install (setup_win.ps1).
# Start-Job (below) inherits this env var. install.ps1 already handles setup.
$env:VAF_SKIP_POSTINSTALL = "1"

# Upgrade pip
Write-Info "Upgrading pip..."
$pipStart = Get-Date
& python -m pip install --upgrade pip --no-cache-dir --quiet 2>&1 | Out-Null
$pipTime = [math]::Round(((Get-Date) - $pipStart).TotalSeconds, 1)
if ($LASTEXITCODE -eq 0) { Write-Success "pip upgraded (${pipTime}s)" }
else { Write-Warn "pip self-upgrade failed (exit $LASTEXITCODE) - continuing with the bundled pip" }

# Install pywin32 first (Windows-specific). Required for the tray/COM + shortcut creation.
Write-Info "Installing Windows-specific packages..."
$pywinOut = & python -m pip install pywin32 --no-cache-dir --quiet 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Success "Windows packages installed"
} else {
    Write-Warn "pywin32 install failed (exit $LASTEXITCODE) - tray/COM + shortcuts may not work; fix_venv.py will try to repair it."
    $pywinOut | Select-Object -Last 12 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
}

# Install core dependencies with progress
Write-Host -NoNewline "  [" -ForegroundColor Gray
$coreStart = Get-Date
$job = Start-Job -ScriptBlock { 
    Set-Location $using:PROJECT_ROOT
    $env:VIRTUAL_ENV = $using:venvPath
    $env:Path = "$using:venvPath\Scripts;$env:Path"
    & "$using:venvPath\Scripts\python.exe" -m pip install -e . --no-cache-dir --quiet 2>&1
    "VAF_PIP_EXIT=$LASTEXITCODE"
}
$spinIndex = 0
while ($job.State -eq 'Running') {
    Write-Host -NoNewline "`b$($spinChars[$spinIndex])" -ForegroundColor Yellow
    $spinIndex = ($spinIndex + 1) % 4
    Start-Sleep -Milliseconds 200
}
$coreResult = Receive-Job -Job $job
Remove-Job -Job $job -Force
$coreTime = [math]::Round(((Get-Date) - $coreStart).TotalSeconds, 1)

# A finished Start-Job reports 'Completed' even when pip exited non-zero - check pip's OWN
# exit code. The editable 'vaf' package is mandatory; a failed install must abort, not be
# reported as success (it would only surface much later as import errors at launch).
$coreExit = -1
foreach ($line in $coreResult) {
    if ("$line" -match '^VAF_PIP_EXIT=(\d+)$') { $coreExit = [int]$Matches[1] }
}
if ($coreExit -eq 0) {
    Write-Host "`b] Core dependencies installed (${coreTime}s)" -ForegroundColor Green
} else {
    Write-Host "`b] Core install FAILED (pip exit $coreExit, ${coreTime}s)" -ForegroundColor Red
    Write-Warn "pip could not install the editable 'vaf' package - VAF cannot import or run without it."
    Write-Info "Last lines of pip output:"
    $coreResult | Where-Object { "$_" -notmatch '^VAF_PIP_EXIT=' } | Select-Object -Last 25 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
    Write-Host ""
    $coreWheelFailed = $coreResult | Where-Object { "$_" -match 'Failed building wheel|Failed to build installable wheels|failed-wheel-build-for-install' }
    if ($coreWheelFailed) {
        $venvVer = Get-VenvPythonVersion
        $venvOutOfRange = $false
        if ($venvVer) {
            $venvMM = [version]("$($venvVer.Major).$($venvVer.Minor)")
            $venvOutOfRange = ($venvMM -lt $MIN_PYTHON_VERSION -or $venvMM -gt $MAX_PYTHON_VERSION)
        }
        if ($venvOutOfRange) {
            Write-Warn "A package could not be BUILT from source - there is no prebuilt wheel for Python $venvVer on Windows."
            Write-Info "VAF supports Python $MIN_PYTHON_VERSION-$MAX_PYTHON_VERSION. Delete the venv folder and re-run install.bat -"
            Write-Info "the installer will provision a supported Python via uv automatically."
        } else {
            Write-Warn "A package could not be BUILT from source - usually a missing build tool (e.g. MSVC Build Tools) or a package without a prebuilt wheel."
            Write-Info "Check the pip output above for the failing package, fix the build requirement, then re-run install.bat."
        }
    } else {
        Write-Warn "Usually a transient network hiccup or a missing build tool. Fix it, then re-run:"
        Write-Info "  cd `"$PROJECT_ROOT`""
        Write-Info "  .\venv\Scripts\python.exe -m pip install -e ."
    }
    exit 1
}

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
    & "$using:venvPath\Scripts\python.exe" -m pip install -r requirements.txt --no-cache-dir --quiet 2>&1
    "VAF_PIP_EXIT=$LASTEXITCODE"
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
Remove-Job -Job $job -Force
$reqTime = [math]::Round(((Get-Date) - $reqStart).TotalSeconds, 1)

# A finished Start-Job reports 'Completed' even when pip exited non-zero (a native
# non-zero exit is NOT a PowerShell error), so we must check pip's OWN exit code -
# otherwise a failed or interrupted install is silently reported as success.
$pipExit = -1
foreach ($line in $reqResult) {
    if ("$line" -match '^VAF_PIP_EXIT=(\d+)$') { $pipExit = [int]$Matches[1] }
}

if ($pipExit -eq 0) {
    Write-Host "`b] All dependencies installed (${reqTime}s)" -ForegroundColor Green
} else {
    Write-Host "`b] Dependency installation FAILED (pip exit $pipExit, ${reqTime}s)" -ForegroundColor Red
    Write-Warn "pip could not install the requirements - VAF cannot run without them (fastapi/uvicorn/...)."
    Write-Info "Last lines of pip output:"
    $reqResult | Where-Object { "$_" -notmatch '^VAF_PIP_EXIT=' } | Select-Object -Last 25 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
    Write-Host ""
    # Classify the failure honestly: a wheel-BUILD failure is a Python-version/wheel problem,
    # not a network problem - the old blanket "network hiccup" message sent users down the
    # wrong path (real case: pyaudio source build on an unsupported Python 3.14).
    $wheelBuildFailed = $reqResult | Where-Object { "$_" -match 'Failed building wheel|Failed to build installable wheels|failed-wheel-build-for-install' }
    if ($wheelBuildFailed) {
        $venvVer = Get-VenvPythonVersion
        $venvOutOfRange = $false
        if ($venvVer) {
            $venvMM = [version]("$($venvVer.Major).$($venvVer.Minor)")
            $venvOutOfRange = ($venvMM -lt $MIN_PYTHON_VERSION -or $venvMM -gt $MAX_PYTHON_VERSION)
        }
        if ($venvOutOfRange) {
            Write-Warn "A package could not be BUILT from source - there is no prebuilt wheel for Python $venvVer on Windows."
            Write-Info "VAF supports Python $MIN_PYTHON_VERSION-$MAX_PYTHON_VERSION. Delete the venv folder and re-run install.bat -"
            Write-Info "the installer will provision a supported Python via uv automatically."
        } else {
            Write-Warn "A package could not be BUILT from source - usually a missing build tool (e.g. MSVC Build Tools) or a package without a prebuilt wheel."
            Write-Info "Check the pip output above for the failing package, fix the build requirement, then re-run install.bat."
        }
    } else {
        Write-Warn "This is usually a transient network hiccup. Fix connectivity, then re-run:"
        Write-Info "  cd `"$PROJECT_ROOT`""
        Write-Info "  .\venv\Scripts\python.exe -m pip install -r requirements.txt"
    }
    exit 1
}

# Fix pywin32 COM registration. fix_venv.py prints failures to stdout (and now exits
# non-zero on failure), so capture + inspect instead of swallowing with 2>$null.
Write-Info "Registering Windows COM components..."
try {
    $comOut = & python scripts\fix_venv.py 2>&1
    $comFailed = ($LASTEXITCODE -ne 0) -or ($comOut | Where-Object { "$_" -match 'Failed to patch pywin32|FAILED|CRASHED' })
    if ($comFailed) {
        Write-Warn "pywin32 COM registration may have failed - the tray uses pythoncom (CoInitialize). Last lines:"
        $comOut | Select-Object -Last 12 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
    } else {
        Write-Success "Windows COM components registered"
    }
} catch {
    Write-Warn "COM registration step errored: $($_.Exception.Message)"
}

# ============================================================================
# 8. WEB UI SETUP (Node.js)
# ============================================================================
if ($nodeInstalled) {
    Write-Step "Setting up Web UI (Next.js)..."
    
    $webPath = Join-Path $PROJECT_ROOT "web"
    if (Test-Path $webPath) {
        Write-Info "Installing npm packages (Web UI) - live npm output below, can take a few minutes:"
        Write-Host ""
        $npmStart = Get-Date
        Push-Location $webPath
        # Run npm in the FOREGROUND (no hidden Start-Job, no --silent) so its real
        # progress is visible in the terminal instead of a confusing silent spinner.
        # EAP=Continue so npm's stderr (progress/warnings) does not throw under Stop.
        $prevEAP = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & npm install --no-fund --no-audit
        $npmExit = $LASTEXITCODE
        $ErrorActionPreference = $prevEAP
        Pop-Location
        $npmTime = [math]::Round(((Get-Date) - $npmStart).TotalSeconds, 1)
        if ($npmExit -eq 0) {
            Write-Success "Web UI dependencies installed (${npmTime}s)"
            # Build the production UI NOW so first launch only runs `next start`. Otherwise the
            # first launch does a multi-minute cold `next build` and the window opens to a
            # connection-refused/blank page on a slow machine.
            Write-Info "Building the Web UI (production) - avoids a slow build on first launch:"
            Write-Host ""
            $buildStart = Get-Date
            Push-Location $webPath
            $prevEAP2 = $ErrorActionPreference
            $ErrorActionPreference = "Continue"
            & npm run build
            $buildExit = $LASTEXITCODE
            $ErrorActionPreference = $prevEAP2
            Pop-Location
            $buildTime = [math]::Round(((Get-Date) - $buildStart).TotalSeconds, 1)
            if ($buildExit -eq 0) {
                Write-Success "Web UI built (${buildTime}s)"
            } else {
                Write-Warn "Web UI production build returned exit $buildExit (${buildTime}s) - VAF will retry the build on first launch (slower)."
            }
        } else {
            Write-Warn "Web UI dependency install returned exit $npmExit (${npmTime}s) - skipping the build; the UI may rebuild on first launch."
        }
    }
}

# ============================================================================
# 9. DOCKER SETUP (Memory System) - Smart Update
# ============================================================================
$composeFile = Join-Path $PROJECT_ROOT "docker-compose.memory.yml"
$composeChanged = $false

# Check if docker-compose.memory.yml changed in the latest commit
try {
    $changedFiles = & git diff --name-only HEAD~1 HEAD 2>$null
    if ($changedFiles -match "docker-compose\.memory\.yml") {
        $composeChanged = $true
        Write-Info "docker-compose.memory.yml changed - will update Docker stack"
    }
} catch { }

# Also treat as changed if stack is not currently running
if (-not $composeChanged) {
    try {
        $running = & docker ps --filter "name=vaf-memory-db" --format "{{.Names}}" 2>$null
        if (-not $running) { $composeChanged = $true }
    } catch { $composeChanged = $true }
}

if (-not $SkipDocker) {
    Write-Step "Bringing up the container stack (database + services)..."

    # Run this whole section with EAP=Continue: docker/rdctl write harmless warnings to stderr
    # (e.g. "daemon is not using the default seccomp profile") which would otherwise raise a
    # terminating NativeCommandError under the script's EAP=Stop and abort the installer.
    $eapDocker = $ErrorActionPreference
    $ErrorActionPreference = "Continue"

    # Refresh PATH from the registry so a freshly-installed Rancher/Docker CLI (rdctl/docker)
    # is found even though THIS installer process started before that CLI existed on PATH.
    try {
        # Re-prepend the venv Scripts dir: the registry refresh below otherwise REPLACES the
        # in-process PATH, dropping the venv we activated, so later `& python` calls (shortcut,
        # verification) would miss the venv interpreter.
        $env:Path = "$venvPath\Scripts;" + [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    } catch { }

    # Resolve docker.exe ROBUSTLY each call: Rancher creates ~/.rd/bin/docker.exe and adds it to
    # PATH only during first-run, AFTER this installer started - so a one-time PATH refresh is not
    # enough. Re-read the registry PATH every call, then fall back to Rancher's known locations.
    # (This is why a fully-started Rancher used to keep printing "...still waiting" forever.)
    function Resolve-DockerExe {
        try {
            $env:Path = "$venvPath\Scripts;" + [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
        } catch { }
        $cmd = Get-Command docker -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
        # docker.exe sits in the SAME dir as rdctl (Rancher bundles its CLIs together); rdctl is
        # reliably present, so derive docker.exe from it before trying the hard-coded paths.
        $rd = Get-Command rdctl -ErrorAction SilentlyContinue
        if ($rd) {
            $cand = Join-Path (Split-Path $rd.Source) "docker.exe"
            if (Test-Path $cand) { return $cand }
        }
        $known = @(
            (Join-Path ${env:USERPROFILE} ".rd\bin\docker.exe"),
            (Join-Path ${env:LOCALAPPDATA} "Programs\Rancher Desktop\resources\resources\win32\bin\docker.exe"),
            (Join-Path ${env:ProgramFiles} "Rancher Desktop\resources\resources\win32\bin\docker.exe")
        )
        foreach ($d in $known) { if ($d -and (Test-Path $d)) { return $d } }
        return $null
    }

    function Test-DockerUp {
        $dk = Resolve-DockerExe
        if (-not $dk) { return $false }
        # EAP=Continue locally: docker emits a harmless stderr WARNING ("daemon is not using the
        # default seccomp profile") which, under the script's EAP=Stop + 2>&1, would raise a
        # terminating NativeCommandError and make this ALWAYS return false (engine never detected).
        $prev = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try { & $dk info 2>&1 | Out-Null; $ok = ($LASTEXITCODE -eq 0) }
        catch { $ok = $false }
        finally { $ErrorActionPreference = $prev }
        return $ok
    }

    $haveDocker = $null -ne (Resolve-DockerExe)
    $haveRdctl  = $null -ne (Get-Command rdctl -ErrorAction SilentlyContinue)

    if (-not $haveDocker -and -not $haveRdctl) {
        Write-Warn "No container CLI found yet - VAF will bring the containers up on first launch"
        Write-Info "(it auto-starts the runtime and downloads the images then; one-time, a few minutes)."
    } else {
        # Bring the engine up WITHOUT restarting an already-running Rancher. Three cases:
        #   (a) docker already reachable -> build now (no start, no wait)
        #   (b) Rancher process running  -> engine still spinning up; ONLY wait (no reconfigure!)
        #   (c) Rancher not running      -> start it once (rdctl headless, or the GUI)
        # Case (c)'s rdctl start triggers a Rancher RECONFIGURE+RESTART; doing that on a running
        # engine (the old bug) knocked it offline and caused an endless "...still waiting".
        if (Test-DockerUp) {
            Write-Success "Container engine already running - building containers now."
        } else {
            $rancherRunning = $null -ne (Get-Process "Rancher Desktop" -ErrorAction SilentlyContinue)
            if ($rancherRunning) {
                Write-Info "Rancher Desktop is already running - waiting for its engine to finish (no restart)..."
            } else {
                # Rancher is NOT running -> start it once. (Normally it was already launched early in
                # step 3; this path only hits if it was closed or a pre-existing runtime is down.)
                if ($haveRdctl) {
                    Write-Info "Starting the container engine via rdctl (dockerd/moby, Kubernetes off)..."
                    try { & rdctl start --container-engine.name moby --kubernetes.enabled=false 2>&1 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray } } catch { Write-Warn "rdctl start: $_" }
                } else {
                    $rdExe = @(
                        (Join-Path ${env:ProgramFiles} "Rancher Desktop\Rancher Desktop.exe"),
                        (Join-Path ${env:LOCALAPPDATA} "Programs\Rancher Desktop\Rancher Desktop.exe")
                    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
                    if ($rdExe) {
                        Write-Info "Launching Rancher Desktop ($rdExe)..."
                        try { Start-Process $rdExe } catch { Write-Warn "Could not launch Rancher Desktop: $_" }
                    } else {
                        Write-Warn "Rancher Desktop executable not found to launch."
                    }
                }
            }

            # Without WSL2 / Virtual Machine Platform the engine can NEVER come up - detect that
            # so we don't burn ~10 minutes silently and then mislabel it 'not ready in time'.
            $wslOk = $true
            try {
                $vmp  = Get-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform -ErrorAction Stop
                $wslf = Get-WindowsOptionalFeature -Online -FeatureName Microsoft-Windows-Subsystem-Linux -ErrorAction Stop
                if ($vmp.State -ne 'Enabled' -or $wslf.State -ne 'Enabled') { $wslOk = $false }
            } catch { $wslOk = $true }  # can't query (e.g. non-admin) -> fall through to the timed wait
            if (-not $wslOk) {
                Write-Warn "WSL2 / Virtual Machine Platform is NOT enabled - the container engine cannot start."
                Write-Info "Enable it (admin PowerShell), REBOOT, then re-run this installer:"
                Write-Info "    wsl --install"
                Write-Info "  (or: dism /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart"
                Write-Info "       dism /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart, then reboot)"
            } else {
                Write-Info "Waiting for the container engine (first run sets up WSL2 - can take a few minutes)..."
                for ($i = 1; $i -le 120; $i++) {
                    if (Test-DockerUp) { break }
                    # Diagnostic: show WHY we are still waiting (docker path + last info line),
                    # so a real failure is visible instead of a silent mystery.
                    if ($i % 6 -eq 0) {
                        $dk = Resolve-DockerExe
                        if ($dk) {
                            $prevD = $ErrorActionPreference; $ErrorActionPreference = "Continue"
                            try { $infoLine = (& $dk info 2>&1 | Select-Object -Last 1) } catch { $infoLine = "$_" }
                            $ErrorActionPreference = $prevD
                            Write-Info "  ...still waiting ($($i * 5)s) [docker=$([System.IO.Path]::GetFileName($dk)); $infoLine]"
                        } else {
                            Write-Info "  ...still waiting ($($i * 5)s) [docker.exe not found yet]"
                        }
                    }
                    Start-Sleep -Seconds 5
                }
            }
        }

        if ((Test-DockerUp) -and (Test-Path $composeFile)) {
            $dockerExe = Resolve-DockerExe
            Write-Step "Building + starting containers (first run sets up the database + services)..."
            Write-Info "Bringing up the core stack first (database + services), then the optional"
            Write-Info "build images (TTS, browser). A failed optional build will NOT block the database."
            Write-Host ""
            Push-Location $PROJECT_ROOT
            $prevEAP = $ErrorActionPreference
            $ErrorActionPreference = "Continue"
            # Two-phase so a failed OPTIONAL build (tts/vaf-browser - e.g. a VM clock skew breaking
            # apt-get) can never abort the whole 'up' and leave us with ZERO containers (the old bug:
            # 'docker compose up' builds every image first and starts nothing if one build fails).
            # Phase 1 = registry-image services (no build); phase 2 = the build services, best-effort.
            # --quiet-pull hides the noisy per-layer pull progress.
            & $dockerExe compose version 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) {
                & $dockerExe compose -f docker-compose.memory.yml up -d --quiet-pull postgres redis sandbox stt gotenberg
                $dcExit = $LASTEXITCODE
                Write-Info "Building optional services (TTS, browser); if this fails the core stack still runs..."
                & $dockerExe compose -f docker-compose.memory.yml up -d --quiet-pull tts vaf-browser
                if ($LASTEXITCODE -ne 0) {
                    Write-Warn "Optional TTS/browser did not build - the core stack is up; VAF will retry them later."
                    Write-Info "(Usually a VM CLOCK SKEW breaking apt in the build: fix the Windows time, then 'wsl --shutdown'.)"
                }
            } else {
                Write-Info "docker compose plugin not found - using standalone docker-compose."
                & docker-compose -f docker-compose.memory.yml up -d postgres redis sandbox stt gotenberg
                $dcExit = $LASTEXITCODE
                & docker-compose -f docker-compose.memory.yml up -d tts vaf-browser 2>&1 | Out-Null
            }
            $ErrorActionPreference = $prevEAP
            Pop-Location
            if ($dcExit -eq 0) {
                Write-Success "Core container stack is up - PostgreSQL + services running."
                Write-Info "Verify anytime with: docker ps"
                $dockerInfo.Running = $true
            } else {
                Write-Warn "Core container start returned $dcExit - VAF will retry on first launch."
            }
        } else {
            Write-Warn "Container engine not ready in time - VAF will bring the containers up on first launch"
            Write-Info "(it auto-starts the runtime and downloads the images then)."
        }
    }

    $ErrorActionPreference = $eapDocker
}

# ============================================================================
# 10. CREATE SHORTCUTS
# ============================================================================
if (-not $SkipShortcuts) {
    Write-Step "Creating Desktop Shortcuts..."

    $shortcutOutput = & python scripts\create_app_shortcut.py 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Success "Shortcuts created"
    } else {
        Write-Warn "Could not create shortcuts (details below):"
        $shortcutOutput | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
    }
}

# Register the 'vaf' command: put the install directory (which ships vaf.bat) on the
# USER PATH so `vaf update`, `vaf`, etc. work from any terminal. Without this there is no
# `vaf` command on Windows and users cannot self-update. Idempotent, User scope (no admin).
try {
    $vafBat = Join-Path $PSScriptRoot "vaf.bat"
    if (Test-Path $vafBat) {
        $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
        if ($null -eq $userPath) { $userPath = "" }
        $entries = $userPath -split ";" | Where-Object { $_ -ne "" }
        $already = $entries | Where-Object { $_.TrimEnd("\") -ieq $PSScriptRoot.TrimEnd("\") }
        if (-not $already) {
            $newPath = if ($userPath.TrimEnd(";") -eq "") { $PSScriptRoot } else { $userPath.TrimEnd(";") + ";" + $PSScriptRoot }
            [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
            Write-Success "'vaf' command registered on PATH (open a NEW terminal to use it)."
        } else {
            Write-Success "'vaf' command already on PATH."
        }
    }
} catch {
    Write-Warn "Could not add VAF to PATH automatically; run VAF via .\run_vaf.bat instead."
}

# ============================================================================
# 11. VERIFICATION
# ============================================================================
Write-Step "Verifying Installation..."

# Merge stderr into the success stream and discard it (2>&1 | Out-Null) so a failed
# import does NOT raise a NativeCommandError that aborts the whole installer under
# $ErrorActionPreference = "Stop". We only care about python's exit code.
$checks = @(
    @{ Name = "VAF Module"; Required = $true;  Test = { & python -c "import vaf" 2>&1 | Out-Null; $LASTEXITCODE -eq 0 } },
    @{ Name = "FastAPI"; Required = $true;  Test = { & python -c "import fastapi" 2>&1 | Out-Null; $LASTEXITCODE -eq 0 } },
    # pyttsx3 removed - caused 1-4GB RAM explosion on Windows via SAPI/comtypes.
    # TTS is now handled by Docker (Piper). See docs/web-ui/SPEECH_FEATURES.md.
    # @{ Name = "TTS Engine"; Test = { & python -c "import pyttsx3" 2>&1 | Out-Null; $LASTEXITCODE -eq 0 } },
    @{ Name = "Speech Recognition"; Required = $false; Test = { & python -c "import speech_recognition" 2>&1 | Out-Null; $LASTEXITCODE -eq 0 } }
)

$requiredFailed = $false
foreach ($check in $checks) {
    if (& $check.Test) {
        Write-Success "$($check.Name)"
    } elseif ($check.Required) {
        Write-Err "$($check.Name) - FAILED to import (a required dependency is missing)"
        $requiredFailed = $true
    } else {
        Write-Warn "$($check.Name) - not available"
    }
}

if ($requiredFailed) {
    Write-Host ""
    Write-Err "Core imports failed - the install is INCOMPLETE. Fix the dependency errors above and re-run install.ps1:"
    Write-Info "  cd `"$PROJECT_ROOT`""
    Write-Info "  .\venv\Scripts\python.exe -m pip install -e . -r requirements.txt"
    exit 1
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
} elseif (-not $SkipDocker) {
    Write-Host "  Memory system pending:" -ForegroundColor Yellow
    Write-Host "    - The container engine was not ready yet; VAF will finish bringing up PostgreSQL/pgvector on first launch." -ForegroundColor Yellow
    Write-Host "    - If WSL2 / Virtual Machine Platform was just enabled, REBOOT before the engine can start." -ForegroundColor Yellow
    Write-Host ""
}

if (-not $nodeInstalled) {
    Write-Host "  Web UI: NOT installed - Node.js could not be set up." -ForegroundColor Yellow
    Write-Host "    Install Node.js 18+ (winget install OpenJS.NodeJS.LTS), then re-run install.ps1." -ForegroundColor White
    Write-Host ""
}

Write-Host "  GPU Acceleration: $($gpuInfo.Recommendation)" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Documentation: https://github.com/Veyllo-Labs/VAF" -ForegroundColor Gray
Write-Host ""

if ($VAF_INSTALL_LOG) {
    Write-Host "  Full install log saved to (share this if something went wrong):" -ForegroundColor Cyan
    Write-Host "    $VAF_INSTALL_LOG" -ForegroundColor White
    Write-Host ""
}
try { Stop-Transcript | Out-Null } catch { }
