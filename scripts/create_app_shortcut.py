import os
import sys
import platform
import shutil
import subprocess

def create_mac_app():
    """Create a minimal VAF.app bundle for macOS."""
    print("🍎 Creating macOS Application Bundle...")
    
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    logo_path = os.path.join(base_dir, "web", "public", "logo.png")
    
    app_name = "VAF.app"
    app_dir = os.path.join(os.path.expanduser("~/Applications"), app_name)
    
    # Create App Structure
    contents_dir = os.path.join(app_dir, "Contents")
    macos_dir = os.path.join(contents_dir, "MacOS")
    resources_dir = os.path.join(contents_dir, "Resources")
    
    if os.path.exists(app_dir):
        print(f"⚠️  Removing existing {app_dir}")
        shutil.rmtree(app_dir)
        
    os.makedirs(macos_dir, exist_ok=True)
    os.makedirs(resources_dir, exist_ok=True)
    
    # 1. Info.plist
    # LSUIElement=False allows the app to show in Dock (with correct icon).
    # If the user wants Tray-Only, it should be True, but Rumps apps showing 'Python' in Dock
    # means they are failing to register as the bundle.
    # By setting it False, we force it to be a normal app, which usually verifies the icon works.
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
    <false/>
</dict>
</plist>"""
    
    with open(os.path.join(contents_dir, "Info.plist"), "w") as f:
        f.write(info_plist)
        
    launcher_src_path = os.path.join(macos_dir, "launcher.c")
    launcher_exe_path = os.path.join(macos_dir, "VAF")
    
    # Improved C Launcher
    # We use posix_spawn or execv, but ensuring environment is robust.
    # We also attempt to re-assert the bundle ID if possible, but simplest is usually
    # just exec-ing the python binary with correct argv[0] or similar.
    
    c_source = r'''
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <string.h>
#include <libgen.h>
#include <limits.h>
#include <sys/stat.h>

int main(int argc, char *argv[]) {
    // 1. Setup Paths
    // Injected by build script
    const char *vaf_dir = "VAF_DIR_PLACEHOLDER";
    const char *python_bin = "PYTHON_BIN_PLACEHOLDER";
    
    // 2. Environment
    setenv("VAF_DIR", vaf_dir, 1);
    setenv("PYTHONPATH", vaf_dir, 1);
    
    // PATH for Node/npm
    const char *old_path = getenv("PATH");
    char new_path[4096];
    snprintf(new_path, sizeof(new_path), 
        "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:%s/.nvm/versions/node/current/bin:%s", 
        getenv("HOME"), old_path ? old_path : "");
    setenv("PATH", new_path, 1);
    
    // 3. Execution
    // We exec, but we might want to try to keep argv[0] as ourself?
    // No, python needs to be the executable.
    
    char *new_argv[] = {
        (char*)python_bin,
        "-m",
        "vaf.main",
        "tray",
        NULL
    };
    
    // Log
    FILE *log = fopen("/tmp/vaf_launch_c.log", "a");
    if (log) {
        fprintf(log, "VAF C Wrapper (v2) Launching...\n");
        fprintf(log, "Python: %s\n", python_bin);
        fclose(log);
    }
    
    if (execv(python_bin, new_argv) == -1) {
        perror("execv failed");
        return 1;
    }
    
    return 0;
}
'''
    # Inject actual paths
    python_cmd = os.path.join(base_dir, "venv", "bin", "python3")
    if not os.path.exists(python_cmd):
        python_cmd = sys.executable 
        
    c_source = c_source.replace("VAF_DIR_PLACEHOLDER", base_dir)
    c_source = c_source.replace("PYTHON_BIN_PLACEHOLDER", python_cmd)
    
    # Write Source
    with open(launcher_src_path, "w") as f:
        f.write(c_source)
        
    # Compile
    # We check for gcc or clang
    cc = "clang"
    if subprocess.call(["which", "clang"], stdout=subprocess.DEVNULL) != 0:
        cc = "gcc" # try gcc
        
    print(f"🔨 Compiling launcher with {cc}...")
    try:
        subprocess.run([cc, "-o", launcher_exe_path, launcher_src_path], check=True)
        print("✅ Compilation successful.")
    except subprocess.CalledProcessError as e:
        print(f"❌ Compilation failed: {e}")
        # Fallback to shell script?
        return

    # Cleanup source
    # os.remove(launcher_src_path)
    
    # 3. Icon (Convert png to icns if simple tools exist, else just cp png)
    # properly making .icns on mac can be done with sips + iconutil
    # For now, we'll try a simple sips conversion or skip if complex
    
    icon_dest = os.path.join(resources_dir, "AppIcon.icns")
    
    if os.path.exists(logo_path):
        # Create a temporary iconset
        iconset_dir = os.path.join(resources_dir, "AppIcon.iconset")
        os.makedirs(iconset_dir, exist_ok=True)
        
        # Resize to standard sizes
        sizes = [16, 32, 64, 128, 256, 512, 1024]
        try:
            for size in sizes:
                subprocess.run([
                    "sips", "-z", str(size), str(size), 
                    logo_path, 
                    "--out", os.path.join(iconset_dir, f"icon_{size}x{size}.png")
                ], check=True, stdout=subprocess.DEVNULL)
                # Retina (2x)
                subprocess.run([
                    "sips", "-z", str(size*2), str(size*2), 
                    logo_path, 
                    "--out", os.path.join(iconset_dir, f"icon_{size}x{size}@2x.png")
                ], check=False, stdout=subprocess.DEVNULL) # 2x might fail if source small
            
            # Convert to icns
            subprocess.run([
                "iconutil", "-c", "icns", 
                iconset_dir, 
                "-o", icon_dest
            ], check=True, stdout=subprocess.DEVNULL)
            
            # Cleanup
            shutil.rmtree(iconset_dir)
            print("✅ Created AppIcon.icns")
        except Exception as e:
            print(f"⚠️  Failed to create .icns icon: {e}. App will have generic icon.")
    
    print(f"✅ VAF.app created at {app_dir}")
    print("👉 You can now open Spotlight and type 'VAF'")


def create_linux_shortcut():
    """Create a .desktop entry for Linux."""
    print("🐧 Creating Linux .desktop shortcut...")
    
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    start_script = os.path.join(base_dir, "start_vaf.sh")
    logo_path = os.path.join(base_dir, "web", "public", "logo.png")
    
    desktop_entry = f"""[Desktop Entry]
Name=VAF Agent
Comment=VAF AI Agent & Tray App
Exec="{start_script}"
Icon={logo_path}
Terminal=false
Type=Application
Categories=Utility;
"""
    
    app_dir = os.path.expanduser("~/.local/share/applications")
    os.makedirs(app_dir, exist_ok=True)
    
    desktop_file = os.path.join(app_dir, "vaf.desktop")
    with open(desktop_file, "w") as f:
        f.write(desktop_entry)
        
    os.chmod(desktop_file, 0o755)
    print(f"✅ Shortcut created: {desktop_file}")


    print(f"✅ Shortcut created: {desktop_file}")


def create_windows_shortcut():
    """Create a .lnk shortcut on Windows using PowerShell."""
    print("🪟 Creating Windows Desktop Shortcut...")
    
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # We use pythonw.exe to run without a console window if possible,
    # or just python.exe with the tray script
    
    # Ideally, we point to start_vaf.bat if we had one, or direct python command
    # Let's point to the start_vaf.sh equivalent.
    # Actually, proper way is usually a .bat wrapper or pointing to python.exe with args.
    
    target = sys.executable
    # Using pythonw to hide console? Actually users might want to see it initially, 
    # but for tray app usually hidden.
    if "python.exe" in target:
        target = target.replace("python.exe", "pythonw.exe")
        
    arguments = "-m vaf.main tray"
    
    # Icon
    icon_path = os.path.join(base_dir, "web", "public", "logo.png")
    # Windows shortcuts prefer .ico. If only png exists, it might not pick it up well.
    # But usually it falls back or we can convert if pillow is there.
    # For robust dependency-free, we just try to point to it.
    
    desktop = os.path.join(os.environ["USERPROFILE"], "Desktop")
    shortcut_path = os.path.join(desktop, "VAF Agent.lnk")
    working_dir = base_dir

    ps_script = f"""
    $WshShell = New-Object -comObject WScript.Shell
    $Shortcut = $WshShell.CreateShortcut("{shortcut_path}")
    $Shortcut.TargetPath = "{target}"
    $Shortcut.Arguments = "{arguments}"
    $Shortcut.WorkingDirectory = "{working_dir}"
    $Shortcut.IconLocation = "{icon_path}"
    $Shortcut.Description = "VAF AI Agent"
    $Shortcut.Save()
    """
    
    try:
        subprocess.run(["powershell", "-Command", ps_script], check=True)
        print(f"✅ Shortcut created at: {shortcut_path}")
    except Exception as e:
        print(f"❌ Failed to create shortcut: {e}")


if __name__ == "__main__":
    system = platform.system()
    if system == "Darwin":
        create_mac_app()
    elif system == "Linux":
        create_linux_shortcut()
    elif system == "Windows":
        create_windows_shortcut()
    else:
        print(f"⚠️  Shortcut creation not implemented for {system} yet.")
