import sys
from unittest.mock import MagicMock

sys.modules.setdefault("llama_cpp", MagicMock())

from vaf.core.main_persistence import MainPersistenceManager  # noqa: E402


def _mgr(tmp_path):
    return MainPersistenceManager(str(tmp_path), session_id="t")


# ── Plan-without-tasks reminder: steps belong in tasks, so a plan with no tasks is flagged ─────────

# The dynamic per-turn reminder lines start with ">>"; the static instruction text also mentions
# "current step" and "plan but no tasks", so the tests anchor on the ">>" prefix to target the live line.
_PLAN_LINE = ">> You have a plan but no tasks"
_STEP_LINE = ">> CURRENT STEP"


def test_reminder_plan_without_tasks(tmp_path):
    m = _mgr(tmp_path)
    m.update_working_memory(plan=["my approach"])                  # a plan, but no tasks
    assert _PLAN_LINE in m.build_context_injection()


def test_reminder_silent_with_pending_task(tmp_path):
    m = _mgr(tmp_path)
    m.update_working_memory(plan=["my approach"], add_task="step one")   # plan + a tracked step
    ctx = m.build_context_injection()
    assert _STEP_LINE in ctx                                        # step reminder takes over
    assert _PLAN_LINE not in ctx                                    # the plan-without-tasks line is silent


def test_reminder_silent_without_plan(tmp_path):
    m = _mgr(tmp_path)
    ctx = m.build_context_injection()                              # plain chat: no plan, no tasks
    assert _PLAN_LINE not in ctx and _STEP_LINE not in ctx          # no nagging


def test_reminder_disabled_by_flag(tmp_path, monkeypatch):
    # Kill-switch off -> no plan-without-tasks reminder even with a plan and no tasks. (The step
    # reminder defaults to True via the passed-through default, which is fine here.)
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda k, d=None: False if k == "plan_without_tasks_reminder_enabled" else d
    ))
    m = _mgr(tmp_path)
    m.update_working_memory(plan=["my approach"])
    assert _PLAN_LINE not in m.build_context_injection()


# ── Task-overwrite guard: confirm-once before replacing pending steps ──────────────────────────────

def test_overwrite_guard_blocks_then_confirms(tmp_path):
    import vaf.tools.context_tools as ct
    ct._TASK_OVERWRITE_CONFIRM.clear()
    tool = ct.UpdateWorkingMemoryTool()
    base = str(tmp_path)

    tool.run(base_dir=base, add_task="in progress step")           # one pending task
    r1 = tool.run(base_dir=base, tasks=[{"text": "fresh list"}])   # replace while pending -> bounce
    assert "pending" in r1.lower() and "confirm" in r1.lower()

    # the old task survived the bounce (nothing was overwritten)
    survived = MainPersistenceManager(base, session_id=ct._current_session_id()).get_working_memory()
    assert any(t["text"] == "in progress step" for t in survived["tasks"])

    r2 = tool.run(base_dir=base, tasks=[{"text": "fresh list"}])   # re-call within window -> confirmed
    assert "updated" in r2.lower()
    after = MainPersistenceManager(base, session_id=ct._current_session_id()).get_working_memory()
    assert [t["text"] for t in after["tasks"]] == ["fresh list"]


def test_overwrite_guard_silent_when_no_pending(tmp_path):
    import vaf.tools.context_tools as ct
    ct._TASK_OVERWRITE_CONFIRM.clear()
    tool = ct.UpdateWorkingMemoryTool()
    base = str(tmp_path)

    tool.run(base_dir=base, add_task="done step")
    tool.run(base_dir=base, mark_task_done=0)                       # no pending left
    r = tool.run(base_dir=base, tasks=[{"text": "brand new"}])     # replacing with nothing pending
    assert "updated" in r.lower()                                   # no bounce
    after = MainPersistenceManager(base, session_id=ct._current_session_id()).get_working_memory()
    assert [t["text"] for t in after["tasks"]] == ["brand new"]


# ── Dedupe: a confused model re-adds the same note/plan/task many times (observed 5x) ───────────────

def test_add_task_dedupes_identical_text(tmp_path):
    m = _mgr(tmp_path)
    for _ in range(5):
        m.update_working_memory(add_task="Lesen des Dokuments")
    m.update_working_memory(add_task="  lesen   des   dokuments  ")   # whitespace/case variant
    tasks = m.get_working_memory()["tasks"]
    assert len(tasks) == 1


def test_add_notes_and_plan_dedupe(tmp_path):
    m = _mgr(tmp_path)
    for _ in range(3):
        m.update_working_memory(add_notes=["Doc opened: x.md"], add_plan=["Step A"])
    wm = m.get_working_memory()
    assert len(wm["notes"]) == 1
    assert len(wm["plan"]) == 1


def test_tasks_rebuild_dedupes_and_keeps_done(tmp_path):
    m = _mgr(tmp_path)
    m.update_working_memory(tasks=[
        {"text": "X", "status": "pending"},
        {"text": "X", "status": "done"},      # same text, done -> kept one is done
        {"text": "Y", "status": "pending"},
    ])
    tasks = {t["text"]: t["status"] for t in m.get_working_memory()["tasks"]}
    assert tasks == {"X": "done", "Y": "pending"}


# ── Bulk completion: "mark everything done" in one call ─────────────────────────────────────────────

def test_mark_all_done_marks_every_pending(tmp_path):
    m = _mgr(tmp_path)
    m.update_working_memory(add_task="a")
    m.update_working_memory(add_task="b")
    m.update_working_memory(add_task="c")
    m.update_working_memory(mark_task_done=0)        # one already done
    m.update_working_memory(mark_all_done=True)
    statuses = [t["status"] for t in m.get_working_memory()["tasks"]]
    assert statuses == ["done", "done", "done"]


def test_mark_all_done_tool_reports_count(tmp_path):
    import vaf.tools.context_tools as ct
    tool = ct.UpdateWorkingMemoryTool()
    base = str(tmp_path)
    tool.run(base_dir=base, add_task="a")
    tool.run(base_dir=base, add_task="b")
    r = tool.run(base_dir=base, mark_all_done=True)
    assert "2" in r and "done" in r.lower()


def test_tool_reports_duplicate_add_not_re_added(tmp_path):
    import vaf.tools.context_tools as ct
    tool = ct.UpdateWorkingMemoryTool()
    base = str(tmp_path)
    tool.run(base_dir=base, add_task="same step")
    r = tool.run(base_dir=base, add_task="same step")
    assert "already" in r.lower()
    after = MainPersistenceManager(base, session_id=ct._current_session_id()).get_working_memory()
    assert len(after["tasks"]) == 1
