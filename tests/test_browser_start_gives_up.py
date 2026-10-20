# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The browser window stops spinning when its start is never answered.

The globe marks the `browser_interactive_start` it sent as status 'starting', and only a
reply, a closed socket or a chat switch used to clear it: a server that never answered left
the spinner up for good. The client now gives up after BROWSER_START_TIMEOUT_MS, a value
that has to outlast the server's own bounded waits on one start, which are read here from
the server code so the two cannot drift apart."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")


def _toggle_browser_window() -> str:
    start = PAGE.index("const toggleBrowserWindow = () => {")
    return PAGE[start:PAGE.index("\n    };\n", start)]


def _default(path: str, pattern: str) -> float:
    """A default read off the server's source. Not off the live objects: the suite's own
    fixtures replace these functions so no test ever waits on a container."""
    m = re.search(pattern, (ROOT / path).read_text(encoding="utf-8"))
    assert m, f"{pattern!r} moved in {path}; re-point this guard"
    return float(m.group(1))


def _server_floor_seconds() -> float:
    """The longest chain of the server's own deadlines before it answers one start."""
    num = r"(\d+(?:\.\d+)?)"
    cdp_s = _default("vaf/core/browser_interactive.py",
                     r'os\.environ\.get\("VAF_BROWSER_CDP_WAIT_S", "' + num + r'"\)')
    vnc_s = _default("vaf/core/browser_pool.py",
                     r'os\.environ\.get\("VAF_BROWSER_VNC_WAIT_S", "' + num + r'"\)')
    wipe_s = _default("vaf/core/browser_interactive.py",
                      r"def verified_profile_wipe\([^)]*deadline_s: float = " + num + r"\)")
    # A fresh per-user browser: DevTools, then the stream. The shared browser after a scope
    # change: DevTools, then the verified profile wipe.
    return max(cdp_s + vnc_s, cdp_s + wipe_s)


def test_the_wait_outlasts_the_servers_own_deadlines():
    """MUTATION: set BROWSER_START_TIMEOUT_MS to 30000 (the first suggestion) and a start on
    the shared browser that is still wiping its profile loses its spinner - red."""
    m = re.search(r"^const BROWSER_START_TIMEOUT_MS = (\d+);$", PAGE, re.M)
    assert m, "BROWSER_START_TIMEOUT_MS moved; re-point this guard"
    timeout_ms = int(m.group(1))
    floor_ms = _server_floor_seconds() * 1000
    # The comment above BROWSER_START_TIMEOUT_MS names these numbers (30 s DevTools, 8 s
    # stream, 40 s wipe); a server default that moves makes it false, so say so here.
    assert floor_ms == 70000, f"server deadlines changed ({floor_ms} ms): update page.tsx"
    assert timeout_ms > floor_ms, (timeout_ms, floor_ms)
    # A safety net, not a second way of spinning for minutes.
    assert timeout_ms <= 180000


def test_only_a_sent_start_is_waited_on_and_the_wait_drops_only_starting():
    """MUTATION: drop the `sent ?` guard, or clear `interactive` outright instead of through
    dropPendingBrowserStart (which would also close a stream that opened meanwhile) - red."""
    body = _toggle_browser_window()
    assert "const sent = sendBrowserInteractive({ type: 'browser_interactive_start' });" in body
    flat = " ".join(body.split())
    assert ("if (pendingBrowserStartRef.current) window.clearTimeout(pendingBrowserStartRef.current); "
            "pendingBrowserStartRef.current = sent ? window.setTimeout(() => { "
            "pendingBrowserStartRef.current = null; setSubAgentState(dropPendingBrowserStart); "
            "}, BROWSER_START_TIMEOUT_MS) : null;") in flat
    # The drop clears a pending start and nothing else, so a later reply still opens the stream.
    assert ("return s.interactive?.status === 'starting' ? { ...s, interactive: null } : s;"
            in PAGE)


def test_a_reply_ends_the_wait():
    """Any state for this chat replaces 'starting', so it also clears the give-up timer.
    MUTATION: delete the clear from the browser_interactive_state branch - red."""
    branch = PAGE.split("data.type === 'browser_interactive_state'", 1)[1]
    branch = branch.split("else if (data.type === 'artifact_update')", 1)[0]
    flat = " ".join(branch.split())
    belongs = "if (!eventBelongsHere(data, activeSessionId, 'worker')) return;"
    clear = ("if (pendingBrowserStartRef.current) { window.clearTimeout(pendingBrowserStartRef.current); "
             "pendingBrowserStartRef.current = null; }")
    assert belongs in flat and clear in flat
    # After the session check: a reply for another chat must not end this chat's wait.
    assert flat.index(belongs) < flat.index(clear)
