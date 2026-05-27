"""
VAF Desktop Window — pywebview wrapper.

Manages the app window: create, show, hide, navigate, destroy.
pywebview uses the system-native WebView (Edge/WebView2 on Windows,
WKWebView on macOS, WebKitGTK on Linux) — no bundled Chromium needed.

Threading rules:
  - init() and start() MUST be called from the main thread.
  - show(), hide(), navigate(), destroy() are safe to call from any thread.
"""
from __future__ import annotations

import json
import logging
import pathlib
import threading

_log = logging.getLogger(__name__)

_window = None   # pywebview Window instance
_webview = None  # pywebview module (lazy import so it can be optional)
_state_path: pathlib.Path | None = None  # path to window_state.json
_save_timer: threading.Timer | None = None  # debounce timer for state saves


def _load_state(default_w: int, default_h: int) -> tuple[int, int]:
    """Return (width, height) from saved state, or defaults if not available."""
    if _state_path and _state_path.exists():
        try:
            data = json.loads(_state_path.read_text(encoding="utf-8"))
            w = int(data.get("width", default_w))
            h = int(data.get("height", default_h))
            if 400 <= w <= 7680 and 300 <= h <= 4320:
                return w, h
        except Exception:
            pass
    return default_w, default_h


def _save_state(width: int, height: int) -> None:
    """Persist window size (debounced — only writes after 1s of no further resize)."""
    global _save_timer
    if not _state_path:
        return
    if _save_timer:
        _save_timer.cancel()
    def _write():
        try:
            _state_path.write_text(
                json.dumps({"width": width, "height": height}),
                encoding="utf-8",
            )
        except Exception as exc:
            _log.debug("[DesktopWindow] Could not save window state: %s", exc)
    _save_timer = threading.Timer(1.0, _write)
    _save_timer.daemon = True
    _save_timer.start()


def init(url: str, title: str = "VAF", width: int = 1280, height: int = 800) -> None:
    """Create the desktop window. Must be called before start()."""
    global _window, _webview
    import os, sys
    if sys.platform == "linux":
        # Force X11/XWayland for both GTK and Qt — native Wayland causes protocol errors.
        # GDK_BACKEND: affects GTK (pystray/AppIndicator)
        # QT_QPA_PLATFORM: affects Qt (pywebview/Qt WebEngine) — must match GDK to avoid
        #   GLX vs EGL conflict that causes QWebEngineProfile to qFatal() on startup.
        os.environ.setdefault("GDK_BACKEND", "x11")
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
        # Ensure system typelib path is visible to the venv's PyGObject
        _typelib = "/usr/lib64/girepository-1.0:/usr/lib/girepository-1.0"
        existing = os.environ.get("GI_TYPELIB_PATH", "")
        if _typelib not in existing:
            os.environ["GI_TYPELIB_PATH"] = f"{_typelib}:{existing}".strip(":")
        # GPU + framerate flags for Qt WebEngine (Chromium backend).
        # Must be set before QApplication is created.
        #
        # NOTE: --use-gl=desktop is intentionally omitted — it forces GLX which conflicts
        #   with EGL/Wayland and causes QWebEngineProfile to qFatal() on some drivers.
        #
        # --disable-frame-rate-limit : removes Chromium's internal 60fps cap so rAF
        #   runs at the display's actual refresh rate (e.g. 144Hz).
        # --disable-gpu-vsync        : decouples the GPU process vsync from Chromium's
        #   compositor — Qt handles display sync; double-vsync = dropped frames.
        # --enable-gpu-rasterization : GPU-based rasterization instead of CPU tiles.
        # --enable-zero-copy         : zero-copy texture upload (GPU tile → display).
        # --enable-accelerated-2d-canvas : Canvas API on GPU (CSS animations, WebGL).
        # --num-raster-threads=4     : parallel CPU fallback raster threads.
        _cf = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
        if "--disable-frame-rate-limit" not in _cf:
            _gpu_flags = (
                "--disable-frame-rate-limit "
                "--disable-gpu-vsync "
                "--enable-gpu-rasterization "
                "--enable-accelerated-2d-canvas "
                "--num-raster-threads=4"
            )
            os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = f"{_gpu_flags} {_cf}".strip()
            _log.info("[DesktopWindow] Chromium flags: %s", os.environ["QTWEBENGINE_CHROMIUM_FLAGS"])

    import webview as _wv  # ImportError propagates if pywebview not installed
    _webview = _wv

    # Load saved window size (falls back to defaults if no saved state yet)
    saved_w, saved_h = _load_state(width, height)

    _window = _wv.create_window(
        title,
        url,
        width=saved_w,
        height=saved_h,
        resizable=True,
        confirm_close=False,
        text_select=True,   # allow the user to select/copy text in the webview
    )
    _window.events.closing += _on_closing
    _window.events.loaded += _on_loaded
    _window.events.resized += _on_resized
    _log.info("[DesktopWindow] Window created → %s (size %dx%d)", url, saved_w, saved_h)


_INTERCEPT_JS = """
(function() {
    if (window.__vafLinksPatched) return;
    window.__vafLinksPatched = true;

    // Intercept window.open() — redirect localhost URLs into the same window,
    // let external URLs fall through to pywebview (opens system browser).
    var _origOpen = window.open;
    window.open = function(url, target, features) {
        if (url) {
            try {
                var u = new URL(url, window.location.href);
                if (u.hostname === 'localhost' || u.hostname === '127.0.0.1') {
                    window.location.href = url;
                    return null;
                }
            } catch(e) {}
        }
        return _origOpen.call(this, url, target, features);
    };

    // Intercept clicks on target="_blank" anchors pointing to localhost.
    document.addEventListener('click', function(e) {
        var link = e.target && e.target.closest ? e.target.closest('a[target="_blank"]') : null;
        if (!link) return;
        try {
            var u = new URL(link.href, window.location.href);
            if (u.hostname === 'localhost' || u.hostname === '127.0.0.1') {
                e.preventDefault();
                e.stopPropagation();
                window.location.href = link.href;
            }
            // External links pass through → pywebview opens them in system browser.
        } catch(e) {}
    }, true);
})();
"""


def _on_resized(width: int, height: int) -> None:
    """Persist window size whenever the user resizes it."""
    _save_state(width, height)


def _on_loaded() -> None:
    """Inject link-interception JS after every page load."""
    if _window:
        try:
            _window.evaluate_js(_INTERCEPT_JS)
        except Exception as e:
            _log.debug("[DesktopWindow] JS inject failed: %s", e)


def _on_closing() -> bool:
    """Intercept the close button: hide instead of destroying the window."""
    _log.info("[DesktopWindow] Close button → hiding window")
    if _window:
        _window.hide()
    return False  # returning False prevents the default destroy


def start(icon_path: str = None) -> None:
    """Start the pywebview GUI loop. Blocks until all windows are destroyed."""
    if not _webview:
        return

    def _apply_icon():
        """Set app icon after QApplication is ready (pywebview worker thread).
        NOTE: Only QApplication/QIcon/QWidget ops are safe here — QWebEngineProfile
        must only be accessed from the Qt main thread."""
        if not icon_path:
            return
        import time, os
        time.sleep(0.3)
        try:
            from PyQt6.QtGui import QIcon
            from PyQt6.QtWidgets import QApplication
            _app = QApplication.instance()
            if _app and os.path.exists(icon_path):
                _icon = QIcon(icon_path)
                _app.setWindowIcon(_icon)
                for _w in _app.topLevelWidgets():
                    _w.setWindowIcon(_icon)
                _log.info("[DesktopWindow] Window icon set: %s", icon_path)
        except Exception as e:
            _log.debug("[DesktopWindow] Could not set Qt icon: %s", e)

    # Determine a stable storage path for cookies / localStorage so the user
    # stays logged in across restarts.  Stored inside the VAF data dir.
    global _state_path
    _base = pathlib.Path(__file__).resolve().parents[2] / ".vaf_webview"
    _base.mkdir(parents=True, exist_ok=True)
    _state_path = _base / "window_state.json"
    _storage = str(_base)

    _log.info("[DesktopWindow] Starting GUI loop (main thread), storage=%s", _storage)
    _webview.start(
        func=_apply_icon if icon_path else None,
        debug=False,
        private_mode=False,   # persist cookies / localStorage across restarts
        storage_path=_storage,
    )


def show() -> None:
    """Show the window and bring it to the front (safe to call from any thread)."""
    if _window:
        _window.show()
        try:
            _window.move_to_front()
        except AttributeError:
            pass  # move_to_front available in pywebview >= 5


def hide() -> None:
    """Hide the window (safe to call from any thread)."""
    if _window:
        _window.hide()


def navigate(url: str) -> None:
    """Navigate the window to a new URL (safe to call from any thread after start)."""
    if _window:
        _log.info("[DesktopWindow] Navigating → %s", url)
        _window.load_url(url)


def destroy() -> None:
    """Destroy the window, which causes webview.start() to return."""
    global _window
    if _window:
        try:
            _window.destroy()
        except Exception:
            pass
        _window = None
