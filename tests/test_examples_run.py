# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The one example that CI can actually run.

Six of the seven examples need a model backend, so they are verified by hand against a real
provider at release time and cannot be a CI gate. `07_tool_caller_and_authorizer.py` drives
the tool layer directly - no provider, no key, no network - which makes it the only one that
can rot silently, and therefore the only one worth pinning here.

It is worth pinning for a second reason: it is the smallest end-to-end statement of the
public surface. It builds a `BaseTool` subclass, declares `identity_kwargs`, runs it through
`ToolCaller`, attaches an authorizer, and reads the event stream - so if any of those names
or their behaviour move, this file says so before a reader finds out. The docs page it
belongs to (docs/EMBEDDING.md) makes exactly these promises in prose; this checks them by
running them.

Executed in a SUBPROCESS on purpose. The example writes to stdout and is meant to be run as a
script; importing it would test a different thing than the command a reader is told to type.
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "07_tool_caller_and_authorizer.py"


@pytest.fixture(scope="module")
def output():
    assert EXAMPLE.is_file(), "the no-backend example is gone"
    proc = subprocess.run([sys.executable, str(EXAMPLE)], cwd=ROOT,
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, (
        "the example a reader is told to run first failed:\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return proc.stdout


def test_a_declared_identity_reaches_the_tool(output):
    assert "alice -> buy milk" in output
    assert "bob   -> call the dentist" in output


def test_a_caller_cannot_claim_someone_elses_identity(output):
    """The escalation the declaration exists to close: arguments start out as whatever a
    model produced, so an identity in them is the attacker's own answer."""
    assert "bob, claiming to be alice -> call the dentist" in output


def test_policy_applies_to_a_third_party_tool(output):
    assert "user  -> Security Error:" in output
    assert "admin -> (the admin report)" in output


def test_a_gated_tool_returns_a_string_rather_than_blocking(output):
    """The embedder guarantee: no hang on a human who is not there."""
    assert "headless    -> [ERROR]" in output
    assert "requires confirmation" in output


def test_a_supplied_asker_can_let_the_gated_tool_through(output):
    assert "with a human-> (pretended to delete everything)" in output


def test_the_authorizer_can_refuse_by_identity(output):
    assert "bob, denied    -> Security Error: bob's plan does not include notes" in output


def test_ask_gates_a_tool_and_carries_its_own_reason(output):
    assert "ask() headless -> [ERROR]" in output
    assert "this cannot be undone" in output


def test_a_crashing_authorizer_refuses(output):
    """Fail-closed, the opposite polarity from the event sink."""
    assert "crashing guard -> Security Error:" in output
    assert "treated as a refusal" in output


def test_the_documented_event_pair_is_emitted(output):
    assert "tool_start   tool=tenant_notes" in output
    assert "tool_end     tool=tenant_notes ok=True" in output


def test_a_blocked_call_stays_silent(output):
    assert "a blocked call emitted 0 events" in output


def test_the_account_allowlist_blocks_a_scoped_user(output):
    assert "alice, unrestricted   -> buy milk" in output
    assert "bob, not on his plan  -> Security Error:" in output
    assert "not enabled for your account" in output


def test_the_account_ban_is_not_liftable_by_allow(output):
    """The rank promise from docs/EMBEDDING.md: the account stage sits before the
    authorizer, so a blanket allow() cannot lift what the backend revoked."""
    assert "allow() cannot lift it-> Security Error:" in output


def test_a_crashing_account_resolver_refuses(output):
    """Same polarity as the authorizer: a registered guard that crashed refuses."""
    assert "crashing resolver     -> Security Error:" in output
    assert "resolver failed" in output


def test_it_really_needs_no_backend(output):
    """If this example ever grows a model call it stops being CI-runnable, and the README
    tells readers it needs no provider."""
    src = EXAMPLE.read_bytes().decode()
    for forbidden in ("chat_step", "load_model", "api_key", "Agent("):
        assert forbidden not in src, f"the no-backend example now references {forbidden}"
