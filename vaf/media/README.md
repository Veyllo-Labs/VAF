# VAF Media Assets

This directory contains static media assets used for branding, icons, and UI elements across the Veyllo Agentic Framework.

## Contents

- **logo_original.png**: The source high-resolution logo for VAF.
- **app_icon.ico**: The current Windows application icon.
- **vaf_icon_v6.ico**: Versioned icon optimized for the Windows Desktop and Start Menu (maximized zoom).
- **sounds/**: Directory containing audio files for notifications and speech feedback.

## Usage

Assets from this directory are referenced by:
- `vaf/tray.py` for the system tray icon generation.
- `scripts/create_app_shortcut.py` for creating platform-specific shortcuts.
- The Web Dashboard for branding.

## Adding Assets

When adding new visual assets:
1. Ensure images are in high-resolution PNG format with transparency (if applicable).
2. For icons, use the provided generation scripts to ensure cross-platform compatibility.
