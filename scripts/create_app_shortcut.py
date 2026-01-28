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
    
    c_source = r'''
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <string.h>
#include <libgen.h>
#include <limits.h>
#include <sys/stat.h>

int main(int argc, char *argv[]) {
    const char *vaf_dir = "VAF_DIR_PLACEHOLDER";
    const char *python_bin = "PYTHON_BIN_PLACEHOLDER";
    
    setenv("VAF_DIR", vaf_dir, 1);
    setenv("PYTHONPATH", vaf_dir, 1);
    
    const char *old_path = getenv("PATH");
    char new_path[4096];
    snprintf(new_path, sizeof(new_path), 
        "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:%s/.nvm/versions/node/current/bin:%s", 
        getenv("HOME"), old_path ? old_path : "");
    setenv("PATH", new_path, 1);
    
    char *new_argv[] = {
        (char*)python_bin,
        "-m",
        "vaf.main",
        "tray",
        NULL
    };
    
    if (execv(python_bin, new_argv) == -1) {
        perror("execv failed");
        return 1;
    }
    
    return 0;
}
'''
    python_cmd = os.path.join(base_dir, "venv", "bin", "python3")
    if not os.path.exists(python_cmd):
        python_cmd = sys.executable 
        
    c_source = c_source.replace("VAF_DIR_PLACEHOLDER", base_dir)
    c_source = c_source.replace("PYTHON_BIN_PLACEHOLDER", python_cmd)
    
    with open(launcher_src_path, "w") as f:
        f.write(c_source)
        
    cc = "clang"
    if subprocess.call(["which", "clang"], stdout=subprocess.DEVNULL) != 0:
        cc = "gcc"
        
    try:
        subprocess.run([cc, "-o", launcher_exe_path, launcher_src_path], check=True)
    except Exception:
        return

    icon_dest = os.path.join(resources_dir, "AppIcon.icns")
    
    if os.path.exists(logo_path):
        iconset_dir = os.path.join(resources_dir, "AppIcon.iconset")
        os.makedirs(iconset_dir, exist_ok=True)
        sizes = [16, 32, 64, 128, 256, 512, 1024]
        try:
            for size in sizes:
                subprocess.run(["sips", "-z", str(size), str(size), logo_path, "--out", os.path.join(iconset_dir, f"icon_{size}x{size}.png")], check=True, stdout=subprocess.DEVNULL)
                subprocess.run(["sips", "-z", str(size*2), str(size*2), logo_path, "--out", os.path.join(iconset_dir, f"icon_{size}x{size}@2x.png")], check=False, stdout=subprocess.DEVNULL)
            subprocess.run(["iconutil", "-c", "icns", iconset_dir, "-o", icon_dest], check=True, stdout=subprocess.DEVNULL)
            shutil.rmtree(iconset_dir)
        except Exception:
            pass
    
    print(f"✅ VAF.app created at {app_dir}")


def create_windows_shortcut():
    """Create a .lnk shortcut on Windows using PowerShell."""
    print("🪟 Creating Windows Desktop Shortcut...")
    
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    venv_python = os.path.join(base_dir, "venv", "Scripts", "pythonw.exe")
    target = venv_python if os.path.exists(venv_python) else sys.executable
    if "python.exe" in target:
        target = target.replace("python.exe", "pythonw.exe")

    arguments = "-m vaf.main tray"
    
    logo_candidates = [
        os.path.join(base_dir, "vaf", "media", "logo_original.png"),
        os.path.join(base_dir, "web", "public", "logo.png")
    ]
    
    logo_path = None
    for cand in logo_candidates:
        if os.path.exists(cand):
            logo_path = cand
            break
            
    icon_path = logo_path
    
    if logo_path:
        try:
            from PIL import Image
            # NEW FILENAME to bypass icon cache
            ico_filename = "vaf_icon_v6.ico"
            ico_path = os.path.join(base_dir, "vaf", "media", ico_filename)
            img = Image.open(logo_path).convert("RGBA")
            
            # Aggressive Autocrop
            bbox = img.getbbox()
            if bbox:
                img = img.crop(bbox)
            
            # PERFECT FILL: 98% of canvas for maximum size with tiny margin
            canvas_size = 256
            new_img = Image.new('RGBA', (canvas_size, canvas_size), (0, 0, 0, 0))
            
            fill_factor = 0.98
            target_size = int(canvas_size * fill_factor)
            
            w, h = img.size
            if w > h:
                new_w = target_size
                new_h = int(h * (target_size / w))
            else:
                new_h = target_size
                new_w = int(w * (target_size / h))
                
            img_resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            offset = ((canvas_size - new_w) // 2, (canvas_size - new_h) // 2)
            new_img.paste(img_resized, offset, img_resized)
            
            new_img.save(ico_path, format='ICO', sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
            icon_path = ico_path
            print(f"✅ Converted icon to .ico (Max Fill): {icon_path}")
        except Exception as e:
            print(f"⚠️  Icon conversion failed: {e}")

    desktop = os.path.join(os.environ["USERPROFILE"], "Desktop")
    start_menu = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu", "Programs")
    shortcut_paths = {
        "Desktop": os.path.join(desktop, "VAF Agent.lnk"),
        "StartMenu": os.path.join(start_menu, "VAF Agent.lnk")
    }

    ps_script = "$WshShell = New-Object -comObject WScript.Shell\n"
    for name, path in shortcut_paths.items():
        ps_script += f'if (Test-Path "{path}") {{ Remove-Item "{path}" -Force }}\n'
        ps_script += f'$Shortcut = $WshShell.CreateShortcut("{path}")\n'
        ps_script += f'$Shortcut.TargetPath = "{target}"\n'
        ps_script += f'$Shortcut.Arguments = "{arguments}"\n'
        ps_script += f'$Shortcut.WorkingDirectory = "{base_dir}"\n'
        ps_script += f'$Shortcut.IconLocation = "{icon_path}"\n'
        ps_script += f'$Shortcut.Save()\n'
        ps_script += f'Write-Host "✅ Recreated {name} Shortcut"\n'
    
    try:
        subprocess.run(["powershell", "-Command", ps_script], check=True)
    except Exception as e:
        print(f"❌ Failed: {e}")


if __name__ == "__main__":
    system = platform.system()
    if system == "Darwin":
        create_mac_app()
    elif system == "Windows":
        create_windows_shortcut()
    else:
        print(f"Shortcut creation not implemented for {system}.")