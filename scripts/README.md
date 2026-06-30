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
| `generate_vocab.py` | Dev/build-time tooling that expands the backend Vocabulary Book into many languages via the configured LLM (never called at runtime) |

## CI / Lint Gates

These run in CI and can be run locally before pushing. Each fails (exit 1) on a violation.

| Script | Description |
|--------|-------------|
| `ci_check.sh` | Runs the same checks as CI locally (lint gates plus tests) |
| `check_doc_links.py` | Verifies that every relative link in the project's Markdown resolves |
| `check_license_headers.py` | Ensures every first-party source file carries the AGPL SPDX header |
| `check_soul_prompt.py` | Verifies Soul/identity loading and system-prompt building, writing to `logs/` |

## Memory / Database Utilities

| Script | Description |
|--------|-------------|
| `init-memory-db.sql` | PostgreSQL initialization for the Memory System |
| `reembed_memories.py` | Re-embeds all memories and chunks with the current embedding model (run after changing the model) |
| `migrate_users_to_scopes.py` | One-time migration of user data from `users/<username>/` to `scopes/<user_scope_id>/` |

### Row-Level Security (RLS) SQL

SQL for the per-user Row-Level Security hardening of the Memory database. Run as the `vaf` owner role against the `vaf-memory-db` container.

| File | Description |
|------|-------------|
| `rls_app_role.sql` | Creates the non-superuser application role (`vaf_app`) and grants DML |
| `rls_enforce.sql` | Cutover: enables fail-closed, forced Row-Level Security on `memories` |
| `rls_disable.sql` | Rollback that removes RLS enforcement at the database level |

## Dev / Diagnostics

Ad-hoc tooling for local development and troubleshooting (not part of normal install/run).

| Script | Description |
|--------|-------------|
| `check_users.py` | Lists the local users in the auth database |
| `clear_user_data.py` | Wipes user and RAG data for a clean slate |
| `list_mics.py` | Lists available input microphones (voice setup) |
| `repro_stream.py` | Reproduces/inspects streaming-response parsing |
| `attachment_rag_stage_test.py` | Memory profiling harness for attachment-RAG indexing/search |
| `attachment_rag_vector_ramp_test.py` | Memory profiling harness ramping attachment-RAG vector load |

## macOS Tray (`macos/`)

Native macOS menu-bar tray app and its bundler.

| File | Description |
|------|-------------|
| `macos/VAFTray.swift` | Swift source for the native macOS status-bar tray |
| `macos/VAFTray` | Compiled tray binary (built from `VAFTray.swift`) |
| `macos/build_app.sh` | Builds `VAF.app` around the native tray binary |

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
