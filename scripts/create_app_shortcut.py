# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import os
import sys
import platform
import shutil
import subprocess

def create_mac_app():
    """Create a VAF.app bundle that exactly mimics terminal execution."""
    print("Creating macOS Application Bundle (Terminal Mimic Mode)...")

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    logo_path = os.path.join(base_dir, "vaf", "media", "logo_original.png") # Use original high-res logo

    app_name = "VAF.app"
    app_dir = os.path.join(os.path.expanduser("~/Applications"), app_name)

    contents_dir = os.path.join(app_dir, "Contents")
    macos_dir = os.path.join(contents_dir, "MacOS")
    resources_dir = os.path.join(contents_dir, "Resources")

    if os.path.exists(app_dir):
        shutil.rmtree(app_dir)

    os.makedirs(macos_dir, exist_ok=True)
    os.makedirs(resources_dir, exist_ok=True)

    # 1. Info.plist (Standard Tray App)
    info_plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>VAF</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundleIdentifier</key>
    <string>com.vaf.agent</string>
    <key>CFBundleName</key>
    <string>VAF</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>LSMinimumSystemVersion</key>
    <string>10.10</string>
    <key>LSUIElement</key>
    <false/> <!-- Both Dock and Tray Icon -->
</dict>
</plist>"""

    with open(os.path.join(contents_dir, "Info.plist"), "w") as f:
        f.write(info_plist)

    launcher_path = os.path.join(macos_dir, "VAF")

    # 2. PATH and VENV setup
    npm_path = shutil.which("npm")
    node_dir = os.path.dirname(npm_path) if npm_path else "/usr/local/bin"

    venv_bin = os.path.join(base_dir, "venv", "bin")
    vaf_bin = os.path.join(venv_bin, "vaf")
    python_bin = os.path.join(venv_bin, "python3")

    # 3. Launcher Script - Source NVM and call run_vaf.sh
    script_content = f"""#!/bin/bash
# VAF Launcher - With NVM environment
LOG_FILE="{os.path.join(base_dir, "logs", "app_launch_debug.log")}"

cd "{base_dir}"

echo "--- Launching VAF via run_vaf.sh ---" >> "$LOG_FILE"
echo "Date: $(date)" >> "$LOG_FILE"

# Smart Check - if backend is running, just open browser
if lsof -i :8001 -sTCP:LISTEN -t >/dev/null ; then
    echo "Backend active. Opening browser." >> "$LOG_FILE"

    # Read actual frontend port
    FRONTEND_PORT_FILE="{os.path.join(base_dir, "vaf", "data", "frontend_port.txt")}"
    if [ -f "$FRONTEND_PORT_FILE" ]; then
        FRONTEND_PORT=$(cat "$FRONTEND_PORT_FILE")
        open "http://localhost:$FRONTEND_PORT"
    else
        open "http://localhost:3000"
    fi
    exit 0
fi

# CRITICAL: Source NVM to make npm available
export NVM_DIR="$HOME/.nvm"
if [ -s "$NVM_DIR/nvm.sh" ]; then
    echo "Sourcing NVM..." >> "$LOG_FILE"
    source "$NVM_DIR/nvm.sh"
fi

# Start VAF using the working run_vaf.sh script
echo "Starting VAF via run_vaf.sh..." >> "$LOG_FILE"
exec ./run_vaf.sh tray >> "$LOG_FILE" 2>&1
"""

    with open(launcher_path, "w") as f:
        f.write(script_content)

    os.chmod(launcher_path, 0o755)

    # Icon Generation
    icon_dest = os.path.join(resources_dir, "AppIcon.icns")
    if os.path.exists(logo_path):
        iconset_dir = os.path.join(resources_dir, "AppIcon.iconset")
        os.makedirs(iconset_dir, exist_ok=True)
        sizes = [16, 32, 64, 128, 256, 512, 1024]
        try:
            for size in sizes:
                subprocess.run(["sips", "-z", str(size), str(size), logo_path, "--out", os.path.join(iconset_dir, f"icon_{size}x{size}.png")], check=False, stdout=subprocess.DEVNULL)
                subprocess.run(["sips", "-z", str(size*2), str(size*2), logo_path, "--out", os.path.join(iconset_dir, f"icon_{size}x{size}@2x.png")], check=False, stdout=subprocess.DEVNULL)
            subprocess.run(["iconutil", "-c", "icns", iconset_dir, "-o", icon_dest], check=True, stdout=subprocess.DEVNULL)
            shutil.rmtree(iconset_dir)
        except Exception:
            pass

    print(f"OK VAF.app created at {app_dir}")

def _build_windows_ico(base_dir):
    """Convert the logo to a multi-size .ico via Pillow. Returns path or "" on failure."""
    logo_candidates = [
        os.path.join(base_dir, "vaf", "media", "logo_original.png"),
        os.path.join(base_dir, "web", "public", "logo.png"),
    ]
    logo_path = next((c for c in logo_candidates if os.path.exists(c)), None)
    if not logo_path:
        return ""
    try:
        from PIL import Image
        ico_path = os.path.join(base_dir, "vaf", "media", "vaf_icon_v6.ico")
        os.makedirs(os.path.dirname(ico_path), exist_ok=True)
        img = Image.open(logo_path).convert("RGBA")
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        canvas_size = 256
        target_size = int(canvas_size * 0.98)
        w, h = img.size
        if w >= h:
            new_w, new_h = target_size, max(1, int(h * (target_size / w)))
        else:
            new_h, new_w = target_size, max(1, int(w * (target_size / h)))
        resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
        canvas.paste(resized, ((canvas_size - new_w) // 2, (canvas_size - new_h) // 2), resized)
        canvas.save(ico_path, format="ICO",
                    sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
        print(f"  icon      = {ico_path}")
        return ico_path
    except Exception as e:
        print(f"WARNING: icon conversion skipped ({e}); shortcut will use the default icon")
        return ""

def create_windows_shortcut():
    """Create .lnk shortcuts on Windows (Desktop + Start Menu).

    Prefers pywin32 (installed by install.ps1) and falls back to a PowerShell
    WScript.Shell call. Failures are reported per location instead of being
    swallowed, and a non-zero exit code is returned if nothing was created so
    the caller can surface a real error.
    """
    import traceback
    print("Creating Windows Desktop Shortcut...")

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Resolve the launcher. Prefer pythonw.exe (no console window); fall back to
    # python.exe, then to whatever interpreter runs this script. Never point the
    # shortcut at a path that does not exist (uv venvs may omit pythonw.exe).
    scripts_dir = os.path.join(base_dir, "venv", "Scripts")
    candidates = [
        os.path.join(scripts_dir, "pythonw.exe"),
        os.path.join(scripts_dir, "python.exe"),
        sys.executable,
    ]
    target = next((c for c in candidates if c and os.path.exists(c)), sys.executable)
    arguments = "-m vaf.main tray"
    print(f"  target    = {target}")

    icon_path = _build_windows_ico(base_dir)

    desktop = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), "Desktop")
    start_menu = os.path.join(
        os.environ.get("APPDATA", os.path.join(os.path.expanduser("~"), "AppData", "Roaming")),
        "Microsoft", "Windows", "Start Menu", "Programs",
    )
    shortcut_paths = {
        "Desktop": os.path.join(desktop, "VAF Agent.lnk"),
        "StartMenu": os.path.join(start_menu, "VAF Agent.lnk"),
    }

    created = []

    def _via_pywin32():
        import win32com.client  # provided by pywin32 (installed on Windows)
        shell = win32com.client.Dispatch("WScript.Shell")
        for name, path in shortcut_paths.items():
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                sc = shell.CreateShortcut(path)
                sc.TargetPath = target
                sc.Arguments = arguments
                sc.WorkingDirectory = base_dir
                if icon_path:
                    sc.IconLocation = icon_path
                sc.Save()
                created.append(path)
                print(f"OK Created {name} shortcut: {path}")
            except Exception as e:
                print(f"WARNING: {name} shortcut failed: {e}")

    def _via_powershell():
        for name, path in shortcut_paths.items():
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
            except Exception as e:
                print(f"WARNING: {name} target folder unavailable: {e}")
                continue
            ps = (
                "$ws = New-Object -ComObject WScript.Shell\n"
                f'if (Test-Path "{path}") {{ Remove-Item "{path}" -Force }}\n'
                f'$s = $ws.CreateShortcut("{path}")\n'
                f'$s.TargetPath = "{target}"\n'
                f'$s.Arguments = "{arguments}"\n'
                f'$s.WorkingDirectory = "{base_dir}"\n'
            )
            if icon_path:
                ps += f'$s.IconLocation = "{icon_path}"\n'
            ps += "$s.Save()\n"
            try:
                subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
                created.append(path)
                print(f"OK Created {name} shortcut: {path}")
            except Exception as e:
                print(f"WARNING: {name} shortcut failed: {e}")

    try:
        _via_pywin32()
    except ImportError:
        print("  pywin32 not available - falling back to PowerShell COM")
        _via_powershell()
    except Exception:
        traceback.print_exc()
        _via_powershell()

    if created:
        print(f"OK {len(created)} shortcut(s) created")
    else:
        print("ERROR: no shortcuts could be created")
        sys.exit(1)


if __name__ == "__main__":
    system = platform.system()
    if system == "Darwin":
        create_mac_app()
    elif system == "Windows":
        create_windows_shortcut()
    else:
        print(f"Shortcut creation not implemented for {system}.")
