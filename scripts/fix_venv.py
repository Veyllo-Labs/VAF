# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import os
import sys
import subprocess
from pathlib import Path

def fix_pywin32():
    print("🔧 Fixing pywin32 in venv...")
    
    # Locate the post_install script
    venv_site_packages = Path(sys.prefix) / "Lib" / "site-packages"
    post_install_script = venv_site_packages / "pywin32_system32" / "pywin32_postinstall.py"
    
    if not post_install_script.exists():
        # Try looking in Scripts
        post_install_script = Path(sys.prefix) / "Scripts" / "pywin32_postinstall.py"

    if post_install_script.exists():
        print(f"   Found script: {post_install_script}")
        try:
            subprocess.check_call([sys.executable, str(post_install_script), "-install"])
            print("✅ pywin32 patched successfully.")
        except Exception as e:
            print(f"❌ Failed to patch pywin32: {e}")
    else:
        print("⚠️  Could not find pywin32_postinstall.py. Attempting simple re-install...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--force-reinstall", "pywin32"])

def check_imports():
    print("\n🧪 Testing Critical Imports...")
    # pyttsx3 removed — caused RAM explosion on Windows. TTS is via Docker (Piper).
    imports = ["pythoncom", "win32api", "win32con", "uvicorn", "fastapi"]
    for mod in imports:
        try:
            __import__(mod)
            print(f"   ✅ {mod} imported OK")
        except ImportError as e:
            print(f"   ❌ {mod} FAILED: {e}")
        except Exception as e:
            print(f"   ❌ {mod} CRASHED: {e}")

if __name__ == "__main__":
    print(f"Running in: {sys.executable}")
    fix_pywin32()
    check_imports()
