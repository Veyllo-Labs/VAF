# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One definition of an empty chat, and who is allowed to act on it.

There were four, and they disagreed: the terminal app's own copy, the husk
sweep's, and - twice - the browser's, which decided from a cached message COUNT
and from an attachment list it only holds for chats it has opened. The browser's
copy is the one that cost something. Nothing refreshes a sidebar row after a
chat turn, so a conversation that filled up while the list stood still went on
reporting zero messages, and the trash icon skipped its confirmation dialog for
it: a full chat deleted on one click, with no dialog and no archive (live
incident).

So the rule is not "the client checks harder". The client does not check at all
any more - it asks, and the server answers from the record. These tests pin both
halves: what `is_untouched` decides, and that the two surfaces which can remove
a chat consult it instead of trusting the request.
"""
import pathlib
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("llama_cpp", MagicMock())

from vaf.core.session import (  # noqa: E402
    SessionManager,
    get_user_projects_root,
    is_untouched,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCOPE = "ab12cd34"


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return SessionManager(storage_dir=str(tmp_path / "sessions"))


# ── what counts as content ────────────────────────────────────────────────

def test_a_new_chat_is_untouched(mgr):
    assert is_untouched(mgr.new(name="new", user_scope_id=SCOPE)) is True


def test_scaffolding_is_not_content(mgr):
    """A system prompt and the wreckage of a turn that produced no text are
    exactly what the husk sweep exists to remove."""
    s = mgr.new(name="husk", user_scope_id=SCOPE)
    s.add_message("system", "you are ...")
    s.add_message("tool", "{}")
    assert is_untouched(s) is True


def test_the_agent_writing_alone_counts_as_content(mgr):
    """MUTATION: drop "assistant" from the roles that count.

    An automation result and a proactive question are delivered into a chat the
    person has not answered yet, so it holds no `user` message at all. Under the
    older rule that read as an abandoned husk, and the next terminal start
    deleted it without asking anybody.
    """
    s = mgr.new(name="proactive", user_scope_id=SCOPE)
    s.add_message("assistant", "your report is ready")
    assert is_untouched(s) is False


def test_an_attachment_counts_without_a_message(mgr):
    """A document can be added and never sent."""
    s = mgr.new(name="attached", user_scope_id=SCOPE)
    s.runtime_state["sidebar_documents"] = [{"name": "contract.pdf"}]
    assert is_untouched(s) is False


def test_a_file_in_the_chats_workspace_counts(mgr):
    """The workspace upload writes straight into the chat's folder, so a chat
    can hold real work while its message list is still empty."""
    s = mgr.new(name="workspace", user_scope_id=SCOPE)
    folder = get_user_projects_root(SCOPE) / s.id
    folder.mkdir(parents=True, exist_ok=True)
    assert is_untouched(s) is True, "an empty folder is not content"
    (folder / ".vaf_workspace.json").write_text("{}", encoding="utf-8")
    assert is_untouched(s) is True, "the folder's own label is not content either"
    (folder / "notes.txt").write_text("real work", encoding="utf-8")
    assert is_untouched(s) is False


def test_a_session_that_cannot_be_read_is_never_untouched():
    """MUTATION: answer True (or raise) for None.

    The ownership gate hands back `(allowed, None)` for a record that exists but
    cannot be loaded - a corrupt file an admin is clearing up. Being unable to
    prove a chat is empty must never read as "it is empty".
    """
    assert is_untouched(None) is False


def test_every_row_shape_is_read():
    """`Message` objects, plain dicts and the terminal app's replay tuples all
    arrive here. A predicate blind to one of them calls a real conversation
    untouched and discards it on exit."""
    assert is_untouched(SimpleNamespace(id="x", messages=[
        SimpleNamespace(role="user", content="hi")])) is False
    assert is_untouched(SimpleNamespace(id="x", messages=[
        {"role": "assistant", "content": "hi"}])) is False
    assert is_untouched(SimpleNamespace(id="x", messages=[
        ("user", "hallo", "09:15")])) is False
    assert is_untouched(SimpleNamespace(id="x", messages=[
        ("system", "prompt", "09:15")])) is True
    # No runtime_state at all is the shape every terminal-app double has.
    assert is_untouched(SimpleNamespace(id="x", messages=[])) is True


# ── the husk sweep asks the same question ─────────────────────────────────

def test_the_husk_sweep_keeps_what_the_dialog_would_ask_about(mgr):
    """MUTATION: put `role == "user"` back into cleanup_empty.

    This runs from the terminal lanes against the same store the web server
    serves. While the server refuses to delete a chat without asking, a second
    process must not be removing it on a looser rule.
    """
    husk = mgr.new(name="husk", user_scope_id=SCOPE)
    husk.add_message("system", "you are ...")
    mgr.save(husk)

    proactive = mgr.new(name="proactive", user_scope_id=SCOPE)
    proactive.add_message("assistant", "your report is ready")
    mgr.save(proactive)

    attached = mgr.new(name="attached", user_scope_id=SCOPE)
    attached.runtime_state["sidebar_documents"] = [{"name": "contract.pdf"}]
    mgr.save(attached)

    assert mgr.cleanup_empty() == 1
    left = sorted(r["name"] for r in mgr.list(user_scope_id=SCOPE))
    assert left == ["attached", "proactive"]


def test_the_husk_sweep_does_not_repoint_the_current_session(mgr):
    """MUTATION: drop `repoint=False` from the sweep's load.

    It walks EVERY session in the store. Loading with the defaults would leave
    the manager pointing at the last file it happened to read - and at None once
    that file is deleted, because deleting the current session clears it.
    """
    live = mgr.new(name="live", user_scope_id=SCOPE)
    live.add_message("user", "hi")
    mgr.save(live)
    husk = mgr.new(name="husk", user_scope_id=SCOPE)
    mgr.save(husk)
    mgr._current = live

    mgr.cleanup_empty(exclude_session_id=live.id)

    assert mgr._current is not None, "the sweep dropped the live session pointer"
    assert mgr._current.id == live.id


# ── the surfaces that can remove a chat ───────────────────────────────────

def _ws_branch(name: str) -> str:
    src = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    block = src[src.index(f'elif type == "{name}"'):]
    return block[:block.index("elif type ==", 20)]


@pytest.mark.parametrize("branch", ["delete_session", "hide_session"])
def test_the_server_decides_whether_to_ask(branch):
    """MUTATION: delete the `confirmed` check, or trust the request instead.

    Both branches take a chat out of the sidebar, and the browser cannot answer
    whether that chat holds anything: its row is a cached copy that no chat turn
    refreshes. `confirmed` is the browser reporting that a person read the
    dialog; without it the RECORD decides.
    """
    block = _ws_branch(branch)

    assert 'cmd.get("confirmed")' in block, "the branch trusts the request again"
    assert "is_untouched()" in block, "the branch asks a question of its own again"
    assert '"needsConfirm": True' in block, "a refusal must say what it wants"
    # The refusal must come BEFORE anything is removed.
    removal = min((block.index(m) for m in ("session_mgr.delete(", "session_mgr.hide(")
                   if m in block), default=len(block))
    assert block.index('"needsConfirm": True') < removal, (
        "the chat is already gone by the time the browser is asked")
    # And the ownership gate still runs before all of it.
    assert block.index("_ws_session_owner_ok") < block.index('cmd.get("confirmed")')
    # MUTATION: drop the existence check. A record that is missing and one that
    # cannot be READ both reach this branch as `(allowed, None)`, and they need
    # opposite answers: the unreadable one is asked about, the absent one has
    # nothing to ask about. Without this, a double-clicked trash icon opens a
    # dialog for a chat that is already gone.
    assert "_session_record_exists(sid)" in block


def test_the_answer_goes_out_before_the_new_session_list():
    """MUTATION: move the reply after the broadcast.

    The browser decides where to go next from the row it is about to lose - its
    title, and whether it was a thinking run. The list broadcast takes that row
    out of its hands, so it must not arrive first.
    """
    block = _ws_branch("delete_session")
    assert block.index('"needsConfirm": False') < block.index("broadcast_to_user")


def test_the_browser_asks_instead_of_deciding():
    """MUTATION: put the client-side emptiness test back in the trash icon.

    `messageCount` arrives with the session list and nothing pushes one after a
    chat turn, so the row it is read from can be arbitrarily old. That is the
    guess this whole round removes; the dialog is opened by the server's answer
    now.
    """
    source = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")

    assert "const isEmpty = (s.messageCount || 0) === 0" not in source, (
        "the sidebar row decides again whether a chat may be deleted unasked")
    assert "archive: !!target.archive, confirmed: !!target.confirmed," in source, (
        "the delete request no longer says whether a person confirmed it")
    assert "archive: archiveOnDelete, confirmed: true" in source, (
        "the dialog's confirm button stopped confirming")
    assert "data.type === 'session_delete_result'" in source, (
        "nothing opens the dialog when the server refuses")
    # The answer is addressed by `id`, never `sessionId`: the socket's master
    # filter drops events stamped with a session that is not the open one, and
    # the chat being deleted usually is not.
    assert '"id": sid, "deleted": False, "needsConfirm": True' in (
        (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8"))
