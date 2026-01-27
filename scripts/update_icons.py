import os
import sys
from pathlib import Path
from PIL import Image, ImageDraw

# Configuration
LOGO_PATH = "/Users/m.c.elsner/VAF/logo_original.png"
ICON_DIR = Path(os.path.expanduser("~/.vaf/icons"))
STATUS_COLORS = {
    "active": (46, 204, 113),  # Green
    "idle": (241, 196, 15),    # Yellow
    "persistent": (52, 152, 219) # Blue
}

def generate_icons():
    if not os.path.exists(LOGO_PATH):
        print(f"Error: Logo file not found at {LOGO_PATH}")
        return

    # Create directory
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    
    # Load and resize base image
    base_img = Image.open(LOGO_PATH).convert("RGBA")
    base_img = base_img.resize((64, 64), Image.Resampling.LANCZOS)
    
    # Generate variants
    for status, color in STATUS_COLORS.items():
        # Copy base
        img = base_img.copy()
        draw = ImageDraw.Draw(img)
        
        # Draw status dot in bottom-right corner
        # Circle size: 16px, Padding: 2px
        x1, y1 = 46, 46
        x2, y2 = 62, 62
        
        # Border (dark background for contrast)
        draw.ellipse((x1-2, y1-2, x2+2, y2+2), fill=(30, 30, 30))
        # Status Color
        draw.ellipse((x1, y1, x2, y2), fill=color)
        
        # Save
        filename = ICON_DIR / f"tray_{status}.png"
        img.save(filename)
        print(f"Generated: {filename}")

if __name__ == "__main__":
    generate_icons()
