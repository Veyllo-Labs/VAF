#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
#
# Enable microphone (WebUI voice input / STT) inside the desktop window's WKWebView.
#
# WebKit only exposes navigator.mediaDevices when the HOST app bundle declares
# NSMicrophoneUsageDescription. The desktop window's host is the Python.app inside
# the (Homebrew) Python framework the venv was built from - it ships WITHOUT the
# key, so the WebUI showed "Microphone access is not supported by this browser".
# This script patches the Info.plist (idempotent) and ad-hoc re-signs the bundle
# (a modified bundle with a broken signature would be killed on Apple Silicon).
#
# NOTES:
# - `brew upgrade python@X.Y` replaces the bundle and removes the patch; re-run
#   this script (or the installer) to restore mic support.
# - The TCC mic grant the user gives later attaches to this SHARED Python.app,
#   so other scripts run with the same interpreter inherit that OS-level grant.
# - Non-framework Pythons (e.g. uv-provisioned standalone builds) have no
#   Python.app; the script then skips cleanly and voice input in the desktop
#   window stays unavailable (use the Web UI in a browser instead).
#
# Usage: macos_mic_plist.sh [path-to-venv-python]   (default: ./venv/bin/python)
set -u

VENV_PY="${1:-./venv/bin/python}"
if [ ! -x "$VENV_PY" ]; then
    echo "WARNING: $VENV_PY not found - skipping microphone patch."
    exit 0
fi

# sys.base_prefix points at the framework's Versions/X.Y directory even from a
# venv (no symlink resolution needed); Python.app lives in its Resources/.
PY_APP="$("$VENV_PY" -c 'import sys, pathlib; print(pathlib.Path(sys.base_prefix) / "Resources" / "Python.app")' 2>/dev/null)"
if [ -z "$PY_APP" ] || [ ! -d "$PY_APP" ]; then
    echo "WARNING: Python.app not found for this interpreter (non-framework Python) - desktop-window voice input stays unavailable."
    exit 0
fi

PLIST="$PY_APP/Contents/Info.plist"
cp "$PLIST" "$PLIST.vaf-bak" 2>/dev/null || true

if plutil -replace NSMicrophoneUsageDescription \
        -string "VAF uses the microphone for voice input (speech-to-text) in the desktop window." \
        "$PLIST" \
    && codesign --force --sign - --preserve-metadata=entitlements,requirements,flags,runtime "$PY_APP" \
    && codesign --verify "$PY_APP"; then
    rm -f "$PLIST.vaf-bak"
    echo "OK: Microphone usage description installed ($PY_APP)"
else
    # Roll back: a patched plist under a broken signature would get EVERY use of
    # this Python killed on Apple Silicon - restoring the old plist and re-signing
    # returns the bundle to a consistent state.
    if [ -f "$PLIST.vaf-bak" ]; then
        mv "$PLIST.vaf-bak" "$PLIST"
        codesign --force --sign - --preserve-metadata=entitlements,requirements,flags,runtime "$PY_APP" >/dev/null 2>&1 || true
    fi
    echo "WARNING: Could not patch $PY_APP - desktop-window voice input may show 'not supported'."
fi
exit 0
