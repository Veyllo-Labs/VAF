# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""webfetch reads a page FOR a question; download_file saves a file into the person's own files.

Two gaps, measured in a long build session: a page was read whole into the conversation to find
one version number (and cut off by the result cap before it got there), and a jar, an archive or
a texture pack could only be downloaded with a shell command.

- `prompt`: the page goes to a model call of its own - the reader web_search already used per
  result page (`search.answer_from_page`, extracted from it) - and only the answer comes back.
- `download_file`: the URL is streamed into a file, a relative path meaning the chat's workspace,
  under the WRITE jail the tool declares (`file_access = "write"`), with a size cap that never
  leaves a partial file. A tool of its own, so reading a page stays a read, and a thinking run,
  which must not create files, is not offered it.

MUTATION: return the page instead of `_answer(...)` and the prompt test goes red; drop
`file_access = "write"` and the jail test goes red; drop the `.part` cleanup and the cap test goes
red; take download_file off the thinking run's exclusion and the registration test goes red.
"""
import pytest

import vaf.core.session as session_mod
import vaf.tools.filesystem as fs
from vaf.tools.download_file import MAX_DOWNLOAD_BYTES, DownloadFileTool
from vaf.tools.webfetch import WebFetchTool

PAGE = """<html><head><title>Paper downloads</title></head><body><main>
<p>Paper is a high performance fork of the Spigot Minecraft server. It is widely used.</p>
<p>Latest stable build: 26.2 build 84, released 2026-09-20. Older builds are listed below.</p>
<p>26.1.2 build 51, released 2026-08-02. This page has a lot of other text around it.</p>
</main></body></html>"""


class _Res:
    def __init__(self, body: bytes, status=200, headers=None):
        self.body, self.status_code = body, status
        self.headers = headers or {"Content-Type": "text/html"}
        self.text = body.decode("utf-8", errors="replace")

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.body), max(1, chunk_size)):
            yield self.body[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def web(monkeypatch, tmp_path):
    import requests
    served = {}
    monkeypatch.setattr(requests, "get", lambda url, **kw: served[url])
    monkeypatch.setattr("vaf.tools.webfetch.MIN_DELAY", 0)
    monkeypatch.setattr(WebFetchTool, "_get_cached_data", lambda self, url, ttl: None)
    monkeypatch.setattr(WebFetchTool, "_save_to_cache", lambda self, *a: None)
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(session_mod, "get_session_workspace_dir",
                        lambda sid, create=False, **kw: ws if sid == "chat1" else None)
    import vaf.core.subagent_ipc as ipc
    monkeypatch.setattr(ipc, "get_current_session_id", lambda: "chat1")
    import vaf.core.web_interface as wi
    monkeypatch.setattr(wi, "notify_file_created", lambda *a, **k: None)
    return served, ws


def test_a_prompt_returns_the_answer_not_the_page(web):
    served, _ = web
    served["https://papermc.io/downloads"] = _Res(PAGE.encode())
    tool = WebFetchTool()
    seen = {}

    def fake_llm(messages, **kw):
        seen["prompt"] = messages[-1]["content"]
        seen["max_tokens"] = kw.get("max_tokens")
        return "26.2 build 84 (2026-09-20)"

    tool.query_llm = fake_llm
    out = tool.run(url="https://papermc.io/downloads", prompt="Which is the latest stable build?")
    assert "26.2 build 84 (2026-09-20)" in out
    assert "high performance fork" not in out, "the page stays out of the conversation"
    assert "Latest stable build: 26.2 build 84" in seen["prompt"], "the reader saw the page"
    assert seen["max_tokens"] > 600, "an extraction may be longer than a search snippet"


def test_no_answer_falls_back_to_the_page(web):
    served, _ = web
    served["https://papermc.io/downloads"] = _Res(PAGE.encode())
    tool = WebFetchTool()
    tool.query_llm = lambda messages, **kw: None
    out = tool.run(url="https://papermc.io/downloads", prompt="latest build?")
    assert "could not be read for your prompt" in out and "Latest stable build" in out


def test_save_to_downloads_into_the_chats_workspace(web):
    served, ws = web
    blob = bytes(range(256)) * 40
    served["https://example.org/files/pack.zip"] = _Res(blob, headers={"Content-Type": "application/zip"})
    out = DownloadFileTool().run(url="https://example.org/files/pack.zip", save_to="downloads/")
    target = ws / "downloads" / "pack.zip"
    assert target.read_bytes() == blob
    assert f"Saved {len(blob)} bytes to {target}" in out


def test_save_to_obeys_the_write_jail(web, monkeypatch, tmp_path):
    served, ws = web
    served["https://example.org/a.bin"] = _Res(b"x" * 100)
    monkeypatch.setattr(fs, "compute_user_jail", lambda scope, role, mode="write": {
        "is_admin": False, "uid8": "ab12cd34", "allowed_roots": [ws]})
    outside = tmp_path / "elsewhere" / "a.bin"
    out = DownloadFileTool().run(url="https://example.org/a.bin", save_to=str(outside),
                             user_scope_id="ab12cd34-0000", user_role="user")
    assert "denied" in out.lower() and not outside.exists()
    ok = DownloadFileTool().run(url="https://example.org/a.bin", save_to="a.bin",
                            user_scope_id="ab12cd34-0000", user_role="user")
    assert ok.startswith("Saved 100 bytes") and (ws / "a.bin").exists()


def test_the_size_cap_never_leaves_a_partial_file(web, monkeypatch):
    served, ws = web
    served["https://example.org/declared.iso"] = _Res(
        b"x", headers={"Content-Length": str(MAX_DOWNLOAD_BYTES + 1)})
    out = DownloadFileTool().run(url="https://example.org/declared.iso", save_to="declared.iso")
    assert "nothing was saved" in out and not (ws / "declared.iso").exists()
    monkeypatch.setattr("vaf.tools.download_file.MAX_DOWNLOAD_BYTES", 1000)
    served["https://example.org/grows.bin"] = _Res(b"y" * 5000)
    out = DownloadFileTool().run(url="https://example.org/grows.bin", save_to="grows.bin")
    assert "nothing was saved" in out
    assert not (ws / "grows.bin").exists() and not (ws / "grows.bin.part").exists()


def test_web_search_reads_its_pages_with_the_same_reader():
    import inspect
    import vaf.tools.search as search
    src = inspect.getsource(search)
    assert "answer = answer_from_page(self, user_question, page_title, page_content, page_url)" in src
    assert src.count("CRITICAL INSTRUCTIONS:") == 1, "one copy of the reader's prompt"


def test_a_thinking_run_is_not_offered_the_download(monkeypatch, tmp_path):
    from vaf.core.agent import Agent
    from vaf.core.platform import Platform
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    cfg = {"provider": "openai", "api_key_openai": "sk-test"}
    assert "download_file" in Agent(register_signals=False, run_kind="chat", config_overrides=cfg).tools
    thinking = Agent(register_signals=False, run_kind="thinking", config_overrides=cfg).tools
    assert "download_file" not in thinking and "webfetch" in thinking
