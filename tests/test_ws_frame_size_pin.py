# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Every uvicorn the product starts passes the shared WebSocket frame ceiling.

run_server set 200 MB while the tray (main port AND the internal 8005 channel
the HTTPS proxy relays into) and the proxy's front listener built their own
uvicorn.Config without it - so on the desktop and LAN paths uploads died at
uvicorn's 16 MB default, mid-transfer, with nothing but the reconnect banner.
The value lives ONCE (log_helper.WS_MAX_SIZE_BYTES); this pin walks every
uvicorn.Config call and fails on any site that neither passes the constant nor
carries a named exemption comment (pattern: test_terminal_spawn_lifetime).

The same census holds every site to the redacting log_config and to the WebSocket
implementation the door names (vaf.network.door.WS_PROTOCOL).
"""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _uvicorn_config_sites():
    """(file, line, window) for every uvicorn.Config( call in the product tree."""
    sites = []
    for path in (ROOT / "vaf").rglob("*.py"):
        if path.name == "log_helper.py":
            # The definer: its docstrings MENTION uvicorn.Config, they never call it.
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        if "uvicorn.Config(" not in text:
            continue
        for m in re.finditer(r"uvicorn\.Config\(", text):
            line = text.count("\n", 0, m.start()) + 1
            window = text[m.start(): m.start() + 800]
            sites.append((path.relative_to(ROOT).as_posix(), line, window))
    return sites


def test_the_constant_is_defined_once_and_is_200mb():
    from vaf.core.log_helper import WS_MAX_SIZE_BYTES

    assert WS_MAX_SIZE_BYTES == 200 * 1024 * 1024
    # ONE definition: a second literal would be the next silent divergence.
    definers = []
    for path in (ROOT / "vaf").rglob("*.py"):
        text = path.read_bytes().decode("utf-8", errors="replace")
        if re.search(r"^WS_MAX_SIZE_BYTES\s*=", text, re.M):
            definers.append(path.name)
    assert definers == ["log_helper.py"], f"WS_MAX_SIZE_BYTES defined in {definers}"


def test_every_uvicorn_config_passes_the_shared_ceiling():
    sites = _uvicorn_config_sites()
    assert len(sites) >= 6, f"uvicorn.Config census shrank unexpectedly: {[s[:2] for s in sites]}"
    offenders = []
    for file, line, window in sites:
        if "ws_max_size=WS_MAX_SIZE_BYTES" in window:
            continue
        if "ws-exempt:" in window:
            # Named exemption: the comment must say WHY this listener may keep
            # the library default.
            continue
        offenders.append(f"{file}:{line}")
    assert not offenders, (
        f"uvicorn.Config without the shared ws_max_size: {offenders} - this lane "
        f"caps WebSocket frames at uvicorn's 16 MB default, and an upload above "
        f"~12 MB raw dies mid-transfer with only the reconnect banner. Pass "
        f"ws_max_size=WS_MAX_SIZE_BYTES (vaf.core.log_helper) or add a named "
        f"'ws-exempt: <reason>' comment."
    )


def test_attachment_gates_share_the_number():
    """Client (before base64) and server (after decode) gate at the same 100 MB;
    a drifted pair silently re-opens the dropped-socket window between them."""
    server = (ROOT / "vaf" / "core" / "web_server.py").read_bytes().decode("utf-8")
    assert "_MAX_ATTACH_BYTES = 100 * 1024 * 1024" in server
    m = re.search(r"if len\(decoded_data\) > _MAX_ATTACH_BYTES:", server)
    assert m, "the server-side attachment gate is gone"
    page = (ROOT / "web" / "app" / "page.tsx").read_bytes().decode("utf-8")
    assert "MAX_ATTACH_BYTES = 100 * 1024 * 1024" in page, \
        "the client-side attachment gate drifted or vanished"
    assert "fileTooLargeToAttach" in page, "the size refusal lost its message"


def test_every_uvicorn_config_carries_the_redacting_log_config():
    """The uvicorn loggers are process-wide. One server built without the redacting
    log_config re-runs dictConfig with uvicorn's defaults and strips the token filter from
    every other server in the process - measured: the HTTPS proxy did it, and full
    /ws?token=<jwt> lines (the desktop's live admin token among them) landed in
    tray_debug.log. MUTATION: drop log_config from the proxy's Config and this goes red."""
    tray = (ROOT / "vaf" / "tray.py").read_bytes().decode("utf-8")
    assert "_log_cfg = redacted_uvicorn_log_config()" in tray
    offenders = []
    for file, line, window in _uvicorn_config_sites():
        call = window.split("\n)", 1)[0].split("        )", 1)[0]
        if "log_config=redacted_uvicorn_log_config()" in call or "log_config=_log_cfg" in call:
            continue
        if "log-exempt:" in call:
            continue
        offenders.append(f"{file}:{line}")
    assert not offenders, (
        f"uvicorn.Config without the redacting log_config: {offenders}. Pass "
        f"log_config=redacted_uvicorn_log_config() (vaf.core.log_helper) or add a named "
        f"'log-exempt: <reason>' comment."
    )


def _config_call(file, line):
    """The census site's uvicorn.Config call, read with ast, and its source text."""
    text = (ROOT / file).read_bytes().decode("utf-8")
    for node in ast.walk(ast.parse(text)):
        func = getattr(node, "func", None)
        if (isinstance(node, ast.Call) and isinstance(func, ast.Attribute)
                and func.attr == "Config" and isinstance(func.value, ast.Name)
                and func.value.id == "uvicorn" and node.lineno == line):
            return node, ast.get_source_segment(text, node)
    raise AssertionError(f"{file}:{line}: the census found a uvicorn.Config ast does not see")


def _names_the_door_protocol(call) -> bool:
    """ws=WS_PROTOCOL as written, or the door's own options, which carry it."""
    explicit = [kw.value for kw in call.keywords if kw.arg == "ws"]
    if explicit:
        value = explicit[0]
        return (isinstance(value, ast.Name) and value.id == "WS_PROTOCOL") or (
            isinstance(value, ast.Attribute) and value.attr == "WS_PROTOCOL")
    for kw in call.keywords:
        if kw.arg is None and isinstance(kw.value, ast.Call):
            func = kw.value.func
            if (isinstance(func, ast.Attribute) and func.attr == "door_options") or (
                    isinstance(func, ast.Name) and func.id == "door_options"):
                return True
    return False


def test_every_uvicorn_config_names_the_doors_websocket_implementation():
    """uvicorn's own WebSocket default (ws="auto") moved under VAF once: from 0.50.0 to 0.52.0
    it loaded a sans-I/O protocol that never made a sender wait, and a socket to a client that
    read nothing grew the process until the out-of-memory killer ended it (measured with
    uvicorn 0.49's sans-I/O protocol, the same code). uvicorn has no upper bound in the
    manifests, so a default that moves again reaches every new install. The door names its
    implementation (vaf.network.door.WS_PROTOCOL), and so does every other listener the
    product starts: the tray's 8005 is the backend leg of every device WebSocket the HTTPS
    proxy relays. A site passes ws=WS_PROTOCOL or **door.door_options(), or carries a named
    'ws-protocol-exempt: <reason>' comment. MUTATION: drop ws=WS_PROTOCOL from the tray's
    8005 Config, or write ws="auto" there - red."""
    sites = _uvicorn_config_sites()
    assert len(sites) >= 6, f"uvicorn.Config census shrank unexpectedly: {[s[:2] for s in sites]}"
    offenders = []
    for file, line, _window in sites:
        call, source = _config_call(file, line)
        if _names_the_door_protocol(call) or "ws-protocol-exempt:" in source:
            continue
        offenders.append(f"{file}:{line}")
    assert not offenders, (
        f"uvicorn.Config without the door's WebSocket implementation: {offenders}. This "
        f"listener takes uvicorn's default, which loaded a protocol that never makes a sender "
        f"wait from 0.50.0 to 0.52.0. Pass ws=WS_PROTOCOL (vaf.network.door) or add a named "
        f"'ws-protocol-exempt: <reason>' comment."
    )
