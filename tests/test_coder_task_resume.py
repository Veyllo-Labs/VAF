# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A new coder request must plan fresh, not resume a leftover plan; a continuation resumes it.

TaskManager persists tasks.json to survive a crash mid-run (a genuine resume). But a leftover
INCOMPLETE plan from a previous, DIFFERENT request must not be picked up by the next request -
that made the coder "continue the old todos". initialize() stamps the request onto the state
and keeps the plan only for the SAME request, or when the caller asks for it by name with
`resume`.

The coder's answer with open tasks recommended a follow-up call whose task text differed from
the stored request, so following it replaced the plan and lost the original requirements
(measured: 3 tasks with 1 done became 0 tasks, and the stored request became the follow-up
text). The answer now names `coding_agent(..., resume=true)`, the main agent's automatic
continuation after a workflow passes `resume` in code, and the run that resumes starts in
the plan's first open task with the original request in every prompt.

The coder's run has no driver in the suite (it needs a streaming model, a TUI and a task
manager at once), so its wiring is pinned through the syntax tree, as in
`tests/test_tool_arguments_decode.py`. `vaf subagent run` is read the same way: driving it
through a CLI runner leaks the sub-agent environment into the test process.
"""
import ast
import inspect
import json
import random
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from vaf.core.persistence import PersistenceManager, ProjectState, Task
from vaf.tools.coder import (CodingAgentTool, TaskManager, _continuation_call,
                             _failed_tasks_note, _nothing_to_resume, _resume_requested)

REPO = Path(__file__).resolve().parent.parent
ORIGINAL = 'Build a "Todo" app with login.\nUse SQLite, one file per page.'


def _tasks_file(d) -> Path:
    return Path(d) / ".vaf" / "tasks.json"


def _plan(d, statuses=("completed", "failed", "pending"), request=ORIGINAL) -> TaskManager:
    """A stored plan for `request` whose tasks have the given statuses, in order."""
    tm = TaskManager(d)
    tm.initialize(d, current_task=request)
    tm.set_todos([f"write file{i}.html" for i in range(len(statuses))])
    for i, status in enumerate(statuses):
        tm.state.tasks[i].status = status
        if status == "failed":
            tm.state.tasks[i].result = f"goal not verified for file{i}"
    tm.pm.save_state(tm.state)
    return tm


# ── The old rule: a different request plans fresh, the same one resumes ──────────────────

def test_different_request_plans_fresh(tmp_path):
    d = str(tmp_path)
    tm = TaskManager(d)
    tm.initialize(d, current_task="Task A: build the login page")
    tm.set_todos(["step 1", "step 2"])          # an incomplete plan for Task A
    assert len(tm.todos) == 2

    tm2 = TaskManager(d)
    tm2.initialize(d, current_task="Task B: fix the footer")   # a DIFFERENT request
    assert len(tm2.todos) == 0                  # fresh: did NOT resume Task A's leftover plan
    assert tm2.resumed is False


def test_a_legacy_plan_without_a_stored_request_plans_fresh_for_a_request(tmp_path):
    """A state written before requests were stored names no request. Any request text now
    plans fresh, because a kept plan's run starts straight in its open task.
    MUTATION: restore `bool(stored)` in `new_request` - red (2 tasks kept)."""
    d = str(tmp_path)
    _tasks_file(d).parent.mkdir(parents=True)
    _tasks_file(d).write_text(json.dumps({
        "project_name": "p", "current_task_idx": 0,
        "tasks": [{"id": 1, "title": "a", "status": "pending"},
                  {"id": 2, "title": "b", "status": "pending"}]}), encoding="utf-8")
    tm = TaskManager(d)                         # the constructor's empty request keeps it
    assert len(tm.todos) == 2
    tm.initialize(d, current_task="fix the footer")
    assert len(tm.todos) == 0
    assert tm.state.task == "fix the footer"


def test_same_request_resumes(tmp_path):
    d = str(tmp_path)
    tm = TaskManager(d)
    tm.initialize(d, current_task="Task A: build the login page")
    tm.set_todos(["step 1", "step 2", "step 3"])   # incomplete plan for Task A

    tm2 = TaskManager(d)
    tm2.initialize(d, current_task="Task A: build the login page")   # SAME request (crash-resume)
    assert len(tm2.todos) == 3                  # resumed the incomplete plan


def test_whitespace_around_the_same_request_keeps_the_plan(tmp_path):
    """MUTATION: compare the request without strip - red (fresh plan)."""
    d = str(tmp_path)
    _plan(d)
    tm = TaskManager()
    tm.initialize(d, current_task=f"  {ORIGINAL}\r\n\t")
    assert len(tm.todos) == 3
    assert tm.resumed is False


# ── AC1: resume keeps the plan as stored ────────────────────────────────────────────────

@pytest.mark.parametrize("call_text", [
    "complete all remaining tasks",
    "",
    ORIGINAL,
    "Continue.\n" + "Line with details about the next step.\n" * 40,
])
def test_resume_keeps_the_stored_plan_byte_for_byte(tmp_path, call_text):
    """MUTATION: drop the resume branch - red (the text differs, 0 tasks), except for the
    original text. MUTATION: stamp the call's text under resume too - red (`task` changes)
    for the texts that differ."""
    d = str(tmp_path)
    _plan(d, statuses=("completed", "failed", "pending", "pending"))
    before = _tasks_file(d).read_bytes()
    tm = TaskManager()                          # the run builds its manager without a folder
    tm.initialize(d, current_task=call_text, resume=True)
    assert tm.resumed is True
    assert _tasks_file(d).read_bytes() == before
    stored = json.loads(before)
    assert [t["status"] for t in tm.todos] == ["completed", "failed", "pending", "pending"]
    assert tm.state.task == stored["task"] == ORIGINAL
    assert tm.current_task_idx == stored["current_task_idx"]


def test_resume_keeps_a_plan_that_stopped_before_its_todo_list(tmp_path):
    """A run stopped before set_todos leaves its request and no tasks: resume continues that
    request rather than stamping the continuation text over it."""
    d = str(tmp_path)
    TaskManager().initialize(d, current_task=ORIGINAL)
    tm = TaskManager()
    tm.initialize(d, current_task="complete all remaining tasks", resume=True)
    assert tm.resumed is True
    assert tm.state.task == ORIGINAL
    assert tm.todos == []


# ── AC3: nothing to resume ──────────────────────────────────────────────────────────────

def _listing(root: Path) -> dict:
    """Every entry under ``root`` with the bytes of each file, so any write shows."""
    return {p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None)
            for p in sorted(root.rglob("*"))}


def test_resume_without_a_stored_plan_writes_nothing(tmp_path):
    """A refused resume leaves no plan behind: an empty plan stamped with the continuation
    text would be continued by the same call a second time.
    MUTATION: drop the read-only check at the top of initialize - red (.vaf is made)."""
    (tmp_path / "notes.txt").write_text("mine")
    before = _listing(tmp_path)
    tm = TaskManager()
    tm.initialize(str(tmp_path), current_task="complete all remaining tasks", resume=True)
    assert tm.resumed is False
    assert tm.state is None and tm.todos == []
    assert _listing(tmp_path) == before


def test_resume_of_a_finished_plan_changes_nothing(tmp_path):
    """MUTATION: keep the plan for every resume, finished or not - red.
    MUTATION: let a resume of a finished plan replace it as a new request does - red."""
    d = str(tmp_path)
    _plan(d, statuses=("completed", "completed"))
    before = _listing(tmp_path)
    tm = TaskManager()
    tm.initialize(d, current_task="complete all remaining tasks", resume=True)
    assert tm.resumed is False
    assert tm.state is None
    assert _listing(tmp_path) == before


def test_a_reused_manager_does_not_keep_the_last_resume(tmp_path):
    """MUTATION: drop the `self.resumed = False` reset in initialize - red."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _plan(str(a))
    tm = TaskManager()
    tm.initialize(str(a), current_task="", resume=True)
    assert tm.resumed is True
    tm.initialize(str(b), current_task="build a page")
    assert tm.resumed is False


# ── AC4: where a kept plan continues ────────────────────────────────────────────────────

def test_resume_point_retries_failed_tasks_from_the_start_of_the_plan(tmp_path):
    """MUTATION: skip the reset of failed tasks - red (returns the pending index 2)."""
    d = str(tmp_path)
    tm = _plan(d, statuses=("completed", "failed", "pending"))
    tm.current_task_idx = 3                     # the cursor stood at the end
    assert tm.resume_point() == (1, "write file1.html")
    assert tm.todos[1]["status"] == "pending"
    assert tm.todos[1]["description"].startswith("Previous attempt: goal not verified")
    assert tm.current_task_idx == 1
    again = TaskManager(d)                      # persisted, not only in memory
    assert again.todos[1]["status"] == "pending" and again.current_task_idx == 1


def test_resume_point_counts_from_the_start_not_from_the_old_cursor(tmp_path):
    """MUTATION: search from the stored cursor instead of 0 - red (returns 2)."""
    d = str(tmp_path)
    tm = _plan(d, statuses=("pending", "completed", "pending"))
    tm.current_task_idx = 2
    assert tm.resume_point() == (0, "write file0.html")
    assert tm.current_task_idx == 0


def test_resume_point_without_tasks_or_open_tasks(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    tm = TaskManager(str(tmp_path / "a"))
    assert tm.resume_point() is None
    done = _plan(str(tmp_path / "b"), statuses=("completed", "skipped"))
    assert done.resume_point() is None


# ── AC5: refused before anything starts ─────────────────────────────────────────────────

@pytest.fixture
def spawn_recorder(monkeypatch):
    calls = []
    import vaf.core.subagent_spawn as spawn

    def _record(agent_type, task, **kw):
        calls.append((agent_type, task, kw))
        return SimpleNamespace(marker="[SUBAGENT_ASYNC:t1:coding_agent]")

    monkeypatch.setattr(spawn, "spawn_subagent", _record)
    for var in ("VAF_IN_SUBAGENT_TERMINAL", "VAF_USER_SCOPE_ID", "VAF_USER_ROLE"):
        monkeypatch.delenv(var, raising=False)
    from vaf.core.config import Config
    real_get = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: True if key == "sub_agents_in_separate_terminals"
        else real_get(key, default)))
    return calls


def test_resume_needs_a_project_path(spawn_recorder):
    """MUTATION: remove the check - red (the spawn is reached)."""
    out = CodingAgentTool().run(task="complete all remaining tasks", resume=True)
    assert out == "Error: resume needs project_path, the folder whose stored plan to continue."
    assert spawn_recorder == []


def test_resume_is_read_strictly(spawn_recorder):
    """The string "false" from a direct run() caller is not a resume.
    MUTATION: read it with bool(kwargs.get("resume")) - red (refused as a resume)."""
    out = CodingAgentTool().run(task="build a small page", resume="false")
    assert out.startswith("[SUBAGENT_ASYNC:"), out
    assert "--resume" not in spawn_recorder[0][2]["args"]


@pytest.mark.parametrize("value", ["true", "TRUE", " yes ", "on", "1"])
def test_resume_written_as_text_is_a_resume(spawn_recorder, tmp_path, value):
    """A model that writes the flag as text means it: read as no, the call would plan fresh
    and replace the plan it asked to keep.
    MUTATION: read only a real True (`is True`) - red."""
    out = CodingAgentTool().run(task="complete all remaining tasks", resume=value,
                                project_path=str(tmp_path))
    assert out.startswith("[SUBAGENT_ASYNC:"), out
    assert "--resume" in spawn_recorder[0][2]["args"]


@pytest.mark.parametrize("value,expected", [
    (True, True), ("true", True), ("Yes", True), ("ON", True), ("1", True),
    (False, False), ("false", False), ("0", False), ("no", False), ("off", False), ("", False),
    (None, False), (1, False), ("truthy", False), (["true"], False),
])
def test_the_resume_read(value, expected):
    """MUTATION: drop "on" from the accepted words - red."""
    assert _resume_requested(value) is expected


def test_a_content_only_run_has_nothing_to_resume(spawn_recorder, tmp_path):
    out = CodingAgentTool().run(task="CONTENT_ONLY automation: write a poem", resume=True,
                                project_path=str(tmp_path))
    assert out == "Error: a content-only run has no stored plan to resume"
    assert spawn_recorder == []


def test_the_resume_refusals_come_before_the_shortcut_and_the_spawn():
    """MUTATION: move the checks below the spawn block - red."""
    tree = _run_tree()
    gate = _first_call(tree, "_project_path_refusal")
    read = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
            and ast.unparse(n.targets[0]) == "resume" and _calls(n, "_resume_requested")]
    needs = [n for n in _ifs_naming(tree, "resume", "kwargs.get", "project_path")
             if isinstance(n.body[-1], ast.Return)]
    content = [n for n in _ifs_naming(tree, "resume", "_content_only_task")
               if isinstance(n.body[-1], ast.Return)]
    assert len(read) == 1 and len(needs) == 1 and len(content) == 1
    assert gate < read[0].lineno < needs[0].lineno
    assert read[0].lineno < content[0].lineno
    last = max(needs[0].lineno, content[0].lineno)
    assert last < _first_call(tree, "_detect_history_rollback_intent")
    assert last < _first_call(tree, "spawn_subagent")


# ── AC6: the flag crosses into the child ────────────────────────────────────────────────

def test_the_spawned_child_gets_resume_in_its_argv(spawn_recorder, tmp_path):
    """MUTATION: drop the argv append - red."""
    proj = tmp_path / "proj"
    proj.mkdir()
    out = CodingAgentTool().run(task="complete all remaining tasks", resume=True,
                                project_path=str(proj))
    assert out.startswith("[SUBAGENT_ASYNC:"), out
    args = spawn_recorder[0][2]["args"]
    assert "--resume" in args
    assert args[args.index("--project-path") + 1] == str(proj)

    spawn_recorder.clear()
    CodingAgentTool().run(task="build a small page", project_path=str(proj))
    assert "--resume" not in spawn_recorder[0][2]["args"]


def _function(path: Path, name: str, cls: str = None):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    scope = tree.body
    if cls:
        scope = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls).body
    return next(n for n in scope if isinstance(n, ast.FunctionDef) and n.name == name)


def _mentions(node) -> set:
    """The names, dotted attributes and string constants a node uses: what a structural test
    matches on, so that a reorder of a condition that behaves the same stays green."""
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(ast.unparse(n))
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.add(n.value)
    return out


def _ifs_naming(tree, *names):
    return [n for n in ast.walk(tree) if isinstance(n, ast.If) and set(names) <= _mentions(n.test)]


def _calls(node, name: str) -> list:
    return [n for n in ast.walk(node) if isinstance(n, ast.Call) and ast.unparse(n.func) == name]


def _first_call(tree, name: str) -> int:
    found = _calls(tree, name)
    assert found, f"no call of {name}"
    return min(n.lineno for n in found)


def _strings(node) -> list:
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def test_vaf_subagent_run_maps_the_flag_for_the_coder():
    """MUTATION: remove the kwargs assignment - red."""
    fn = _function(REPO / "vaf" / "cli" / "cmd" / "subagent.py", "run_subagent")
    names = [a.arg for a in fn.args.args]
    default = fn.args.defaults[names.index("resume") - (len(names) - len(fn.args.defaults))]
    assert ast.unparse(default.func) == "typer.Option"
    assert [ast.literal_eval(a) for a in default.args[:2]] == [False, "--resume"]
    branch = next(n for n in ast.walk(fn) if isinstance(n, ast.If)
                  and {"agent_type", "coding_agent"} <= _mentions(n.test))
    sets = [n for n in ast.walk(branch) if isinstance(n, ast.Assign)
            and ast.unparse(n.targets[0]) == "kwargs['resume']"]
    assert len(sets) == 1 and ast.literal_eval(sets[0].value) is True
    guard = [n for n in ast.walk(branch) if isinstance(n, ast.If) and sets[0] in n.body]
    assert len(guard) == 1 and _mentions(guard[0].test) == {"resume"}
    assert _first_call(branch, "tool.run") > sets[0].lineno


# ── AC7: the run continues the kept plan ────────────────────────────────────────────────

def _run_tree():
    return _function(REPO / "vaf" / "tools" / "coder.py", "run", cls="CodingAgentTool")


def _nested(tree, name: str):
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


def test_the_run_initializes_with_resume_and_answers_nothing_to_resume():
    """MUTATION: drop `resume=resume` - red. MUTATION: drop the answer - red."""
    tree = _run_tree()
    init = _calls(tree, "task_mgr.initialize")
    assert len(init) == 1
    assert {k.arg: ast.unparse(k.value) for k in init[0].keywords}.get("resume") == "resume"
    nothing = _ifs_naming(tree, "resume", "task_mgr.resumed")
    assert len(nothing) == 1 and nothing[0].lineno > init[0].lineno
    assert _calls(nothing[0], "stop_live") and _calls(nothing[0], "animation_running.clear")
    assert isinstance(nothing[0].body[-1], ast.Return)
    assert _calls(nothing[0].body[-1], "_nothing_to_resume")
    assert _nothing_to_resume("/p").startswith("Error: nothing to resume in '/p'")


def test_a_resume_is_decided_by_reading_before_anything_is_written():
    """A resume names its own folder, an existing one, with an unfinished plan stored in it,
    and that is decided before a folder is made, a repository started or the plan store
    opened.
    MUTATION: drop the stored-plan read from the early check - red (and the run test below)."""
    tree = _run_tree()
    early = _ifs_naming(tree, "resume", "_choice.source", "os.path.isdir",
                        "TaskManager.stored_unfinished_plan")
    assert len(early) == 1
    assert _calls(early[0].body[-1], "_nothing_to_resume")
    assert _calls(early[0], "stop_live")
    for later in ("os.makedirs", "self._ensure_git_repo", "task_mgr.initialize"):
        assert early[0].lineno < _first_call(tree, later), later


def _drive_run_to_the_folder(monkeypatch):
    """A child-mode run() that gets past the display and the health check without a server:
    the health answer is stubbed, every other request is refused, and no model is asked
    before the folder decision this drives to."""
    import vaf.tools.coder as coder
    for var in ("VAF_USER_SCOPE_ID", "VAF_USER_ROLE", "VAF_SESSION_ID", "VAF_PARENT_CWD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("VAF_IN_SUBAGENT_TERMINAL", "1")
    monkeypatch.setenv("VAF_PROVIDER", "local")
    import vaf.core.subagent_ipc as ipc
    monkeypatch.setattr(ipc, "get_current_session_id", lambda: None)

    def _get(url, *a, **k):
        if url.endswith("/health"):
            return SimpleNamespace(status_code=200)
        raise AssertionError(f"unexpected request to {url}")

    monkeypatch.setattr(coder.requests, "get", _get)
    monkeypatch.setattr(coder.requests, "post", _get)
    monkeypatch.setattr(CodingAgentTool, "_active_instance", None)


def test_a_refused_resume_leaves_the_folder_as_it_was(tmp_path, monkeypatch):
    """Twice in a row, on a folder with no plan and on a finished plan: the answer is "nothing
    to resume" both times and no file, folder, repository or plan store appears or changes.
    Before, the repository and an empty plan with the continuation text as its request were
    written, and the second identical call continued that empty plan.
    MUTATION: drop the stored-plan read from the early check - red."""
    _drive_run_to_the_folder(monkeypatch)
    no_plan = tmp_path / "no_plan"
    no_plan.mkdir()
    (no_plan / "notes.txt").write_text("mine")
    finished = tmp_path / "finished"
    finished.mkdir()
    _plan(str(finished), statuses=("completed", "completed"))
    for folder in (no_plan, finished):
        before = _listing(folder)
        for _ in range(2):
            monkeypatch.setattr(CodingAgentTool, "_active_instance", None)
            out = CodingAgentTool().run(task="Continue the open tasks of the stored plan.",
                                        project_path=str(folder), resume=True)
            assert out.startswith("Error: nothing to resume in "), out
            assert _listing(folder) == before, folder.name
    assert not (no_plan / ".git").exists() and not (no_plan / ".vaf").exists()


def test_the_run_works_for_the_stored_request():
    """MUTATION: drop the replacement of `task` - red."""
    tree = _run_tree()
    keep = _ifs_naming(tree, "task_mgr.resumed", "_stored_request")
    assert len(keep) == 1
    assign = [s for s in keep[0].body if isinstance(s, ast.Assign)
              and ast.unparse(s.targets[0]) == "task"]
    assert len(assign) == 1
    assert {"_stored_request", "task"} <= _mentions(assign[0].value)
    assert any("[Continuation note from the caller]" in s for s in _strings(assign[0].value))
    assert any(isinstance(s, ast.Assign) and ast.unparse(s.targets[0]) == "tui.task"
               for s in keep[0].body)


def test_a_kept_plan_skips_the_template_and_starts_in_its_open_task():
    """MUTATION: drop `_kept_plan_start is None` from the template guard - red.
    MUTATION: drop the start switch for a kept plan - red.
    MUTATION: switch to the literal task 0 again - red."""
    tree = _run_tree()
    kept = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
            and ast.unparse(n.targets[0]) == "_kept_plan_start"]
    assert len(kept) == 1 and _calls(kept[0], "task_mgr.resume_point")
    assert kept[0].lineno > _first_call(tree, "task_mgr.initialize")

    template = [n for n in _ifs_naming(tree, "skip_template", "_is_api_mode",
                                       "_existing_code_files", "_kept_plan_start")
                if _calls(n, "TemplateManager.detect_template_type_with_llm")]
    assert len(template) == 1
    assert kept[0].lineno < template[0].lineno

    start = [n for n in _ifs_naming(tree, "_kept_plan_start", "guided_mode")
             if any(isinstance(s, ast.Assign) and ast.unparse(s.targets[0])
                    == "guided_mode_skipped_planning" and ast.literal_eval(s.value) is True
                    for s in n.body)]
    assert len(start) == 1

    executor = [n for n in ast.walk(tree) if isinstance(n, ast.If)
                and _mentions(n.test) == {"guided_mode_skipped_planning"}]
    assert len(executor) == 1
    switches = [ast.unparse(c.args[0]) for c in _calls(executor[0], "switch_to_task_context")]
    assert switches == ["task_mgr.current_task_idx"], switches


def test_a_resumed_start_names_the_tasks_that_failed_before():
    """The archived reasons reach the model once, in the fresh context of the first open task.
    MUTATION: drop the note - red."""
    tree = _run_tree()
    executor = next(n for n in ast.walk(tree) if isinstance(n, ast.If)
                    and _mentions(n.test) == {"guided_mode_skipped_planning"})
    notes = [n for n in ast.walk(executor) if isinstance(n, ast.If)
             and _mentions(n.test) == {"_resume_failed"}]
    assert len(notes) == 1
    appended = _calls(notes[0], "history.append")
    assert len(appended) == 1 and _calls(appended[0], "_failed_tasks_note")
    taken = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
             and ast.unparse(n.targets[0]) == "_resume_failed" and _calls(n, "task_mgr.failed_tasks")]
    kept = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                and ast.unparse(n.targets[0]) == "_kept_plan_start")
    assert len(taken) == 1 and taken[0].lineno < kept.lineno   # read before the reset


def test_the_failed_task_note_is_bounded():
    """The note is sent with every request of the task, and the plan is project content.
    MUTATION: list every failed task - red. MUTATION: drop the title cut - red."""
    failed = [{"id": i + 1, "task": "t" * 1000, "result": "r" * 1000} for i in range(25)]
    note = _failed_tasks_note(failed)
    lines = note.splitlines()
    listed = [ln for ln in lines if ln.startswith("- Task ")]
    assert len(listed) == 10
    assert "- ... and 15 more" in lines
    assert all(len(ln) < 2 * 200 + 40 for ln in listed)
    assert _failed_tasks_note(failed[:2]).count("- Task ") == 2
    assert "more" not in _failed_tasks_note(failed[:10])


def test_a_kept_plan_is_not_told_to_call_set_todos():
    """MUTATION: drop the kept-plan user message - red."""
    tree = _run_tree()
    msg = [n for n in ast.walk(tree) if isinstance(n, ast.If)
           and _mentions(n.test) == {"guided_mode"}
           and any(isinstance(o, ast.If) and "_kept_plan_start" in _mentions(o.test)
                   for o in n.orelse)]
    assert len(msg) == 1
    text = " ".join(_strings(ast.Module(body=msg[0].orelse[0].body, type_ignores=[])))
    assert "do not call `set_todos`" in text
    assert "FIRST action MUST be to call `set_todos`" not in text


# ── AC8, AC9, AC11: the recommended call ────────────────────────────────────────────────

def test_the_answer_recommends_the_resume_call():
    """MUTATION: write the literal text again - red. MUTATION: drop the deploy target - red.
    MUTATION: drop the content-only gate - red."""
    tree = _run_tree()
    built = [n for n in ast.walk(tree) if isinstance(n, ast.AugAssign)
             and ast.unparse(n.target) == "task_status"
             and (_calls(n, "_continuation_call")
                  or any("coding_agent(" in s for s in _strings(n)))]
    assert len(built) == 1 and not any("coding_agent(" in s for s in _strings(built[0]))
    call = _calls(built[0], "_continuation_call")[0]
    assert [ast.unparse(a) for a in call.args] == ["base_dir", "_deploy"]
    assert "resume=true" in _continuation_call("/p")
    # A content-only run's temporary folder holds no plan to come back to.
    gate = [n for n in _ifs_naming(tree, "skip_template", "failed_list", "remaining")
            if built[0] in n.body]
    assert len(gate) == 1


def test_the_continue_note_appears_only_with_the_call():
    """"Continue with the call above" is said only where the call is shown: never for a
    content-only run, whose answer names no call.
    MUTATION: drop the content-only branch of the note - red."""
    tree = _run_tree()
    sentence = "Continue with the call above"
    holders = [n for n in ast.walk(tree) if isinstance(n, ast.If)
               and "skip_template" in _mentions(n.test)
               and any(isinstance(s, ast.Assign) and ast.unparse(s.targets[0]) == "completion_note"
                       for s in n.body)]
    assert len(holders) == 1
    assert not any(sentence in s for s in _strings(ast.Module(body=holders[0].body,
                                                              type_ignores=[])))
    assert any(sentence in s for s in _strings(ast.Module(body=holders[0].orelse,
                                                          type_ignores=[])))
    assert sum(sentence in s for s in _strings(tree)) == 1


@pytest.mark.parametrize("path", [
    '/home/user/a "b"/c', "C:\\Users\\user\\x y", "a\nb", "`tick`", "Übung", "''", "",
    "/p/\u2028x/\x00\x1f/\U0001f600",
])
def test_the_continuation_call_reads_back_the_exact_folder(path):
    """MUTATION: write the folder as f'"{p}"' instead of json.dumps - red."""
    text = _continuation_call(path)
    found = re.search(r'project_path=("(?:[^"\\]|\\.)*")', text)
    assert found and json.loads(found.group(1)) == path
    assert text.startswith('coding_agent(task="complete all remaining tasks", project_path=')
    assert text.endswith(", resume=true)")
    assert "deploy_to" not in text


@pytest.mark.parametrize("target", [
    "user@example.com:/srv/site", "ftps://user@example.com:2121/htdocs", 'a"b\\c:/x y',
])
def test_the_continuation_call_keeps_the_deploy_target(target):
    """A resumed run without the target reaches no server and refuses its deploy task.
    MUTATION: leave deploy_to out of the call - red. MUTATION: write it in plain quotes - red
    for the quoted row."""
    from vaf.tools.coder import DeployTarget
    text = _continuation_call("/p", target)
    found = re.search(r'deploy_to=("(?:[^"\\]|\\.)*")', text)
    assert found and json.loads(found.group(1)) == target
    assert text.endswith(", resume=true)")
    ssh = DeployTarget("user@example.com", "/srv/site")
    assert f'deploy_to="{ssh}"' in _continuation_call("/p", ssh)


def test_the_continuation_text_has_one_home():
    """MUTATION: a second hand copy in agent.py - red."""
    hits = []
    for py in (REPO / "vaf").rglob("*.py"):
        for no, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if "complete all remaining tasks" in line:
                hits.append((py.relative_to(REPO).as_posix(), no))
    assert len(hits) == 1, hits
    fn = _function(REPO / "vaf" / "tools" / "coder.py", "_continuation_call")
    assert hits[0][0] == "vaf/tools/coder.py" and fn.lineno <= hits[0][1] <= fn.end_lineno


# ── AC10: the main agent's continuation after a workflow ────────────────────────────────

def test_the_workflow_continuation_resumes_and_reads_the_status_signal():
    """MUTATION: drop `"resume": True` - red. MUTATION: restore "ALL TASKS COMPLETED" - red."""
    path = REPO / "vaf" / "core" / "agent.py"
    fn = _function(path, "_try_workflow", cls="Agent")
    calls = [c for c in _calls(fn, "_assign_declared_identity")
             if ast.unparse(c.args[0]) == "coding_tool"]
    assert len(calls) == 1
    args = {ast.literal_eval(k): v for k, v in zip(calls[0].args[1].keys, calls[0].args[1].values)}
    assert ast.literal_eval(args["resume"]) is True
    assert ast.literal_eval(args["task"]) == "Continue the open tasks of the stored plan."
    assert ast.unparse(args["project_path"]) == "project_path_hint"
    src = ast.get_source_segment(path.read_text(encoding="utf-8"), fn)
    assert r"Tasks:\s*" not in src
    assert "'○' in line" not in src
    assert "ALL TASKS COMPLETED" not in src
    done = _ifs_naming(fn, "[VAF_CODING_AGENT_STATUS: COMPLETE]", "continuation_result")
    assert len(done) == 1
    assert _calls(fn, "_continuation_call")


# ── AC12: the schema ────────────────────────────────────────────────────────────────────

def test_the_schema_declares_resume_as_an_optional_boolean():
    """MUTATION: delete the property - red."""
    params = CodingAgentTool.parameters
    assert params["properties"]["resume"]["type"] == "boolean"
    assert "project_path" in params["properties"]["resume"]["description"]
    assert params["required"] == ["task"]


# ── A plan of many failed tasks, and planted values at the data-model boundary ──────────

def test_resume_point_saves_the_plan_once(tmp_path, monkeypatch):
    """A save per failed task rewrote the whole file once per task (13.8 s for 1000).
    MUTATION: reset each task through reset_task_for_retry again - red (51 saves)."""
    tm = _plan(str(tmp_path), statuses=["failed"] * 50 + ["pending"])
    saves = []
    real = tm.pm.save_state
    monkeypatch.setattr(tm.pm, "save_state", lambda state: (saves.append(1), real(state)))
    assert tm.resume_point() == (0, "write file0.html")
    assert len(saves) == 1
    assert all(t["status"] == "pending" for t in TaskManager(str(tmp_path)).todos)


def test_a_planted_request_or_result_is_text_at_the_boundary(tmp_path):
    """A list as the stored request raised AttributeError out of the run; a dict as a task's
    result raised KeyError on the note's slice.
    MUTATION: drop ProjectState.__post_init__ - red. MUTATION: drop the Task text fields
    coercion - red."""
    assert ProjectState(project_name="p", task=["build", "it"]).task == "['build', 'it']"
    state = ProjectState.from_dict({"project_name": "p", "task": {"text": "build a page"},
                                    "tasks": [{"id": 1, "title": "a", "status": "failed",
                                               "result": {"why": "x"}, "description": 7}]})
    assert state.task == "build a page"
    assert isinstance(state.tasks[0].result, str) and state.tasks[0].description == "7"
    assert isinstance(Task(id=1, title="a", instructions=["x"]).instructions, str)
    _tasks_file(tmp_path).parent.mkdir(parents=True)
    _tasks_file(tmp_path).write_text(json.dumps({
        "project_name": "p", "task": ["planted"],
        "tasks": [{"id": 1, "title": "a", "status": "failed", "result": {"k": 1}}]}))
    tm = TaskManager()
    tm.initialize(str(tmp_path), current_task="complete all remaining tasks", resume=True)
    assert tm.resumed is True and tm.state.task == "['planted']"
    assert "Task 1" in _failed_tasks_note(tm.failed_tasks())


def test_peek_state_reads_without_writing(tmp_path):
    """MUTATION: read through a PersistenceManager instead - red (.vaf is made)."""
    assert PersistenceManager.peek_state(str(tmp_path)) is None
    assert list(tmp_path.iterdir()) == []
    _plan(str(tmp_path), statuses=("pending",))
    assert PersistenceManager.peek_state(str(tmp_path)).task == ORIGINAL


def test_peek_state_does_not_follow_a_linked_plan_store(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    _plan(str(other), statuses=("pending",))
    proj = tmp_path / "proj"
    proj.mkdir()
    try:
        (proj / ".vaf").symlink_to(other / ".vaf", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    assert PersistenceManager.peek_state(str(proj)) is None
    assert TaskManager.stored_unfinished_plan(str(proj)) is False


# ── A code audit fix task keeps its findings across a resume ────────────────────────────

def test_a_fix_task_stores_its_instructions(tmp_path):
    """A resumed audit fix task got back only its generic title; the findings lived in the
    stopped run's history.
    MUTATION: drop `instructions` from ProjectState.from_dict - red.
    MUTATION: clear the instructions in the retry reset - red."""
    d = str(tmp_path)
    tm = _plan(d, statuses=("completed",))
    idx = tm.append_task("Fix the code audit findings (round 1)", description="2 finding(s)",
                         instructions="CODE AUDIT ROUND 1 - fix a.py line 3")
    tm.state.tasks[idx].status = "failed"
    tm.pm.save_state(tm.state)
    again = TaskManager()
    again.initialize(d, current_task="complete all remaining tasks", resume=True)
    assert again.resume_point() == (idx, "Fix the code audit findings (round 1)")
    assert again.task_instructions(idx) == "CODE AUDIT ROUND 1 - fix a.py line 3"
    assert again.task_instructions(0) == "" and again.task_instructions(99) == ""


def test_every_fresh_task_context_carries_the_stored_instructions():
    """The fix task's first context, a retry's and a resumed run's all come from one place.
    MUTATION: drop the injection in switch_to_task_context - red.
    MUTATION: let the audit round append its findings to the history again instead of
    storing them - red."""
    tree = _run_tree()
    switch = _nested(tree, "switch_to_task_context")
    asked = _calls(switch, "task_mgr.task_instructions")
    assert len(asked) == 1 and ast.unparse(asked[0].args[0]) == "task_idx"
    fresh = _first_call(switch, "create_fresh_context_for_task")
    appended = _calls(switch, "task_hist.append")
    assert len(appended) == 1 and appended[0].lineno > fresh
    audit = _nested(tree, "_maybe_start_audit_round")
    added = _calls(audit, "task_mgr.append_task")
    assert len(added) == 1
    assert "instructions" in {k.arg for k in added[0].keywords}
    assert not _calls(audit, "history.append")


# ── Fuzz: the continuation call and the keep-or-replace decision ───────────────────────

_PATH_ALPHABET = (list("abcXYZ /:\\\"`\n\t") + [chr(c) for c in range(0x20)]
                  + ["ü", "€", "\u2028", "\U0001f600"])


def _read_back(name: str, text: str):
    found = re.search(name + r'=("(?:[^"\\]|\\.)*")', text)
    try:
        return json.loads(found.group(1)) if found else None
    except ValueError:
        return None


def continuation_call_mismatches(seed: int, draws: int, make=None) -> list:
    """Folders and deploy targets that do not read back exactly from the recommended call."""
    make = make or _continuation_call
    rng = random.Random(seed)
    bad = []
    for _ in range(draws):
        path = "".join(rng.choice(_PATH_ALPHABET) for _ in range(rng.randint(0, 200)))
        target = (None if rng.random() < 0.5 else
                  "".join(rng.choice(_PATH_ALPHABET) for _ in range(rng.randint(0, 120))))
        text = make(path) if target is None else make(path, target)
        ok = (_read_back("project_path", text) == path
              and text.startswith('coding_agent(task="complete all remaining tasks", '
                                  'project_path=')
              and text.endswith(", resume=true)"))
        if target is None:
            ok = ok and "deploy_to=" not in text
        else:
            ok = ok and _read_back("deploy_to", text) == target
        if not ok:
            bad.append((path, target))
    return bad


def test_fuzz_the_continuation_call_reads_back_every_path():
    assert continuation_call_mismatches(seed=377, draws=500) == []


_WORDS = ["build a page", "fix the footer", "Build a page"]
_SPACE = ["", " ", "\t", "\r\n", "\n  "]
_STATUSES = ["pending", "in_progress", "completed", "failed"]


def _decision_draw(rng):
    def text():
        return rng.choice(_SPACE) + rng.choice(_WORDS) + rng.choice(_SPACE)
    stored = rng.choice([None, "legacy", "", text(), text()])
    statuses = [rng.choice(_STATUSES) for _ in range(rng.randint(0, 4))]
    call = rng.choice(["", text(), text()])
    return stored, statuses, call, rng.random() < 0.5


def _expected(stored, statuses, call, resume):
    """The decision table: (kept, resumed)."""
    if stored is None:
        return False, False
    finished = bool(statuses) and all(s == "completed" for s in statuses)
    if finished:
        return False, False
    if resume:
        return True, True
    stored_text = "" if stored == "legacy" else stored.strip()
    cur = call.strip()
    return (cur == "" or (stored_text != "" and stored_text == cur)), False


def decision_mismatches(root: Path, seed: int, draws: int) -> list:
    """Draws where initialize() kept, replaced or left the plan against the decision table:
    kept as stored, replaced by a fresh state stamped with the call, or, for a resume with
    nothing to continue, left exactly as it was (no plan store made where there was none)."""
    rng = random.Random(seed)
    bad = []
    for n in range(draws):
        stored, statuses, call, resume = _decision_draw(rng)
        d = root / f"d{n}"
        d.mkdir()
        if stored is not None:
            data = {"project_name": "p", "current_task_idx": 0,
                    "tasks": [{"id": i + 1, "title": f"t{i}", "status": s}
                              for i, s in enumerate(statuses)]}
            if stored != "legacy":
                data["task"] = stored
            _tasks_file(d).parent.mkdir(parents=True)
            _tasks_file(d).write_text(json.dumps(data), encoding="utf-8")
        before = _listing(d)
        tm = TaskManager()
        tm.initialize(str(d), current_task=call, resume=resume)
        kept, resumed = _expected(stored, statuses, call, resume)
        if resume and not kept:
            ok = _listing(d) == before and tm.state is None
            back = None
        else:
            back = json.loads(_tasks_file(d).read_text(encoding="utf-8"))
            got_statuses = [t["status"] for t in back["tasks"]]
            if kept:
                ok = (got_statuses == statuses
                      and back.get("task", "") == ("" if stored == "legacy" else stored))
            else:
                ok = got_statuses == [] and back.get("task", "") == call.strip()
        if not ok or tm.resumed is not resumed:
            bad.append((stored, statuses, call, resume, back))
    return bad


def test_fuzz_the_keep_or_replace_decision(tmp_path):
    assert decision_mismatches(tmp_path, seed=377, draws=300) == []


def test_the_fuzz_oracles_see_a_broken_rule(tmp_path, monkeypatch):
    """The decision fuzz must notice the defect it guards: resume ignored."""
    real = TaskManager.initialize

    def without_resume(self, base_dir, current_task="", *, resume=False):
        return real(self, base_dir, current_task)

    monkeypatch.setattr(TaskManager, "initialize", without_resume)
    assert decision_mismatches(tmp_path, seed=377, draws=60) != []
    # ...and the call fuzz a value written between plain quotes.
    naive = lambda p, t=None: (  # noqa: E731
        f'coding_agent(task="complete all remaining tasks", project_path="{p}"'
        + (f', deploy_to="{t}"' if t is not None else "") + ", resume=true)")
    assert continuation_call_mismatches(seed=377, draws=60, make=naive) != []


if __name__ == "__main__":
    import sys
    import tempfile
    which, draws = sys.argv[1], int(sys.argv[2])
    seed = int(sys.argv[3]) if len(sys.argv) > 3 else 377
    if which == "call":
        bad = continuation_call_mismatches(seed, draws)
    else:
        with tempfile.TemporaryDirectory(prefix="F-377-fuzz-") as root:
            bad = decision_mismatches(Path(root), seed, draws)
    print(f"{which}: draws={draws} seed={seed} mismatches={len(bad)}")
    for row in bad[:5]:
        print(repr(row))
    sys.exit(1 if bad else 0)
