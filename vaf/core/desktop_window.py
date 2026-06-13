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

_log = logging.getLogger(__name__)

_window = None   # pywebview Window instance
_webview = None  # pywebview module (lazy import so it can be optional)
_state_path: pathlib.Path | None = None  # path to window_state.json
_save_timer: threading.Timer | None = None  # debounce timer for state saves
# Renderer crash auto-recovery bookkeeping (see _install_crash_recovery).
_recovery = {"reloads": 0, "last": 0.0, "hooked": set()}
# Native dialog wiring bookkeeping (see _install_download_print_handlers).
_dialogs_hooked: set = set()


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
    # Expose a native Save-As bridge to the WebUI. The workspace download relies on
    # this in the desktop window because QtWebEngine's own download path is brittle
    # (a parentless save dialog can open behind the window; downloads started after
    # an awaited fetch are blocked). The WebUI calls window.pywebview.api.save_file_as.
    try:
        _window.expose(save_file_as)
        _log.info("[DesktopWindow] native save_file_as bridge exposed")
    except Exception as e:
        _log.debug("[DesktopWindow] could not expose save bridge: %s", e)
    _log.info("[DesktopWindow] Window created → %s (size %dx%d)", url, saved_w, saved_h)


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
        _log.info("[DesktopWindow] saved %s → %s", src.name, dest)
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


def _install_crash_recovery() -> None:
    """Arm auto-recovery for QtWebEngine RENDER-process crashes (the 'QtWebEngineProcess has
    encountered a fatal error' case). When the renderer dies, `renderProcessTerminated` fires; we
    reload the view, which respawns the renderer instead of leaving a dead/blank window. A
    crash-loop guard stops after repeated crashes in a short window so we don't reload forever.

    Idempotent: only hooks each page once (re-fires of `loaded`, incl. our own recovery reload,
    are ignored)."""
    try:
        from webview.platforms.qt import BrowserView
        from PyQt6.QtWebEngineCore import QWebEnginePage
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
    which does not exist on PyQt6 — and nobody listens to printRequested at all.
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
        from PyQt6.QtWidgets import QFileDialog
    except Exception as e:                       # pragma: no cover - backend/version differences
        _log.debug("[DesktopWindow] download/print dialogs unavailable: %s", e)
        return

    def _pdf_target(page, parent) -> str:
        """Ask the user where to save the PDF; '' when cancelled."""
        import re as _re
        title = ""
        try:
            title = (page.title() or "").strip()
        except Exception:
            pass
        name = _re.sub(r"[^\w\s.-]", "", title).strip().replace(" ", "_") or "document"
        suggested = os.path.join(os.path.expanduser("~"), "Downloads", f"{name}.pdf")
        path, _filt = QFileDialog.getSaveFileName(parent, "Save as PDF", suggested, "PDF (*.pdf)")
        if path and not path.lower().endswith(".pdf"):
            path += ".pdf"
        return path

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
                _log.info("[DesktopWindow] download accepted → %s", path)

            try:
                profile.downloadRequested.connect(_on_download)
                _log.info("[DesktopWindow] download save dialog armed")
            except Exception as e:                # pragma: no cover
                _log.debug("[DesktopWindow] could not arm download handler: %s", e)

        if id(page) not in _dialogs_hooked:
            _dialogs_hooked.add(id(page))

            def _on_print(_page=page, _parent=bv):
                path = _pdf_target(_page, _parent)
                if path:
                    _page.printToPdf(path)
                    _log.info("[DesktopWindow] printing page to PDF → %s", path)

            def _on_print_frame(frame, _page=page, _parent=bv):
                path = _pdf_target(_page, _parent)
                if path:
                    frame.printToPdf(path)
                    _log.info("[DesktopWindow] printing frame to PDF → %s", path)

            try:
                page.printRequested.connect(_on_print)
                page.printRequestedByFrame.connect(_on_print_frame)
                page.pdfPrintingFinished.connect(
                    lambda p, ok: _log.info("[DesktopWindow] PDF print %s: %s", "done" if ok else "FAILED", p)
                )
                _log.info("[DesktopWindow] print-to-PDF dialogs armed")
            except Exception as e:                # pragma: no cover
                _log.debug("[DesktopWindow] could not arm print handlers: %s", e)


def _on_loaded() -> None:
    """Inject link-interception JS after every page load, and arm renderer crash recovery."""
    if _window:
        try:
            _window.evaluate_js(_INTERCEPT_JS)
        except Exception as e:
            _log.debug("[DesktopWindow] JS inject failed: %s", e)
    _install_crash_recovery()
    _install_download_print_handlers()


def _on_closing() -> bool:
    """Intercept the close button: hide instead of destroying the window."""
    _log.info("[DesktopWindow] Close button → hiding window")
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
    _log.info("[MemLog] Diagnostic memory logging → %s", log_path)


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

    # Memory-leak diagnostic logger. Currently ENABLED to investigate the renderer/GPU RAM and the
    # QtWebEngineProcess crash; it appends to logs/leak_diag_<date>.log (columns: totalRSS / gpu /
    # renderer / jsHeap / domNodes) every 2s. To turn it off, set VAF_LEAK_DIAG=0.
    import os as _os
    if _os.environ.get("VAF_LEAK_DIAG", "1") != "0":
        _start_mem_logger()

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
