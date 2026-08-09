# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Contract: the session/turn context API (docs/EMBEDDING.md, concurrency corollary).

EMBEDDING.md's concurrency section names `set_current_session_id`,
`session_context(sid)`, `set_current_turn_id` and `get_current_turn_id` at the
`vaf.core.subagent_ipc` path - that deep path IS the documented contract here,
deliberately not the facade. The documented semantics pinned in this file:

- The setters declare per CONTEXT (thread), never per process, and they SET
  only - they never restore. Told `None` is a remembered declaration, distinct
  from never-told.
- The getters answer the TOLD value when the context was told (including told
  `None`); only a never-told context falls back to the `VAF_SESSION_ID` /
  `VAF_TURN_ID` env vars (the child-process case), stripped, with blank
  reading as `None`.
- `session_context(sid)` serves one block and then restores the prior state
  EXACTLY, including never-told ("restores the thread's context exactly as it
  was").
- A thread you start yourself begins never-told ("a value set on one thread is
  invisible to threads you start yourself").

ISOLATION: because the two bare setters never restore, every test that calls
one runs its body inside `contextvars.copy_context().run(...)` - stdlib-only
containment: `ContextVar.set()` inside the copied context mutates only the
copy, so nothing leaks into the runner's own context or into later tests.
The autouse conftest fixture clears ambient `VAF_SESSION_ID` / `VAF_TURN_ID`.
"""
import contextvars
import threading

import vaf  # noqa: F401 - facade first; the context API below is documented BESIDE it

# Documented path: EMBEDDING.md's concurrency corollary names these functions at
# vaf.core.subagent_ipc explicitly. get_current_session_id is the getter that
# same section implies for set_current_session_id/session_context (same module,
# and the symmetric twin of the explicitly named get_current_turn_id).
from vaf.core.subagent_ipc import (
    get_current_session_id,
    get_current_turn_id,
    session_context,
    set_current_session_id,
    set_current_turn_id,
)


def run_isolated(fn):
    """Run `fn` inside a copy of the current context (see module docstring)."""
    return contextvars.copy_context().run(fn)


def test_a_never_told_context_with_no_env_answers_none():
    """The parent-process default: a helper thread nobody told which run it
    belongs to has no business addressing a browser - the answer is None,
    never a leftover value."""
    assert get_current_turn_id() is None
    assert get_current_session_id() is None


def test_a_never_told_context_falls_back_to_the_env_var(monkeypatch):
    """The child-process case: VAF_TURN_ID is how a turn survives the fork
    into a subprocess that finishes after its turn."""
    monkeypatch.setenv("VAF_TURN_ID", "t-123")
    assert get_current_turn_id() == "t-123"


def test_a_blank_or_whitespace_env_value_reads_as_none(monkeypatch):
    """Strip contract: the env value is stripped before the truth test and an
    empty result becomes None - a blank string is not the safe direction
    (it would take unscoped broadcast branches downstream)."""
    monkeypatch.setenv("VAF_TURN_ID", "")
    assert get_current_turn_id() is None
    monkeypatch.setenv("VAF_TURN_ID", "   ")
    assert get_current_turn_id() is None
    # The same strip that blanks whitespace also trims a padded real id.
    monkeypatch.setenv("VAF_TURN_ID", "  t-9  ")
    assert get_current_turn_id() == "t-9"


def test_a_told_turn_id_wins_over_the_env(monkeypatch):
    monkeypatch.setenv("VAF_TURN_ID", "other")

    def body():
        set_current_turn_id("told")
        assert get_current_turn_id() == "told"

    run_isolated(body)
    # Outside the copied context this test's write is invisible: the runner's
    # own context is still never-told, so the env fallback answers again.
    assert get_current_turn_id() == "other"


def test_told_none_is_a_remembered_declaration_that_beats_the_env(monkeypatch):
    """Told None means "this run belongs to no turn" and is REMEMBERED as
    such - it must NOT fall through to the env var, because that fall-through
    is exactly the cross-tenant borrowing the sentinel exists to prevent."""
    monkeypatch.setenv("VAF_TURN_ID", "ambient")

    def body():
        set_current_turn_id(None)
        assert get_current_turn_id() is None

    run_isolated(body)


def test_session_id_round_trips_and_told_none_is_remembered(monkeypatch):
    monkeypatch.setenv("VAF_SESSION_ID", "env-sid")

    def body():
        set_current_session_id("sess-0001")
        assert get_current_session_id() == "sess-0001"
        # Same told-None contract as the turn twin: a declaration, not a clear.
        set_current_session_id(None)
        assert get_current_session_id() is None

    run_isolated(body)


def test_session_context_serves_the_id_inside_and_restores_a_told_prior():
    # Copied context although session_context restores by token: if THAT
    # restore is what broke, the failure must not leak into other tests.
    def body():
        set_current_session_id("sess-prior")
        with session_context("sess-inner"):
            assert get_current_session_id() == "sess-inner"
        assert get_current_session_id() == "sess-prior"

    run_isolated(body)


def test_session_context_restores_never_told_exactly(monkeypatch):
    """The documented reason session_context exists: "told None" and "never
    told" answer differently, so save-and-restore from outside is impossible -
    only the ContextVar token can put the untold state back. Provable via the
    env fallback: it fires only for never-told, and it must fire AGAIN after
    the block exits."""
    monkeypatch.setenv("VAF_SESSION_ID", "env-sid")

    def body():
        assert get_current_session_id() == "env-sid"  # never told: fallback
        with session_context("x"):
            assert get_current_session_id() == "x"
        # Prior state restored EXACTLY, i.e. never-told - not told-"env-sid"
        # and not told-None. Only then can the fallback answer again.
        assert get_current_session_id() == "env-sid"

    run_isolated(body)


def test_a_fresh_thread_starts_never_told(monkeypatch):
    """EMBEDDING.md: "a value set on one thread is invisible to threads you
    start yourself" - the thread that drives an agent must declare its own
    session. A fresh thread is never-told, so the env fallback applies there
    even while the spawning context holds a told value."""
    monkeypatch.setenv("VAF_SESSION_ID", "env-sid")
    monkeypatch.setenv("VAF_TURN_ID", "env-tid")
    seen = {}

    def in_thread():
        seen["session"] = get_current_session_id()
        seen["turn"] = get_current_turn_id()

    def body():
        set_current_session_id("told-sid")
        set_current_turn_id("told-tid")
        t = threading.Thread(target=in_thread)
        t.start()
        t.join(timeout=10)
        assert not t.is_alive()
        # The spawning context keeps its own declarations...
        assert get_current_session_id() == "told-sid"
        assert get_current_turn_id() == "told-tid"

    run_isolated(body)
    # ...while the fresh thread saw a never-told context: env fallback.
    assert seen == {"session": "env-sid", "turn": "env-tid"}
