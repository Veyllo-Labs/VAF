# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Every fetch tool goes through the destination guard, end to end.

The measured hole: a fetch of http://127.0.0.1:8005/api/contacts returned the owner's
contacts, because a tokenless request from this machine is the owner. Each test hands a
converted tool a URL on this machine, with NO seam on the guard: the real egress session
must refuse it before any connection. MUTATION for each: put that tool's raw
requests.get / urlopen back, and the request goes out (here: to a closed port, so the
answer is a connection error instead of the guard's refusal).
"""
import pytest

import vaf.core.session as session_mod

LOOPBACK = "http://127.0.0.1:9/api/contacts"   # port 9 (discard): nothing listens there
REFUSAL = "Only internet addresses are fetched"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(var, raising=False)
    import vaf.core.security_events as se
    events = []
    monkeypatch.setattr(se, "log_security_event", lambda kind, **kw: events.append((kind, kw)))
    return events


def test_webfetch_refuses_this_machine(monkeypatch, _quiet):
    from vaf.tools.webfetch import WebFetchTool
    monkeypatch.setattr("vaf.tools.webfetch.MIN_DELAY", 0)
    monkeypatch.setattr(WebFetchTool, "_get_cached_data", lambda self, *a, **k: None)
    monkeypatch.setattr(WebFetchTool, "_save_to_cache", lambda self, *a, **k: None)
    out = WebFetchTool().run(url=LOOPBACK, username="alice")
    assert REFUSAL in out
    assert _quiet and _quiet[0][0] == "egress_blocked" and _quiet[0][1]["username"] == "alice"


def test_download_file_refuses_this_machine_and_writes_nothing(monkeypatch, tmp_path):
    from vaf.tools.download_file import DownloadFileTool
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(session_mod, "get_session_workspace_dir",
                        lambda sid, create=False, **kw: ws if sid == "chat1" else None)
    import vaf.core.subagent_ipc as ipc
    monkeypatch.setattr(ipc, "get_current_session_id", lambda: "chat1")
    out = DownloadFileTool().run(url=LOOPBACK, save_to="x.bin", timeout=5)
    assert REFUSAL in out and "Nothing was saved" in out
    assert list(ws.iterdir()) == []


def test_download_file_refuses_other_schemes(monkeypatch, tmp_path):
    """The tool's own scheme check went; the guard's answers instead."""
    from vaf.tools.download_file import DownloadFileTool
    import vaf.core.subagent_ipc as ipc
    monkeypatch.setattr(ipc, "get_current_session_id", lambda: None)
    out = DownloadFileTool().run(url="ftp://example.org/x.bin", save_to=str(tmp_path / "x.bin"))
    assert "only http and https" in out


def test_a_github_download_url_is_judged_too():
    """The URL comes from the server's answer; an Enterprise server is the person's setting."""
    from vaf.network.egress import EgressRefused
    from vaf.tools.github_tools import _download_raw
    with pytest.raises(EgressRefused):
        _download_raw(LOOPBACK, "alice")


def test_the_webfetch_cache_belongs_to_one_account(monkeypatch, tmp_path):
    """One folder keyed on the URL alone served a page one account fetched to every other
    account asking for the same URL. MUTATION: key the cache on the URL alone again."""
    from vaf.core.config import Config
    from vaf.tools.webfetch import WebFetchTool
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)
    tool = WebFetchTool()
    tool._save_to_cache("https://example.org/a", "alice's page", "text/html", "scope-alice")
    assert tool._get_cached_data("https://example.org/a", 3600, "scope-alice")["content"] == "alice's page"
    assert tool._get_cached_data("https://example.org/a", 3600, "scope-bob") is None


def test_without_an_account_a_shared_instance_caches_nothing(monkeypatch, tmp_path):
    """The rule web_search already had (vaf.tools.search.web_cache_scope), now shared."""
    from vaf.core.config import Config
    from vaf.tools.search import web_cache_scope
    from vaf.tools.webfetch import WebFetchTool
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)
    monkeypatch.setattr(Config, "get", classmethod(
        lambda cls, k, d=None: True if k == "local_network_enabled" else d))
    assert web_cache_scope("") == ""
    tool = WebFetchTool()
    tool._save_to_cache("https://example.org/a", "page", "text/html", web_cache_scope(""))
    assert not list((tmp_path / "tmp" / "webfetch_cache").glob("*.json"))
    assert web_cache_scope(" scope-x ") == "scope-x"


def test_the_shared_page_reader_skips_a_result_on_this_machine(_quiet):
    """web_search, the research agent and the coder's deep search read result pages through
    one reader. A search result is a URL a stranger chose; one on this machine is skipped,
    not read. MUTATION: give fetch_page_text a raw requests.get."""
    from vaf.tools.search import fetch_page_text
    assert fetch_page_text(LOOPBACK, timeout=2) is None
    assert _quiet and _quiet[0][0] == "egress_blocked"


def test_the_coders_web_fetch_reports_the_refusal():
    """The coder's web_fetch: the model's URL, and the refusal is what it reads back."""
    from vaf.network.egress import EgressRefused
    from vaf.tools.search import fetch_page_html
    with pytest.raises(EgressRefused, match="Only internet addresses"):
        fetch_page_html(LOOPBACK, timeout=2)


def test_the_page_reader_still_reads_a_page(monkeypatch):
    """The conversion kept the reader's job: scripts out, tags out, whitespace folded, cut."""
    import vaf.network.egress as egress
    from vaf.tools.search import fetch_page_html, fetch_page_text

    class _R:
        status_code = 200
        text = ("<html><script>evil()</script><body><h1>Title</h1>\n\n<p>Body  text</p>"
                "<div class='x'>one</div><div class='x'>two</div></body></html>")

        def raise_for_status(self):
            pass

    class _S:
        def get(self, url, **kw):
            return _R()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(egress, "egress_session", lambda *a, **k: _S())
    assert fetch_page_text("https://example.org/", limit=12) == "Title Body t"
    assert fetch_page_html("https://example.org/", selector="div.x") == (
        '<div class="x">one</div>\n<div class="x">two</div>')


def test_registering_a_server_at_the_metadata_service_is_refused_at_once(monkeypatch):
    """A registered server's own host may be on this machine or the LAN, never an address
    that is never fetched. Said when it is saved, not at the first call. MUTATION: drop the
    save-time check in upsert_server."""
    import socket

    import vaf.core.mcp_registry as reg
    from vaf.network import binding
    real = socket.getaddrinfo

    def fake(host, port, *a, **k):
        if host == "meta.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("169.254.169.254", port or 0))]
        return real(host, port, *a, **k)

    monkeypatch.setattr(binding.socket, "getaddrinfo", fake)
    monkeypatch.setattr(reg, "load_mcp_manifest", lambda: {}, raising=False)
    with pytest.raises(ValueError, match="forbidden"):
        reg.upsert_server("meta", transport="http", url="http://meta.example/mcp")
