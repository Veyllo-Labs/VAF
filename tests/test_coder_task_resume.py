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

from vaf.tools.coder import CodingAgentTool, TaskManager, _continuation_call, _nothing_to_resume

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

def test_resume_without_a_stored_plan_is_a_new_request(tmp_path):
    d = str(tmp_path)
    tm = TaskManager()
    tm.initialize(d, current_task="complete all remaining tasks", resume=True)
    assert tm.resumed is False
    assert tm.todos == []
    assert tm.state.task == "complete all remaining tasks"


def test_resume_of_a_finished_plan_is_a_new_request(tmp_path):
    """MUTATION: keep the plan for every resume, finished or not - red."""
    d = str(tmp_path)
    _plan(d, statuses=("completed", "completed"))
    tm = TaskManager()
    tm.initialize(d, current_task="complete all remaining tasks", resume=True)
    assert tm.resumed is False
    assert tm.todos == []
    assert tm.state.task == "complete all remaining tasks"


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


def test_a_content_only_run_has_nothing_to_resume(spawn_recorder, tmp_path):
    out = CodingAgentTool().run(task="CONTENT_ONLY automation: write a poem", resume=True,
                                project_path=str(tmp_path))
    assert out == "Error: a content-only run has no stored plan to resume"
    assert spawn_recorder == []


def test_the_resume_refusals_come_before_the_shortcut_and_the_spawn():
    """MUTATION: move the checks below the spawn block - red."""
    src = inspect.getsource(CodingAgentTool.run)
    gate = src.index("_project_path_refusal(kwargs.get(\"project_path\")")
    read = src.index('resume = kwargs.get("resume") is True')
    needs = src.index('if resume and not kwargs.get("project_path"):')
    content = src.index("if resume and _content_only_task(task):")
    assert gate < read < needs < content
    assert content < src.index("_detect_history_rollback_intent(task)")
    assert content < src.index("spawned = spawn_subagent(")


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


def test_vaf_subagent_run_maps_the_flag_for_the_coder():
    """MUTATION: remove the kwargs assignment - red."""
    fn = _function(REPO / "vaf" / "cli" / "cmd" / "subagent.py", "run_subagent")
    names = [a.arg for a in fn.args.args]
    default = fn.args.defaults[names.index("resume") - (len(names) - len(fn.args.defaults))]
    assert ast.unparse(default).startswith("typer.Option(False, '--resume'"), ast.unparse(default)
    branch = next(n for n in ast.walk(fn) if isinstance(n, ast.If)
                  and ast.unparse(n.test) == "agent_type == 'coding_agent'")
    sets = [n for n in ast.walk(branch) if isinstance(n, ast.If) and ast.unparse(n.test) == "resume"]
    assert len(sets) == 1
    assert [ast.unparse(s) for s in sets[0].body] == ["kwargs['resume'] = True"]
    call = next(n for n in ast.walk(branch) if isinstance(n, ast.Call)
                and ast.unparse(n.func) == "tool.run")
    assert call.lineno > sets[0].lineno


# ── AC7: the run continues the kept plan ────────────────────────────────────────────────

def _run_tree():
    return _function(REPO / "vaf" / "tools" / "coder.py", "run", cls="CodingAgentTool")


def _ifs(tree, test: str):
    return [n for n in ast.walk(tree) if isinstance(n, ast.If) and ast.unparse(n.test) == test]


def test_the_run_initializes_with_resume_and_answers_nothing_to_resume():
    """MUTATION: drop `resume=resume` - red. MUTATION: drop the answer - red."""
    tree = _run_tree()
    init = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and ast.unparse(n.func) == "task_mgr.initialize"]
    assert len(init) == 1
    assert ast.unparse(init[0]) == "task_mgr.initialize(base_dir, current_task=task, resume=resume)"
    nothing = _ifs(tree, "resume and (not task_mgr.resumed)")
    assert len(nothing) == 1 and nothing[0].lineno > init[0].lineno
    body = [ast.unparse(s) for s in nothing[0].body]
    assert body[-2:] == ["stop_live()", "return _nothing_to_resume(base_dir)"], body
    assert "animation_running.clear()" in body
    assert _nothing_to_resume("/p").startswith("Error: nothing to resume in '/p'")


def test_a_resume_names_its_own_folder_or_nothing():
    """A project_path the choice did not use, or one that does not exist, is refused before
    a folder is created or a repository started there.
    MUTATION: drop the check - red."""
    tree = _run_tree()
    early = _ifs(tree, "resume and (_choice.source != 'project_path' or not "
                       "os.path.isdir(_choice.base_dir))")
    assert len(early) == 1
    assert ast.unparse(early[0].body[-1]) == "return _nothing_to_resume(kwargs.get('project_path'))"
    makedirs = min(n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
                   and ast.unparse(n.func) == "os.makedirs")
    assert early[0].lineno < makedirs


def test_the_run_works_for_the_stored_request():
    """MUTATION: drop the replacement of `task` - red."""
    tree = _run_tree()
    keep = _ifs(tree, "task_mgr.resumed and _stored_request and (task.strip() != _stored_request)")
    assert len(keep) == 1
    assign = keep[0].body[0]
    assert ast.unparse(assign) == (
        "task = f'{_stored_request}\\n\\n[Continuation note from the caller]\\n{task}'")
    assert "tui.task = task" in [ast.unparse(s) for s in keep[0].body]


def test_a_kept_plan_skips_the_template_and_starts_in_its_open_task():
    """MUTATION: drop `_kept_plan_start is None` from the template guard - red.
    MUTATION: drop the start switch for a kept plan - red.
    MUTATION: switch to the literal task 0 again - red."""
    tree = _run_tree()
    kept = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
            and ast.unparse(n) == "_kept_plan_start = task_mgr.resume_point() if task_mgr.todos else None"]
    assert len(kept) == 1
    init = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and ast.unparse(n.func) == "task_mgr.initialize")
    assert kept[0].lineno > init.lineno

    template = [n for n in ast.walk(tree) if isinstance(n, ast.If)
                and "not _existing_code_files" in ast.unparse(n.test)
                and "not skip_template" in ast.unparse(n.test)
                and "_is_api_mode" in ast.unparse(n.test)]
    assert len(template) == 1
    assert "_kept_plan_start is None" in ast.unparse(template[0].test)
    assert kept[0].lineno < template[0].lineno

    start = _ifs(tree, "_kept_plan_start is not None and (not guided_mode)")
    assert len(start) == 1
    assert "guided_mode_skipped_planning = True" in [ast.unparse(s) for s in start[0].body]

    executor = _ifs(tree, "guided_mode_skipped_planning")
    assert len(executor) == 1
    switches = [ast.unparse(n.args[0]) for n in ast.walk(executor[0]) if isinstance(n, ast.Call)
                and ast.unparse(n.func) == "switch_to_task_context"]
    assert switches == ["task_mgr.current_task_idx"], switches


def test_a_resumed_start_names_the_tasks_that_failed_before():
    """The archived reasons reach the model once, in the fresh context of the first open task.
    MUTATION: drop the note - red."""
    tree = _run_tree()
    executor = _ifs(tree, "guided_mode_skipped_planning")[0]
    notes = [n for n in ast.walk(executor) if isinstance(n, ast.If)
             and ast.unparse(n.test) == "_resume_failed"]
    assert len(notes) == 1
    first = notes[0].body[0]
    assert ast.unparse(first.value.func) == "history.append"
    message = first.value.args[0]
    fields = {ast.literal_eval(k): v for k, v in zip(message.keys, message.values)}
    assert ast.literal_eval(fields["role"]) == "system"
    assert "failed in an earlier run" in ast.unparse(fields["content"])
    assert ast.unparse(notes[0].body[-1]) == "_resume_failed = []"
    taken = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
             and ast.unparse(n) == "_resume_failed = task_mgr.failed_tasks() if task_mgr.todos else []"]
    kept = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                and ast.unparse(n.targets[0]) == "_kept_plan_start")
    assert len(taken) == 1 and taken[0].lineno < kept.lineno   # read before the reset


def test_a_kept_plan_is_not_told_to_call_set_todos():
    """MUTATION: drop the kept-plan user message - red."""
    tree = _run_tree()
    msg = [n for n in ast.walk(tree) if isinstance(n, ast.If)
           and ast.unparse(n.test) == "guided_mode"
           and any(isinstance(o, ast.If) and ast.unparse(o.test) == "_kept_plan_start is not None"
                   for o in n.orelse)]
    assert len(msg) == 1
    branch = msg[0].orelse[0]
    text = "\n".join(ast.unparse(s) for s in branch.body)
    assert "do not call `set_todos`" in text
    assert "FIRST action MUST be to call `set_todos`" not in text


# ── AC8, AC9, AC11: the recommended call ────────────────────────────────────────────────

def test_the_answer_recommends_the_resume_call():
    """MUTATION: write the literal text again - red."""
    tree = _run_tree()
    built = [ast.unparse(n.value) for n in ast.walk(tree) if isinstance(n, ast.AugAssign)
             and ast.unparse(n.target) == "task_status"
             and ("coding_agent" in ast.unparse(n.value)
                  or "_continuation_call" in ast.unparse(n.value))]
    assert built == ["f'`{_continuation_call(base_dir)}`'"], built
    assert "resume=true" in _continuation_call("/p")
    # A content-only run's temporary folder holds no plan to come back to.
    gate = [n for n in ast.walk(tree) if isinstance(n, ast.If)
            and any(isinstance(s, ast.AugAssign) and "_continuation_call" in ast.unparse(s)
                    for s in n.body)]
    assert [ast.unparse(g.test) for g in gate] == ["(failed_list or remaining) and (not skip_template)"]


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
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and ast.unparse(n.func) == "_assign_declared_identity"
             and ast.unparse(n.args[0]) == "coding_tool"]
    assert len(calls) == 1
    args = {ast.literal_eval(k): v for k, v in zip(calls[0].args[1].keys, calls[0].args[1].values)}
    assert ast.literal_eval(args["resume"]) is True
    assert ast.literal_eval(args["task"]) == "Continue the open tasks of the stored plan."
    assert ast.unparse(args["project_path"]) == "project_path_hint"
    src = ast.get_source_segment(path.read_text(encoding="utf-8"), fn)
    assert r"Tasks:\s*" not in src
    assert "'○' in line" not in src
    assert "ALL TASKS COMPLETED" not in src
    done = _ifs(fn, "'[VAF_CODING_AGENT_STATUS: COMPLETE]' in continuation_result")
    assert len(done) == 1
    assert "_continuation_call(project_path_hint)" in src


# ── AC12: the schema ────────────────────────────────────────────────────────────────────

def test_the_schema_declares_resume_as_an_optional_boolean():
    """MUTATION: delete the property - red."""
    params = CodingAgentTool.parameters
    assert params["properties"]["resume"]["type"] == "boolean"
    assert "project_path" in params["properties"]["resume"]["description"]
    assert params["required"] == ["task"]


# ── Fuzz: the continuation call and the keep-or-replace decision ───────────────────────

_PATH_ALPHABET = (list("abcXYZ /:\\\"`\n\t") + [chr(c) for c in range(0x20)]
                  + ["ü", "€", "\u2028", "\U0001f600"])


def continuation_call_mismatches(seed: int, draws: int, make=None) -> list:
    """Paths that do not read back exactly from the recommended call."""
    make = make or _continuation_call
    rng = random.Random(seed)
    bad = []
    for _ in range(draws):
        path = "".join(rng.choice(_PATH_ALPHABET) for _ in range(rng.randint(0, 200)))
        text = make(path)
        found = re.search(r'project_path=("(?:[^"\\]|\\.)*")', text)
        try:
            back = json.loads(found.group(1)) if found else None
        except ValueError:
            back = None
        if (back != path
                or not text.startswith('coding_agent(task="complete all remaining tasks", '
                                       'project_path=')
                or not text.endswith(", resume=true)")):
            bad.append(path)
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
    """Draws where initialize() kept or replaced the plan against the decision table."""
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
        tm = TaskManager()
        tm.initialize(str(d), current_task=call, resume=resume)
        back = json.loads(_tasks_file(d).read_text(encoding="utf-8"))
        kept, resumed = _expected(stored, statuses, call, resume)
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
    # ...and the call fuzz a folder written between plain quotes.
    naive = lambda p: (f'coding_agent(task="complete all remaining tasks", '  # noqa: E731
                       f'project_path="{p}", resume=true)')
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
