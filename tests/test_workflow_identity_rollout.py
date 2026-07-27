# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A saved workflow ran as the machine owner, whoever started it.

Seven places construct a ``WorkflowEngine``. Three always passed an identity: the chat user
for a temporary workflow, the task owner for an automation, the paused record on resume. Four
passed none - and one of those four is the saved-template lane, the one most people actually
use. With no identity the engine falls back to ``username="admin"`` and no scope, so every
tool that keys on the caller followed suit: memory writes, messaging, mail, calendar,
contacts. The GitHub tools go further and resolve the ADMIN's token when the username is the
local admin, so a workflow started by anyone reached the owner's account.

It was demonstrated live rather than argued: the same workflow step reading a skill shared
only with the owner was refused when the engine carried an identity and returned the skill in
full when it did not.

Closing it is a behaviour change by definition - that is the point - so it rolls out behind
``workflow_identity_injection``, and this file pins BOTH sides. A switch whose "on" position
is untested is a switch nobody dares to flip; a switch whose "off" position is untested is not
a rollback.

Three values, not a boolean, and the reason is in the middle column: ``off`` is NOT the old
state. The three lanes that always passed an identity keep doing so under every setting - the
resolver governs only the four that did not. A boolean would invite "off = as before" and
silently demote those three, which is how a rollback turns into an incident.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from vaf.workflows.engine import identity_for_engine

SCOPE = "deadbeef-0000-0000-0000-000000000000"   # synthetic; never a real scope UUID


def _with_mode(mode):
    return patch("vaf.core.config.Config.get",
                 side_effect=lambda k, d=None: mode if k == "workflow_identity_injection" else d)


# ── the switch ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["legacy", "off", "LEGACY", " off "])
def test_only_an_explicit_rollback_keeps_the_old_behaviour(mode):
    """Rolling back is a deliberate act and has exactly two spellings."""
    with _with_mode(mode):
        assert identity_for_engine(SCOPE, "tenant") == {}


@pytest.mark.parametrize("mode", ["", None, "anything-else", "declaered"])
def test_a_value_that_is_not_a_rollback_means_declared(mode):
    """The polarity flipped with the default. While "legacy" was the default an unrecognised
    value could safely mean "as before"; now "as before" is the leaky state, so a typo must
    fall towards the strict side. A mistyped ROLLBACK fails visibly instead - the person sees
    the behaviour they were trying to turn off."""
    with _with_mode(mode):
        assert identity_for_engine(SCOPE, "tenant") == {
            "user_scope_id": SCOPE, "username": "tenant"}


@pytest.mark.parametrize("mode", ["declared", "DECLARED", " declared "])
def test_declared_passes_the_real_identity(mode):
    """Tolerant of case and padding, because a config value is typed by a human."""
    with _with_mode(mode):
        assert identity_for_engine(SCOPE, "tenant") == {
            "user_scope_id": SCOPE, "username": "tenant"}


def test_the_default_is_declared():
    """It shipped off for one release so the switch could be turned on deliberately, and it
    is on now. Leaving it off would have meant leaving the hole open by default: a saved
    workflow acting as the machine owner is not a preference, it is the bug."""
    from vaf.core.config import Config

    assert Config.DEFAULTS["workflow_identity_injection"] == "declared"


def test_the_resolver_does_not_shadow_the_default():
    """The default has to be the thing that decides, and that is not automatic.

    ``Config.get(key, fallback)`` prefers a NON-None fallback over ``Config.DEFAULTS``, so a
    resolver passing its own would shadow the shipped default and moving that default would be
    decorative. Asserted on the CALL rather than on the answer: an earlier version of this test
    moved DEFAULTS and watched the result, which passes or fails depending on whether a
    ``~/.vaf/config.json`` happens to exist - `Config.load()` merges the file OVER the defaults
    (`{**DEFAULTS, **data}`), so on a machine whose config carries the key, no patch of DEFAULTS
    can be seen. It passed here and failed on every CI runner."""
    from unittest.mock import patch

    from vaf.workflows.engine import _identity_mode

    seen = []
    with patch("vaf.core.config.Config.get",
               side_effect=lambda *a, **k: seen.append((a, k)) or "declared"):
        _identity_mode()
    assert seen == [(("workflow_identity_injection",), {})], (
        "the resolver passed its own fallback to Config.get, which shadows Config.DEFAULTS - "
        f"moving the shipped default would then change nothing. Called with: {seen}"
    )


def test_a_missing_value_everywhere_still_means_declared():
    """The other half: if nothing answers at all, the strict mode is what is left."""
    from unittest.mock import patch

    from vaf.workflows.engine import _identity_mode

    with patch("vaf.core.config.Config.get", return_value=None):
        assert _identity_mode() == "declared"


def test_an_unreadable_config_falls_back_to_declared():
    """The fail-safe direction moved with the default. "As before" is the state in which a
    workflow acts as the machine owner, so falling back to it would hand a non-admin's
    workflow the owner's files and tokens whenever the config could not be read. Falling
    back to declared costs nothing when identity resolution is failing too - unresolved
    identity is falsy, and a falsy scope takes the same no-jail exemption a direct caller
    does - and refuses to leak when it is not."""
    with patch("vaf.core.config.Config.get", side_effect=RuntimeError("config gone")):
        assert identity_for_engine(SCOPE, "tenant") == {
            "user_scope_id": SCOPE, "username": "tenant"}


# ── what it passes ───────────────────────────────────────────────────────────

def test_nothing_to_pass_means_nothing_is_passed():
    """An empty identity must not turn into username=None - the engine's own default
    ("admin") is the documented behaviour for a lane with no caller."""
    with _with_mode("declared"):
        assert identity_for_engine(None, None) == {}


def test_a_partial_identity_passes_only_what_exists():
    with _with_mode("declared"):
        assert identity_for_engine(SCOPE, None) == {"user_scope_id": SCOPE}
        assert identity_for_engine(None, "tenant") == {"username": "tenant"}


def test_the_session_is_consulted_when_no_scope_was_given(monkeypatch):
    """For the one consumer with no agent object at all - the workflow CLI subprocess - the
    identity comes from the session's own metadata, the way the engine already derives the
    project path."""
    session = SimpleNamespace(metadata={"user_scope_id": SCOPE, "username": "tenant"})
    monkeypatch.setattr("vaf.core.session.SessionManager",
                        lambda *a, **k: SimpleNamespace(load=lambda sid: session))
    with _with_mode("declared"):
        assert identity_for_engine(session_id="s1") == {
            "user_scope_id": SCOPE, "username": "tenant"}


def test_an_explicit_scope_is_not_overridden_by_the_session(monkeypatch):
    """A caller that knows who it is beats a lookup - the paused record and the live agent
    are more authoritative than session metadata that may have been rewritten."""
    session = SimpleNamespace(metadata={"user_scope_id": "ffffffff-0000-0000-0000-000000000000"})
    monkeypatch.setattr("vaf.core.session.SessionManager",
                        lambda *a, **k: SimpleNamespace(load=lambda sid: session))
    with _with_mode("declared"):
        assert identity_for_engine(SCOPE, "tenant")["user_scope_id"] == SCOPE


def test_an_unreadable_session_does_not_break_the_run(monkeypatch):
    """A workflow must not fail to start because a session file is missing."""
    monkeypatch.setattr("vaf.core.session.SessionManager",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no session")))
    with _with_mode("declared"):
        assert identity_for_engine(session_id="s1") == {}


# ── all four consumers actually ask ──────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "vaf/tools/workflow_executor.py",   # saved templates - the main lane
    "vaf/core/agent.py",                # the @workflow_id lane
    "vaf/cli/cmd/run.py",               # resume after a sub-agent
    "vaf/cli/cmd/workflow.py",          # the subprocess with no agent
])
def test_every_consumer_that_passed_nothing_now_resolves_an_identity(path):
    """The switch is worthless if a consumer never asks. Pinned per file so a failure names
    the lane that was forgotten."""
    from pathlib import Path

    src = Path(path).read_text(encoding="utf-8")
    assert "identity_for_engine(" in src, (
        f"{path} constructs a WorkflowEngine without resolving an identity - it will keep "
        f"running as the machine owner even with the switch on"
    )


def test_the_three_lanes_that_always_passed_one_are_untouched():
    """They must NOT go through the resolver: their identity is unconditional, and routing
    them through a switch would let 'off' silently demote them."""
    from pathlib import Path

    for path in ("vaf/tools/agent_workflow_builder.py", "vaf/core/automation.py",
                 "vaf/workflows/resume.py"):
        src = Path(path).read_text(encoding="utf-8")
        assert "identity_for_engine(" not in src, (
            f"{path} already passed an identity unconditionally; putting it behind the "
            f"rollout switch means 'off' would take it away"
        )
        assert "user_scope_id" in src


# ── the role, which the file jail cannot form without ────────────────────────

def test_the_role_travels_too():
    """Scope alone is not an identity for the file jail.

    ``is_admin_identity(role, scope)`` says yes for an admin ROLE or for the local admin's
    SCOPE. A second administrator has the role but not that scope, so dropping the role
    silently demotes them to a tenant inside their own workflows - the exact asymmetry that
    was fixed for the chat lane, reappearing one lane over. The engine has taken a
    ``user_role`` since the identity round; nothing filled it.

    Direction matters for how urgent this is: a missing role RESTRICTS (someone is jailed who
    should not be), it never frees. That is why it is a defect and not an incident."""
    out = identity_for_engine(SCOPE, "tenant", user_role="admin")
    assert out == {"user_scope_id": SCOPE, "username": "tenant", "user_role": "admin"}


def test_an_absent_role_is_absent_rather_than_guessed():
    """No role must never become a default role: "user" would jail a local admin and "admin"
    would free everyone."""
    assert "user_role" not in identity_for_engine(SCOPE, "tenant")


def test_the_lanes_with_a_role_in_reach_actually_pass_it():
    """The wiring, separately from the resolver. Three construction sites hold a live agent
    or a stored record and can therefore answer; a resolver that accepts a role nobody hands
    it is the same dead field this test exists to close."""
    import inspect

    import vaf.cli.cmd.run as run_cmd
    import vaf.core.agent as agent_mod
    import vaf.tools.workflow_executor as wf_exec

    for module, needle in (
        (wf_exec, 'user_role=getattr(_agent, "_current_user_role", None)'),
        (agent_mod, 'user_role=getattr(self, "_current_user_role", None)'),
        (run_cmd, 'user_role=getattr(paused_wf, "user_role", None)'),
    ):
        assert needle in inspect.getsource(module), (
            module.__name__ + " constructs a workflow engine without passing the caller's role"
        )
