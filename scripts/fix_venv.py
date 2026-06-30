# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import sys
import subprocess
from pathlib import Path


def fix_pywin32():
    """Run pywin32's post-install (COM registration). Returns True on success."""
    print("Fixing pywin32 in venv...")

    venv_site_packages = Path(sys.prefix) / "Lib" / "site-packages"
    post_install_script = venv_site_packages / "pywin32_system32" / "pywin32_postinstall.py"
    if not post_install_script.exists():
        # Try looking in Scripts
        post_install_script = Path(sys.prefix) / "Scripts" / "pywin32_postinstall.py"

    if post_install_script.exists():
        print(f"   Found script: {post_install_script}")
        try:
            subprocess.check_call([sys.executable, str(post_install_script), "-install"])
            print("   pywin32 patched successfully.")
            return True
        except Exception as e:
            print(f"   Failed to patch pywin32: {e}")
            return False
    else:
        print("   Could not find pywin32_postinstall.py. Attempting reinstall...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "--force-reinstall", "pywin32"])
            return True
        except Exception as e:
            print(f"   pywin32 reinstall failed: {e}")
            return False


def check_imports():
    """Import the modules the tray/runtime needs. Returns True if all import."""
    print("\nTesting critical imports...")
    # pyttsx3 removed - caused RAM explosion on Windows. TTS is via Docker (Piper).
    imports = ["pythoncom", "win32api", "win32con", "uvicorn", "fastapi"]
    ok = True
    for mod in imports:
        try:
            __import__(mod)
            print(f"   [OK] {mod}")
        except ImportError as e:
            print(f"   [FAILED] {mod}: {e}")
            ok = False
        except Exception as e:
            print(f"   [CRASHED] {mod}: {e}")
            ok = False
    return ok


if __name__ == "__main__":
    # ASCII only + an explicit non-zero exit on failure: this runs as its own process
    # (without main.py's UTF-8 stdout reconfigure), so emoji would crash the cp1252 Windows
    # console, and the installer relies on the exit code to detect a real COM-setup failure.
    print(f"Running in: {sys.executable}")
    patched = fix_pywin32()
    imports_ok = check_imports()
    if not (patched and imports_ok):
        sys.exit(1)
