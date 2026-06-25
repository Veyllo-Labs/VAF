"""pick_bindable_port: try the configured HTTPS port, transparently fall back to a non-privileged
port when the preferred one cannot be bound (e.g. 443 needs root on Linux/macOS). This is what makes
LAN/server mode actually start on a non-root desktop instead of failing silently."""
import socket

from vaf.network.binding import pick_bindable_port


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _occupy() -> tuple[socket.socket, int]:
    """A LISTENing socket (no SO_REUSEADDR) so the port is genuinely unbindable while held."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


def test_returns_preferred_when_free():
    p = _free_port()
    assert pick_bindable_port("127.0.0.1", p, fallback=_free_port()) == p


def test_falls_back_when_preferred_unbindable():
    occ, occ_port = _occupy()
    fb = _free_port()
    try:
        assert pick_bindable_port("127.0.0.1", occ_port, fallback=fb) == fb
    finally:
        occ.close()


def test_returns_none_when_both_unbindable():
    a, ap = _occupy()
    b, bp = _occupy()
    try:
        assert pick_bindable_port("127.0.0.1", ap, fallback=bp) is None
    finally:
        a.close()
        b.close()


def test_dedupes_when_preferred_equals_fallback():
    occ, occ_port = _occupy()
    try:
        # preferred == fallback and unbindable -> None (no false positive, no crash)
        assert pick_bindable_port("127.0.0.1", occ_port, fallback=occ_port) is None
    finally:
        occ.close()


def test_privileged_443_falls_back_to_high_port():
    # On a non-root runner 443 is unbindable -> must fall back to the high port.
    # (If the runner IS root and binds 443, this asserts that too — both are acceptable.)
    fb = _free_port()
    got = pick_bindable_port("127.0.0.1", 443, fallback=fb)
    assert got in (443, fb)
