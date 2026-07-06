#Requires -Version 5.1
<#
  VAF - one-click bootstrap (Windows).

  Hosted entry point. Once the VAF repo is PUBLIC on GitHub, a user runs:

      irm https://raw.githubusercontent.com/Veyllo-Labs/VAF/main/packaging/install/bootstrap.ps1 | iex

  It provisions a bare machine with NO prerequisites (no admin rights):
    1. ensures `git` (downloads portable MinGit to a user folder if missing),
    2. clones the repo (so `vaf update` keeps working - it requires a git checkout),
    3. hands off to install.ps1, which provisions uv->Python, a portable Node, and
       sets up the venv / deps / shortcut.

  The URLs below are already final (owner/repo verified against `git remote`).
  They resolve the day the repo goes public - nothing to "fill in" later.
  Until then (private alpha) use a local clone + `.\install.bat`.

  NOTE: untested on Windows from the dev box - TEST on a clean Windows VM before shipping.
#>
[CmdletBinding()]
param(
    [string]$Repo       = "Veyllo-Labs/VAF",                       # single source of truth
    [string]$Ref        = "main",                                  # branch or release tag
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Veyllo\VAF")
)

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"
$VafHome = Join-Path $env:LOCALAPPDATA "Veyllo"

function Info($m) { Write-Host "  [i] $m" -ForegroundColor Gray }
function Ok($m)   { Write-Host "  [OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [!] $m" -ForegroundColor Yellow }

Write-Host "`n== VAF bootstrap (Windows) ==`n" -ForegroundColor Cyan

# --- 1. uv (provisions Python without admin) -------------------------------
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Info "Installing uv (provisions Python, user-scoped, no admin)..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
if (Get-Command uv -ErrorAction SilentlyContinue) { Ok "uv ready" } else { Warn "uv not on PATH yet (install.ps1 will retry)" }

# --- 2. git (portable MinGit if absent - fetched, NOT bundled; git is GPLv2) ---
$gitExe = "git"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Info "git not found - downloading portable MinGit (user-scoped, no admin)..."
    try {
        $gitDir = Join-Path $VafHome "git"
        $rel = Invoke-RestMethod "https://api.github.com/repos/git-for-windows/git/releases/latest" -Headers @{ "User-Agent" = "vaf-bootstrap" }
        $asset = $rel.assets | Where-Object { $_.name -match '^MinGit-.*-64-bit\.zip$' } | Select-Object -First 1
        if (-not $asset) { throw "no MinGit 64-bit asset in latest git-for-windows release" }
        $zip = Join-Path $env:TEMP $asset.name
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip
        if (Test-Path $gitDir) { Remove-Item -Recurse -Force $gitDir }
        Expand-Archive -Path $zip -DestinationPath $gitDir -Force
        $gitExe = Join-Path $gitDir "cmd\git.exe"
        $cmdDir = Join-Path $gitDir 'cmd'
        $env:Path = "$cmdDir;$env:Path"
        # Persist portable git on the USER PATH so `git` (and `vaf update`) work in future terminals,
        # not just this install process. VAF also finds this git without PATH (git.py _resolve_git).
        try {
            $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
            if ($null -eq $userPath) { $userPath = "" }
            if (($userPath -split ';') -notcontains $cmdDir) {
                [Environment]::SetEnvironmentVariable("Path", ($userPath.TrimEnd(';') + ';' + $cmdDir), "User")
            }
        } catch { }
        Ok "portable git ready ($gitExe)"
    } catch {
        Warn "portable git failed: $_"
        Warn "Install Git (winget install Git.Git) and re-run, or download the repo ZIP manually."
        throw
    }
} else { Ok "git found" }

# --- 3. clone (or update) the repo -----------------------------------------
if (Test-Path (Join-Path $InstallDir ".git")) {
    Info "Existing checkout at $InstallDir - updating..."
    & $gitExe -C $InstallDir fetch --depth 1 origin $Ref
    & $gitExe -C $InstallDir checkout $Ref
    & $gitExe -C $InstallDir pull --ff-only
} else {
    Info "Cloning https://github.com/$Repo (ref: $Ref) -> $InstallDir"
    New-Item -ItemType Directory -Force -Path (Split-Path $InstallDir) | Out-Null
    & $gitExe clone --depth 1 --branch $Ref "https://github.com/$Repo.git" $InstallDir
}
Ok "Repository ready: $InstallDir"

# --- 4. hand off to the full installer -------------------------------------
Info "Running install.ps1 ..."
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $InstallDir "install.ps1")
if ($LASTEXITCODE -ne 0) {
    Write-Host "`n== install.ps1 FAILED (exit $LASTEXITCODE). VAF is NOT installed - see the errors above. ==`n" -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host "`n== Done. Launch with the Desktop shortcut or: $InstallDir\run_vaf.bat ==`n" -ForegroundColor Green
