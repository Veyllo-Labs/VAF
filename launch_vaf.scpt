#!/usr/bin/osascript
# VAF Tray Launcher via AppleScript
tell application "Terminal"
    do script "cd \"$HOME/VAF\" && ./run_vaf.sh tray"
end tell
