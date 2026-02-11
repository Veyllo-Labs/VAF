#!/bin/bash
# Build VAF.app using the native Swift binary
set -e

VAF_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
APP_DIR="$HOME/Applications/VAF.app"
CONTENTS="$APP_DIR/Contents"
MACOS="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"

echo "🔨 Building VAF.app..."

# Remove old app
rm -rf "$APP_DIR"

# Create bundle structure
mkdir -p "$MACOS" "$RESOURCES"

# Copy Swift binary
cp "$VAF_DIR/scripts/macos/VAFTray" "$MACOS/VAF"
chmod +x "$MACOS/VAF"

# Write VAF directory path
echo "$VAF_DIR" > "$RESOURCES/vaf_dir.txt"

# Copy icon if available
ICON_SRC="$VAF_DIR/vaf/data/vaf_logo.png"
if [ -f "$ICON_SRC" ]; then
    cp "$ICON_SRC" "$RESOURCES/AppIcon.png"
fi

# Create Info.plist
cat > "$CONTENTS/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>VAF</string>
    <key>CFBundleIdentifier</key>
    <string>com.vaf.agent</string>
    <key>CFBundleName</key>
    <string>VAF</string>
    <key>CFBundleDisplayName</key>
    <string>VAF</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
</dict>
</plist>
PLIST

echo "✅ VAF.app built at $APP_DIR"
echo "   Binary: $MACOS/VAF (native Swift)"
echo "   VAF Dir: $VAF_DIR"
