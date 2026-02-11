
import os
import sys
import platform
from pathlib import Path

print(f"Platform: {platform.system()}")

try:
    import rumps
    print("Rumps imported successfully")
except ImportError as e:
    print(f"Rumps import failed: {e}")

try:
    from PIL import Image
    print("Pillow imported successfully")
except ImportError as e:
    print(f"Pillow import failed: {e}")

# Check Icon Path Logic
try:
    from vaf.core.config import Config
    vaf_dir = Path(Config.load().get("vaf_dir", os.path.expanduser("~/.vaf")))
    icon_dir = vaf_dir / "icons"
    filename = icon_dir / "tray_v2_idle.png"
    
    print(f"Expected Icon Path: {filename}")
    if filename.exists():
        print("Icon file exists!")
        print(f"Size: {os.path.getsize(filename)} bytes")
    else:
        print("Icon file does NOT exist.")
        
    # Check source logo
    base_dir = Path("/Users/m.c.elsner/VAF")
    logo_path = base_dir / "vaf" / "media" / "logo_original.png"
    print(f"Source Logo Path: {logo_path}")
    if logo_path.exists():
        print("Source logo exists!")
    else:
        print("Source logo does NOT exist.")

except Exception as e:
    print(f"Error checking paths: {e}")
