# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One Escape press dismisses exactly one layer, and the chat page hand-rolls none.

Every overlay in the web UI used to bind its own `keydown` listener on `window`.
That cannot compose: `stopPropagation` is consulted between the NODES of a
dispatch path, never between two listeners on the same node, so a listener on
`window` cannot stop a sibling listener on `window`. Measured before the fix: 16
hand-rolled window listeners across 14 files, and the calls written to keep them
from firing together were inert. The chat's workspace explorer had none at all,
so Escape did nothing there while it dismissed things behind it.

Pinned here: the primitive keeps ONE listener for the whole app and stops the
press immediately; the explorer's ladder is ordered and complete; the page keeps
no listener of its own; and the set of files that still hand-roll one only ever
shrinks.
"""
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_HOOK = _REPO / "web" / "hooks" / "useEscapeLayer.ts"
_PAGE = _REPO / "web" / "app" / "page.tsx"

# The files that still bind their own Escape listener. A ratchet, not a
# snapshot: converting one means deleting its line here, and a new entry is a
# regression. The hook itself is on the list because it IS the one listener.
_HAND_ROLLED = {
    "web/components/AutomationCalendarModal.tsx",
    "web/components/CodeViewer.tsx",
    "web/components/CreateAutomationPopup.tsx",
    "web/components/NotificationsModal.tsx",
    "web/components/SettingsModal.tsx",
    "web/components/SubAgentWindow.tsx",
    "web/components/connections/ContactsDashboard.tsx",
    "web/components/connections/DiscordDashboard.tsx",
    "web/components/connections/GitHubDashboard.tsx",
    "web/components/connections/TelegramDashboard.tsx",
    "web/components/connections/WhatsAppDashboard.tsx",
    "web/components/settings/UserVisibilityPicker.tsx",
    "web/hooks/useEscapeLayer.ts",
}


def _src(path: Path) -> str:
    # CRLF-normalised: git can check this out with CRLF on the Windows CI runner.
    assert path.exists(), f"{path} is missing - the Escape primitive was removed"
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def _layers(src: str):
    """Every useEscapeLayer({...}) block in source order."""
    return [m.group(1) for m in re.finditer(r"useEscapeLayer\(\{(.*?)\}\);", src, re.S)]


def test_the_primitive_keeps_one_listener_for_the_whole_app():
    src = _src(_HOOK)
    binds = re.findall(r"addEventListener\(\s*'keydown'", src)
    assert len(binds) == 1, f"the registry binds {len(binds)} keydown listeners, it may bind one"
    assert "window.addEventListener('keydown', onKeyDown, true)" in src, \
        "the listener is no longer capture-phase - React's delegated handlers would answer first"
    assert "stopImmediatePropagation" in src, \
        "without stopImmediatePropagation a sibling window listener still answers the same press"
    assert "e.stopPropagation()" not in src, \
        "stopPropagation does nothing between two listeners on window - that was the original defect"


def test_the_workspace_explorer_answers_escape():
    """The reported bug: the explorer could only be left through the X."""
    src = _src(_PAGE)
    assert "from '@/hooks/useEscapeLayer'" in src, "the chat page does not use the shared registry"
    window_layer = [b for b in _layers(src)
                    if "workspaceWindowOnScreen" in b and "level: 100" in b and "closeWorkspaceModal" in b]
    assert len(window_layer) == 1, \
        "the workspace window has no level-100 Escape layer - Escape does not close it"
    # The guard the rung is keyed on must still be the modal's own render condition.
    assert "const workspaceWindowOnScreen = isWorkspaceModalOpen &&" in src, \
        "the window's on-screen guard no longer follows isWorkspaceModalOpen"


def test_the_explorer_ladder_is_ordered_and_complete():
    """One press is one step back. A missing rung means a press skips a layer."""
    src = _src(_PAGE)
    expected = [
        (104, "workspaceDeleteTarget"),
        (103, "workspaceMenu"),
        (102, "workspaceNewFolder"),
        (101, "workspaceSearch"),
        (100, "workspaceWindowOnScreen"),
    ]
    blocks = _layers(src)
    found = []
    for level, state in expected:
        owned = [b for b in blocks if f"level: {level}" in b and state in b]
        assert len(owned) == 1, f"level {level} ({state}) is not registered exactly once"
        found.append(blocks.index(owned[0]))
    assert found == sorted(found), \
        "the ladder is out of order in the source - read top to bottom it no longer describes the screen"


def test_every_rung_is_keyed_on_what_is_actually_on_screen():
    """A rung keyed on its state alone answers a press while its own UI is not
    painted, and the press is spent on nothing. The window's Back button leaves
    the folder pane without clearing the draft row, so this is reachable, not
    theoretical."""
    src = _src(_PAGE)
    on_screen = {
        104: "workspaceWindowOnScreen",     # the confirmation covers both views
        103: "workspaceFolderPaneOnScreen",  # the menu belongs to the file grid
        102: "workspaceFolderPaneOnScreen",  # so does the draft row
        101: "isWorkspaceModalOpen",         # the search box is the index view's
        100: "workspaceWindowOnScreen",
    }
    for level, guard in on_screen.items():
        block = [b for b in _layers(src) if f"level: {level}" in b]
        assert block, f"level {level} is not registered"
        active = block[0].split("active:", 1)[1].split(",", 1)[0]
        assert guard in active, \
            f"level {level} is keyed on state alone ({active.strip()}) - it can eat Escape while invisible"
    assert "workspaceSearch.trim()" not in src.split("level: 101", 1)[0].rsplit("useEscapeLayer", 1)[-1], \
        "the search rung is keyed on a trimmed query while the Clear button is keyed on the raw one"


def test_an_upload_keeps_the_folder_it_was_aimed_at():
    """The upload loop outlives a navigation and a close: it reads a file and
    awaits a POST per file. Reading the destination live sent the rest of the
    batch wherever the window had moved to."""
    src = _src(_PAGE)
    body = src.split("const uploadWorkspaceFiles = useCallback", 1)[1].split("}, [currentSessionId", 1)[0]
    assert "const wsSubpath = workspaceSubpathRef.current;" in body, \
        "the upload no longer captures its target folder before the loop"
    assert "subpath: wsSubpath," in body, "the upload reads the live subpath again inside the loop"


def test_the_delete_dialog_does_not_hand_escape_to_the_window_underneath():
    """While the delete runs, Cancel is disabled and Escape must not stand in for
    it - nor fall through to the window, which would close the explorer with the
    confirmation still set and show it again on the next opening."""
    src = _src(_PAGE)
    block = [b for b in _layers(src) if "level: 104" in b]
    assert block, "the delete confirmation has no Escape layer"
    assert "workspaceDeleting ? null" in block[0], \
        "the in-flight delete no longer swallows Escape - the press reaches the window underneath"


def test_the_chat_page_hand_rolls_no_escape_listener():
    src = _src(_PAGE)
    assert "useEscapeLayer(" in src, "collector check: the page uses no layers at all, so this proves nothing"
    for m in re.finditer(r"addEventListener\(\s*'keydown'", src):
        window = src[max(0, m.start() - 1200):m.end() + 1200]
        line = src[:m.start()].count("\n") + 1
        assert "'Escape'" not in window, \
            f"page.tsx:{line} hand-rolls an Escape listener again - it answers the same press as the registry"


def test_the_hand_rolled_escape_listeners_only_shrink():
    """The ratchet. Converting a file means deleting its line from the frozen set;
    a NEW file binding its own Escape listener fails here."""
    found = set()
    for sub in ("app", "components", "hooks"):
        for path in sorted((_REPO / "web" / sub).rglob("*")):
            if path.suffix not in (".ts", ".tsx") or not path.is_file():
                continue
            if ".next" in path.parts or "node_modules" in path.parts:
                continue
            src = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
            if re.search(r"addEventListener\(\s*'keydown'", src) and "'Escape'" in src:
                found.add(path.relative_to(_REPO).as_posix())
    # Collector honesty: a regex that stopped matching would pass an empty set.
    assert len(found) >= 8, f"the collector found only {len(found)} sites - it stopped seeing them"
    assert found <= _HAND_ROLLED, (
        f"new hand-rolled Escape listeners: {sorted(found - _HAND_ROLLED)}. "
        "Register the overlay with useEscapeLayer instead - two window listeners both answer one press."
    )


def test_one_way_out_of_the_explorer():
    """Escape, the X and the four file-open paths must all leave the same way, or
    the window reopens carrying the last visit's folder, view and search."""
    src = _src(_PAGE)
    assert "const closeWorkspaceModal = useCallback" in src, "the shared close path is gone"
    raw = [m.start() for m in re.finditer(r"setIsWorkspaceModalOpen\(false\)", src)]
    assert len(raw) == 1, (
        f"{len(raw)} places close the explorer directly - all but the one inside closeWorkspaceModal "
        "skip the reset, so the next opening starts wherever the last visit ended"
    )
    body = src.split("const closeWorkspaceModal = useCallback", 1)[1].split("}, []);", 1)[0]
    for cleared in ("setWorkspaceMenu(null)", "setWorkspaceNewFolder(null)",
                    "setWorkspaceDeleteTarget(null)", "setWorkspaceSearch('')",
                    "viewedWorkspaceSidRef.current = null"):
        assert cleared in body, f"closing the explorer no longer clears {cleared}"
