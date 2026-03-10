from pathlib import Path

from vaf.core import thinking_workspace as tw
from vaf.core.thinking_mode import _history_delta


def _patch_data_dir(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(tw.Platform, "data_dir", staticmethod(lambda: tmp_path))


def test_workspace_isolated_by_scope(monkeypatch, tmp_path):
    _patch_data_dir(monkeypatch, tmp_path)
    t1 = tw.create_task("scope-a", title="A", source="test")
    t2 = tw.create_task("scope-b", title="B", source="test")
    assert t1["id"] != t2["id"]
    a_tasks = tw.list_tasks("scope-a")
    b_tasks = tw.list_tasks("scope-b")
    assert len(a_tasks) == 1
    assert len(b_tasks) == 1
    assert a_tasks[0]["title"] == "A"
    assert b_tasks[0]["title"] == "B"


def test_handoff_approve_reject_flow(monkeypatch, tmp_path):
    _patch_data_dir(monkeypatch, tmp_path)
    task = tw.create_task("scope-x", title="X", source="test")
    task_id = task["id"]

    tw.write_workspace_file("scope-x", task_id, "draft.md", "# Draft")
    handoff = tw.create_handoff("scope-x", task_id, title="Review", content="Please review", proposed_action="review")
    hid = handoff["id"]
    pending = tw.list_pending_handoffs("scope-x")
    assert any(h.get("id") == hid for h in pending)

    assert tw.approve_handoff("scope-x", task_id, hid) is True
    task_meta = tw.get_task("scope-x", task_id)
    assert task_meta is not None
    assert task_meta.get("status") == "approved"

    # Create a second handoff and reject it
    handoff2 = tw.create_handoff("scope-x", task_id, title="Review2", content="Please reject")
    hid2 = handoff2["id"]
    assert tw.reject_handoff("scope-x", task_id, hid2, reason="not needed") is True
    task_meta2 = tw.get_task("scope-x", task_id)
    assert task_meta2 is not None
    assert task_meta2.get("status") == "rejected"


def test_history_delta_only_new_entries():
    history = [
        {"role": "system", "content": "base"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old-reply"},
        {"role": "assistant", "content": "new-a"},
        {"role": "tool", "content": "new-tool"},
    ]
    delta = _history_delta(history, 3)
    assert len(delta) == 2
    assert delta[0]["content"] == "new-a"
    assert delta[1]["content"] == "new-tool"


def test_working_memory_mirror_snapshot(monkeypatch, tmp_path):
    _patch_data_dir(monkeypatch, tmp_path)
    snapshot = {
        "notes": [{"t": "2026-03-10T10:00:00.000Z", "text": "n1"}],
        "plan": [{"t": "2026-03-10T10:01:00.000Z", "text": "p1"}],
        "tasks": [{"text": "t1", "status": "pending", "ts": "2026-03-10T10:02:00.000Z"}],
    }
    task_id = tw.mirror_working_memory_snapshot("scope-sync", snapshot)
    assert task_id is not None
    latest = tw.read_workspace_file("scope-sync", task_id, "working_memory/latest.json")
    assert '"notes"' in latest
    assert '"plan"' in latest
    assert '"tasks"' in latest


def test_approve_handoff_triggers_automation_action(monkeypatch, tmp_path):
    _patch_data_dir(monkeypatch, tmp_path)
    task = tw.create_task("scope-act", title="Action task", source="test")
    h = tw.create_handoff(
        "scope-act",
        task["id"],
        title="Create automation",
        content="Please create it",
        automation_action={"operation": "create", "prompt": "Daily summary"},
    )
    called = {"ok": False}

    def _fake_apply(user_scope_id, action):
        called["ok"] = True
        return {"ok": True, "operation": "create", "task_id": "auto1234"}

    monkeypatch.setattr(tw, "_apply_automation_action", _fake_apply)
    assert tw.approve_handoff("scope-act", task["id"], h["id"]) is True
    assert called["ok"] is True
    stored = tw.get_handoff("scope-act", task["id"], h["id"])
    assert stored is not None
    assert stored.get("automation_action_result", {}).get("ok") is True


def test_sync_automation_status_to_workspace(monkeypatch, tmp_path):
    _patch_data_dir(monkeypatch, tmp_path)
    payload = {
        "id": "a1b2c3d4",
        "name": "Daily Check",
        "description": "desc",
        "frequency": "daily",
        "time": "08:00",
        "enabled": True,
        "last_run": None,
        "next_run": "2026-03-11T08:00:00",
    }
    task_id = tw.sync_automation_status_to_workspace(
        "scope-auto",
        payload,
        run_status="success",
        summary="ok",
        event="automation_run",
    )
    assert task_id is not None
    task = tw.get_task("scope-auto", task_id)
    assert task is not None
    assert task.get("automation", {}).get("id") == "a1b2c3d4"

