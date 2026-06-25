"""In-process runtime status of the integrated HTTPS proxy.

The UI used to display a LAN URL/port computed purely from config (e.g. "https://<ip>:443"), even when
the proxy never actually bound that port (443 is privileged and fails for a non-root desktop). This
module records the REAL outcome — the effective port that was bound, or the bind error — so the UI can
show the true URL and surface failures instead of pretending success.

Shared in-process: in desktop mode the tray runs the backend (web_server.app, which serves the
network routes) and the HTTPS proxy in threads of the SAME process, so this module global is visible to
both. In CLI/headless mode (no integrated proxy) it simply stays unset and callers fall back to config.
"""
import threading
from typing import Optional

_lock = threading.Lock()
_status = {
    "configured_https_port": None,   # what config asked for (e.g. 443)
    "effective_https_port": None,    # what actually bound (e.g. 8443 after fallback)
    "bound": False,                  # is the proxy actually listening?
    "error": None,                   # last bind error, if any
}


def set_proxy_bound(effective_port: int, configured_port: int) -> None:
    """The proxy is listening on `effective_port` (configured was `configured_port`)."""
    with _lock:
        _status.update(
            effective_https_port=int(effective_port),
            configured_https_port=int(configured_port),
            bound=True,
            error=None,
        )


def set_proxy_failed(configured_port: int, error: str) -> None:
    """The proxy could not bind (neither the configured port nor the fallback)."""
    with _lock:
        _status.update(
            configured_https_port=int(configured_port) if configured_port else None,
            effective_https_port=None,
            bound=False,
            error=str(error),
        )


def reset() -> None:
    with _lock:
        _status.update(configured_https_port=None, effective_https_port=None, bound=False, error=None)


def get_proxy_status() -> dict:
    with _lock:
        return dict(_status)


def effective_https_port(default: Optional[int] = None) -> Optional[int]:
    """The port the proxy actually bound, or `default` if the proxy hasn't reported yet."""
    with _lock:
        return _status["effective_https_port"] if _status["effective_https_port"] is not None else default
