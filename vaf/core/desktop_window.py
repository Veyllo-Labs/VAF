# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
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
import os
import pathlib
import threading

# ── Qt binding: prefer PySide6 (LGPLv3) over PyQt6 (GPLv3) ───────────────────
# qtpy — used both here and inside pywebview's Qt backend — selects the Qt binding
# from the QT_API env var. We pin PySide6 (LGPLv3) because it is the official Qt for
# Python binding and dynamically linked / user-replaceable. VAF is AGPL-3.0, so PyQt6
# (GPLv3) would now be license-compatible, but PySide6's weak copyleft avoids forcing
# GPL/AGPL onto downstream apps that merely embed VAF as a library.
# setdefault so a developer holding a commercial Qt/PyQt license can still override.
# Must run before any qtpy import; this module owns all Qt access + the window.
os.environ.setdefault("QT_API", "pyside6")

_log = logging.getLogger(__name__)

_window = None   # pywebview Window instance
_webview = None  # pywebview module (lazy import so it can be optional)
_state_path: pathlib.Path | None = None  # path to window_state.json
_save_timer: threading.Timer | None = None  # debounce timer for state saves
_last_state: dict = {}  # latest known {width,height,x,y}; merged so resize/move don't clobber each other
# Renderer crash auto-recovery bookkeeping (see _install_crash_recovery).
_recovery = {"reloads": 0, "last": 0.0, "hooked": set()}
# Native dialog wiring bookkeeping (see _install_download_print_handlers).
_dialogs_hooked: set = set()
# Offscreen PDF renderer (created on the Qt main thread, see _ensure_pdf_renderer).
_pdf_renderer = None


def _ensure_state_path() -> None:
    """Set _state_path if not already set. Must be available during init() (which runs BEFORE
    start()), otherwise the saved size/position is never read back and the window always opens at
    the default. Idempotent; safe to call from init() and start()."""
    global _state_path
    if _state_path is not None:
        return
    try:
        _base = pathlib.Path(__file__).resolve().parents[2] / ".vaf_webview"
        _base.mkdir(parents=True, exist_ok=True)
        _state_path = _base / "window_state.json"
    except Exception as exc:
        _log.debug("[DesktopWindow] Could not resolve window-state path: %s", exc)


def _load_state(default_w: int, default_h: int) -> tuple[int, int, int | None, int | None]:
    """Return (width, height, x, y) from saved state; x/y are None when no valid position is saved.
    Falls back to the given defaults for size when nothing usable is stored."""
    _ensure_state_path()
    w, h, x, y = default_w, default_h, None, None
    if _state_path and _state_path.exists():
        try:
            data = json.loads(_state_path.read_text(encoding="utf-8"))
            cw, ch = int(data.get("width", default_w)), int(data.get("height", default_h))
            if 400 <= cw <= 7680 and 300 <= ch <= 4320:
                w, h = cw, ch
            if "x" in data and "y" in data:
                cx, cy = int(data["x"]), int(data["y"])
                # Allow modest negatives so a window on a secondary/left monitor is restored.
                if -7680 <= cx <= 15360 and -4320 <= cy <= 8640:
                    x, y = cx, cy
            _last_state.update({"width": w, "height": h})
            if x is not None:
                _last_state.update({"x": x, "y": y})
        except Exception:
            pass
    return w, h, x, y


def _save_state(**fields: int) -> None:
    """Persist window size/position (debounced — writes after 1s of no further change).
    Merges into _last_state so a resize event does not wipe the saved position and vice versa."""
    global _save_timer
    _ensure_state_path()
    if not _state_path:
        return
    _last_state.update({k: v for k, v in fields.items() if v is not None})
    if _save_timer:
        _save_timer.cancel()
    snapshot = dict(_last_state)
    def _write():
        try:
            _state_path.write_text(json.dumps(snapshot), encoding="utf-8")
        except Exception as exc:
            _log.debug("[DesktopWindow] Could not save window state: %s", exc)
    _save_timer = threading.Timer(1.0, _write)
    _save_timer.daemon = True
    _save_timer.start()


def init(url: str, title: str = "VAF", width: int | None = None, height: int | None = None, html: str | None = None) -> None:
    """Create the desktop window. Must be called before start().

    Default size is 1920x1080 (overridable via the config keys `desktop_window_width` /
    `desktop_window_height`). The last size AND position are remembered across restarts
    (window_state.json); the saved values take precedence over the defaults."""
    global _window, _webview
    import os, sys
    if width is None or height is None:
        try:
            from vaf.core.config import Config
            width = width or int(Config.get("desktop_window_width", 1920) or 1920)
            height = height or int(Config.get("desktop_window_height", 1080) or 1080)
        except Exception:
            width = width or 1920
            height = height or 1080
    if sys.platform == "linux":
        # Point BOTH toolkits at X11/XWayland - native Wayland causes GTK protocol errors and
        # an EGL/GLX conflict that makes QWebEngineProfile qFatal(); GDK_BACKEND and
        # QT_QPA_PLATFORM must MATCH. Shared with vaf/tray.py (which normally runs first, at
        # process start) so the two copies cannot drift; this call is the fallback for a
        # window opened without the tray entry point. VAF_ALLOW_WAYLAND=1 opts out.
        from vaf.core.display_platform import force_x11 as _force_x11
        _log.info("[DesktopWindow] display platform: %s", _force_x11())
        # Ensure system typelib path is visible to the venv's PyGObject
        _typelib = "/usr/lib64/girepository-1.0:/usr/lib/girepository-1.0"
        existing = os.environ.get("GI_TYPELIB_PATH", "")
        if _typelib not in existing:
            os.environ["GI_TYPELIB_PATH"] = f"{_typelib}:{existing}".strip(":")
        # GPU + framerate flags for Qt WebEngine (Chromium backend).
        # Must be set before QApplication is created.
        #
        # ⚠️ ANTI-LEAK RULES OF THUMB (learned the hard way — RSS hit 7 GB):
        #   • Do NOT add --disable-frame-rate-limit or --disable-gpu-vsync. Uncapping the
        #     framerate makes the in-process GPU pile up tiles/textures — on a big high-Hz
        #     display (5120x1440 @ 240Hz here) RSS climbs ~40 MB/s with a flat JS heap.
        #     Capped (vsync on, Chromium default ~60fps) it stays bounded and self-reclaims.
        #   • Do NOT add --enable-accelerated-2d-canvas (GPU-backs <canvas> buffers).
        #   • The frontend must never run continuous *repainting* animations (animated
        #     border-radius/filter/box-shadow, or a <canvas>). See web/app/globals.css and
        #     web/components/CustomCursor.tsx. Compositor-only (transform/opacity) is safe.
        #   • To re-diagnose: uncomment _start_mem_logger() below and read logs/leak_diag.
        #
        # NOTE: --use-gl=desktop is intentionally omitted — it forces GLX which conflicts
        #   with EGL/Wayland and causes QWebEngineProfile to qFatal() on some drivers.
        #
        # --disable-frame-rate-limit : lifts Chromium's internal 60fps cap so rAF runs at
        #   the display's refresh rate (e.g. 144Hz). SAFE to keep ONLY because vsync stays
        #   ON (--disable-gpu-vsync is NOT set): vsync paces presentation to the display, so
        #   frames are bounded at ~display Hz, not the thousands/sec that this flag produced
        #   when vsync was ALSO disabled. The earlier runaway leak needed both flags off
        #   AND a leaking per-frame repaint source (the GPU canvas / animated blur / avatar
        #   border-radius morph) — all of which are now gone, so the remaining animations
        #   are compositor-only (transform/opacity) and don't allocate per frame.
        # NOTE: --disable-gpu-vsync stays REMOVED — it disabled frame pacing entirely.
        # --enable-gpu-rasterization : GPU-based rasterization instead of CPU tiles.
        # --enable-zero-copy         : zero-copy texture upload (GPU tile → display).
        # --enable-accelerated-2d-canvas : Canvas API on GPU (CSS animations, WebGL).
        # --num-raster-threads=4     : parallel CPU fallback raster threads.
        _cf = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
        if "--aggressive-cache-discard" not in _cf:
            _gpu_flags = (
                # --disable-frame-rate-limit stays OUT. Empirically it leaks on this machine
                # even with vsync ON and all per-frame repaint sources removed: on a
                # 5120x1440 @ 240Hz display, uncapping rAF lets the compositor churn ~7.3 MP
                # tiles at 240 fps and the in-process GPU piles them up — renderer RSS hit
                # 3.7 GB. Capped (Chromium default ~60fps) it stays bounded at ~1.5 GB and
                # self-reclaims. High refresh vs. no-leak is a hard trade-off on this engine.
                "--enable-gpu-rasterization "
                # NOTE: --enable-accelerated-2d-canvas was REMOVED. With the GPU running
                #   in-process (no separate gpu-process), the full-screen cursor canvas's
                #   GPU-backed buffers piled up in the RENDERER process — RSS climbed
                #   ~30 MB/s to 6+ GB while the JS heap stayed flat at 10 MB (confirmed via
                #   logs/leak_diag). The canvas itself was removed too (CustomCursor.tsx).
                "--num-raster-threads=4 "
                # RAM containment (see desktop_window memory notes):
                # --max-old-space-size : hard-cap V8 heap per renderer (1 GB — 512 risks
                #   OOM crashes given the Timeline/ReactFlow/Calendar SPA views).
                # --aggressive-cache-discard : release unused RAM caches eagerly.
                # --renderer-process-limit=1 : bundle renderers without losing the GPU
                #   process separation (safer than --single-process, which breaks GPU
                #   rasterization and crashes the whole app on a renderer fault).
                # --disk-cache-size : cap on-disk HTTP cache at 50 MB (keeps it off RAM;
                #   set via flag to avoid touching QWebEngineProfile off the Qt main thread).
                "--js-flags=--max-old-space-size=1024 "
                "--aggressive-cache-discard "
                "--renderer-process-limit=1 "
                "--disk-cache-size=52428800"
            )
            os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = f"{_gpu_flags} {_cf}".strip()
            _log.info("[DesktopWindow] Chromium flags: %s", os.environ["QTWEBENGINE_CHROMIUM_FLAGS"])

    import webview as _wv  # ImportError propagates if pywebview not installed
    _webview = _wv

    # macOS: install the WKWebView media-capture grant BEFORE the window/delegate
    # exists so WebUI voice input (getUserMedia/STT) works in the desktop window.
    if sys.platform == "darwin":
        _install_media_permissions_macos()

    # Load saved window size + position (falls back to defaults if no saved state yet).
    _ensure_state_path()
    saved_w, saved_h, saved_x, saved_y = _load_state(width, height)

    _create_kwargs = dict(
        width=saved_w,
        height=saved_h,
        resizable=True,
        confirm_close=False,
        text_select=True,   # allow the user to select/copy text in the webview
    )
    if saved_x is not None and saved_y is not None:
        _create_kwargs["x"] = saved_x
        _create_kwargs["y"] = saved_y

    # `html` (a self-contained splash string) takes precedence over `url` so the
    # window can show a loading screen immediately, before the frontend is up.
    if html is not None:
        _window = _wv.create_window(title, html=html, **_create_kwargs)
    else:
        _window = _wv.create_window(title, url, **_create_kwargs)
    _window.events.closing += _on_closing
    _window.events.loaded += _on_loaded
    _window.events.resized += _on_resized
    try:
        _window.events.moved += _on_moved   # pywebview >= 4; persist position too
    except Exception as e:
        _log.debug("[DesktopWindow] window 'moved' event unavailable: %s", e)
    # Expose a native Save-As bridge to the WebUI. The workspace download relies on
    # this in the desktop window because QtWebEngine's own download path is brittle
    # (a parentless save dialog can open behind the window; downloads started after
    # an awaited fetch are blocked). The WebUI calls window.pywebview.api.save_file_as.
    try:
        _window.expose(save_file_as, save_text_as, render_pdf)
        _log.info("[DesktopWindow] native save/print bridges exposed")
    except Exception as e:
        _log.debug("[DesktopWindow] could not expose save bridge: %s", e)
    _log.info("[DesktopWindow] Window created -> %s (size %dx%d)", "splash.html" if html is not None else url, saved_w, saved_h)


def save_file_as(src_path: str) -> dict:
    """Exposed to the WebUI (window.pywebview.api.save_file_as): copy a local file
    to a user-chosen location via a native Save dialog.

    Runs on a pywebview worker thread; create_file_dialog marshals to the Qt main
    thread internally, so the dialog is safe to call here. src_path must live under
    an allowed root (same roots the /api/file endpoint serves) — defense in depth
    so a page cannot read arbitrary files off disk."""
    import shutil
    from pathlib import Path
    try:
        if not _window or not src_path:
            return {"ok": False, "error": "unavailable"}
        src = Path(src_path).resolve()
        if not src.is_file():
            return {"ok": False, "error": "not found"}
        try:
            from vaf.core.platform import Platform
            allowed = [
                Platform.documents_dir().resolve(),
                Platform.downloads_dir().resolve(),
                Platform.data_dir().resolve(),
                Platform.get_vaf_output_dir().resolve(),
            ]
            if not any(_is_relative_to(src, root) for root in allowed):
                return {"ok": False, "error": "forbidden"}
        except Exception:
            pass  # if roots can't be resolved, fall through (local desktop, trusted UI)
        result = _window.create_file_dialog(_webview.SAVE_DIALOG, save_filename=src.name)
        dest = None
        if result:
            dest = result[0] if isinstance(result, (list, tuple)) else result
        if not dest:
            return {"ok": False, "cancelled": True}
        shutil.copyfile(str(src), str(dest))
        _log.info("[DesktopWindow] saved %s -> %s", src.name, dest)
        return {"ok": True, "path": str(dest)}
    except Exception as e:
        _log.warning("[DesktopWindow] save_file_as failed: %s", e)
        return {"ok": False, "error": str(e)}


def _is_relative_to(path, root) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _main_window_widget():
    """The QMainWindow behind the webview (dialog parent), or None."""
    try:
        from webview.platforms.qt import BrowserView
        for bv in list(getattr(BrowserView, "instances", {}).values()):
            return bv
    except Exception:
        pass
    return None


def _ensure_pdf_renderer():
    """Create the offscreen PDF renderer on the Qt main thread (idempotent).

    iframe.print() does not reliably emit printRequestedByFrame in this embedded
    QtWebEngine, so the Document Editor's Print/PDF buttons render the document
    HTML in a headless QWebEnginePage and use printToPdf — a direct path like the
    working Download bridge, with no dependency on the print signal."""
    global _pdf_renderer
    if _pdf_renderer is not None:
        return _pdf_renderer
    try:
        from qtpy.QtCore import QObject, Slot, QUrl, QTimer
        from qtpy.QtWebEngineCore import QWebEnginePage
        from qtpy.QtWidgets import QFileDialog
        from qtpy.QtGui import QDesktopServices
    except Exception as e:
        _log.debug("[DesktopWindow] PDF renderer unavailable: %s", e)
        return None

    class _PdfRenderer(QObject):
        def __init__(self):
            super().__init__()
            self._pages = []  # keep refs until each render finishes

        @Slot(str, str, str)
        def render(self, html, mode, name):
            import os as _os, re as _re, tempfile
            clean = _re.sub(r"[^\w\s.-]", "", name or "document").strip().replace(" ", "_") or "document"
            if mode == "print":
                dest = _os.path.join(tempfile.gettempdir(), f"vaf_print_{clean}.pdf")
            else:
                suggested = _os.path.join(_os.path.expanduser("~"), "Downloads", f"{clean}.pdf")
                dest, _filt = QFileDialog.getSaveFileName(_main_window_widget(), "Save as PDF", suggested, "PDF (*.pdf)")
                if not dest:
                    return
                if not dest.lower().endswith(".pdf"):
                    dest += ".pdf"
            page = QWebEnginePage(self)
            self._pages.append(page)

            def _cleanup():
                try:
                    self._pages.remove(page)
                except ValueError:
                    pass
                page.deleteLater()

            def _on_pdf(data):
                ok = False
                try:
                    raw = bytes(data) if data is not None else b""
                    if raw:
                        with open(dest, "wb") as fh:
                            fh.write(raw)
                        ok = True
                except Exception as e:
                    _log.warning("[DesktopWindow] PDF write failed: %s", e)
                if ok and mode == "print":
                    QDesktopServices.openUrl(QUrl.fromLocalFile(dest))
                _log.info("[DesktopWindow] %s -> %s (%s)", mode, dest, "ok" if ok else "FAILED")
                _cleanup()

            def _on_load(loaded_ok):
                if not loaded_ok:
                    _log.warning("[DesktopWindow] offscreen render load failed")
                    _cleanup()
                    return
                # Give layout/fonts a beat, then render.
                QTimer.singleShot(180, lambda: page.printToPdf(_on_pdf))

            page.loadFinished.connect(_on_load)
            page.setHtml(html or "", QUrl("file:///"))

    _pdf_renderer = _PdfRenderer()
    # pywebview fires the `loaded` event (and thus _ensure_pdf_renderer) on a
    # WORKER thread, so the QObject is born with the wrong thread affinity and a
    # QueuedConnection to it would never run. Push it onto the Qt main thread
    # (the QApplication's thread) so the marshaled render slot actually executes.
    try:
        from qtpy.QtWidgets import QApplication
        _app = QApplication.instance()
        if _app is not None and _pdf_renderer.thread() is not _app.thread():
            _pdf_renderer.moveToThread(_app.thread())
            _log.info("[DesktopWindow] PDF renderer moved to the Qt main thread")
    except Exception as e:
        _log.debug("[DesktopWindow] could not move PDF renderer to main thread: %s", e)
    _log.info("[DesktopWindow] offscreen PDF renderer ready")
    return _pdf_renderer


def render_pdf(html: str, name: str = "document", mode: str = "pdf") -> dict:
    """Exposed to the WebUI: render document HTML to PDF. mode 'pdf' prompts for a
    save location; mode 'print' writes a temp PDF and opens it in the system
    viewer (whose print dialog targets any printer). Marshals the Qt work to the
    main thread (this runs on a pywebview worker thread)."""
    try:
        _log.info("[DesktopWindow] render_pdf called: mode=%s name=%s html_len=%d", mode, name, len(html or ""))
        # Must already exist (created + moved to the main thread in _on_loaded);
        # never create it here — this runs on a worker thread.
        renderer = _pdf_renderer
        if renderer is None:
            return {"ok": False, "error": "renderer not ready"}
        from qtpy.QtCore import QMetaObject, Qt, Q_ARG
        QMetaObject.invokeMethod(
            renderer, "render", Qt.ConnectionType.QueuedConnection,
            Q_ARG(str, html or ""),
            Q_ARG(str, "print" if str(mode).lower() == "print" else "pdf"),
            Q_ARG(str, name or "document"),
        )
        return {"ok": True}
    except Exception as e:
        _log.warning("[DesktopWindow] render_pdf failed: %s", e)
        return {"ok": False, "error": str(e)}


def save_text_as(content: str, suggested_name: str = "document.html") -> dict:
    """Exposed to the WebUI: write edited text/HTML to a user-chosen file via a
    native Save dialog (the Document Editor's Download button)."""
    try:
        if not _window:
            return {"ok": False, "error": "unavailable"}
        result = _window.create_file_dialog(_webview.SAVE_DIALOG, save_filename=(suggested_name or "document"))
        dest = None
        if result:
            dest = result[0] if isinstance(result, (list, tuple)) else result
        if not dest:
            return {"ok": False, "cancelled": True}
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(content or "")
        _log.info("[DesktopWindow] saved edited content -> %s", dest)
        return {"ok": True, "path": str(dest)}
    except Exception as e:
        _log.warning("[DesktopWindow] save_text_as failed: %s", e)
        return {"ok": False, "error": str(e)}


_INTERCEPT_JS = """
(function() {
    if (window.__vafLinksPatched) return;
    window.__vafLinksPatched = true;

    // A raw file endpoint must NEVER replace the SPA: on failure it returns bare
    // JSON (e.g. {"detail":"Access denied"}), and the desktop window has no back
    // button - the user is stranded. Let these fall through to pywebview, which
    // routes them to the system browser / native download instead.
    function _isFileEndpoint(u) {
        return u.pathname === '/api/file' || u.pathname === '/api/download';
    }

    // Intercept window.open() — redirect localhost URLs into the same window,
    // let external URLs fall through to pywebview (opens system browser).
    var _origOpen = window.open;
    window.open = function(url, target, features) {
        if (url) {
            try {
                var u = new URL(url, window.location.href);
                if ((u.hostname === 'localhost' || u.hostname === '127.0.0.1') && !_isFileEndpoint(u)) {
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
            if ((u.hostname === 'localhost' || u.hostname === '127.0.0.1') && !_isFileEndpoint(u)) {
                e.preventDefault();
                e.stopPropagation();
                window.location.href = link.href;
            }
            // File endpoints and external links pass through → pywebview opens
            // them in the system browser / native download.
        } catch(e) {}
    }, true);
})();
"""


def _on_resized(width: int, height: int) -> None:
    """Persist window size whenever the user resizes it."""
    _save_state(width=width, height=height)


def _on_moved(x: int, y: int) -> None:
    """Persist window position whenever the user moves it."""
    _save_state(x=x, y=y)


def _install_crash_recovery() -> None:
    """Arm auto-recovery for QtWebEngine RENDER-process crashes (the 'QtWebEngineProcess has
    encountered a fatal error' case). When the renderer dies, `renderProcessTerminated` fires; we
    reload the view, which respawns the renderer instead of leaving a dead/blank window. A
    crash-loop guard stops after repeated crashes in a short window so we don't reload forever.

    Idempotent: only hooks each page once (re-fires of `loaded`, incl. our own recovery reload,
    are ignored)."""
    try:
        from webview.platforms.qt import BrowserView
        from qtpy.QtWebEngineCore import QWebEnginePage
    except Exception as e:                       # pragma: no cover - backend/version differences
        _log.debug("[DesktopWindow] crash recovery unavailable: %s", e)
        return
    Status = QWebEnginePage.RenderProcessTerminationStatus
    for bv in list(getattr(BrowserView, "instances", {}).values()):
        view = getattr(bv, "webview", None)
        page = view.page() if view is not None else None
        if page is None or id(page) in _recovery["hooked"]:
            continue
        _recovery["hooked"].add(id(page))

        def _on_terminated(status, exit_code, _view=view):
            import time as _t
            name = getattr(status, "name", str(status))
            _log.error("[DesktopWindow] render process terminated: status=%s exit=%s", name, exit_code)
            if status == Status.NormalTerminationStatus:
                return                            # clean shutdown (app closing) -> nothing to recover
            now = _t.time()
            if now - _recovery["last"] > 60:
                _recovery["reloads"] = 0          # crashes are spaced out -> reset the counter
            _recovery["reloads"] += 1
            _recovery["last"] = now
            if _recovery["reloads"] > 5:
                _log.error("[DesktopWindow] renderer crashed %d× in <60s -- NOT reloading (crash loop); "
                           "leaving the window for a manual restart", _recovery["reloads"])
                return
            _log.warning("[DesktopWindow] respawning the renderer via reload (recovery attempt %d)",
                         _recovery["reloads"])
            try:
                _view.reload()                    # reloads the current URL -> spawns a fresh renderer
            except Exception as e:                # pragma: no cover
                _log.error("[DesktopWindow] recovery reload failed: %s", e)

        try:
            page.renderProcessTerminated.connect(_on_terminated)
            _log.info("[DesktopWindow] crash recovery armed on renderer")
        except Exception as e:                    # pragma: no cover
            _log.debug("[DesktopWindow] could not arm crash recovery: %s", e)


def _install_download_print_handlers() -> None:
    """Wire native Save/Print dialogs for the embedded QtWebEngine view.

    Without this, Download / Print / Save-as-PDF clicks in the WebUI do exactly
    nothing in the desktop window: pywebview only connects its download slot when
    ALLOW_DOWNLOADS is set — and that slot calls the Qt5-only download.setPath(),
    which does not exist on Qt6 — and nobody listens to printRequested at all.
    We connect our own Qt6-correct handlers instead:
      - profile.downloadRequested        -> native save dialog + accept()
      - page.printRequested              -> save-as-PDF dialog + page.printToPdf()
        (window.print() in the main frame)
      - page.printRequestedByFrame       -> save-as-PDF dialog + frame.printToPdf()
        (window.print() inside an iframe, e.g. the research report print document
        and the Document Editor — prints the frame's content, not the app shell)

    Idempotent like _install_crash_recovery (hooks each profile/page once).
    """
    try:
        from webview.platforms.qt import BrowserView
        from qtpy.QtWidgets import QFileDialog
    except Exception as e:                       # pragma: no cover - backend/version differences
        _log.debug("[DesktopWindow] download/print dialogs unavailable: %s", e)
        return

    def _do_print(frame_or_page, parent) -> None:
        """Fallback for any window.print() that DOES emit the signal (e.g. the
        research report's in-iframe print): save the frame/page to a chosen PDF.
        The Document Editor uses the render_pdf bridge instead, because its
        iframe.print() does not reliably reach this signal."""
        suggested = os.path.join(os.path.expanduser("~"), "Downloads", "document.pdf")
        path, _filt = QFileDialog.getSaveFileName(parent, "Save as PDF", suggested, "PDF (*.pdf)")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"

        def _cb(data):
            try:
                raw = bytes(data) if data is not None else b""
                if raw:
                    with open(path, "wb") as fh:
                        fh.write(raw)
                    _log.info("[DesktopWindow] saved PDF -> %s", path)
                else:
                    _log.warning("[DesktopWindow] PDF render produced no data")
            except Exception as e:
                _log.warning("[DesktopWindow] writing PDF failed: %s", e)
        frame_or_page.printToPdf(_cb)

    for bv in list(getattr(BrowserView, "instances", {}).values()):
        view = getattr(bv, "webview", None)
        page = view.page() if view is not None else None
        if page is None:
            continue
        profile = page.profile()

        if id(profile) not in _dialogs_hooked:
            _dialogs_hooked.add(id(profile))

            def _on_download(download, parent=bv):
                # Parent the dialog to the main window: a parentless QFileDialog can
                # open BEHIND the webview window on X11 (modal but hidden), so the
                # click looks like "nothing happens".
                _log.info("[DesktopWindow] downloadRequested fired: %s", getattr(download, "downloadFileName", lambda: "?")())
                try:
                    suggested = os.path.join(download.downloadDirectory(), download.downloadFileName())
                except Exception:
                    suggested = ""
                path, _filt = QFileDialog.getSaveFileName(parent, "Save file", suggested)
                if not path:
                    download.cancel()
                    return
                download.setDownloadDirectory(os.path.dirname(path) or os.path.expanduser("~"))
                download.setDownloadFileName(os.path.basename(path))
                download.accept()
                _log.info("[DesktopWindow] download accepted -> %s", path)

            try:
                profile.downloadRequested.connect(_on_download)
                _log.info("[DesktopWindow] download save dialog armed")
            except Exception as e:                # pragma: no cover
                _log.debug("[DesktopWindow] could not arm download handler: %s", e)

        if id(page) not in _dialogs_hooked:
            _dialogs_hooked.add(id(page))

            def _on_print(_page=page, _parent=bv):
                _do_print(_page, _parent)

            def _on_print_frame(frame, _page=page, _parent=bv):
                _do_print(frame, _parent)

            try:
                page.printRequested.connect(_on_print)
                page.printRequestedByFrame.connect(_on_print_frame)
                _log.info("[DesktopWindow] print-to-PDF dialogs armed")
            except Exception as e:                # pragma: no cover
                _log.debug("[DesktopWindow] could not arm print handlers: %s", e)


_clipboard_hooked: set = set()


# Media capture (mic/STT) is granted ONLY to the local WebUI. The desktop window's main
# frame can host non-local pages (in-window GitHub OAuth; links in the HuggingFace
# model-card preview navigate wherever the publisher points them), and the OS-level TCC
# prompt fires only ONCE per host app - so an unconditional grant would hand any such
# page a silent live mic after the user's first legitimate STT use.
_LOCAL_MEDIA_HOSTS = {"127.0.0.1", "localhost", "::1"}
_WK_MEDIA_CAPTURE_MICROPHONE = 1  # WKMediaCaptureType: 0=Camera, 1=Microphone, 2=CameraAndMicrophone


def _media_capture_decision(hosts, media_type) -> bool:
    """PURE decision (unit-testable off-macOS): True = grant media capture.

    Grants ONLY microphone capture requested by the local WebUI. `hosts` holds every
    security-origin host readable for the request (requesting frame AND top page -
    Apple's docs are ambiguous about which one the delegate's origin parameter
    carries, so ALL of them must be local). Camera is denied: the host bundle only
    declares NSMicrophoneUsageDescription, and touching a TCC-protected resource
    without its usage description gets the process killed by macOS.
    """
    return bool(hosts) and set(hosts) <= _LOCAL_MEDIA_HOSTS and media_type == _WK_MEDIA_CAPTURE_MICROPHONE


def _install_media_permissions_macos() -> None:
    """Grant WebKit microphone capture for the local WebUI (macOS) so STT works.

    pywebview (<= 6.2.x) does not implement the WKUIDelegate method
    webView:requestMediaCapturePermissionForOrigin:initiatedByFrame:type:decisionHandler:,
    so navigator.mediaDevices.getUserMedia() in the WebUI hung forever ("pending")
    and voice input was dead in the desktop window. We add the method to pywebview's
    BrowserDelegate at runtime; the decision itself is _media_capture_decision (local
    origins + microphone only, deny otherwise). The OS-level TCC prompt ("Python
    wants to access the microphone") still protects the user on first use - and note
    that this TCC grant attaches to the SHARED (Homebrew) Python.app, so other
    scripts run with the same interpreter inherit it.

    WebKit only exposes navigator.mediaDevices at all when the HOST bundle has
    NSMicrophoneUsageDescription. The host of this window is the framework
    Python.app, patched by scripts/macos_mic_plist.sh (called from install.sh and
    setup_mac.sh). A brew upgrade of python@X.Y replaces the bundle and removes the
    patch until that script runs again (symptom returns: "Microphone access is not
    supported by this browser") - the startup log below makes that state visible.
    """
    try:
        import objc
        import WebKit  # noqa: F401 - loads WKUIDelegate protocol metadata (block signatures)
        from webview.platforms.cocoa import BrowserView

        grant = getattr(WebKit, "WKPermissionDecisionGrant", 1)
        deny = getattr(WebKit, "WKPermissionDecisionDeny", 2)

        def webView_requestMediaCapturePermissionForOrigin_initiatedByFrame_type_decisionHandler_(
            self, webview_obj, origin, frame, media_type, decision_handler
        ):
            hosts = []
            try:
                if origin is not None and origin.host():
                    hosts.append(str(origin.host()))
                if frame is not None and frame.securityOrigin() is not None and frame.securityOrigin().host():
                    hosts.append(str(frame.securityOrigin().host()))
            except Exception:
                hosts = []  # unreadable origin -> fail closed
            decision_handler(grant if _media_capture_decision(hosts, media_type) else deny)

        _m = objc.typedSelector(b"v@:@@@q@?")(
            webView_requestMediaCapturePermissionForOrigin_initiatedByFrame_type_decisionHandler_
        )
        objc.classAddMethods(BrowserView.BrowserDelegate, [_m])

        # Make the plist half of the fix visible: without NSMicrophoneUsageDescription
        # in the host bundle the mediaDevices API is not exposed at all, and a brew
        # python upgrade silently reverts the plist patch.
        try:
            from Foundation import NSBundle
            if NSBundle.mainBundle().objectForInfoDictionaryKey_("NSMicrophoneUsageDescription"):
                _log.info("[DesktopWindow] WKWebView media-capture grant installed (mic/STT, local origins only)")
            else:
                _log.warning("[DesktopWindow] media-capture grant installed, but the host bundle lacks NSMicrophoneUsageDescription - run scripts/macos_mic_plist.sh (a brew python upgrade reverts it)")
        except Exception:
            _log.info("[DesktopWindow] WKWebView media-capture grant installed (mic/STT, local origins only)")
    except Exception as e:  # pragma: no cover - pyobjc/pywebview version differences
        _log.warning("[DesktopWindow] media-capture grant unavailable: %s", e)


def _install_clipboard_permissions() -> None:
    """Make the WebUI's copy buttons work AND stop the pywebview feature-permission crash.

    1) QtWebEngine gates JS clipboard access behind JavascriptCanAccessClipboard (default OFF), so
       navigator.clipboard.writeText() in the WebUI silently did nothing — the copy buttons (e.g. the LAN
       access URL) appeared dead. We enable it (+ JavascriptCanPaste) so copy works.
    2) pywebview's onFeaturePermissionRequested calls setFeaturePermission(url, feature, <int 1/2>), but
       Qt6 (PySide6) requires the QWebEnginePage.PermissionPolicy ENUM, not an int — so every non-media
       permission request raised `TypeError: argument 3 has unexpected type 'int'` in the terminal. We
       replace that slot with an enum-correct one (grant media like pywebview intended, deny the rest).
    Idempotent per page."""
    try:
        from webview.platforms.qt import BrowserView
        from qtpy.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
    except Exception as e:                          # pragma: no cover - backend/version differences
        # This hook is Qt-only (Linux). macOS/WKWebView is handled separately:
        # _install_media_permissions_macos() (called from init()) adds the Cocoa
        # WKUIDelegate requestMediaCapturePermission handler via pyobjc - tested
        # on-device 2026-07-03 (getUserMedia returns a live audio track). Also
        # requires NSMicrophoneUsageDescription in the host Python.app Info.plist
        # (patched by scripts/macos_mic_plist.sh; a brew python upgrade reverts it).
        _log.debug("[DesktopWindow] clipboard/permission hook unavailable: %s", e)
        return
    Attr = QWebEngineSettings.WebAttribute
    Pol = QWebEnginePage.PermissionPolicy
    Feat = QWebEnginePage.Feature
    _media = (Feat.MediaAudioCapture, Feat.MediaVideoCapture, Feat.MediaAudioVideoCapture)
    for bv in list(getattr(BrowserView, "instances", {}).values()):
        view = getattr(bv, "webview", None)
        page = view.page() if view is not None else None
        if page is None:
            continue
        try:
            s = page.settings()
            s.setAttribute(Attr.JavascriptCanAccessClipboard, True)
            s.setAttribute(Attr.JavascriptCanPaste, True)
        except Exception as e:                      # pragma: no cover
            _log.debug("[DesktopWindow] enabling clipboard access failed: %s", e)
        if id(page) in _clipboard_hooked:
            continue
        _clipboard_hooked.add(id(page))

        def _on_feature(url, feature, _page=page):
            try:
                # Media capture only for the local WebUI: the main frame can host
                # non-local pages (in-window OAuth, model-card links), which must
                # never inherit mic/camera. Mirrors the macOS WKUIDelegate policy.
                _local = str(url.host()) in _LOCAL_MEDIA_HOSTS
                policy = Pol.PermissionGrantedByUser if (feature in _media and _local) else Pol.PermissionDeniedByUser
                _page.setFeaturePermission(url, feature, policy)
            except Exception as e:                  # pragma: no cover
                _log.debug("[DesktopWindow] feature permission handler error: %s", e)

        try:
            page.featurePermissionRequested.disconnect()   # drop pywebview's int-passing (crashing) slot
        except Exception:
            pass
        try:
            page.featurePermissionRequested.connect(_on_feature)
            _log.info("[DesktopWindow] clipboard access enabled + feature-permission handler fixed")
        except Exception as e:                      # pragma: no cover
            _log.debug("[DesktopWindow] could not install feature-permission handler: %s", e)


def _on_loaded() -> None:
    """Inject link-interception JS after every page load, and arm renderer crash recovery."""
    if _window:
        try:
            _window.evaluate_js(_INTERCEPT_JS)
        except Exception as e:
            _log.debug("[DesktopWindow] JS inject failed: %s", e)
    _install_crash_recovery()
    _install_download_print_handlers()
    _install_clipboard_permissions()
    # Create the offscreen PDF renderer here so it lives on the Qt main thread
    # (render_pdf is invoked from a pywebview worker thread and only marshals to it).
    _ensure_pdf_renderer()


def _on_closing() -> bool:
    """Intercept the close button: hide instead of destroying the window."""
    _log.info("[DesktopWindow] Close button -> hiding window")
    if _window:
        _window.hide()
    return False  # returning False prevents the default destroy


# ── Memory-leak diagnostic logger (opt-in, DISABLED by default) ───────────────
# Kept on purpose: re-enable by uncommenting the _start_mem_logger() call in start().
# Logs a correlated timeline to logs/leak_diag_<date>.log so we can tell WHERE the
# RAM goes: QtWebEngine GPU process vs renderer process (OS RSS, via psutil) against
# the in-page JS heap and DOM-node count (via performance.memory / evaluate_js).
#   - gpu_MB climbs, jsUsedMB flat   → compositor / canvas / image GPU leak (not JS)
#   - renderer_MB climbs, jsHeap flat→ in-process-GPU tile/texture leak from continuous
#                                       repaints (animated border-radius/filter/box-shadow,
#                                       <canvas>, or an uncapped framerate) — the May-2026 case
#   - renderer_MB + domNodes climb   → detached-DOM leak in the page
#   - jsUsedMB climbs                → JS heap leak (arrays / closures / listeners)
_MEMLOG_JS = """(function(){
  try {
    var m = (window.performance && performance.memory) ? performance.memory : {};
    return JSON.stringify({
      jsUsedMB: Math.round((m.usedJSHeapSize||0)/1048576),
      jsTotalMB: Math.round((m.totalJSHeapSize||0)/1048576),
      dom: document.getElementsByTagName('*').length,
      imgs: document.images.length,
      canvas: document.getElementsByTagName('canvas').length,
      listeners: 0
    });
  } catch(e){ return JSON.stringify({error:String(e)}); }
})()"""


def _start_mem_logger(interval: float = 2.0) -> None:
    """Spawn a daemon thread that appends a memory timeline to a log file."""
    import datetime
    try:
        import psutil
    except Exception:
        _log.warning("[MemLog] psutil unavailable — diagnostic logging disabled")
        return

    log_path = pathlib.Path(__file__).resolve().parents[2] / "logs" / \
        f"leak_diag_{datetime.date.today().isoformat()}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)   # ensure logs/ exists, else open() fails silently
    proc = psutil.Process()

    def _loop():
        # Wait until the page has actually loaded before probing JS.
        while _window is None:
            __import__("time").sleep(0.5)
        try:
            f = log_path.open("a", encoding="utf-8")
        except Exception as exc:
            _log.warning("[MemLog] cannot open %s: %s", log_path, exc)
            return
        with f:
            f.write(f"\n=== leak_diag start {datetime.datetime.now().isoformat()} "
                    f"(interval={interval}s) ===\n")
            f.write("time\ttotalRSS_MB\tgpu_MB\trenderer_MB\tother_MB\t"
                    "jsUsedMB\tjsTotalMB\tdomNodes\timgs\tcanvas\n")
            f.flush()
            while _window is not None:
                total = gpu = rend = other = 0
                try:
                    for c in proc.children(recursive=True):
                        try:
                            rss = c.memory_info().rss
                            name = c.name()
                            try:
                                cmd = " ".join(c.cmdline())
                            except Exception:
                                cmd = ""
                        except Exception:
                            continue
                        total += rss
                        if "QtWebEngine" in name or "QtWebEngine" in cmd:
                            if "--type=gpu-process" in cmd:
                                gpu += rss
                            elif "--type=renderer" in cmd:
                                rend += rss
                            else:
                                other += rss
                        else:
                            other += rss
                except Exception:
                    pass

                js: dict = {}
                try:
                    raw = _window.evaluate_js(_MEMLOG_JS) if _window else None
                    if raw:
                        import json as _json
                        js = _json.loads(raw)
                except Exception as exc:
                    js = {"error": str(exc)}

                mb = lambda b: round(b / 1048576)
                row = [
                    datetime.datetime.now().strftime("%H:%M:%S"),
                    mb(total), mb(gpu), mb(rend), mb(other),
                    js.get("jsUsedMB", "-"), js.get("jsTotalMB", "-"),
                    js.get("dom", "-"), js.get("imgs", "-"), js.get("canvas", "-"),
                ]
                f.write("\t".join(str(x) for x in row) + "\n")
                f.flush()
                __import__("time").sleep(interval)

    import threading as _th
    _th.Thread(target=_loop, daemon=True, name="vaf-memlog").start()
    _log.info("[MemLog] Diagnostic memory logging -> %s", log_path)


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
            from qtpy.QtGui import QIcon
            from qtpy.QtWidgets import QApplication
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
    # _ensure_state_path() already ran in init(); this keeps storage co-located with it.
    _ensure_state_path()
    _base = (_state_path.parent if _state_path
             else pathlib.Path(__file__).resolve().parents[2] / ".vaf_webview")
    _base.mkdir(parents=True, exist_ok=True)
    _storage = str(_base)

    # Memory-leak diagnostic logger. Currently ENABLED to investigate the renderer/GPU RAM and the
    # QtWebEngineProcess crash; it appends to logs/leak_diag_<date>.log (columns: totalRSS / gpu /
    # renderer / jsHeap / domNodes) every 2s. To turn it off, set VAF_LEAK_DIAG=0.
    import os as _os
    if _os.environ.get("VAF_LEAK_DIAG", "1") != "0":
        _start_mem_logger()

    _log.info("[DesktopWindow] Starting GUI loop (main thread), storage=%s", _storage)
    # pywebview's cross-platform window/taskbar icon. The Qt _apply_icon above only runs
    # on the Qt backend (Linux); on Windows pywebview uses the EdgeChromium backend, where
    # the window icon must come from start(icon=...). Fall back gracefully if the installed
    # pywebview is too old to accept the kwarg.
    _start_kwargs = dict(
        func=_apply_icon if icon_path else None,
        debug=False,
        private_mode=False,   # persist cookies / localStorage across restarts
        storage_path=_storage,
    )
    if icon_path:
        try:
            _webview.start(icon=icon_path, **_start_kwargs)
            return
        except TypeError:
            _log.debug("[DesktopWindow] pywebview start() has no 'icon' kwarg; using fallback")
    _webview.start(**_start_kwargs)


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
        _log.info("[DesktopWindow] Navigating -> %s", url)
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
