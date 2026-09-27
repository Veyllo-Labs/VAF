# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""An MCP server's secrets live in the key ring, and the stdio loop serves one caller at a time.

Measured before the change:
- `env` values (an API key a server reads, `GITHUB_TOKEN` style) sat in plaintext in
  `mcp_servers.json`, and the Settings list sent them to the admin's browser as they were.
- Saving a server from the Settings form rewrote its whole entry and dropped the keys the form
  does not know (`tool_permissions`, which exists only in the manifest).
- Every stdio call used the same JSON-RPC id and nothing serialised the shared server process:
  two chats calling the same tool at once could read each other's answer.
"""
import json
import sys
import threading

import pytest

import vaf.core.mcp_registry as reg
from vaf.core import mcp_secrets


@pytest.fixture
def manifest(tmp_path, monkeypatch):
    path = tmp_path / "mcp_servers.json"
    monkeypatch.setattr(reg, "get_mcp_manifest_path", lambda: path)

    def write(servers):
        path.write_text(json.dumps({"servers": servers}), encoding="utf-8")

    def read():
        return json.loads(path.read_text(encoding="utf-8"))

    return type("M", (), {"write": staticmethod(write), "read": staticmethod(read), "path": path})


def test_env_values_written_into_the_file_move_into_the_ring(manifest):
    manifest.write({"gh": {"command": "npx gh-server", "env": {"GITHUB_TOKEN": "ghp_secret", "HOME_DIR": "/data"}},
                    "remote": {"transport": "http", "url": "https://example.com/mcp",
                               "headers": {"Authorization": "Bearer tok_secret", "X-Team": "a"}}})
    reg.load_mcp_manifest()
    text = manifest.path.read_text(encoding="utf-8")
    assert "ghp_secret" not in text and "tok_secret" not in text and "/data" not in text
    disk = manifest.read()["servers"]
    assert disk["gh"]["env"] == {"GITHUB_TOKEN": "", "HOME_DIR": ""}, "the names stay"
    assert disk["remote"]["headers"] == {"X-Team": "a"}, "a header that is not a secret stays"
    assert mcp_secrets.server_env("gh") == {"GITHUB_TOKEN": "ghp_secret", "HOME_DIR": "/data"}
    assert mcp_secrets.server_token("remote") == "tok_secret"
    assert mcp_secrets.effective_env("gh", disk["gh"]) == {"GITHUB_TOKEN": "ghp_secret", "HOME_DIR": "/data"}


def test_a_failed_move_keeps_the_only_copy(manifest, monkeypatch):
    from vaf.core import data_keyring
    manifest.write({"gh": {"command": "x", "env": {"GITHUB_TOKEN": "ghp_secret"}}})

    def boom(name, value):
        raise RuntimeError("ring unavailable")

    monkeypatch.setattr(data_keyring, "set_data_secret", boom)
    reg.load_mcp_manifest()
    assert manifest.read()["servers"]["gh"]["env"] == {"GITHUB_TOKEN": "ghp_secret"}


@pytest.mark.parametrize("ring_works", [True, False], ids=["moved", "move-failed"])
def test_the_browser_sees_names_never_values(manifest, monkeypatch, ring_works):
    """Also while a value is still in the file because the ring refused it: the list is
    redacted on its own, not only because the move usually got there first."""
    manifest.write({"gh": {"command": "x", "env": {"GITHUB_TOKEN": "ghp_secret"}},
                    "remote": {"transport": "http", "url": "https://example.com/mcp", "token": "tok_secret"}})
    if not ring_works:
        from vaf.core import data_keyring

        def boom(name, value):
            raise RuntimeError("ring unavailable")

        monkeypatch.setattr(data_keyring, "set_data_secret", boom)
    shown = {s["name"]: s for s in reg.servers_for_display({})}
    dumped = json.dumps(shown)
    assert "ghp_secret" not in dumped and "tok_secret" not in dumped
    assert shown["gh"]["env"] == {"GITHUB_TOKEN": ""}
    assert shown["gh"]["token_set"] is False
    if ring_works:
        assert shown["remote"]["token_set"] is True
    else:
        assert "ghp_secret" in manifest.path.read_text(encoding="utf-8"), "the file kept the only copy"


def test_a_save_keeps_what_the_form_does_not_resend(manifest):
    manifest.write({"gh": {"command": "npx gh-server", "tool_permissions": {"delete_repo": "dangerous"}}})
    reg.upsert_server("gh", command="npx gh-server", env={"GITHUB_TOKEN": "ghp_secret", "LOG": "1"})
    reg.upsert_server("gh", command="npx gh-server --v2", env={"GITHUB_TOKEN": ""})   # the browser's echo
    entry = manifest.read()["servers"]["gh"]
    assert entry["tool_permissions"] == {"delete_repo": "dangerous"}, "the form used to drop it"
    assert entry["command"] == "npx gh-server --v2"
    assert mcp_secrets.server_env("gh") == {"GITHUB_TOKEN": "ghp_secret"}, "empty keeps, left out removes"

    reg.upsert_server("r", transport="http", url="https://example.com/mcp", token="tok_1")
    reg.upsert_server("r", transport="http", url="https://example.com/mcp", token="")
    assert mcp_secrets.server_token("r") == "tok_1"
    reg.upsert_server("r", transport="http", url="https://example.com/mcp", clear_token=True)
    assert mcp_secrets.server_token("r") == ""


def test_removing_a_server_removes_its_secrets(manifest):
    reg.upsert_server("r", transport="http", url="https://example.com/mcp", token="tok_1")
    reg.upsert_server("gh", command="x", env={"A": "1"})
    assert reg.remove_server("r") and reg.remove_server("gh")
    assert mcp_secrets.server_token("r") == "" and mcp_secrets.server_env("gh") == {}
    assert manifest.read()["servers"] == {}


@pytest.mark.parametrize("bad", [
    dict(name="1bad", command="x"),
    dict(name="ok", transport="stdio"),
    dict(name="ok", transport="http", url="ftp://x"),
    dict(name="ok", transport="carrier-pigeon", command="x"),
])
def test_a_bad_entry_is_refused_with_a_reason(manifest, bad):
    name = bad.pop("name")
    with pytest.raises(ValueError):
        reg.upsert_server(name, **bad)


def test_the_settings_list_is_the_frameworks():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    body = src[src.index("def _mcp_servers_payload"):src.index("def _attach_learned_states")]
    assert "servers_for_display(" in body and '"env": cfg.get("env")' not in body
    handlers = src[src.index('elif type in ("create_mcp_server", "update_mcp_server")'):src.index('elif type == "update_custom_tool_permissions"')]
    assert "upsert_server(" in handlers and "remove_server(" in handlers
    assert "save_mcp_manifest" not in handlers, "the handlers write through the framework"


# -- one caller at a time on a shared stdio server -------------------------------------------

# Answers in REVERSE order when a second request arrives while it holds the first one: what a
# busy server may do, and what exposed the shared id. With one request at a time it never
# holds two.
_REORDERING_STUB = r'''
import json, queue, sys, threading
lines = queue.Queue()
def pump():
    for line in sys.stdin:
        lines.put(line)
threading.Thread(target=pump, daemon=True).start()
def answer(msg):
    mid, method = msg.get("id"), msg.get("method")
    if mid is None:
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {"capabilities": {}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}}
    text = ((msg.get("params") or {}).get("arguments") or {}).get("text", "")
    return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "echo:" + text}]}}
while True:
    first = json.loads(lines.get())
    held = [first]
    if first.get("method") == "tools/call":
        try:
            held.append(json.loads(lines.get(timeout=0.5)))
        except queue.Empty:
            pass
    for msg in reversed(held):
        out = answer(msg)
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n"); sys.stdout.flush()
'''


def test_two_callers_on_one_stdio_server_each_get_their_own_answer(tmp_path):
    from vaf.tools.mcp_client import MCPClientTool
    stub = tmp_path / "reorder.py"
    stub.write_text(_REORDERING_STUB, encoding="utf-8")
    cmd = f"{sys.executable} {stub}"
    client = MCPClientTool()
    assert client.list_server_tools(cmd)          # warm the process
    results = {}

    def call(word):
        results[word] = client._call_stdio(cmd, "echo", {"text": word})

    threads = [threading.Thread(target=call, args=(w,), daemon=True) for w in ("alpha", "beta")]
    for th in threads:
        th.start()
    for th in threads:
        th.join(15)
    try:
        assert results == {"alpha": "echo:alpha", "beta": "echo:beta"}, results
    finally:
        for proc in client._server_processes.values():
            proc.kill()


def test_secure_status_names_a_server_whose_secret_is_still_in_the_file(manifest):
    from vaf.core import data_keyring
    manifest.write({"gh": {"command": "x", "env": {"GITHUB_TOKEN": "ghp_secret"}}, "plain": {"command": "y"}})
    assert "mcp_servers.json:gh" in data_keyring.ring_status()["legacy_in_config"]
    reg.load_mcp_manifest()                                          # moves it
    assert not [k for k in data_keyring.ring_status()["legacy_in_config"] if k.startswith("mcp_servers.json")]


def test_a_removal_the_file_did_not_take_keeps_the_secrets(manifest, monkeypatch):
    reg.upsert_server("r", transport="http", url="https://example.com/mcp", token="tok_1")
    monkeypatch.setattr(reg, "save_mcp_manifest", lambda data: False)
    assert reg.remove_server("r") is False
    assert mcp_secrets.server_token("r") == "tok_1", "the server is still configured; so is its token"


def test_removing_a_name_the_file_does_not_have_touches_nothing(manifest):
    mcp_secrets.set_server_token("ghost", "tok_ghost")
    manifest.write({})
    assert reg.remove_server("ghost") is False
    assert mcp_secrets.server_token("ghost") == "tok_ghost"


def test_a_stored_token_does_not_follow_the_server_to_another_host(manifest):
    reg.upsert_server("r", transport="http", url="https://mcp.example.com/mcp", token="tok_1")
    reg.upsert_server("r", transport="http", url="https://mcp.example.com/v2/mcp")
    assert mcp_secrets.server_token("r") == "tok_1", "a new path on the same host keeps it"
    reg.upsert_server("r", transport="http", url="https://other.example.net/mcp")
    assert mcp_secrets.server_token("r") == "", "another host gets no old token"
    reg.upsert_server("r", transport="http", url="https://third.example.org/mcp", token="tok_3")
    assert mcp_secrets.server_token("r") == "tok_3", "unless a new one comes with it"


def test_same_origin():
    assert reg.same_origin("https://a.example/mcp", "https://A.example:443/other")
    assert not reg.same_origin("https://a.example/mcp", "http://a.example/mcp")
    assert not reg.same_origin("https://a.example/mcp", "https://a.example.evil/mcp")
    assert not reg.same_origin("", "")
