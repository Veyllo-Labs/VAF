# Scripts

This directory contains utility scripts for development, maintenance, and deployment of VAF.

## Contents

- **bump_version.py**: Automates the process of updating the project version across multiple files (e.g., `vaf/version.py`, `setup.py`).
- **setup_mac.sh**: Automated installation script for macOS and Linux users to set up dependencies and the `vaf` command shortcut.

## Usage

- **Development Setup**: Run `./scripts/setup_mac.sh` on Unix-like systems for a quick start.
- **Releasing**: Use `python scripts/bump_version.py` when preparing a new release to ensure version consistency.

## Conventions

- Scripts should be self-contained or use project-level dependencies defined in `requirements.txt`.
- Bash scripts should include appropriate error handling and platform checks.
- Python scripts should follow the project's coding standards and include docstrings.
