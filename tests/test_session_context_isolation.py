# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Which session a run serves is a property of the run, not of the process.

THE DEFECT THIS CLOSES, measured on the live tree (2026-08-04). Every tool
dispatch wrote `os.environ["VAF_SESSION_ID"]` process-wide, before policy ran,
and nothing ever restored it. Three tool-dispatching lanes are unconditional
daemon threads in the web-server process: the chat worker, thinking (enabled by
default) and the automation scheduler. Beside that write, the session resolver
kept a module-global fallback for "a background helper thread that reads it
without a context".

On the automation lane that was not a race, it was every run. `automation.py`
called neither `set_current_session_id` nor `load_session_context` - zero
occurrences - so a scheduled task started with an empty context and resolved
whichever session a chat turn had left behind. Then:

  document_writer -> resolve_agent_output_dir(session_id=None)
                  -> get_session_workspace_dir reads the process environment
                  -> builds VAF_Projects/<other tenant>/<their session>/
                  -> writes with a raw open()
                  -> notifies THEIR browser and persists the path into THEIR
                     session record.

The per-user file jail does not catch it: `document_writer` and `document_agent`
call `is_safe_path` zero times, against fourteen in `filesystem.py`, and the jail
is only ever consulted from inside `is_safe_path`. In the same run the borrowed
id also drives the Stop check, so a foreign user's Stop aborts this tool and the
real owner's Stop does nothing.

THE THREE RULES PINNED HERE, because all three are silent when broken:

1. A context that was TOLD wins over the environment. The environment is the
   process-boundary channel and nothing else: a child is spawned with it and
   declares it into its own context at bootstrap.
2. Told-`None` is a declaration, not an absence. A scheduled run answers nobody,
   and a stale environment value must not resurrect a session behind it.
3. A fresh thread inherits NOTHING. That is the property that made the old
   module-global fallback a tenant selector, so it is asserted directly.
"""
import os
import threading

import pytest

from vaf.core.subagent_ipc import get_current_session_id, set_current_session_id

SESSION_A = "green123456"
SESSION_B = "red654321"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Isolate BOTH channels, and restore the context rather than bare-clearing it.

    The ContextVar lives on the calling thread, which under pytest is the same
    thread for every test in the file and for every test after it. Setting it
    without restoring leaked a session into later modules and broke two tests that
    legitimately pin the child case (env set, context never told).
    """
    import vaf.core.subagent_ipc as ipc

    monkeypatch.delenv("VAF_SESSION_ID", raising=False)
    token = ipc._session_ctx.set(ipc._UNSET)
    yield
    ipc._session_ctx.reset(token)


def _resolved_in_new_thread() -> list:
    """What a bare thread resolves - the automation and thinking lanes' shape."""
    seen = []
    t = threading.Thread(target=lambda: seen.append(get_current_session_id()))
    t.start()
    t.join()
    return seen


# ── rule 3: a fresh thread inherits nothing ──────────────────────────────────

def test_a_fresh_thread_does_not_inherit_another_lanes_session() -> None:
    """The property the module global destroyed: it turned "never told" into
    "whatever the last chat turn set"."""
    set_current_session_id(SESSION_A)
    assert get_current_session_id() == SESSION_A
    assert _resolved_in_new_thread() == [None]


# ── rule 1: the context wins, the environment is the process boundary ────────

def test_a_told_context_wins_over_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("VAF_SESSION_ID", SESSION_A)
    set_current_session_id(SESSION_B)
    assert get_current_session_id() == SESSION_B


def test_a_never_told_context_reads_the_environment(monkeypatch) -> None:
    """The child case, and the only case: a sub-agent child is spawned with the
    variable and has no context of its own until it bootstraps."""
    monkeypatch.setenv("VAF_SESSION_ID", SESSION_A)
    assert _resolved_in_new_thread() == [SESSION_A]


def test_a_blank_environment_value_is_no_session(monkeypatch) -> None:
    """Empty is not the safe direction: a blank string reaching the broadcast
    takes the unscoped branch, which reaches every connected client."""
    monkeypatch.setenv("VAF_SESSION_ID", "   ")
    assert _resolved_in_new_thread() == [None]


# ── rule 2: told-None is a declaration ───────────────────────────────────────

def test_told_nobody_is_remembered_as_nobody(monkeypatch) -> None:
    """The automation lane. Without this, its scheduled run resolves a live
    tenant's session and writes into their workspace."""
    monkeypatch.setenv("VAF_SESSION_ID", SESSION_A)
    set_current_session_id(None)
    assert get_current_session_id() is None


# ── the interleave, with a real dispatch ─────────────────────────────────────

def test_a_background_lane_cannot_borrow_a_live_turns_session(monkeypatch, tmp_path) -> None:
    """The two-lane sequence that produced the cross-tenant write.

    Lane A is a user's turn that dispatches a tool and parks inside it. Lane B is
    a scheduled run on its own thread, which declares that it belongs to nobody.
    While A is parked mid-dispatch, B resolves its output directory - and must not
    land in A's workspace.
    """
    from vaf.core.session import get_session_workspace_dir

    monkeypatch.setattr("vaf.core.platform.Platform.documents_dir", lambda: tmp_path)
    # A stale value in the process environment is the realistic starting state: the
    # variable is inherited at launch and, before this fix, rewritten by every dispatch.
    # If the resolver consulted it before the context, B would land in A's workspace.
    monkeypatch.setenv("VAF_SESSION_ID", SESSION_A)

    in_dispatch = threading.Barrier(2, timeout=10)
    released = threading.Event()
    resolved_by_b = []

    def lane_a():
        # A user's turn: the worker declares its session, then dispatches.
        set_current_session_id(SESSION_A)
        in_dispatch.wait()          # B runs while this dispatch is still open
        released.wait(timeout=10)

    def lane_b():
        # A scheduled automation: declares that it serves no web session, exactly
        # as vaf/core/automation.py now does before running a task.
        set_current_session_id(None)
        in_dispatch.wait()
        # create=True is what the leak path uses - this is the call that MAKES the
        # directory the deliverable is then written into. With create=False the
        # resolver only returns folders that already exist, so a test written that
        # way passes whether or not the defect is present.
        resolved_by_b.append(get_session_workspace_dir(None, create=True))
        released.set()

    a = threading.Thread(target=lane_a)
    b = threading.Thread(target=lane_b)
    a.start(), b.start()
    a.join(timeout=15), b.join(timeout=15)

    assert resolved_by_b == [None], (
        f"the background lane resolved a workspace while another user's turn was "
        f"open: {resolved_by_b}. That directory is the other tenant's, the write "
        f"that follows uses a raw open() and the file jail never sees it."
    )
    created = list((tmp_path / "VAF_Projects").rglob("*")) if (tmp_path / "VAF_Projects").exists() else []
    assert not created, f"a directory was created for a foreign session: {created}"


def test_the_scheduled_lane_declares_before_it_runs_anything(monkeypatch) -> None:
    """The declaration must live in the automation lane itself, not only in this
    test's stand-in. Driven through the real `_run_scheduled_task`, whose thread is
    where a scheduled run actually starts.
    """
    from vaf.core.automation import AutomationManager, AutomationTask

    seen = []
    mgr = AutomationManager.__new__(AutomationManager)
    monkeypatch.setattr(AutomationManager, "_log_scheduler_event", lambda self, m: None)
    monkeypatch.setattr(
        AutomationManager, "run_task",
        lambda self, task, new_terminal=False: seen.append(get_current_session_id()) or "ok")

    monkeypatch.setenv("VAF_SESSION_ID", SESSION_A)
    set_current_session_id(SESSION_A)

    task = AutomationTask(id="auto-1", name="nightly", prompt="do a thing",
                          workflow_steps=[], created_at="2026-08-04T10:00:00",
                          parameters={})
    mgr._run_scheduled_task(task)
    for t in threading.enumerate():
        if t.name == "automation-auto-1":
            t.join(timeout=10)

    assert seen == [None], (
        f"the scheduled lane ran as {seen}, a session it does not own. Without its own "
        "declaration the thread starts with an empty context and adopts whatever a live "
        "chat turn left behind - which is how its output reached another tenant."
    )


def test_the_parent_never_publishes_a_session_into_the_process_environment() -> None:
    """Source ratchet. The single write that started this is gone; every spawn
    site puts the id into the CHILD's env dict instead, which the platform layer
    merges over a copy of the parent's."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in sorted((root / "vaf").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                # os.environ["VAF_SESSION_ID"] = ... , but not some_dict[...] = ...
                if (isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Attribute)
                        and target.value.attr == "environ"
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value == "VAF_SESSION_ID"):
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not offenders, (
        f"a lane publishes its session into the process environment: {offenders}. "
        "Every other lane in this process shares that variable; pass it in the "
        "child's env dict instead, and declare it with set_current_session_id."
    )
