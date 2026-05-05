#!/usr/bin/osascript
# VAF Tray Launcher via AppleScript
tell application "Terminal"
    do script "cd /Users/m.c.elsner/VAF && ./run_vaf.sh tray"
end tell
