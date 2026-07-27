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

@pytest.mark.parametrize("mode", ["legacy", "off", "", None, "anything-else"])
def test_anything_but_declared_keeps_todays_behaviour(mode):
    """Default and rollback both mean: these consumers pass nothing, exactly as before."""
    with _with_mode(mode):
        assert identity_for_engine(SCOPE, "tenant") == {}


@pytest.mark.parametrize("mode", ["declared", "DECLARED", " declared "])
def test_declared_passes_the_real_identity(mode):
    """Tolerant of case and padding, because a config value is typed by a human."""
    with _with_mode(mode):
        assert identity_for_engine(SCOPE, "tenant") == {
            "user_scope_id": SCOPE, "username": "tenant"}


def test_the_default_is_off():
    """Shipping it on would flip behaviour for every existing install on update."""
    from vaf.core.config import Config

    assert Config.DEFAULTS["workflow_identity_injection"] == "legacy"


def test_an_unreadable_config_falls_back_to_legacy():
    """Fail-safe direction: if the setting cannot be read, behave as before rather than
    changing what a running workflow does."""
    with patch("vaf.core.config.Config.get", side_effect=RuntimeError("config gone")):
        assert identity_for_engine(SCOPE, "tenant") == {}


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
