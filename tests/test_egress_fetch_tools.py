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
