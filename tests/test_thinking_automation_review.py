# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The automation-review rung: once a user already has automations, improve one instead of adding another.

Three properties this rung has to hold, and all three are enforced in code rather than asked for in
the prompt:

1. It PROPOSES. A background pass that edits a user's schedule on its own judgment is exactly what the
   downstream confirmation gate exists to prevent, so the write tools are refused on this node.
2. It repeats itself on the AUTOMATION, not on the phrasing. An unfixed finding is still true next
   run; keying de-duplication on wording would re-send it every run, and since a run stops after one
   message that would starve every rung below it.
3. Its findings reach the evidence pool, so a proposal quoting one passes the existing grounded gate
   with no new gate of its own.
"""
import types

import vaf.core.thinking_mode as tm
from vaf.core.agent import Agent


def _obj(node):
    ns = types.SimpleNamespace(_run_kind="thinking", _thinking_node=node)
    ns._is_thinking_run = types.MethodType(Agent._is_thinking_run, ns)
    return ns


def _finding(task_id="a1", kind="never_completed", name="Wetter"):
    return {"key": f"{task_id}:{kind}", "kind": kind, "task_id": task_id,
            "task_name": name, "detail": "created 2026-07-01 and has never recorded a successful run"}


# ── 1. propose-only, enforced ─────────────────────────────────────────────────────────────

def test_write_tools_are_refused_on_the_review_node():
    o = _obj("automation_review")
    for tool in ("update_automation", "create_automation", "delete_automation"):
        blocked = Agent._thinking_node_mutation_block(o, tool)
        assert blocked, f"{tool} must not be callable from the review rung"
        assert blocked.startswith("[BLOCKED]")      # tool_result_is_error() keys on this lead
        assert "ask_user" in blocked                 # and it must say what to do instead


def test_ask_user_is_not_blocked_on_the_review_node():
    assert Agent._thinking_node_mutation_block(_obj("automation_review"), "ask_user") is None
    assert Agent._thinking_node_mutation_block(_obj("automation_review"), "thinking_done") is None


def test_the_forced_housekeeping_node_may_still_create_an_automation():
    """A user's own todo asking for an automation is created on the forced node. Blocking the write
    tools globally would break that, which is why the block is per node."""
    assert Agent._thinking_node_mutation_block(_obj("forced_item"), "create_automation") is None


def test_a_chat_turn_is_never_blocked():
    ns = types.SimpleNamespace(_run_kind="chat", _thinking_node="automation_review")
    ns._is_thinking_run = types.MethodType(Agent._is_thinking_run, ns)
    assert Agent._thinking_node_mutation_block(ns, "update_automation") is None


# ── 2. de-duplication is on the automation ────────────────────────────────────────────────

def test_a_finding_already_raised_is_dropped(monkeypatch):
    import vaf.core.thinking_requests as treq
    monkeypatch.setattr(treq, "list_requests", lambda scope, within_runs=None, current_run_seq=None: [
        {"question": "Deine Automation Wetter (a1) hat noch nie erfolgreich laufen koennen.",
         "proposed_action": "update automation a1: Zeit anpassen", "details": ""},
    ])
    kept = tm._drop_recently_raised("u1", [_finding("a1"), _finding("a2", name="Kalender")])
    assert [f["task_id"] for f in kept] == ["a2"]


def test_nothing_is_dropped_when_nothing_was_raised(monkeypatch):
    import vaf.core.thinking_requests as treq
    monkeypatch.setattr(treq, "list_requests", lambda scope, within_runs=None, current_run_seq=None: [])
    kept = tm._drop_recently_raised("u1", [_finding("a1"), _finding("a2")])
    assert len(kept) == 2


def test_dedup_fails_open_when_the_request_store_is_unreadable(monkeypatch):
    """Losing the history must not silence the rung - at worst it repeats itself once."""
    import vaf.core.thinking_requests as treq

    def boom(*a, **kw):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(treq, "list_requests", boom)
    assert len(tm._drop_recently_raised("u1", [_finding("a1")])) == 1


# ── 3. the findings are the evidence ──────────────────────────────────────────────────────

def test_digest_carries_name_id_and_detail():
    text = tm._build_automation_review_digest([_finding("a1"), _finding("a2", name="Kalender")])
    assert "a1" in text and "Wetter" in text and "never recorded a successful run" in text
    assert "Kalender" in text


def test_a_proposal_quoting_a_finding_passes_the_grounded_gate(monkeypatch, tmp_path):
    """No new gate: the digest goes into the same evidence pool a retrieved memory does, so the
    existing verbatim-quote backstop accepts a proposal built from it."""
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path))
    scope = "u-review"
    tm.clear_run_evidence(scope)
    tm.add_run_evidence(scope, tm._build_automation_review_digest([_finding("a1")]))
    tm.set_proactive_mode(scope, "grounded")
    pool = tm.get_run_evidence(scope)
    assert tm._evidence_grounded("has never recorded a successful run", pool, 24) is True
    # and an invented one does not
    assert tm._evidence_grounded("crashes every morning with a timeout", pool, 24) is False


# ── the prompt keeps the rung inside what the record supports ─────────────────────────────

def test_prompt_forbids_the_claims_the_record_cannot_carry():
    p = tm._PROMPT_AUTOMATION_REVIEW
    assert "may NOT say an automation failed" in p
    assert "propose" in p.lower()
    assert "Do NOT call update_automation" in p
    assert "thinking_done" in p          # it is allowed to raise nothing
