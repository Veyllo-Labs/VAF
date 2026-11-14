# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A workflow step whose tool refused or failed is a failed step, and content is never one.

Measured before the change, through `WorkflowEngine.execute` with the real workflow primitives
under a scratch home: a `write_file`, `read_file` or `move_file` step refused with `Access
denied: ...` / `Source Error: ...` was booked SUCCESS and the run reported success, although
nothing was written. The engine judged a step by its own hand list, which knew none of those
refusals and also matched "Task Failed" anywhere in the answer, so a page or file that merely
mentioned it failed the step. A successful answer that mentioned a context size was run three
times (a send step sent three times), a spawned helper that failed in the queue was a success
whenever its text did not open with an error word, and a resumed run booked the awaited step
SUCCESS without looking at its answer.

The engine now asks one shared rule, `vaf.core.tool_dispatch.tool_call_failed`, through
`_step_answer_failed` (content-carrying: the answer's opening only); `read_file` declares its
own `failure_openers`, which the coding loop reads too. No LLM and no network.

MUTATIONS, each turns the named test red:
- `tool_call_failed` without its `abort_kind` check, without `FUNNEL_FAILURE_OPENERS` in the
  openers branch, or ignoring its `content_carrying` argument -> test_tool_call_failed_rules;
- `ReadFileTool.failure_openers = ()` -> test_the_read_tool_declares_its_openers,
  test_content_never_fails_a_step (the "Failed attempts" read);
- either new `_ERROR_PREFIXES` entry removed -> test_coder_failure_reports_open_as_failures;
- the old hand list restored in `execute` -> test_refused_file_steps_fail_the_run,
  test_content_never_fails_a_step, test_sandbox_and_coder_failures_fail_the_step,
  test_the_engine_has_one_step_rule;
- `_step_answer_failed` without `content_carrying=True` -> test_content_never_fails_a_step;
- the failure branch without `not step.optional` -> test_optional_and_on_failure_keep_their_meaning;
- the context retry gate without `_step_answer_failed(...) and`
  -> test_a_success_mentioning_context_is_not_rerun;
- the exhausted context retry back to the old `continue` branch
  -> test_an_exhausted_context_failure_is_a_failed_step;
- `_await_subagent` returning `task.result or task.error` again -> test_await_subagent_keeps_the_childs_status,
  test_a_failed_child_is_a_failed_step;
- `resume_workflow` booking the awaited step SUCCESS unconditionally, or going on after a
  non-optional failure -> test_resume_books_a_failed_awaited_step.
"""
import inspect

import pytest

from vaf.workflows.engine import StepStatus, WorkflowEngine, WorkflowStep


class _Tool:
    """A step tool that answers with what `fn` returns and counts its calls."""

    def __init__(self, fn, **attrs):
        self._fn = fn
        self.calls = 0
        for k, v in attrs.items():
            setattr(self, k, v)

    def run(self, **kwargs):
        self.calls += 1
        return self._fn(**kwargs)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A scratch home (HOME, USERPROFILE, XDG), so the `.vaf` refusals are the test's own on
    every platform, and a fresh sub-agent queue inside it."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    for var, sub in (("XDG_DATA_HOME", ".local/share"), ("XDG_CONFIG_HOME", ".config"),
                     ("XDG_CACHE_HOME", ".cache"), ("XDG_STATE_HOME", ".local/state")):
        monkeypatch.setenv(var, str(h.joinpath(*sub.split("/"))))
    for var in ("VAF_USER_SCOPE_ID", "VAF_USER_ROLE", "VAF_IN_SUBAGENT_TERMINAL",
                "VAF_SESSION_ID", "VAF_PARENT_CWD", "VAF_IN_WORKFLOW"):
        monkeypatch.delenv(var, raising=False)
    (h / ".vaf").mkdir()
    (h / ".vaf" / "config.json").write_text("{}", encoding="utf-8")
    import vaf.tools.filesystem as fs
    fs._shared_room_roots_cache.clear()
    monkeypatch.setattr(fs, "_shared_room_roots", lambda scope: [])
    from vaf.core import subagent_ipc
    ipc = subagent_ipc.SubAgentIPC()
    monkeypatch.setattr(subagent_ipc, "get_ipc", lambda: ipc)
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "open_new_terminal", staticmethod(lambda *a, **k: True))
    monkeypatch.chdir(tmp_path)

    class Home:
        pass
    out = Home()
    out.path, out.ipc = h, ipc
    yield out
    fs._shared_room_roots_cache.clear()


def _engine(tools):
    return WorkflowEngine(tools=tools, callback=lambda *a, **k: None)


def _step(tool, output, args=None, **kw):
    return WorkflowStep(tool=tool, input_template="go", output_name=output,
                        args_template=args, **kw)


def _primitives():
    from vaf.workflows.tool_overlay import workflow_primitives
    return workflow_primitives()


# ── AC1: the one rule ─────────────────────────────────────────────────────────────────

def test_tool_call_failed_rules():
    from vaf.core.bounded_run import STOPPED_PREFIX, TIMEOUT_PREFIX
    from vaf.core.tool_dispatch import tool_call_failed

    class Plain:
        pass

    class Content:
        result_is_deliverable = True

    class Declares:
        failure_openers = ("Nope:",)

    for tool in (Plain(), Content(), Declares(), None):
        assert tool_call_failed(tool, f"{TIMEOUT_PREFIX} x") is True
        assert tool_call_failed(tool, f"{STOPPED_PREFIX} x") is True

    # A declaring tool: exactly its openers and the funnel's own.
    d = Declares()
    assert tool_call_failed(d, "Nope: refused") is True
    assert tool_call_failed(d, "Tool Error: boom") is True
    assert tool_call_failed(d, "Security Error: not here") is True
    assert tool_call_failed(d, "Failed attempts are listed below") is False
    assert tool_call_failed(d, "Error: not one of mine") is False

    # Otherwise the shared classifier, content-carrying as the caller says.
    body = "Summary\nthe tool failed twice before it worked"   # whole-content belt only
    assert tool_call_failed(Plain(), body) is True
    assert tool_call_failed(Plain(), body, content_carrying=True) is False
    assert tool_call_failed(Content(), body, content_carrying=False) is True
    # ... or as the tool declares through result_is_deliverable when the caller passes None.
    assert tool_call_failed(Content(), body) is False
    assert tool_call_failed(Plain(), "Access denied: /etc", content_carrying=True) is True


# ── AC2: the read tool declares its openers; the coder reads them ─────────────────────

def test_the_read_tool_declares_its_openers():
    from vaf.tools import coder
    from vaf.tools.base import BaseTool
    from vaf.tools.filesystem import ReadFileTool
    assert BaseTool.failure_openers == ()
    assert ReadFileTool.failure_openers == ("Access denied:", "Invalid path", "Error:",
                                            "Error reading ")
    assert not hasattr(coder, "_READ_FAILURE_OPENERS")
    assert "tool_call_failed(" in inspect.getsource(coder._loop_call_failed)
    tools = {"read_file": ReadFileTool()}
    assert coder._loop_call_failed(tools, "read_file", "Failed attempts so far: 3") is False
    assert coder._loop_call_failed(tools, "read_file", "Error reading PDF: bad") is True


# ── AC3: the coding agent's failure reports open as failures ──────────────────────────

def test_coder_failure_reports_open_as_failures():
    from vaf.core.context import tool_result_is_error
    repeated = "### ❌ Repeated Error Detected\n\nThe same error occurred 3 times in a row"
    failed = "[VAF_CODING_AGENT_STATUS: FAILED]\n\n### ❌ Task Failed\n\nNo files were created"
    for text in (repeated, failed):
        assert tool_result_is_error(text) is True
        assert tool_result_is_error(text, content_carrying=True) is True
    partial = ("[VAF_CODING_AGENT_STATUS: PARTIAL]\n\n### ⚠️ Task Partially Complete\n\n"
               "**Note**: Some tasks FAILED goal verification despite retries")
    complete = "[VAF_CODING_AGENT_STATUS: COMPLETE]\n\n### ✅ Task Completed\n\nAll done."
    assert tool_result_is_error(partial, content_carrying=True) is False
    assert tool_result_is_error(complete, content_carrying=True) is False


# ── AC4: a refused file step fails the run ────────────────────────────────────────────

def _one_step(tools, step):
    result = _engine(tools).execute([step], variables={})
    return result, result.steps[0]


@pytest.mark.parametrize("tool,args,refusal", [
    ("write_file", {"path": "~/.vaf/config.json", "content": "{\"x\": 1}"}, "Access denied"),
    ("read_file", {"path": "~/.vaf/config.json"}, "Access denied"),
    ("move_file", {"src": "~/.vaf/config.json", "dst": "moved.json"}, "Access denied"),
])
def test_refused_file_steps_fail_the_run(home, tool, args, refusal):
    result, step = _one_step(_primitives(), _step(tool, "out", args))
    assert step.status == StepStatus.FAILED, step.result
    assert result.success is False
    assert refusal in str(result.error), result.error
    assert (home.path / ".vaf" / "config.json").read_text(encoding="utf-8") == "{}"


# ── AC5: content never fails a step ───────────────────────────────────────────────────

def test_content_never_fails_a_step(home, tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "failed.md").write_text("Failed attempts so far: 3\nall retried\n", encoding="utf-8")
    (notes / "nightly.md").write_text("Nightly log\n\nThe nightly Task Failed at 02:00\n",
                                      encoding="utf-8")
    prims = _primitives()
    cases = [
        (prims, _step("read_file", "a", {"path": str(notes / "failed.md")})),
        (prims, _step("read_file", "b", {"path": str(notes / "nightly.md")})),
        ({"search": _Tool(lambda **kw: "Forum results\nTask Failed error in build 12, fixed")},
         _step("search", "c")),
        ({"search": _Tool(lambda **kw: "Forum results\n❌ marks the broken builds")},
         _step("search", "d")),
        (prims, _step("write_file", "e", {"path": "failed_tool_log.txt", "content": "ok"})),
    ]
    for tools, step in cases:
        result, done = _one_step(tools, step)
        assert done.status == StepStatus.SUCCESS, (step.output_name, done.result, done.error)
        assert result.success is True, (step.output_name, result.error)


# ── AC6: the sandbox's and the coder's failures ───────────────────────────────────────

def test_sandbox_and_coder_failures_fail_the_step(home):
    sandbox = _Tool(lambda **kw: "[ERROR] python_sandbox: the sandbox container did not start")
    coder = _Tool(lambda **kw: ("[VAF_CODING_AGENT_STATUS: FAILED]\n\n### ❌ Task Failed\n\n"
                                "No files were created in 4 loops."))
    for name, tool in (("python_sandbox", sandbox), ("coding_agent", coder)):
        result, step = _one_step({name: tool}, _step(name, "out"))
        assert step.status == StepStatus.FAILED, (name, step.result)
        assert result.success is False


# ── AC7: optional and on_failure keep their meaning ───────────────────────────────────

def test_optional_and_on_failure_keep_their_meaning(home):
    def _run(**first_kw):
        second = _Tool(lambda **kw: "two")
        tools = {"first": _Tool(lambda **kw: "Access denied: outside your own data"),
                 "second": second}
        steps = [_step("first", "a", **first_kw), _step("second", "b")]
        result = _engine(tools).execute(steps, variables={})
        return [s.status.value for s in result.steps], result.success, second.calls

    assert _run() == (["failed", "pending"], False, 0)
    assert _run(optional=True) == (["failed", "success"], False, 1)
    assert _run(on_failure="b") == (["skipped", "success"], True, 1)


# ── AC8: the context retry runs only on a failed answer ───────────────────────────────

def test_a_success_mentioning_context_is_not_rerun(home):
    send = _Tool(lambda **kw: "Message sent: the context size is 8k tokens, as you asked.")
    result, step = _one_step({"send_note": send}, _step("send_note", "out"))
    assert send.calls == 1
    assert step.status == StepStatus.SUCCESS
    assert result.success is True and result.error is None


def test_an_exhausted_context_failure_is_a_failed_step(home):
    tool = _Tool(lambda **kw: "Error: context length exceeded (400)")
    events = []
    engine = WorkflowEngine(tools={"summarize": tool},
                            callback=lambda kind, *a, **k: events.append(kind))
    result = engine.execute([_step("summarize", "out")], variables={})
    step = result.steps[0]
    assert tool.calls == 3
    assert step.status == StepStatus.FAILED
    assert result.success is False
    # Booked once, by the step outcome: the workflow panel gets one error event.
    assert events.count("error") == 1, events


# ── AC9: a child that failed in the queue is a failed step ────────────────────────────

class _Task:
    def __init__(self, status, result=None, error=None):
        self.status, self.result, self.error = status, result, error


def _await_with(monkeypatch, task):
    from vaf.core import subagent_ipc, subagent_spawn

    class _Ipc:
        def consume_result(self, task_id):
            return task

    monkeypatch.setattr(subagent_ipc, "get_ipc", lambda: _Ipc())
    monkeypatch.setattr(subagent_ipc, "mark_engine_owned", lambda task_id: None)
    monkeypatch.setattr(subagent_spawn, "delegated_task", lambda text, **kw: ("t1", "research_agent"))
    engine = _engine({})
    return engine._await_subagent("[SUBAGENT_ASYNC:t1:research_agent]", "research_agent",
                                  None, 5.0, 0.1)


def test_await_subagent_keeps_the_childs_status(monkeypatch):
    for status in ("failed", "timeout"):
        out = _await_with(monkeypatch, _Task(status, error="CRASH DETECTED: exit 137"))
        assert out.startswith("Error:"), out
        assert "CRASH DETECTED: exit 137" in out
    done = _await_with(monkeypatch, _Task("completed", result="the report"))
    assert done == "the report"


def test_a_failed_child_is_a_failed_step(home):
    def _step_fn(**kw):
        from vaf.core.subagent_spawn import spawn_subagent
        spawned = spawn_subagent("document_agent", "write it")
        home.ipc.fail_task(spawned.task_id, "CRASH DETECTED: the child exited")
        return spawned.marker

    after = _Tool(lambda **kw: "next")
    result = _engine({"document_agent": _Tool(_step_fn), "after": after}).execute(
        [_step("document_agent", "a"), _step("after", "b")], variables={},
        wait_for_subagents=True)
    assert result.paused is False
    assert result.steps[0].status == StepStatus.FAILED, result.steps[0].result
    assert "CRASH DETECTED" in str(result.error)
    assert result.success is False and after.calls == 0


# ── AC10: a resumed run judges the awaited step ───────────────────────────────────────

def _paused(home, *, optional=False):
    from vaf.core.subagent_ipc import PausedWorkflow

    def data(tool, out, status="pending", opt=False):
        return {"tool": tool, "input_template": "go", "output_name": out, "description": "",
                "optional": opt, "condition": None, "args_template": None,
                "on_success": None, "on_failure": None, "validate": False,
                "status": status, "result": None, "error": None, "duration": 0.0}

    record = PausedWorkflow(
        workflow_id="wf000001", waiting_for_task_id="t1", current_step_index=0,
        outputs={}, variables={}, workflow_name="resume check", created_at="2026-01-01T00:00:00",
        steps_data=[data("coding_agent", "code", opt=optional), data("next_step", "after")])
    home.ipc.pause_workflow(record)
    return record


def _resume(home, answer, *, optional=False):
    after = _Tool(lambda **kw: "after ran")
    engine = _engine({"coding_agent": _Tool(lambda **kw: "unused"), "next_step": after})
    result = engine.resume_workflow(_paused(home, optional=optional), answer)
    return result, after.calls


def test_resume_books_a_failed_awaited_step(home):
    failed = "### ❌ Repeated Error Detected\n\nThe same error occurred 3 times in a row"
    result, calls = _resume(home, failed)
    assert result.success is False
    assert str(result.error).startswith("Tool returned error message:"), result.error
    assert result.steps[0].status == StepStatus.FAILED
    assert calls == 0
    assert home.ipc.get_all_paused_workflows() == []

    result, calls = _resume(home, failed, optional=True)
    assert calls == 1
    assert result.steps[0].status == StepStatus.FAILED
    assert result.success is False

    done = "[VAF_CODING_AGENT_STATUS: COMPLETE]\n\n### ✅ Task Completed\n\nAll done."
    result, calls = _resume(home, done)
    assert calls == 1
    assert result.steps[0].status == StepStatus.SUCCESS
    assert result.success is True and result.error is None


# ── AC11: one rule in the engine ──────────────────────────────────────────────────────

def test_the_engine_has_one_step_rule():
    src = inspect.getsource(WorkflowEngine.execute)
    assert '"Task Failed" in' not in src
    assert 'startswith("### ❌")' not in src
    assert "is_error_result = (" not in src
    assert "is_error_result = _step_answer_failed(tool, result)" in src
    assert "is_context_error_in_result = _step_answer_failed(tool, result) and" in src
    assert "_step_answer_failed(" in inspect.getsource(WorkflowEngine.resume_workflow)
