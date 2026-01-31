# Scripts

This directory contains utility scripts for development, maintenance, and deployment of VAF.

## Main Installation Scripts

> **Note**: The recommended installation method is to use the root-level install scripts.
> See [Installation Guide](#installation-guide) below.

| Script | Platform | Description |
|--------|----------|-------------|
| `../install.sh` | macOS/Linux | Complete auto-installer with Docker detection |
| `../install.ps1` | Windows | Complete auto-installer with Docker detection |
| `../install.bat` | Windows | Wrapper for install.ps1 (double-click to run) |

## Helper Scripts

| Script | Description |
|--------|-------------|
| `setup_mac.sh` | Legacy macOS/Linux setup (use install.sh instead) |
| `setup_win.ps1` | Legacy Windows setup (use install.ps1 instead) |
| `bump_version.py` | Updates the project version across files |
| `create_app_shortcut.py` | Creates desktop shortcuts for VAF |
| `fix_venv.py` | Repairs Windows venv COM registration |
| `update_icons.py` | Regenerates app and tray icons |
| `init-memory-db.sql` | PostgreSQL initialization for Memory System |

## Installation Guide

### Quick Install (Recommended)

**Windows:**
```powershell
# Option 1: Double-click install.bat

# Option 2: PowerShell
.\install.ps1
```

**macOS/Linux:**
```bash
chmod +x install.sh
./install.sh
```

### What the Installer Does

1. **System Detection**
   - OS type (Windows/macOS/Linux distribution)
   - CPU architecture (x64/ARM64)
   - GPU type (NVIDIA/AMD/Apple Silicon/Intel)

2. **Dependency Checks**
   - Python 3.10+ (required)
   - Node.js 18+ (for Web UI)
   - Git (for version control features)
   - Docker (for Memory System)

3. **Installation**
   - Creates Python virtual environment
   - Installs all pip dependencies
   - Installs npm packages for Web UI
   - Configures GPU acceleration if available

4. **Docker Setup (Optional)**
   - Detects Docker Desktop/Engine
   - Starts PostgreSQL + pgvector container
   - Initializes Memory System database

5. **Shortcuts**
   - Windows: Desktop + Start Menu shortcuts
   - macOS: Application bundle in ~/Applications
   - Linux: Desktop entry in ~/.local/share/applications
   - Shell alias: `vaf` command

### Installation Options

```bash
# Skip Docker setup
./install.sh --skip-docker

# Windows equivalent
.\install.ps1 -SkipDocker

# Force recreate virtual environment
.\install.ps1 -Force

# Show help
./install.sh --help
```

### Requirements

| Requirement | Status | Notes |
|-------------|--------|-------|
| Python 3.10+ | Required | Core runtime |
| pip | Required | Package manager |
| Node.js 18+ | Optional | For Web UI |
| Docker | Optional | For Memory System |
| Git | Optional | For coding features |
| NVIDIA CUDA | Optional | GPU acceleration |

### Memory System (Docker)

The Memory System requires PostgreSQL with pgvector for vector similarity search.

**Start Memory Database:**
```bash
docker compose -f docker-compose.memory.yml up -d
```

**Stop Memory Database:**
```bash
docker compose -f docker-compose.memory.yml down
```

**Connection URL:**
```
postgresql://vaf:vaf_dev_secret@localhost:5432/vaf_memory
```

## Development Scripts

### Version Bumping

```bash
python scripts/bump_version.py
```

### Fix Windows venv

```bash
python scripts/fix_venv.py
```

## Conventions

- Scripts should be self-contained or use project-level dependencies defined in `requirements.txt`
- Bash scripts should include appropriate error handling and platform checks
- Python scripts should follow the project's coding standards and include docstrings
- All scripts should support `--help` flag for documentation
