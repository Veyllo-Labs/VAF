# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""An open sub-agent window is context the model gets to know about.

The interactive browser has told the agent for a while that the user is looking
at a page, and that it can take that same browser over. The other specialists
had nothing: someone could open the Coder window, type "fix the failing test"
and the model had no idea which work they meant.

Two things are different from the browser lane and both shape this design:

- The browser's fact is SERVER-side (it holds a CDP lease that can be read at
  send time). These windows exist only in the browser tab, so the client has to
  state it - which is why the value is validated against the known kinds before
  it is stored: it ends up inside a prompt, and an unchecked client string in a
  prompt is an injection lane.
- The block pairs the state with the AFFORDANCE. The browser block carries a
  measured note that naming where the person is, without saying what can be done
  about it, changed nothing about the model's behaviour.
"""
import os
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_RUNNER = _REPO / "vaf" / "core" / "headless_runner.py"
_SERVER = _REPO / "vaf" / "core" / "web_server.py"
_PAGE = _REPO / "web" / "app" / "page.tsx"

_KINDS = ("coder", "research", "document", "librarian")
_TOOLS = {"coder": "coding_agent", "research": "research_agent",
          "document": "document_agent", "librarian": "librarian_agent"}


def _runner_block() -> str:
    """The whole block including its kind table, with the source's own line
    wrapping folded away: the prose is split across adjacent string literals,
    so searching the raw text for a sentence finds nothing."""
    src = _RUNNER.read_bytes().decode("utf-8")
    block = src.split("sw_kind = ((getattr", 1)[1].split("# Image viewer:", 1)[0]
    return re.sub(r'"\s*\n\s*(?:f?")?', "", block)


# ── the prompt block ──────────────────────────────────────────────────────────

def test_every_kind_names_its_own_tool():
    """A block that describes the Coder and then offers research_agent would send
    the model to the wrong specialist - and the mapping is easy to fat-finger."""
    block = _runner_block()
    for kind, tool in _TOOLS.items():
        entry = re.search(rf'"{kind}": \("([^"]+)", "([^"]+)"', block)
        assert entry, f"{kind} has no entry in the window block"
        assert entry.group(2) == tool, \
            f"{kind} points at {entry.group(2)}, should be {tool}"


def test_the_block_says_what_can_be_done_and_not_only_where_the_user_is():
    # The browser block records this as a MEASURED lesson: state alone changed
    # nothing. Its twin must not quietly regress to a status line.
    block = _runner_block()
    assert "call the {_tool} tool" in block, "the block stopped naming the action"
    assert "most likely" in block, "the block stopped saying what the message is about"


def test_the_block_does_not_invite_a_second_run():
    """It coexists with the SUB-AGENT ACTIVE system block, which says the opposite
    ('do not delegate this again'). An open window is not a running one, and the
    text has to hold both truths or the two blocks contradict each other."""
    block = _runner_block()
    assert "does NOT mean a run is going" in block
    assert "do not start a second one" in block or "not start a second one" in block


def test_it_is_injected_for_one_turn_and_cleared():
    src = _RUNNER.read_bytes().decode("utf-8")
    assert 'session_for_sw.runtime_state.pop("subagent_window", None)' in src, \
        "the block would repeat on every later turn"
    assert "effective_input = sw_block + effective_input" in src


# ── the lane that carries it ──────────────────────────────────────────────────

def test_the_server_validates_the_kind_before_it_reaches_a_prompt():
    src = _SERVER.read_bytes().decode("utf-8")
    seg = src.split('_saw_kind = cmd.get("subAgentWindow")', 1)[1][:1400]
    assert '_known_kinds = ("coder", "research", "document", "librarian")' in seg
    assert "_saw_kind in _known_kinds" in seg, \
        "an unvalidated client string would reach the prompt"


def test_closing_the_window_removes_the_fact_by_itself():
    # The delete branch is the whole close mechanism: the client simply stops
    # sending the field, so there is no close event that can be missed.
    src = _SERVER.read_bytes().decode("utf-8")
    seg = src.split('_saw_kind = cmd.get("subAgentWindow")', 1)[1][:2800]
    assert 'del loaded.runtime_state["subagent_window"]' in seg
    assert 'loaded.runtime_state.pop("subagent_window_detail", None)' in seg, \
        "the detail outlives the window it described"


def test_the_browser_keeps_its_own_richer_lane():
    """The browser is deliberately NOT folded into this: its context carries the
    page, the selection and a screenshot, read server-side from the lease."""
    page = _PAGE.read_bytes().decode("utf-8")
    assert "subAgentState.agentKind !== 'browser'" in page, \
        "the browser fell into the generic window lane"
    src = _SERVER.read_bytes().decode("utf-8")
    assert '"browser_context"' in src, "the browser's own capture disappeared"


def test_the_chip_and_the_turn_agree_on_what_is_open():
    """One derivation feeds both, so what the user is told and what the model is
    told cannot drift apart."""
    page = _PAGE.read_bytes().decode("utf-8")
    assert "const subAgentWindowKind: SubAgentKind | null =" in page
    assert "{ subAgentWindow: subAgentWindowKind }" in page, \
        "the turn stopped carrying the open window"
    assert "title={tMain('subAgentContextChipTitle'" in page, \
        "the composer stopped showing which window is open"


def test_a_hotbar_icon_opens_that_agents_window():
    page = _PAGE.read_bytes().decode("utf-8")
    assert "onClick={() => toggleSubAgentWindow(kind)}" in page
    body = page.split("const toggleSubAgentWindow", 1)[1][:900]
    assert "openSubAgentWindow(true)" in body, "the icon stopped opening the window"
    assert "closeSubAgentWindow(true)" in body, \
        "a second click no longer closes it (and loses the slide-out)"


def test_the_chip_sits_next_to_the_workspace_not_adrift_in_the_middle():
    """The composer row is justify-end, so a single `mr-auto` is what separates
    the left group from the right-aligned rest - it belongs on the LAST chip of
    that group. The browser chip already took it off the workspace chip; the
    sub-agent chip did not, so the workspace kept it and pushed the sub-agent
    chip out into the middle of the row (screenshot-confirmed)."""
    page = _PAGE.read_bytes().decode("utf-8")
    seg = page.split("Workspace chip: leftmost element", 1)[1][:2600]
    assert '!subAgentState.interactive?.active && !subAgentWindowKind && "mr-auto"' in seg, \
        "the workspace chip no longer hands the auto margin to the sub-agent chip"


def test_the_open_agent_is_the_bright_mark_in_the_rail():
    """The globe wears the hover state permanently while its window is open, so
    the rail always shows WHICH window is up. The hotbar icons had no open state
    at all: the coder window could be showing and its icon stayed grey, which
    reads as 'nothing is open' (screenshot-confirmed)."""
    page = _PAGE.read_bytes().decode("utf-8")
    seg = page.split("The hotbar itself", 1)[1][:2600]
    assert "subAgentState.isOpen && subAgentState.agentKind === kind" in seg, \
        "the rail icon no longer knows whether its own window is open"
    assert '"text-gray-900 scale-110"' in seg, \
        "the open rail icon lost the globe's active look"


def test_each_accent_is_the_one_that_agents_own_window_uses():
    """Rule 2: the accent now exists twice - in the specialist's view inside
    SubAgentWindow and on the chip/badge that stand for it elsewhere. Taking the
    hue from the view is the whole point; a chip in a colour its own window never
    shows would teach the wrong association."""
    page = _PAGE.read_bytes().decode("utf-8")
    window = (_REPO / "web" / "components" / "SubAgentWindow.tsx").read_bytes().decode("utf-8")
    table = page.split("const SUBAGENT_ACCENT", 1)[1].split("};", 1)[0]

    # the coder is deliberately hueless - a code editor is black and white
    assert re.search(r"coder:\s*\{ chip: \"text-gray-", table), \
        "the coder grew an accent; its window is deliberately neutral"

    expected_hue = {"research": "violet", "document": "teal",
                    "librarian": "orange", "browser": "sky"}
    # Every view anchors on its dock branch: the bare kind strings now also
    # appear in the idle-kind derivation far above the views themselves.
    view_start = {"research": "mode === 'dock' && agentKind === 'research'",
                  "document": "mode === 'dock' && agentKind === 'document'",
                  "librarian": "mode === 'dock' && agentKind === 'librarian'",
                  "browser": "mode === 'dock' && agentKind === 'browser'"}
    for kind, hue in expected_hue.items():
        row = re.search(rf"{kind}:\s*\{{ chip: \"([^\"]+)\"", table)
        assert row, f"{kind} has no accent"
        assert hue in row.group(1), f"{kind}'s chip is not {hue}: {row.group(1)}"
        # and that hue really is what the view paints with
        seg = window.split(view_start[kind], 1)[1][:6000]
        assert f"-{hue}-" in seg, \
            f"{kind}'s window no longer uses {hue}; the chip and the view drifted apart"


# ── the hand-open hold ────────────────────────────────────────────────────────

def test_a_hand_opened_window_never_closes_itself_for_any_kind():
    """The browser earned this rule first: a window the person opened stays until
    THEY close it, because the shared manual-open flag is reset by every streamed
    event's auto-open and the 3s auto-close then reaped hand-opened windows after
    a run. The hold now stores the KIND the person opened, so the globe and every
    hotbar specialist follow one rule - and the hold is exactly as wide as the
    click (a hand-opened coder does not also hold a research view a run swapped
    in, which is the per-kind semantics the browser always had)."""
    page = _PAGE.read_bytes().decode("utf-8")
    assert "const handOpenedKindRef = useRef<SubAgentKind | null>(null);" in page
    assert "handOpenedKindRef.current && subAgentState.agentKind === handOpenedKindRef.current" in page, \
        "the auto-close hold is no longer kind-generic"
    assert "subAgentState.agentKind === 'browser' && browserManualOpenRef" not in page, \
        "the browser-only hold came back"
    # the toggle takes the hold on open and releases it on the person's own close
    body = page.split("const toggleSubAgentWindow", 1)[1][:1400]
    assert "handOpenedKindRef.current = kind;" in body
    assert "handOpenedKindRef.current = null;" in body


def test_the_hold_clears_on_session_switch_and_manual_close():
    # A hold that outlives its chat pins the next chat's window open; one that
    # survives the person's own close makes the X button lie.
    page = _PAGE.read_bytes().decode("utf-8")
    assert "handOpenedKindRef.current = null;   // the hand-opened window belongs to the previous chat" in page
    assert "handOpenedKindRef.current = null;   // the person closed it: their hold is released" in page


def test_the_give_back_still_belongs_to_the_browser_alone():
    # The generalized hold must not widen the browser's interactive give-back:
    # a hand-opened coder window must never start an interactive browser lease.
    page = _PAGE.read_bytes().decode("utf-8")
    assert "handOpenedKindRef.current === 'browser'" in page, \
        "the give-back no longer checks that the BROWSER was the hand-opened kind"


# ── what the person is DOING in the window ────────────────────────────────────

def test_the_window_detail_is_flattened_and_capped_before_the_prompt():
    """Client-sent prose that lands inside a prompt block: newlines or control
    characters would let it impersonate the block's own structure ("--- ..."),
    and an uncapped string is a token sink. Flatten and cap, never trust."""
    src = _SERVER.read_bytes().decode("utf-8")
    seg = src.split('_saw_detail = cmd.get("subAgentWindowDetail")', 1)[1][:900]
    assert '" ".join(_saw_detail.split())[:200]' in seg, \
        "the detail reaches the prompt without flattening or a cap"


def test_the_detail_rides_only_with_a_valid_kind():
    # The detail branch lives INSIDE the known-kind branch: without a valid kind
    # nothing of the client's prose is stored at all.
    src = _SERVER.read_bytes().decode("utf-8")
    seg = src.split("_saw_kind in _known_kinds:", 1)[1]
    stored_at = seg.find('loaded.runtime_state["subagent_window_detail"]')
    else_at = seg.find("elif \"subagent_window\" in loaded.runtime_state:")
    assert 0 < stored_at < else_at, "the detail is stored outside the validated-kind branch"


def test_the_runner_says_what_the_person_is_doing():
    block = _runner_block()
    assert "Right now they are: {sw_detail}" in block, \
        "the block no longer carries what the person is doing in the window"
    src = _RUNNER.read_bytes().decode("utf-8")
    assert 'session_for_sw.runtime_state.pop("subagent_window_detail", None)' in src, \
        "the detail would repeat on every later turn"


# ── the idle librarian ────────────────────────────────────────────────────────

def _window_src() -> str:
    return (_REPO / "web" / "components" / "SubAgentWindow.tsx").read_bytes().decode("utf-8")


def test_the_idle_librarian_browses_through_the_existing_jailed_endpoint():
    """No new surface: the idle browse rides the session-jailed workspace API the
    workspace modal already uses. A second listing endpoint would be a second
    jail to keep correct."""
    win = _window_src()
    assert "/api/session/workspace?sessionId=" in win
    assert not re.search(r"/api/(files|browse|fs)\b", win), \
        "the idle browse grew its own listing endpoint"


def test_idle_lives_inside_each_windows_own_structure():
    """The first cut of this replaced every window with one generic folder list -
    the owner's verdict was blunt and right: each window HAS a structure, idle
    must move INTO it. The librarian browses in its own folder view (the
    workspace synthesized into the currentFolder shape), the coder gets the
    editor's native no-folder-open state, and research/document keep their
    paper shells untouched - an empty paper is their honest idle face."""
    win = _window_src()
    assert "inferredPresence === 'online' ? null" in win, \
        "a running agent no longer suppresses the idle browse"
    # no generic takeover branch may exist
    assert "mode === 'dock' && idleKind && agentKind === idleKind" not in win, \
        "the generic idle view that replaced the real windows came back"
    # librarian: idle rides the EXISTING folder view
    assert "(librarian?.currentFolder || idleFolder)" in win
    assert "const idleMode = !lib?.currentFolder && !!idleFolder;" in win
    # coder: idle rides the EXISTING explorer + editor
    assert "idleKind === 'coder' ? (<>" in win
    assert "No project folder selected" in win, \
        "the editor's native welcome state (with the project pick) is gone"
    # research/document deliberately have no idle browse of their own
    assert "researchHasRunData" not in win and "documentHasRunData" not in win


def test_the_idle_folder_reports_itself_and_cleans_up():
    # The composer mirror: the folder rides with the next turn while shown, and
    # reporting null IS the cleanup - a stale folder must never tag along with an
    # unrelated message after the window closed or a run took over.
    win = _window_src()
    assert 'onIdleContext(`${picked}browsing workspace folder' in win
    assert "return () => { onIdleContext(null); };" in win
    page = _PAGE.read_bytes().decode("utf-8")
    assert "{ subAgentWindowDetail: subAgentWindowDetailRef.current }" in page
    assert "subAgentWindowKind && subAgentWindowDetailRef.current" in page, \
        "a detail could be sent without its window kind"


def test_stale_folder_answers_cannot_win_the_race():
    # Two clicks in quick succession: the slower (older) response must not
    # overwrite the newer folder - the classic fetch race.
    win = _window_src()
    # TWO checkpoints on purpose - after the fetch resolves and after the json
    # parse - because both awaits are windows an older response can slip through.
    assert win.count("if (seq !== idleBrowseSeq.current) return;") >= 2, \
        "the idle browse lost one of its response-ordering checkpoints"


def test_idle_files_open_the_way_each_window_natively_opens_things():
    """The librarian hands files to the app's one file-type wheel (code viewer,
    document viewer, image viewer - the routing every surface shares); the coder
    opens them read-only in ITS OWN tabs, because that is what an editor does
    with a file. Neither invents a second routing."""
    win = _window_src()
    assert "onOpenWorkspaceFile?.(idleAbsFor(e.name), e.name);" in win, \
        "the librarian's idle rows stopped using the shared wheel"
    assert ": openIdleTab(abs, e.name)}" in win, \
        "the coder's idle explorer stopped opening files into its own tabs"
    page = _PAGE.read_bytes().decode("utf-8")
    assert "const openWorkspaceFileAt = useCallback(async (full: string, name: string)" in page
    assert "onOpenWorkspaceFile={openWorkspaceFileAt}" in page, \
        "the window no longer receives the shared opener"
    # the modal's own click delegates too - one wheel, two callers
    assert "await openWorkspaceFileAt(full, name);" in page


# ── honest idle, and the coder's project pick ────────────────────────────────

def test_no_window_claims_activity_while_nothing_runs():
    """The UX lie the owner caught: an idle coder said "Planning…", every
    hand-opened window said "Starting X - waiting for the agent…", the librarian
    overview said "Scanning" - all with no run behind them. The browser wrote
    the rule down long ago: only presence online may claim activity."""
    win = _window_src()
    assert "const coderLoading = !coder && inferredPresence === 'online';" in win
    assert "const librarianLoading = !librarian && inferredPresence === 'online';" in win
    assert "isLive ? 'Planning…' : 'No plan yet" in win
    assert ": isLive ? 'Scanning' : 'Idle'}" in win
    assert win.count("isLive ? 'Waiting for output…'") >= 3


def test_the_pick_can_be_found_and_covers_the_current_folder():
    """The owner's own question - "aber wie?" - was the finding: the mark only
    existed on hover, so the welcome text pointed at an invisible control, and
    the folder you are IN (including the workspace root, the most common
    project) had no row to mark at all. The mark is visible at rest now, and
    the welcome carries a button that takes the current folder."""
    win = _window_src()
    assert "'text-gray-300 hover:text-emerald-600'" in win, \
        "the pick mark is hover-only again - unfindable by reading about it"
    assert 'opacity-0' not in win.split("group/idlerow", 1)[1][:1600], \
        "the pick mark hides until hover again"
    assert 'as the project`}' in win, \
        "the use-current-folder button is gone"


def test_the_project_pick_reaches_the_coder_run():
    """The pick is not decoration: it rides the context detail with its ABSOLUTE
    path, and the runner's coder entry tells the model to pass exactly that as
    coding_agent's project_path - the parameter the tool already has."""
    win = _window_src()
    assert 'setIdleProject(isProject ? null : { abs, label: e.name });' in win
    assert 'as the Coder\'s project folder' in win, \
        "the pick no longer rides the context detail"
    block = _runner_block()
    assert "project_path" in block, \
        "the runner no longer tells the model how to use the picked folder"


# ── the idle explorer's one write: New folder ────────────────────────────────

def _mkdir(tmp_path, monkeypatch, name, subpath=""):
    """Drive the REAL mkdir handler with only the workspace root faked - the
    subpath jail and the name validation under test stay the product's own."""
    import asyncio
    from types import SimpleNamespace
    import vaf.core.web_server as ws
    monkeypatch.setattr(ws, "_resolve_session_workspace", lambda *a, **k: str(tmp_path))
    req = ws.WorkspaceMkdirRequest(sessionId="s", subpath=subpath, name=name)
    return asyncio.run(ws.create_session_workspace_folder(req, SimpleNamespace(client=None)))


def test_new_folder_lands_inside_the_workspace(tmp_path, monkeypatch):
    out = _mkdir(tmp_path, monkeypatch, "notes")
    assert out["ok"] is True
    assert (tmp_path / "notes").is_dir()


def test_new_folder_refuses_names_that_leave_the_row(tmp_path, monkeypatch):
    """The name is the only thing the client contributes, so it is the only
    thing that can attack: separators, dotfiles and traversal all 400."""
    import pytest as _pytest
    from fastapi import HTTPException
    for bad in ("../up", "a/b", ".hidden", "..", "", "   "):
        with _pytest.raises(HTTPException) as e:
            _mkdir(tmp_path, monkeypatch, bad)
        assert e.value.status_code == 400, f"{bad!r} must be refused"
    assert list(tmp_path.iterdir()) == [], "a refusal must create nothing"


def test_new_folder_refuses_duplicates(tmp_path, monkeypatch):
    import pytest as _pytest
    from fastapi import HTTPException
    _mkdir(tmp_path, monkeypatch, "twice")
    with _pytest.raises(HTTPException) as e:
        _mkdir(tmp_path, monkeypatch, "twice")
    assert e.value.status_code == 409


def test_new_folder_cannot_escape_through_the_subpath(tmp_path, monkeypatch):
    """The parent folder comes from the browse's subpath and must pass the same
    jail as the listing - this drives the real _resolve_workspace_subdir."""
    import pytest as _pytest
    from fastapi import HTTPException
    with _pytest.raises(HTTPException) as e:
        _mkdir(tmp_path, monkeypatch, "ok", subpath="../outside")
    assert e.value.status_code == 400
    assert not (tmp_path.parent / "outside").exists()


def test_the_idle_explorer_offers_new_folder_through_a_context_menu():
    """Right-click in the idle explorer opens a context menu whose New folder
    writes through the jailed mkdir endpoint - carrying the session and the
    folder the browse currently stands in, never a client-invented path."""
    win = _window_src()
    assert "onContextMenu={idleKind === 'coder' ? (ev) => openIdleMenu(ev, null) : undefined}" in win, \
        "the explorer background lost its context menu"
    assert "openIdleMenu(ev, e.isDir ? e.name : null)" in win, \
        "the rows lost their context menu"
    assert "'/api/session/workspace/mkdir'" in win
    assert "sessionId, subpath: idleBrowse.subpath, name" in win, \
        "the mkdir call no longer sends the browsed folder as the parent"
    assert "New folder…" in win
    assert win.count("Use as the project folder") >= 2, \
        "the menu no longer carries the project pick the rows have"


def test_the_welcome_offers_the_folders_beneath_you():
    """While you stand in a folder, its subfolders show beneath the use-this-one
    button as one-click picks - not only as rows in the Explorer sidebar."""
    win = _window_src()
    assert "or pick a folder in here:" in win
    assert "setIdleProject({ abs: idleAbsFor(en.name), label: en.name })" in win, \
        "the quick picks no longer travel as absolute paths"


def test_the_one_file_opener_is_a_real_dependency_of_its_caller():
    """The idle windows open files through `openWorkspaceFileAt`, and the
    workspace modal's own opener now delegates to it. A delegate defined BELOW
    its caller cannot appear in the caller's dependency array (temporal dead
    zone), so the caller gets memoized without it and keeps the delegate from
    an earlier render - which captures a stale WebSocket after a reconnect, and
    the opened file then silently never reaches the agent. Order is the fix:
    the delegate is defined first, so the dependency can be declared."""
    page = _PAGE.read_bytes().decode("utf-8")
    at = page.index("const openWorkspaceFileAt = useCallback")
    caller = page.index("const openWorkspaceFile = useCallback")
    assert at < caller, \
        "openWorkspaceFileAt moved below its caller - its dependency cannot be declared there"
    deps = page[caller:page.index("\n", page.index("}, [", caller))]
    assert "openWorkspaceFileAt" in deps.rsplit("}, [", 1)[-1], \
        "the delegate is not in the caller's dependency array - stale-closure lane is open"


def _ws_module():
    import vaf.core.web_server as ws
    return ws


def _planted_symlink_workspace(tmp_path):
    """A workspace holding a link that points OUT of it.

    Reachable in the product: the coder's shell runs with this very folder
    bound writable, and an A2A room workspace holds content a foreign harness
    wrote. Creating the link needs no privilege the runs do not already have.
    """
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "keep.txt").write_text("host file")
    os.symlink(outside, root / "shared")
    return root, outside


def test_no_workspace_lane_writes_through_a_planted_symlink(tmp_path, monkeypatch):
    """Containment is decided on RESOLVED paths, for every lane at once.

    All four share `_resolve_workspace_subdir`, which compared normalized
    STRINGS: the joined path never leaves the root, so the prefix test passed
    and the write landed at the link's target. Driving the real handlers here
    rather than the helper is the point - the helper could be fixed while a
    lane kept its own second copy of the old comparison.
    """
    import asyncio
    import base64
    from types import SimpleNamespace
    from fastapi import HTTPException
    ws = _ws_module()
    root, outside = _planted_symlink_workspace(tmp_path)
    monkeypatch.setattr(ws, "_resolve_session_workspace", lambda *a, **k: str(root))
    monkeypatch.setattr(ws, "_requester_name", lambda request: "admin")
    stub = SimpleNamespace(client=None)

    lanes = [
        ("mkdir", ws.create_session_workspace_folder,
         ws.WorkspaceMkdirRequest(sessionId="s", subpath="shared", name="planted")),
        ("upload", ws.upload_session_workspace_file,
         ws.WorkspaceUploadRequest(sessionId="s", subpath="shared", filename="planted.txt",
                                   content_base64=base64.b64encode(b"x").decode())),
        ("delete", ws.delete_session_workspace_entry,
         ws.WorkspaceDeleteRequest(sessionId="s", subpath="shared", name="keep.txt")),
    ]
    for label, handler, req in lanes:
        with pytest.raises(HTTPException) as e:
            asyncio.run(handler(req, stub))
        assert e.value.status_code == 400, f"{label} did not refuse the link"

    assert not (outside / "planted").exists(), "mkdir wrote outside the workspace"
    assert not (outside / "planted.txt").exists(), "upload wrote outside the workspace"
    assert (outside / "keep.txt").exists(), "delete removed a file outside the workspace"


def test_the_browse_listing_refuses_the_link_too(tmp_path, monkeypatch):
    """Reading through the link is its own leak: the listing would show a
    folder outside the workspace as if it belonged to the chat."""
    import asyncio
    from types import SimpleNamespace
    from fastapi import HTTPException
    ws = _ws_module()
    root, _ = _planted_symlink_workspace(tmp_path)
    monkeypatch.setattr(ws, "_resolve_session_workspace", lambda *a, **k: str(root))
    with pytest.raises(HTTPException) as e:
        asyncio.run(ws.get_session_workspace(SimpleNamespace(client=None),
                                             sessionId="s", subpath="shared"))
    assert e.value.status_code == 400


def test_the_workspace_lane_uses_the_framework_primitive(tmp_path):
    """The conversion, not just the behaviour: a second hand-rolled prefix
    comparison next to the primitive is how the two broken copies appeared in
    the first place, so the lexical shape may not come back."""
    src = _SERVER.read_bytes().decode("utf-8")
    assert "from vaf.core.path_jail import" in src
    body = src.split("def _resolve_workspace_subdir", 1)[1].split("\n@app", 1)[0]
    assert "contained_path" in body
    assert "startswith(root_norm" not in body, "the lexical containment is back"
    delete = src.split("async def delete_session_workspace_entry", 1)[1][:1800]
    assert "startswith(root_norm" not in delete, \
        "the delete lane kept its own lexical copy of the containment"


def test_a_bad_name_is_a_refusal_not_an_internal_error(tmp_path, monkeypatch):
    """A null byte reaches os.mkdir as a ValueError, which a surface reports as
    a 500 - an internal failure for what is plainly bad input."""
    import asyncio
    from types import SimpleNamespace
    from fastapi import HTTPException
    ws = _ws_module()
    monkeypatch.setattr(ws, "_resolve_session_workspace", lambda *a, **k: str(tmp_path))
    for bad in ("foo\x00bar", "line\nbreak", "a/b", "..", ".hidden", ""):
        with pytest.raises(HTTPException) as e:
            asyncio.run(ws.create_session_workspace_folder(
                ws.WorkspaceMkdirRequest(sessionId="s", subpath="", name=bad),
                SimpleNamespace(client=None)))
        assert e.value.status_code == 400, f"{bad!r} answered {e.value.status_code}"


def test_a_workspace_reached_through_a_linked_home_still_works(tmp_path, monkeypatch):
    """The root itself may lie under a link, and that is ordinary, not an
    attack: a documents directory reached through a symlinked home is the
    normal shape on more than one platform. Containment resolves both sides, so
    such a workspace must behave exactly like a plain one - a delete inside it
    is a delete, not an escape. A containment that mixes a resolved folder with
    an unresolved root reports an upward walk here and refuses the lot."""
    import asyncio
    from types import SimpleNamespace
    ws = _ws_module()
    real = tmp_path / "real_home" / "workspace"
    real.mkdir(parents=True)
    (real / "sub").mkdir()
    (real / "sub" / "note.txt").write_text("keep")
    linked_home = tmp_path / "home"
    os.symlink(tmp_path / "real_home", linked_home)
    root_through_link = str(linked_home / "workspace")

    monkeypatch.setattr(ws, "_resolve_session_workspace", lambda *a, **k: root_through_link)
    monkeypatch.setattr(ws, "_requester_name", lambda request: "admin")
    stub = SimpleNamespace(client=None)

    listing = asyncio.run(ws.get_session_workspace(stub, sessionId="s", subpath="sub"))
    assert [f["name"] for f in listing["files"]] == ["note.txt"]

    out = asyncio.run(ws.create_session_workspace_folder(
        ws.WorkspaceMkdirRequest(sessionId="s", subpath="sub", name="made"), stub))
    assert out["ok"] is True and (real / "sub" / "made").is_dir()

    asyncio.run(ws.delete_session_workspace_entry(
        ws.WorkspaceDeleteRequest(sessionId="s", subpath="sub", name="note.txt"), stub))
    assert not (real / "sub" / "note.txt").exists(), "a legitimate delete was refused"


def test_the_idle_lane_does_not_survive_a_chat_switch():
    """A window carries no trace of the chat it was open in before.

    The idle lane is workspace-scoped state living in a component that does NOT
    remount on a chat switch: the picked project, the listing, the draft row,
    the read-only tabs. Left standing, chat B's window listed chat A's folders,
    and chat A's absolute project path rode along on chat B's next message,
    because the detail lives in a ref that is read at send time - so clearing
    the detail is part of the reset, not a consequence of it.
    """
    win = _window_src()
    assert "idleSessionRef" in win, "no session identity is tracked for the idle lane"
    body = win.split("idleSessionRef.current = sessionId;", 1)[1].split("}, [sessionId", 1)[0]
    for cleared in ("setIdleBrowse(", "setIdleProject(null)", "setNewFolder(null)",
                    "setIdleMenu(null)", "setOpenTabs([])"):
        assert cleared in body, f"a chat switch no longer clears {cleared}"
    assert "onIdleContext?.(null)" in body, \
        "the previous chat's window detail still rides along after a switch"
    assert "idleBrowseSeq.current++" in body, \
        "an in-flight listing from the previous chat can still land in the new one"


def test_a_read_only_tab_is_identified_by_its_path_not_its_name():
    """Two files may share a basename, and the explorer browses folders.

    Identity by basename made `docs/README.md` and `src/README.md` one tab: the
    second click silently re-selected the first, so the content on screen
    belonged to a different file than the one the person clicked - and then
    described to the agent.
    """
    win = _window_src()
    assert "useState<Array<{ id: string; name: string; content: string }>>" in win, \
        "tabs no longer carry an identity separate from their label"
    idle = win.split("const openIdleTab", 1)[1][:700]
    assert "t.id === absPath" in idle, "the idle tab opener dedupes by label again"
    assert "t.name === name" not in idle, "the idle tab opener dedupes by label again"
    assert "openTabs.find(t => t.id === activeTab)" in win, \
        "the active tab is no longer resolved by identity"
