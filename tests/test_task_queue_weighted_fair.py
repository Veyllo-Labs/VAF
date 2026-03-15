from vaf.core.task_queue import TaskQueue


def _fresh_queue() -> TaskQueue:
    # Reset singleton for deterministic tests.
    TaskQueue._instance = None  # type: ignore[attr-defined]
    return TaskQueue()


def test_weighted_fair_scheduler_serves_background_class() -> None:
    tq = _fresh_queue()
    tq._legacy_mode = False  # type: ignore[attr-defined]
    tq._class_weights = {  # type: ignore[attr-defined]
        tq.TASK_CLASS_INTERACTIVE: 5,
        tq.TASK_CLASS_AUTOMATION: 3,
        tq.TASK_CLASS_BACKGROUND: 1,
    }
    tq._reset_scheduler_budget_locked()  # type: ignore[attr-defined]

    # 6 interactive + 2 background, unique sessions (no in-flight blocking side effects).
    for i in range(6):
        tq.add(
            session_id=f"web-{i}",
            input_text="hello",
            source="web",
            metadata={"task_class": "interactive"},
        )
    for i in range(2):
        tq.add(
            session_id=f"bg-{i}",
            input_text="__CMD__:NOP",
            source="system",
            metadata={"task_class": "background"},
        )

    pulled = []
    for _ in range(6):
        task = tq.get(timeout=0.01, worker_id=f"w{_}")
        assert task is not None
        pulled.append(task.task_class)
        tq.task_done(task=task, worker_id=f"w{_}")

    # Weighted fairness must schedule at least one background task.
    assert "background" in pulled


def test_per_session_inflight_guard_blocks_second_task_same_session() -> None:
    tq = _fresh_queue()
    tq._legacy_mode = False  # type: ignore[attr-defined]
    tq._class_weights = {  # type: ignore[attr-defined]
        tq.TASK_CLASS_INTERACTIVE: 5,
        tq.TASK_CLASS_AUTOMATION: 3,
        tq.TASK_CLASS_BACKGROUND: 1,
    }
    tq._reset_scheduler_budget_locked()  # type: ignore[attr-defined]

    tq.add("same-session", "first", source="web", metadata={"task_class": "interactive"})
    tq.add("same-session", "second", source="web", metadata={"task_class": "interactive"})
    tq.add("other-session", "third", source="web", metadata={"task_class": "interactive"})

    first = tq.get(timeout=0.01, worker_id="w1")
    assert first is not None
    assert first.session_id == "same-session"

    # While first is in-flight, scheduler must not return another task from same session.
    second = tq.get(timeout=0.01, worker_id="w2")
    assert second is not None
    assert second.session_id == "other-session"

    tq.task_done(task=first, worker_id="w1")
    tq.task_done(task=second, worker_id="w2")

    # Now queued second task of same-session becomes runnable.
    third = tq.get(timeout=0.01, worker_id="w3")
    assert third is not None
    assert third.session_id == "same-session"
    tq.task_done(task=third, worker_id="w3")
