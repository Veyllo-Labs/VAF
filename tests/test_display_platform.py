# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Linux display-server guard (vaf/core/display_platform.py) contract tests.

The guard exists because Qt WebEngine on NATIVE Wayland hits an EGL/GLX conflict and can
deadlock the Chromium compositor against the Qt scene graph (live incident 2026-07-20:
SIGABRT in RenderWidgetHostViewQtDelegateItem::updatePaintNode vs Compositor::bind).

The bug being pinned here: the old guard used os.environ.setdefault(), which does NOT
override a value the session already exported - and KDE/GNOME Wayland sessions DO export
QT_QPA_PLATFORM=wayland, so the guard silently did nothing on exactly the systems it
protects. Qt and GTK must always be switched TOGETHER (a mismatch is itself a GLX/EGL
conflict).
"""
from vaf.core.display_platform import ALLOW_WAYLAND_ENV, force_x11


def _run(env, platform="linux"):
    """Run the guard over a copy of `env`; return (status, QT_QPA_PLATFORM, GDK_BACKEND)."""
    e = dict(env)
    status = force_x11(e, platform)
    return status, e.get("QT_QPA_PLATFORM"), e.get("GDK_BACKEND")


def test_overrides_session_wayland_when_xwayland_is_available():
    """THE regression: a KDE/GNOME Wayland session exports QT_QPA_PLATFORM=wayland, and the
    old setdefault left it. With XWayland up (DISPLAY set) it must be overridden to xcb."""
    status, qt, gdk = _run({
        "QT_QPA_PLATFORM": "wayland", "XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0",
    })
    assert (qt, gdk) == ("xcb", "x11")
    assert "forced" in status


def test_wayland_session_type_alone_also_switches_both_toolkits():
    """Only XDG_SESSION_TYPE=wayland (Qt/GTK unset) still means a Wayland session."""
    status, qt, gdk = _run({"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"})
    assert (qt, gdk) == ("xcb", "x11")
    assert "forced" in status


def test_keeps_wayland_when_no_xwayland_is_available():
    """Without an X server, forcing xcb would leave Qt with no display at all - that turns a
    rendering risk into a guaranteed failure to start, so the guard must stand down."""
    status, qt, gdk = _run({"QT_QPA_PLATFORM": "wayland", "XDG_SESSION_TYPE": "wayland"})
    assert qt == "wayland"      # untouched
    assert gdk is None
    assert "no DISPLAY" in status


def test_explicit_opt_out_is_honored():
    """VAF_ALLOW_WAYLAND=1 keeps whatever the session configured (native Wayland works on
    some GPUs), so the guard must not fight the user."""
    status, qt, gdk = _run({
        "QT_QPA_PLATFORM": "wayland", "DISPLAY": ":0", ALLOW_WAYLAND_ENV: "1",
    })
    assert qt == "wayland"
    assert gdk is None
    assert "skipped" in status


def test_unset_environment_keeps_the_historical_default():
    """With nothing requested, behavior is unchanged from before the fix: xcb/x11."""
    status, qt, gdk = _run({})
    assert (qt, gdk) == ("xcb", "x11")
    assert "default" in status


def test_existing_x11_choice_is_left_alone():
    status, qt, gdk = _run({"QT_QPA_PLATFORM": "xcb", "DISPLAY": ":0"})
    assert (qt, gdk) == ("xcb", "x11")


def test_non_linux_is_a_no_op():
    """macOS and Windows must not be touched by a Linux display heuristic."""
    for platform in ("darwin", "win32"):
        status, qt, gdk = _run(
            {"QT_QPA_PLATFORM": "wayland", "DISPLAY": ":0"}, platform)
        assert qt == "wayland"   # untouched
        assert gdk is None
        assert "not linux" in status


def test_never_raises_on_a_broken_environment():
    """A display heuristic must never be able to break startup."""
    class _Hostile(dict):
        def get(self, *a, **kw):
            raise RuntimeError("boom")

    assert isinstance(force_x11(_Hostile(), "linux"), str)


def test_explicit_non_wayland_platform_is_honored(monkeypatch):
    """An operator/CI harness that exported QT_QPA_PLATFORM=offscreen inside a Wayland
    session opted out DELIBERATELY. A session-type HINT must never beat an explicit request -
    otherwise a headless run silently acquires a live-X dependency (and vaf/tray.py runs this
    at import time, so a pytest run would have its process-wide env rewritten)."""
    monkeypatch.setattr("os.path.exists", lambda p: True)
    for explicit in ("offscreen", "minimal", "eglfs", "vnc"):
        status, qt, _gdk = _run({
            "QT_QPA_PLATFORM": explicit, "XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0",
        })
        assert qt == explicit, f"{explicit} must survive (got {qt})"
    # ...and the GTK side likewise.
    _status, _qt, gdk = _run({
        "GDK_BACKEND": "broadway", "XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0",
    })
    assert gdk == "broadway"


def test_explicit_wayland_still_overridden(monkeypatch):
    """The opt-out above must not weaken the actual fix: an explicit wayland IS overridden."""
    monkeypatch.setattr("os.path.exists", lambda p: True)
    _status, qt, gdk = _run({
        "QT_QPA_PLATFORM": "wayland", "XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0",
    })
    assert (qt, gdk) == ("xcb", "x11")


def test_wayland_display_alone_is_detected(monkeypatch):
    """A compositor started by hand from a TTY (sway/Hyprland) may have no
    XDG_SESSION_TYPE, but it always sets WAYLAND_DISPLAY."""
    monkeypatch.setattr("os.path.exists", lambda p: True)
    status, qt, gdk = _run({"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"})
    assert (qt, gdk) == ("xcb", "x11")
    assert "forced" in status


def test_stands_down_when_display_is_set_but_x_socket_is_missing(monkeypatch):
    """DISPLAY being present is NOT proof X is reachable (stale value, missing auth, partial
    imported environment). Qt's xcb plugin qFatal()s on a failed connection, so forcing it
    would turn a session that at least STARTED into one that does not come up at all."""
    monkeypatch.setattr("os.path.exists", lambda p: False)   # no /tmp/.X11-unix/X0
    status, qt, gdk = _run({
        "QT_QPA_PLATFORM": "wayland", "XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0",
    })
    assert qt == "wayland"       # untouched - better a working Wayland than no window
    assert gdk is None
    assert "no reachable X socket" in status


def test_remote_display_is_assumed_reachable(monkeypatch):
    """A remote 'host:0' cannot be probed cheaply; the guard must not block on it."""
    monkeypatch.setattr("os.path.exists", lambda p: False)
    _status, qt, gdk = _run({
        "QT_QPA_PLATFORM": "wayland", "XDG_SESSION_TYPE": "wayland", "DISPLAY": "box:0",
    })
    assert (qt, gdk) == ("xcb", "x11")
