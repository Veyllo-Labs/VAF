import os
import sys
import platform
import shutil
import subprocess

def create_mac_app():
    """Create a VAF.app bundle that exactly mimics terminal execution."""
    print("🍎 Creating macOS Application Bundle (Terminal Mimic Mode)...")
    
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
    
    print(f"✅ VAF.app created at {app_dir}")

def create_windows_shortcut():
    pass # Not needed for Mac task

if __name__ == "__main__":
    create_mac_app()