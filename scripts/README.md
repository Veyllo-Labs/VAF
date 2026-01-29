# Scripts

This directory contains utility scripts for development, maintenance, and deployment of VAF.

## Contents

- **bump_version.py**: Updates the project version across files.
- **create_app_shortcut.py**: Creates desktop shortcuts for VAF.
- **fix_venv.py**: Repairs or re-creates the virtual environment.
- **setup_mac.sh**: Automated installation script for macOS/Linux.
- **setup_win.ps1**: Automated installation script for Windows.
- **update_icons.py**: Regenerates app and tray icons.

## Usage

- **Development Setup (macOS/Linux)**: Run `./scripts/setup_mac.sh`.
- **Development Setup (Windows)**: Run `powershell -ExecutionPolicy Bypass -File scripts/setup_win.ps1`.
- **Releasing**: Use `python scripts/bump_version.py` when preparing a new release to ensure version consistency.

## Conventions

- Scripts should be self-contained or use project-level dependencies defined in `requirements.txt`.
- Bash scripts should include appropriate error handling and platform checks.
- Python scripts should follow the project's coding standards and include docstrings.
