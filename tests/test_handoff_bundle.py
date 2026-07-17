# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Handoff bundles: a background automation that hits a genuine blocker stores its FULL working context
as a per-scope bundle and raises ONE tracked question; the user's main agent loads the bundle on reply
and continues. Tests cover the store (create/load/isolation/expiry/status/sanitize), deliver_handoff (the
bundle + linked request + waiting state), the automation ask_user routing, the merge digest, and that a
bundle is only ever readable under the scope it was written for. Storage isolated to a tmp vaf_dir."""
from datetime import datetime, timedelta

import vaf.core.handoff_bundle as hb
from vaf.core.platform import Platform

SCOPE_A = "0a0b0c0d-0000-4000-8000-000000000002"   # e.g. user A
SCOPE_B = "ab12cd34-0000-4000-8000-000000000001"   # e.g. user B

_HIST = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "book a flight"},
    {"role": "assistant", "content": "searching flights", "tool_calls": [{"id": "x"}]},
    {"role": "tool", "name": "web_search", "content": "found flights to X, Y, Z"},
    {"role": "assistant", "content": "which destination?"},
]


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path))


def test_create_and_load_same_scope(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    b = hb.create(SCOPE_A, history=_HIST, summary="found flights", question="which destination?",
                  proposed_action="book it", session_id="sess1")
    assert b["status"] == "open" and b["id"] and len(b["history"]) == 5
    loaded = hb.load(SCOPE_A, b["id"])
    assert loaded and loaded["id"] == b["id"]
    assert loaded["question"] == "which destination?" and loaded["proposed_action"] == "book it"


def test_cross_scope_isolation(monkeypatch, tmp_path):
    """A bundle written for one user is NOT readable for another (different per-scope directory)."""
    _isolate(monkeypatch, tmp_path)
    b = hb.create(SCOPE_A, history=_HIST, summary="s", question="q")
    assert hb.load(SCOPE_A, b["id"]) is not None
    assert hb.load(SCOPE_B, b["id"]) is None


def test_status_update(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    b = hb.create(SCOPE_A, history=_HIST, summary="s", question="q")
    assert hb.update_status(SCOPE_A, b["id"], "resolved")["status"] == "resolved"
    assert hb.load(SCOPE_A, b["id"])["status"] == "resolved"
    assert hb.update_status(SCOPE_A, b["id"], "bogus") is None   # invalid status ignored


def test_expiry_and_cleanup(monkeypatch, tmp_path):
    """An expired bundle does not load and is dropped by the lazy cleanup."""
    _isolate(monkeypatch, tmp_path)
    p = hb._path(SCOPE_A, "deadbeef")
    hb._write_atomic(p, {
        "id": "deadbeef", "status": "open", "history": [],
        "created_at": (datetime.now() - timedelta(days=8)).isoformat(),
        "expires_at": (datetime.now() - timedelta(days=1)).isoformat(),
    })
    assert hb.load(SCOPE_A, "deadbeef") is None
    assert not p.exists()   # cleanup deleted it


def test_sanitize_history(monkeypatch, tmp_path):
    """A non-serializable content is coerced to str; non-dict messages are dropped — the atomic write
    can never be corrupted by a transient object on the live history."""
    _isolate(monkeypatch, tmp_path)
    weird = [{"role": "assistant", "content": {"nested": "obj"}},
             {"role": "tool", "name": "t", "content": "ok"},
             "notadict"]
    b = hb.create(SCOPE_A, history=weird, summary="s", question="q")
    loaded = hb.load(SCOPE_A, b["id"])
    assert len(loaded["history"]) == 2
    assert all(isinstance(m["content"], str) for m in loaded["history"])


def test_newest_n_cap(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    ids = [hb.create(SCOPE_A, history=[], summary=str(i), question="q")["id"] for i in range(hb._MAX_ENTRIES + 5)]
    remaining = {x["id"] for x in hb.list_bundles(SCOPE_A)}
    assert len(remaining) == hb._MAX_ENTRIES
    assert ids[0] not in remaining and ids[-1] in remaining   # oldest dropped, newest kept


def _stub_delivery(monkeypatch):
    """Stub the web-delivery side so deliver_handoff records + defers without a live session."""
    import vaf.core.thinking_mode as tm
    monkeypatch.setattr(tm, "_latest_web_session_id", lambda scope: None)
    monkeypatch.setattr(tm, "_main_agent_busy", lambda scope: True)   # defer -> no emit needed
    return tm


def test_deliver_handoff_creates_bundle_and_linked_request(monkeypatch, tmp_path):
    """deliver_handoff stores the bundle, records a tracked request carrying its bundle_id, and sets the
    waiting state — all under the same scope, so the main agent's reply pickup finds them."""
    _isolate(monkeypatch, tmp_path)
    tm = _stub_delivery(monkeypatch)
    import vaf.core.thinking_requests as tr

    req = hb.deliver_handoff(
        SCOPE_A, message="Which destination — X, Y or Z?", proposed_action="book the flight",
        details="found flights to X, Y, Z", history=_HIST, username="max",
    )
    assert req and req["bundle_id"]
    # the request is recorded under the same scope and links the bundle
    reqs = tr.list_requests(SCOPE_A, status="asked")
    assert len(reqs) == 1 and reqs[0]["bundle_id"] == req["bundle_id"]
    assert reqs[0]["question"] == "Which destination — X, Y or Z?"
    # the bundle holds the full history under the same scope
    bundle = hb.load(SCOPE_A, req["bundle_id"])
    assert bundle and len(bundle["history"]) == 5
    # waiting state points the main agent at the request
    waiting = tm.get_waiting_for_reply(SCOPE_A)
    assert waiting and waiting.get("request_id") == reqs[0]["id"]
    # isolation: another user sees neither the request nor the bundle
    assert tr.list_requests(SCOPE_B, status="asked") == []
    assert hb.load(SCOPE_B, req["bundle_id"]) is None


def test_ask_user_routes_to_handoff_in_automation(monkeypatch, tmp_path):
    """In an automation run, AskUserTool.run routes to deliver_handoff (NOT the thinking path), snapshots
    the injected agent's history into a bundle, and records the linked request."""
    _isolate(monkeypatch, tmp_path)
    _stub_delivery(monkeypatch)
    monkeypatch.setenv("VAF_IN_AUTOMATION", "1")
    import vaf.core.thinking_requests as tr

    class _FakeAgent:
        history = _HIST

    from vaf.tools.ask_user import AskUserTool
    out = AskUserTool().run(
        message="I found 3 flights — which destination?",
        proposed_action="book it",
        details="X, Y, Z",
        user_scope_id=SCOPE_A,
        username="max",
        _agent=_FakeAgent(),
    )
    reqs = tr.list_requests(SCOPE_A, status="asked")
    assert len(reqs) == 1 and reqs[0]["bundle_id"]
    assert "bundle" in out.lower()
    bundle = hb.load(SCOPE_A, reqs[0]["bundle_id"])
    assert bundle and len(bundle["history"]) == 5   # the agent history was captured


def test_render_handoff_bundle_digest_and_resolves(monkeypatch, tmp_path):
    """The main-agent merge helper renders a bounded digest (summary + recent steps) from the linked
    bundle and marks it resolved; a request without a bundle yields ''. Returns (digest, curated)."""
    _isolate(monkeypatch, tmp_path)
    from vaf.core.agent import Agent
    render = Agent._render_handoff_bundle.__get__(object())   # method does not use self attributes

    b = hb.create(SCOPE_A, history=_HIST, summary="found flights to X, Y, Z",
                  question="which?", proposed_action="book it")
    digest, curated = render(SCOPE_A, {"bundle_id": b["id"]})
    assert curated is True                               # genuine findings present
    assert "found flights to X, Y, Z" in digest          # the summary (curated findings)
    assert "web_search" in digest                        # a recent step from the history
    assert "system" not in digest.split("Recent steps")[-1]  # the bundle's own system turn is excluded
    assert hb.load(SCOPE_A, b["id"])["status"] == "resolved"  # marked resolved on pickup

    # no bundle linked -> empty (caller falls back to the normal reply path)
    assert render(SCOPE_A, {"bundle_id": None}) == ("", False)
    assert render(SCOPE_A, {}) == ("", False)
    # wrong scope -> empty (isolation)
    assert render(SCOPE_B, {"bundle_id": b["id"]}) == ("", False)


def test_render_uncurated_bundle_is_not_curated(monkeypatch, tmp_path):
    # Incident 2026-07-13: bundle 6cffd5e7 had summary=None and unrelated chat
    # history - it must never carry the automation-continuation framing.
    _isolate(monkeypatch, tmp_path)
    from vaf.core.agent import Agent
    render = Agent._render_handoff_bundle.__get__(object())
    b = hb.create(SCOPE_A, history=_HIST, summary="", question="check-back?")
    digest, curated = render(SCOPE_A, {"bundle_id": b["id"]})
    assert curated is False
    assert hb.load(SCOPE_A, b["id"])["status"] == "resolved"


def test_render_mislabeled_bundle_is_consumed_empty(monkeypatch, tmp_path):
    # Defense in depth: a bundle whose source is not 'automation' yields no
    # digest at all and is consumed so it cannot poison a later pickup.
    _isolate(monkeypatch, tmp_path)
    from vaf.core.agent import Agent
    render = Agent._render_handoff_bundle.__get__(object())
    b = hb.create(SCOPE_A, history=_HIST, summary="s", question="q", source="thinking")
    assert render(SCOPE_A, {"bundle_id": b["id"]}) == ("", False)
    assert hb.load(SCOPE_A, b["id"])["status"] == "resolved"


def test_reply_pickup_note_three_lanes():
    """The injected reply note: continuation is reply-CONDITIONAL and only for curated
    handoffs; uncurated/plain lanes never claim an automation must be continued, and
    every lane carries the decline + ambiguity guidance (incident: 'nein bitte nicht'
    triggered unconfirmed mutations under an unconditional CONTINUE imperative)."""
    from vaf.core.agent import Agent
    build = Agent._build_reply_pickup_note

    q = "ZIM-Skizze oder Website-Updates?"
    rich = build(q, " If they CLEARLY confirm, carry out this proposal now: prepare it.",
                 "What the background run worked out: findings", True, " facts")
    assert q in rich and "BACKGROUND" in rich
    assert "If they CLEARLY agree" in rich and "If they DECLINE, change NOTHING" in rich
    assert "ask ONE short confirming question" in rich
    assert "CONTINUE the task now" not in rich  # the old unconditional imperative is gone

    # incident replay: uncurated handoff digest is dropped -> plain lane, no automation framing
    uncurated = build(q, "", "garbage digest from an unrelated chat", False, " facts")
    assert q in uncurated
    assert "AUTOMATION" not in uncurated and "garbage digest" not in uncurated
    assert "If they DECLINE, change NOTHING" in uncurated

    plain = build(q, "", "", False, " facts")
    assert q in plain and "AUTOMATION" not in plain
    assert "ask ONE short confirming question" in plain
