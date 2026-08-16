# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Reading one user's timeline out of the shared log.

Two things decide whether this feature is honest rather than merely useful:
a name that resolves to nobody must show NOTHING (the answer to "show me this
person" is never "here is everybody"), and the audit chain must keep describing
the whole file rather than the selection, or a filtered view would quietly
report a verified chain it never checked.

The scope itself never crosses the wire in either direction: the client sends a
username, the backend resolves it.
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import vaf.api.logs_routes as lr


ALICE = "aaaaaaaa-1111-2222-3333-444444444444"
BOB = "bbbbbbbb-1111-2222-3333-444444444444"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(lr, "get_app_log_dir", lambda: tmp_path)
    app = FastAPI()
    app.include_router(lr.router)
    app.dependency_overrides[lr.require_admin] = lambda: {"username": "admin", "role": "admin"}
    return TestClient(app)


def _write_day(tmp_path, date="2026-08-16"):
    """A day with two people's tool calls and one unattributed run."""
    rows = [
        {"type": "tool_start", "call_id": "1", "tool": "read_file", "scope": ALICE},
        {"type": "tool_end", "call_id": "1", "status": "ok", "scope": ALICE},
        {"type": "tool_start", "call_id": "2", "tool": "send_mail", "scope": BOB},
        {"type": "tool_end", "call_id": "2", "status": "ok", "scope": BOB},
        {"type": "tool_start", "call_id": "3", "tool": "cron_tick"},          # no scope
        {"type": "tool_end", "call_id": "3", "status": "ok"},
    ]
    path = tmp_path / f"timeline_{date}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def _names(monkeypatch, mapping):
    async def fake_map():
        return mapping

    import vaf.api.security_routes as sr
    monkeypatch.setattr(sr, "_scope_username_map", fake_map)


def test_without_a_user_everything_is_returned(client, tmp_path, monkeypatch):
    _write_day(tmp_path)
    _names(monkeypatch, {ALICE: "alice", BOB: "bob"})
    body = client.get("/api/logs/timeline/events?date=2026-08-16").json()
    assert len(body["events"]) == 3
    assert body["total_raw"] == 6


def test_a_user_sees_only_that_user_s_calls(client, tmp_path, monkeypatch):
    _write_day(tmp_path)
    _names(monkeypatch, {ALICE: "alice", BOB: "bob"})
    body = client.get("/api/logs/timeline/events?date=2026-08-16&user=alice").json()
    assert [e["tool"] for e in body["events"]] == ["read_file"]


def test_an_unknown_name_shows_nothing_not_everything(client, tmp_path, monkeypatch):
    """The failure that would matter: falling back to the unfiltered list would
    hand a whole machine's activity to a typo."""
    _write_day(tmp_path)
    _names(monkeypatch, {ALICE: "alice"})
    body = client.get("/api/logs/timeline/events?date=2026-08-16&user=nosuchuser").json()
    assert body["events"] == []


def test_an_empty_name_is_no_filter_at_all(client, tmp_path, monkeypatch):
    _write_day(tmp_path)
    _names(monkeypatch, {ALICE: "alice", BOB: "bob"})
    body = client.get("/api/logs/timeline/events?date=2026-08-16&user=").json()
    assert len(body["events"]) == 3


def test_the_chain_verdict_describes_the_file_not_the_selection(client, tmp_path, monkeypatch):
    """chain_ok and total_raw are the audit badge. Computed over a filtered
    subset they would claim a verification that never happened."""
    _write_day(tmp_path)
    _names(monkeypatch, {ALICE: "alice", BOB: "bob"})
    everyone = client.get("/api/logs/timeline/events?date=2026-08-16").json()
    just_alice = client.get("/api/logs/timeline/events?date=2026-08-16&user=alice").json()
    assert just_alice["total_raw"] == everyone["total_raw"] == 6
    assert just_alice["chain_ok"] == everyone["chain_ok"]


def test_unattributed_rows_are_counted_so_a_short_list_explains_itself(client, tmp_path, monkeypatch):
    _write_day(tmp_path)
    _names(monkeypatch, {ALICE: "alice", BOB: "bob"})
    body = client.get("/api/logs/timeline/events?date=2026-08-16&user=alice").json()
    assert body["unattributed"] == 1


def test_a_scope_id_is_not_accepted_as_a_user(client, tmp_path, monkeypatch):
    """The client never receives scope ids and must not be able to pass one:
    the filter resolves NAMES, so a raw UUID matches nobody."""
    _write_day(tmp_path)
    _names(monkeypatch, {ALICE: "alice"})
    body = client.get(f"/api/logs/timeline/events?date=2026-08-16&user={ALICE}").json()
    assert body["events"] == []
