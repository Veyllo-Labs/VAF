# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The raw `mcp_call` starts only a server an administrator registered.

Its arguments are the model's. It used to start whatever `server_command` said, on the host:
measured through the real dispatcher, a regular account's headless turn created a file with a
`touch` command. Every test here goes through `ToolCaller`, the pipeline every lane shares, and
proves the effect on the file system, not only the answer's text.
"""
import json
import sys
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("llama_cpp", MagicMock())

import vaf.core.mcp_registry as reg
from vaf.core.tool_dispatch import ToolCaller
from vaf.tools.mcp_client import MCPClientTool, get_mcp_client

_STUB = (
    "import sys, json\n"
    "for line in sys.stdin:\n"
    "    line=line.strip()\n"
    "    if not line: continue\n"
    "    try: msg=json.loads(line)\n"
    "    except Exception: continue\n"
    "    mid=msg.get('id'); method=msg.get('method')\n"
    "    if mid is None: continue\n"
    "    if method=='initialize': r={'jsonrpc':'2.0','id':mid,'result':{'capabilities':{}}}\n"
    "    elif method=='tools/call':\n"
    "        t=((msg.get('params') or {}).get('arguments') or {}).get('text','')\n"
    "        r={'jsonrpc':'2.0','id':mid,'result':{'content':[{'type':'text','text':'echoed: '+t}]}}\n"
    "    else: r={'jsonrpc':'2.0','id':mid,'error':{'code':-32601,'message':'no'}}\n"
    "    sys.stdout.write(json.dumps(r)+'\\n'); sys.stdout.flush()\n"
)

# What the attack runs instead of `touch`, so the proof holds on every platform: a process that
# leaves a file behind and speaks no MCP at all.
_MAKER = "import sys\nopen(sys.argv[1], 'w').close()\n"

ACCOUNTS = [("user", "alice-scope"), ("admin", "admin-scope")]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    if " " in sys.executable or " " in str(tmp_path):
        pytest.skip("the MCP client splits a server command on spaces")
    stub = tmp_path / "stub_mcp.py"
    stub.write_text(_STUB, encoding="utf-8")
    maker = tmp_path / "maker.py"
    maker.write_text(_MAKER, encoding="utf-8")
    marker = tmp_path / "mcp_ran"
    path = tmp_path / "mcp_servers.json"
    monkeypatch.setattr(reg, "get_mcp_manifest_path", lambda: path)

    def write(servers):
        path.write_text(json.dumps({"servers": servers}), encoding="utf-8")

    stub_cmd = f"{sys.executable} {stub}"
    write({"stub": {"command": stub_cmd}})
    env = type("S", (), {"write": staticmethod(write), "stub_cmd": stub_cmd, "marker": marker,
                         "attack": f"{sys.executable} {maker} {marker}"})
    yield env
    client = get_mcp_client()
    for command in (stub_cmd, env.attack):
        proc = client._server_processes.pop(command, None)
        if proc is not None and proc.poll() is None:
            proc.kill()


def _call(role, scope, **args):
    caller = ToolCaller({"mcp_call": MCPClientTool()}, user_scope_id=scope, username="alice",
                        user_role=role, interactive=False)
    return caller.execute("mcp_call", {"tool_name": "echo", **args})


@pytest.mark.parametrize("role,scope", ACCOUNTS)
@pytest.mark.parametrize("field", ["server_command", "server"])
def test_an_unregistered_command_starts_nothing_for_any_account(setup, role, scope, field):
    """The measured attack, for a regular account and for the admin: the admin did not write
    the command either. MUTATION: let `registered_server` answer any command as a server of
    its own, and the file appears."""
    out = _call(role, scope, **{field: setup.attack})
    assert not setup.marker.exists(), f"{role}: the model's command ran on the host"
    assert out.startswith("Error:") and "never a command of its own" in out, out
    assert "Registered: stub." in out, "the refusal names what the model may call instead"


def test_a_call_without_any_server_starts_nothing(setup):
    """The stdio default with no server named is a refusal, not an empty command."""
    out = _call("user", "alice-scope")
    assert out.startswith("Error:") and "name one in `server`" in out, out


@pytest.mark.parametrize("role,scope", ACCOUNTS)
def test_a_registered_server_is_reached_by_name_and_by_its_command(setup, role, scope):
    """The registered path still works, by the server's name and, for a model that learned the
    old field, by its exact command. MUTATION: drop the name lookup in `registered_server`, and
    the call by name is refused."""
    assert _call(role, scope, server="stub", arguments={"text": "hi"}) == "echoed: hi"
    assert _call(role, scope, server_command=setup.stub_cmd,
                 arguments={"text": "again"}) == "echoed: again"


def test_a_switched_off_server_is_not_started(setup):
    """A server the administrator switched off is not one the model may start. MUTATION: drop
    the `enabled` filter in `registered_server`, and the file appears."""
    setup.write({"off": {"command": setup.attack, "enabled": False}})
    out = _call("admin", "admin-scope", server="off")
    assert not setup.marker.exists(), "a switched-off server was started"
    assert out.startswith("Error:") and "None is registered" in out, out


def test_a_tool_that_asks_every_time_stays_with_its_own_tool(setup):
    """A tool the administrator set to `dangerous` asks for a confirmation through its own
    `mcp_<server>_<tool>`; the raw lane must not be the way around that. MUTATION: drop the
    level check in `_call_registered`, and the tool answers."""
    setup.write({"stub": {"command": setup.stub_cmd, "tool_permissions": {"echo": "dangerous"}}})
    out = _call("user", "alice-scope", server="stub", arguments={"text": "hi"})
    assert out.startswith("Error:") and "mcp_stub_echo" in out and "echoed" not in out, out


def test_a_server_that_signs_every_account_in_stays_with_its_own_tools(setup):
    """Its own tools carry the caller's identity; the raw lane has none and must not fall back
    to the local admin's session. MUTATION: drop the sign-in check in `_call_registered`."""
    setup.write({"svc": {"transport": "http", "url": "https://mcp.example.com/mcp", "auth": "oauth"}})
    out = _call("user", "alice-scope", server="svc")
    assert out.startswith("Error:") and "signs every account in" in out and "mcp_svc_echo" in out, out


def test_the_model_is_offered_no_free_command():
    """The schema names a registered server, not a command line. MUTATION: put `server_command`
    back into the parameters."""
    props = MCPClientTool().parameters["properties"]
    assert "server" in props and "server_command" not in props
