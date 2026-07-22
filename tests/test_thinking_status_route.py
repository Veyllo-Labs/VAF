# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Background-agent status for the admin dashboard: the cross-user snapshot is
read-only (an expired waiting latch is skipped, never deleted), the route is
admin-gated by wiring, and the payload only carries whitelisted request fields
(replies/details stay in the chat surfaces)."""
import asyncio
import json
import time

import pytest

from vaf.core.platform import Platform


@pytest.fixture()
def iso_dirs(tmp_path, monkeypatch):
    data = tmp_path / "data"
    vaf = tmp_path / "vaf"
    data.mkdir()
    vaf.mkdir()
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: data))
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: vaf))
    return data, vaf


def test_snapshot_aggregates_all_scopes_read_only(iso_dirs):
    data, vaf = iso_dirs
    now = time.time()
    (data / "thinking_mode_locks.json").write_text(json.dumps({
        "default": {"run_id": "r9", "started_at_ts": now - 60},
    }))
    (data / "thinking_waiting_reply.json").write_text(json.dumps({
        "scope-b": {"question_text": "Backup einrichten?", "question_sent_at_ts": now - 300,
                    "channel": "telegram", "nudge_sent_at_ts": now - 100, "username": "bob"},
        "scope-old": {"question_text": "stale", "question_sent_at_ts": now - 100 * 3600,
                      "channel": "web", "username": "old"},
    }))
    (data / "thinking_last_completed.json").write_text(json.dumps({
        "scope-b": {"completed_at_ts": now - 3600},
    }))
    logdir = vaf / "thinking_mode_logs" / "scope-b"
    logdir.mkdir(parents=True)
    (logdir / "r1_20260722_120000.json").write_text(json.dumps({
        "run_id": "r1", "ended_at": "2026-07-22T12:05:00", "duration_seconds": 300.0,
        "messages": [{"role": "assistant", "content": "", "tool_calls": ["memory_search", "send_to_user"]}],
    }))

    from vaf.core.thinking_mode import thinking_status_snapshot
    snap = thinking_status_snapshot()

    assert snap["default"]["running"] is True
    b = snap["scope-b"]
    assert b["waiting"]["question"] == "Backup einrichten?"
    assert b["waiting"]["channel"] == "telegram" and b["waiting"]["nudged"] is True
    assert b["minutes_since_last_run"] == pytest.approx(60, abs=2)
    assert b["last_run"]["tools"] == ["memory_search", "send_to_user"]
    # expired latch: skipped in the result...
    assert snap["scope-old"]["waiting"] is None
    # ...but NOT deleted from disk (status probes must never mutate lifecycle state)
    on_disk = json.loads((data / "thinking_waiting_reply.json").read_text())
    assert "scope-old" in on_disk


def test_route_is_admin_gated_by_wiring():
    from vaf.api.thinking_routes import router
    from vaf.api.user_routes import require_admin
    route = next(r for r in router.routes if r.path == "/api/thinking/status")
    assert any(d.call is require_admin for d in route.dependant.dependencies)


def test_route_joins_requests_and_usernames_sanitized(iso_dirs, monkeypatch):
    _, vaf = iso_dirs
    from vaf.core import thinking_requests as tr
    tr.add_request("scope-b", "Soll ich Tests automatisieren?", proposed_action="secret plan",
                   details="internal notes", run_seq=1)
    tr.add_request(None, "Admin-Frage?", run_seq=1)

    async def fake_names():
        return {"scope-b": "bob"}
    import vaf.api.security_routes as sr
    monkeypatch.setattr(sr, "_scope_username_map", fake_names)

    from vaf.api.thinking_routes import thinking_status
    out = asyncio.run(thinking_status(_={"role": "admin"}))
    assert out["enabled"] in (True, False)
    by_name = {u["username"]: u for u in out["users"]}
    assert "bob" in by_name
    req = by_name["bob"]["requests"][0]
    assert req["question"] == "Soll ich Tests automatisieren?"
    assert req["status"] == "asked"
    # whitelist: internal fields never reach the dashboard payload
    assert "proposed_action" not in req and "details" not in req and "user_reply" not in req
    # the _default dir maps to the admin user, not a phantom scope
    admin_rows = [u for u in out["users"] if u["scope"] == ""]
    assert len(admin_rows) == 1 and admin_rows[0]["requests"][0]["question"] == "Admin-Frage?"
