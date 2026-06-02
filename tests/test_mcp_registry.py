import sys
from unittest.mock import MagicMock

sys.modules.setdefault("llama_cpp", MagicMock())

import vaf.core.mcp_registry as reg


# ── pure-python: factory + naming + manifest I/O (no subprocess) ──────────────────────────────────

def test_make_mcp_tool_builds_native_tool():
    tool_meta = {
        "name": "read_file",
        "description": "Read a file",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    }
    cls = reg.make_mcp_tool("filesystem", {"command": "x", "permission_level": "read"}, tool_meta)
    inst = cls()
    assert inst.name == "mcp_filesystem_read_file"
    assert inst.permission_level == "read"
    assert inst.side_effect_class == "irreversible"
    assert inst.parameters["properties"]["path"]["type"] == "string"
    # The LLM-facing schema must build like any native tool.
    schema = inst.get_schema()
    assert schema["function"]["name"] == "mcp_filesystem_read_file"


def test_make_mcp_tool_defaults_permission_to_write():
    cls = reg.make_mcp_tool("srv", {"command": "x"}, {"name": "do", "inputSchema": {}})
    assert cls().permission_level == "write"  # default is plan-gated but automation-safe
    # An invalid level falls back to write.
    cls2 = reg.make_mcp_tool("srv", {"command": "x", "permission_level": "bogus"}, {"name": "do"})
    assert cls2().permission_level == "write"


def test_safe_naming():
    assert reg._safe("file system") == "file_system"
    assert reg._safe("a.b/c") == "a_b_c"
    assert reg._safe("__weird__") == "weird"
    assert reg._safe("") == "x"


def test_load_manifest_missing_and_malformed(tmp_path, monkeypatch):
    missing = tmp_path / "nope.json"
    monkeypatch.setattr(reg, "get_mcp_manifest_path", lambda: missing)
    assert reg.load_mcp_manifest() == {}

    bad = tmp_path / "mcp_servers.json"
    bad.write_text("{ not json")
    monkeypatch.setattr(reg, "get_mcp_manifest_path", lambda: bad)
    assert reg.load_mcp_manifest() == {}


def test_discover_empty_manifest(monkeypatch):
    monkeypatch.setattr(reg, "load_mcp_manifest", lambda: {})
    assert reg.discover_mcp_tools() == {}
    monkeypatch.setattr(reg, "load_mcp_manifest", lambda: {"servers": {}})
    assert reg.discover_mcp_tools() == {}


# ── end-to-end via a real stub MCP server (JSON-RPC over stdio) ────────────────────────────────────

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
    "    elif method=='tools/list': r={'jsonrpc':'2.0','id':mid,'result':{'tools':[{'name':'echo','description':'Echo','inputSchema':{'type':'object','properties':{'text':{'type':'string'}}}}]}}\n"
    "    elif method=='tools/call':\n"
    "        t=((msg.get('params') or {}).get('arguments') or {}).get('text','')\n"
    "        r={'jsonrpc':'2.0','id':mid,'result':{'content':[{'type':'text','text':'echoed: '+t}]}}\n"
    "    else: r={'jsonrpc':'2.0','id':mid,'error':{'code':-32601,'message':'no'}}\n"
    "    sys.stdout.write(json.dumps(r)+'\\n'); sys.stdout.flush()\n"
)


def test_discover_via_stub_server(tmp_path, monkeypatch):
    stub = tmp_path / "stub_mcp.py"
    stub.write_text(_STUB)
    cmd = f"{sys.executable} {stub}"
    monkeypatch.setattr(reg, "load_mcp_manifest",
                        lambda: {"servers": {"stub": {"command": cmd, "enabled": True}}})

    tools = reg.discover_mcp_tools(timeout_seconds=10)
    assert "mcp_stub_echo" in tools
    inst = tools["mcp_stub_echo"]
    assert inst.run(text="hi") == "echoed: hi"


def test_discover_skips_bad_command(tmp_path, monkeypatch):
    stub = tmp_path / "stub_mcp.py"
    stub.write_text(_STUB)
    good = f"{sys.executable} {stub}"
    monkeypatch.setattr(reg, "load_mcp_manifest", lambda: {"servers": {
        "bad": {"command": "definitely_not_a_real_cmd_zzz", "enabled": True},
        "stub": {"command": good, "enabled": True},
    }})
    tools = reg.discover_mcp_tools(timeout_seconds=10)
    # The bad server is skipped; the good one still registers — discovery never raises.
    assert "mcp_stub_echo" in tools
    assert not any("bad" in k for k in tools)
