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
"""
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
