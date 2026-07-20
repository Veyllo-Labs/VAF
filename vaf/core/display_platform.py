# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Linux display-server selection for the Qt/GTK desktop shell.

VAF renders its window in Qt WebEngine (Chromium). On NATIVE Wayland that stack hits the
EGL/GLX conflict documented in docs/platform/LINUX.md, and with the GPU running in-process
it can deadlock Chromium's compositor against the Qt scene graph: live incident 2026-07-20,
SIGABRT with the GUI thread in `RenderWidgetHostViewQtDelegateItem::updatePaintNode`
blocked on a mutex while the compositor thread waited for the write lock in
`Compositor::bind`. VAF therefore points BOTH toolkits at X11/XWayland.

This module exists because that guard used `os.environ.setdefault()`, which does NOT
override a value the session already exported - and KDE/GNOME Wayland sessions DO export
`QT_QPA_PLATFORM=wayland`. The guard was therefore a silent no-op on exactly the systems it
was written to protect. Both call sites (`vaf/tray.py` at process start, and
`desktop_window.init()` as the fallback for a window opened without the tray) now share this
one implementation, so the two copies cannot drift apart again.

Deliberately stdlib-only and free of vaf-internal imports: `tray.py` calls this at the very
top of its module body, before any heavy import and long before Qt is loaded.
"""
from __future__ import annotations

import os
import sys
from typing import MutableMapping, Optional

# Explicit opt-out for users whose native Wayland works fine (e.g. AMD/Intel):
# VAF_ALLOW_WAYLAND=1 keeps whatever the session configured.
ALLOW_WAYLAND_ENV = "VAF_ALLOW_WAYLAND"

_TRUTHY = ("1", "true", "yes", "on")


def _is_truthy(value: str) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def _x_socket_available(display: str) -> bool:
    """True when DISPLAY plausibly points at a REACHABLE X server.

    Merely having `DISPLAY` set does not mean X is usable: it is inherited or hardcoded in
    plenty of contexts where the connection fails (a stale value, a partially imported
    systemd/user environment, a switched user without the auth cookie). Qt's xcb plugin does
    not fall back - it qFatal()s - so forcing xcb there would turn a session that at least
    STARTED (on Wayland) into one that does not come up at all. That is worse than the
    rendering risk we are avoiding, so the guard stands down unless the socket is really there.

    Only a LOCAL display can be checked cheaply; a remote form ("host:0") is assumed
    reachable rather than blocking on a network probe. Fail-open: any parsing problem
    returns True, so this check can only ever ADD safety, never withhold the fix.
    """
    try:
        display = str(display or "").strip()
        head, sep, tail = display.rpartition(":")
        if not sep:
            return True
        if head and not head.startswith("/"):
            return True  # remote "host:0" - cannot verify cheaply
        number = tail.split(".")[0].strip()
        if not number.isdigit():
            return True  # unusual form - do not block on it
        return os.path.exists(f"/tmp/.X11-unix/X{number}")
    except Exception:
        return True


def force_x11(env: Optional[MutableMapping[str, str]] = None,
              platform: Optional[str] = None) -> str:
    """Point Qt AND GTK at X11/XWayland on Linux. Returns a short status string to log.

    Qt (`QT_QPA_PLATFORM`) and GTK (`GDK_BACKEND`) are always set TOGETHER: a mismatch is
    itself a GLX-vs-EGL conflict that makes `QWebEngineProfile` qFatal() on startup.

    Decision order:
      1. `VAF_ALLOW_WAYLAND=1`             -> change nothing (explicit opt-out).
      2. Session wants Wayland AND `DISPLAY` is set (XWayland is up) -> OVERRIDE to xcb/x11.
         This is the case `setdefault` used to miss.
      3. Session wants Wayland but `DISPLAY` is EMPTY (no XWayland) -> change nothing:
         forcing xcb without an X server would leave Qt with no display at all, i.e. turn a
         rendering risk into a guaranteed failure to start.
      4. Nothing requested                 -> set xcb/x11 as the default (unchanged behavior).

    Pure with respect to its arguments (pass `env`/`platform` in tests); never raises.
    """
    try:
        env = os.environ if env is None else env
        platform = sys.platform if platform is None else platform

        if not str(platform or "").startswith("linux"):
            return "skipped (not linux)"

        if _is_truthy(env.get(ALLOW_WAYLAND_ENV, "")):
            return f"skipped ({ALLOW_WAYLAND_ENV} set, keeping session display server)"

        qt = str(env.get("QT_QPA_PLATFORM", "") or "").strip()
        gdk = str(env.get("GDK_BACKEND", "") or "").strip()
        session = str(env.get("XDG_SESSION_TYPE", "") or "").strip().lower()
        qt_l, gdk_l = qt.lower(), gdk.lower()

        # An explicit NON-Wayland toolkit choice (offscreen / minimal / eglfs / vnc /
        # broadway) is a deliberate opt-out - headless runs, CI, screenshot harnesses - and
        # must be honored. Only the session TYPE hints are overridable; a hint must never
        # beat an explicit request. (The old setdefault honored these by accident; keep it.)
        explicit_non_wayland = (
            (bool(qt_l) and "wayland" not in qt_l) or (bool(gdk_l) and "wayland" not in gdk_l)
        )
        # WAYLAND_DISPLAY is checked too: a compositor started by hand from a TTY
        # (sway/Hyprland) often has no XDG_SESSION_TYPE, but always sets this.
        session_is_wayland = (
            session == "wayland" or bool(str(env.get("WAYLAND_DISPLAY", "") or "").strip())
        )
        wants_wayland = (
            "wayland" in qt_l
            or "wayland" in gdk_l
            or (session_is_wayland and not explicit_non_wayland)
        )

        if not wants_wayland:
            # Nothing (or already X11/offscreen/...) requested: historical default behavior.
            env.setdefault("QT_QPA_PLATFORM", "xcb")
            env.setdefault("GDK_BACKEND", "x11")
            return "default xcb/x11"

        display = str(env.get("DISPLAY", "") or "").strip()
        if not display:
            return "kept wayland (no DISPLAY - XWayland unavailable)"
        if not _x_socket_available(display):
            return f"kept wayland (DISPLAY {display!r} has no reachable X socket)"

        previous = qt or gdk or session
        env["QT_QPA_PLATFORM"] = "xcb"
        env["GDK_BACKEND"] = "x11"
        return f"forced xcb/x11 over session wayland (was {previous!r})"
    except Exception as exc:  # never let a display heuristic break startup
        return f"skipped (error: {exc})"
