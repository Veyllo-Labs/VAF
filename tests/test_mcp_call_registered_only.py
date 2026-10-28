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
from vaf.core.config import Config
from vaf.core.tool_dispatch import (ToolCaller, get_account_allowlist_resolver,
                                    set_account_allowlist_resolver)
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
    # Each test states its own account allowlist and lane: a resolver another test module
    # registered, or a coder child's environment, must not decide here.
    previous_resolver = get_account_allowlist_resolver()
    set_account_allowlist_resolver(None)
    for key in ("VAF_ALLOWED_TOOLS", "VAF_AGENT_TYPE"):
        monkeypatch.delenv(key, raising=False)
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
    set_account_allowlist_resolver(previous_resolver)
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


# -- the account allowlist names the server's own tools -------------------------------------

def test_the_account_allowlist_holds_for_the_tool_mcp_call_reaches(setup):
    """A list that grants mcp_call and not mcp_stub_echo (what the Standard preset does with a
    server's delete tool) must not run that tool here; one that names it does. MUTATION: drop
    the allowlist question in `_call_registered`, and the restricted account reaches it."""
    lists = {"alice-scope": {"mcp_call"}, "bob-scope": {"mcp_call", "mcp_stub_echo"}}
    set_account_allowlist_resolver(lambda scope: lists.get(scope))
    out = _call("user", "alice-scope", server="stub", arguments={"text": "hi"})
    assert out.startswith("Error:") and "mcp_stub_echo" in out and "not enabled" in out, out
    assert _call("user", "bob-scope", server="stub", arguments={"text": "hi"}) == "echoed: hi"
    assert _call("admin", "alice-scope", server="stub", arguments={"text": "hi"}) == "echoed: hi", \
        "an admin is never restricted"


def test_a_broken_allowlist_refuses(setup):
    """A resolver that raises is no answer, and no answer is a refusal."""
    def _raise(scope):
        raise RuntimeError("auth store down")
    set_account_allowlist_resolver(_raise)
    caller_out = MCPClientTool().run(server="stub", tool_name="echo", user_scope_id="alice-scope",
                                     user_role="user")
    assert caller_out.startswith("Error:") and "could not be read" in caller_out, caller_out


def test_in_the_coders_child_the_list_that_crossed_decides(setup, monkeypatch):
    """No resolver is registered in the coder's child process; the parent's answer arrives as
    VAF_ALLOWED_TOOLS. MUTATION: ask `account_allows_tool` (the resolver only) instead of
    `caller_allows_tool`, and the child reads "no resolver" as unrestricted."""
    monkeypatch.setenv("VAF_AGENT_TYPE", "coding_agent")
    setup.write({"stub": {"command": setup.stub_cmd, "permission_level": "read"}})
    monkeypatch.setenv("VAF_ALLOWED_TOOLS", "mcp_call")
    out = _call("user", "alice-scope", server="stub", arguments={"text": "hi"})
    assert out.startswith("Error:") and "not enabled" in out, out
    monkeypatch.setenv("VAF_ALLOWED_TOOLS", "mcp_call,mcp_stub_echo")
    assert _call("user", "alice-scope", server="stub", arguments={"text": "hi"}) == "echoed: hi"


def test_the_refusal_names_only_servers_the_account_has_tools_of(setup):
    """MUTATION: drop the filter in `_refuse_unregistered`, and the hidden server is named."""
    setup.write({"stub": {"command": setup.stub_cmd}, "hidden": {"command": setup.stub_cmd + " x"}})
    set_account_allowlist_resolver(lambda scope: {"mcp_call", "mcp_stub_echo"})
    out = _call("user", "alice-scope", server_command=setup.attack)
    assert "Registered: stub." in out and "hidden" not in out, out
    assert "hidden" in _call("admin", "admin-scope", server_command=setup.attack)


# -- the kill switch, the tool name, the coder lane, the URL in a message --------------------

def test_the_kill_switch_keeps_registered_servers_off_too(setup, monkeypatch):
    """`mcp_native_tools_enabled` off turns every account's MCP tools off; mcp_call must not be
    the way back to them. MUTATION: drop the switch in `_call_registered`, and the registered
    command runs."""
    setup.write({"maker": {"command": setup.attack}})
    real_get = Config.get
    monkeypatch.setattr(Config, "get", lambda key, default=None: False
                        if key == "mcp_native_tools_enabled" else real_get(key, default))
    out = _call("admin", "admin-scope", server="maker")
    assert out.startswith("Error:") and "mcp_native_tools_enabled" in out, out
    assert not setup.marker.exists(), "a registered server started with MCP switched off"


def test_a_padded_tool_name_meets_the_same_level(setup):
    """"echo " used to miss the per-tool level of "echo" and still reach it. MUTATION: drop the
    strip in `run`."""
    setup.write({"stub": {"command": setup.stub_cmd, "tool_permissions": {"echo": "dangerous"}}})
    out = _call("user", "alice-scope", server="stub", tool_name=" echo ", arguments={"text": "hi"})
    assert out.startswith("Error:") and "confirmation" in out and "echoed" not in out, out


@pytest.mark.parametrize("level", ["write", "dangerous"])
def test_a_coding_run_reaches_only_read_level_tools(setup, monkeypatch, level):
    """The coder's tools are for reading (vaf/core/coder_tools.py). MUTATION: drop the
    coding-run check in `_call_registered`, and a write-level tool answers."""
    monkeypatch.setenv("VAF_AGENT_TYPE", "coding_agent")
    setup.write({"stub": {"command": setup.stub_cmd, "permission_level": level}})
    out = _call("admin", "admin-scope", server="stub", arguments={"text": "hi"})
    assert out.startswith("Error:") and "read-level" in out and "echoed" not in out, out
    setup.write({"stub": {"command": setup.stub_cmd, "permission_level": "read"}})
    assert _call("admin", "admin-scope", server="stub", arguments={"text": "hi"}) == "echoed: hi"


def test_a_coding_run_reaches_no_unregistered_remote_server(setup, monkeypatch):
    """A server at a URL the model chose has no level anybody set. MUTATION: drop the
    coding-run check before the server_url branch."""
    monkeypatch.setenv("VAF_AGENT_TYPE", "coding_agent")
    out = _call("admin", "admin-scope", transport="http", server_url="https://mcp.example.com/mcp")
    assert out.startswith("Error:") and "in a coding run" in out, out


def test_a_server_url_in_a_message_carries_no_secret():
    """MUTATION: show the URL as given in `call_remote`'s error, and the token is in it."""
    from vaf.core.mcp_remote import display_url, registered_egress
    assert display_url("https://bob:pw@mcp.example.com:8443/mcp?token=S3CRET#x") == \
        "https://mcp.example.com:8443/mcp"
    assert display_url("http://[::1]:9/mcp?k=v") == "http://[::1]:9/mcp"
    url = "http://127.0.0.1:9/mcp?token=S3CRET"
    out = get_mcp_client().call_remote("http", url, "echo", {}, egress=registered_egress(url))
    assert out.startswith("Error: MCP server at http://127.0.0.1:9/mcp:"), out
    assert "S3CRET" not in out, out
