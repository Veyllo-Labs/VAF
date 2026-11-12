# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A picture a tool takes to check its own work belongs to the step that took it.

`render_check` and `sandbox_preview` save a screenshot per look and announce it with
`file_created`. Without more, the browser could only file it as an attachment of the
answer, and a coder run that looks after every change left a column of them under it.
Two facts make the step view possible, and both are pinned here:

- the event names the STEP: `toolId`, the id of the tool call the agent loop is running
  (`tool_call_context`), carried into the coder child as `VAF_TOOL_CALL_ID`;
- the event says WHY the file exists: `purpose: "check"`, from a closed set, so a typo
  leaves the file an ordinary attachment instead of hiding it.

MUTATIONS, each turns the named test red:
- stamp no `toolId` in notify_file_created -> test_the_event_names_the_step;
- drop the VAF_TOOL_CALL_ID fallback -> test_the_event_names_the_step;
- skip the token reset in tool_call_context -> test_the_context_restores_on_every_exit;
- store the id without coercing "" to None -> test_an_empty_call_id_declares_no_step;
- send `purpose` unconditionally -> test_only_a_known_purpose_is_sent;
- drop tool_call_context from _execute_tool_and_spawns -> test_the_agent_declares_the_call_for_its_tool;
- drop the kwarg at one call site -> test_every_dispatch_passes_the_call_id;
- remove purpose="check" in render_check -> test_render_check_announces_a_check_picture;
- write VAF_TOOL_CALL_ID into os.environ in the coder spawn -> test_the_coder_child_gets_the_step;
- extra="ignore" on WorkflowUpdate -> test_the_subprocess_lane_keeps_both_fields.
"""
import inspect
import os
import re
import types
from pathlib import Path

import pytest

from vaf.core import subagent_ipc as ipc

_REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_ctx(monkeypatch):
    """Context variables are per thread and survive between tests; start each one untold."""
    monkeypatch.delenv("VAF_TOOL_CALL_ID", raising=False)
    monkeypatch.delenv("VAF_TURN_ID", raising=False)
    t1 = ipc._tool_call_ctx.set(ipc._UNSET)
    t2 = ipc._turn_ctx.set(ipc._UNSET)
    yield
    ipc._turn_ctx.reset(t2)
    ipc._tool_call_ctx.reset(t1)


@pytest.fixture
def sent(monkeypatch):
    from vaf.core import web_interface as wi

    out = []
    # The instance, not the class: the singleton may carry an instance attribute from a
    # neighbouring test that would shadow a class-level patch.
    w = wi.get_web_interface()
    monkeypatch.setattr(w, "_push_session_update", lambda sid, payload: out.append(payload))
    monkeypatch.setattr(w, "_server_loop", object(), raising=False)
    monkeypatch.setattr("vaf.core.session.record_created_file", lambda *a, **kw: None)
    return out


def test_the_event_names_the_step(sent, monkeypatch):
    from vaf.core import web_interface as wi

    with ipc.tool_call_context("call-1"):
        wi.notify_file_created("s1", "/tmp/a.png")
    assert sent[-1]["toolId"] == "call-1"

    wi.notify_file_created("s1", "/tmp/b.png")
    assert sent[-1]["toolId"] is None, "outside any call the file names no step"

    # The coder child: the step crosses the fork in the environment.
    monkeypatch.setenv("VAF_TOOL_CALL_ID", " call-env ")
    wi.notify_file_created("s1", "/tmp/c.png")
    assert sent[-1]["toolId"] == "call-env"

    # A told None is a declaration and beats the environment.
    with ipc.tool_call_context(None):
        wi.notify_file_created("s1", "/tmp/d.png")
    assert sent[-1]["toolId"] is None


def test_only_a_known_purpose_is_sent(sent):
    from vaf.core import web_interface as wi

    wi.notify_file_created("s1", "/tmp/a.png", purpose="check")
    assert sent[-1]["purpose"] == "check"
    wi.notify_file_created("s1", "/tmp/b.png")
    assert "purpose" not in sent[-1]
    wi.notify_file_created("s1", "/tmp/c.png", purpose="bogus")
    assert "purpose" not in sent[-1], "an unknown purpose must leave the file an attachment"


def test_the_subprocess_lane_sends_the_same_fields(monkeypatch):
    """Without a server loop the event is POSTed to /api/workflow/update: same dict."""
    from vaf.core import web_interface as wi

    w = wi.get_web_interface()
    monkeypatch.setattr(w, "_server_loop", None, raising=False)
    posted = []
    import requests
    monkeypatch.setattr(requests, "post", lambda url, json=None, timeout=None: posted.append(json))
    with ipc.tool_call_context("call-9"):
        wi.notify_file_created("s1", "/tmp/a.png", purpose="check")
    assert posted and posted[-1]["toolId"] == "call-9" and posted[-1]["purpose"] == "check"


def test_the_subprocess_lane_keeps_both_fields():
    from vaf.core.web_server import WorkflowUpdate

    data = WorkflowUpdate(type="file_created", sessionId="s1", filePath="/tmp/a.png",
                          purpose="check", toolId="c1").dict(exclude_none=True)
    assert data["purpose"] == "check" and data["toolId"] == "c1"


def test_the_context_restores_on_every_exit():
    assert ipc.get_current_tool_call_id() is None
    with ipc.tool_call_context("outer"):
        with ipc.tool_call_context("inner"):
            assert ipc.get_current_tool_call_id() == "inner"
        assert ipc.get_current_tool_call_id() == "outer"
        with pytest.raises(RuntimeError):
            with ipc.tool_call_context("boom"):
                raise RuntimeError("tool failed")
        assert ipc.get_current_tool_call_id() == "outer"
    assert ipc._tool_call_ctx.get() is ipc._UNSET, "never told must come back as never told"


def test_an_empty_call_id_declares_no_step(monkeypatch):
    """A provider that sends an empty tool-call id gives the step no id; that is a
    declaration of "no step", so a parent's id in the environment does not apply either."""
    monkeypatch.setenv("VAF_TOOL_CALL_ID", "from-parent")
    with ipc.tool_call_context(""):
        assert ipc.get_current_tool_call_id() is None
    assert ipc.get_current_tool_call_id() == "from-parent"


def test_the_agent_declares_the_call_for_its_tool():
    from vaf.core.agent import Agent

    seen = []
    stub = types.SimpleNamespace(
        _execute_tool_outcome=lambda name, args: seen.append(ipc.get_current_tool_call_id()) or "outcome")
    outcome, spawned = Agent._execute_tool_and_spawns(stub, "render_check", {}, tool_call_id="c7")
    assert seen == ["c7"] and outcome == "outcome" and spawned == frozenset()
    assert ipc.get_current_tool_call_id() is None, "the id must not outlive its call"


def test_every_dispatch_passes_the_call_id():
    src = (_REPO / "vaf" / "core" / "agent.py").read_bytes().decode("utf-8").replace("\r\n", "\n")
    starts = [m.end() for m in re.finditer(r"self\._execute_tool_and_spawns\(", src)]
    assert len(starts) == 3, starts
    for at in starts:
        call = src[at:src.index("\n", src.index("\n", at) + 1)]  # the call spans two lines
        assert "tool_call_id=tc.get('id')" in call, call


def test_render_check_announces_a_check_picture(monkeypatch, tmp_path):
    import vaf.core.session as session_mod
    import vaf.core.web_interface as wi
    from vaf.tools.render_check import RenderCheckTool

    monkeypatch.setattr(ipc, "get_current_session_id", lambda: "green123456")
    monkeypatch.setattr(session_mod, "get_session_workspace_dir",
                        lambda sid, create=False, **kw: tmp_path)
    calls = []
    monkeypatch.setattr(wi, "notify_file_created",
                        lambda sid, path, title=None, turn_id=None, purpose=None: calls.append(purpose))
    note = RenderCheckTool.__new__(RenderCheckTool)._save_screenshot("aGVsbG8=")
    assert note.startswith("Screenshot: ")
    assert calls == ["check"]


def test_the_coder_child_gets_the_step(monkeypatch):
    import vaf.core.config as cfg
    import vaf.core.subagent_spawn as spawn
    from vaf.tools import coder

    monkeypatch.delenv("VAF_IN_SUBAGENT_TERMINAL", raising=False)
    real_get = cfg.Config.get
    monkeypatch.setattr(cfg.Config, "get", classmethod(
        lambda cls, k, d=None: True if k == "sub_agents_in_separate_terminals" else real_get(k, d)))
    seen = {}

    def _spawn(kind, task, args=(), extra_env=None, **kw):
        seen["env"] = dict(extra_env or {})
        seen["parent"] = os.environ.get("VAF_TOOL_CALL_ID")
        return types.SimpleNamespace(marker="[SUBAGENT_ASYNC:t:coding_agent]")

    monkeypatch.setattr(spawn, "spawn_subagent", _spawn)
    with ipc.tool_call_context("call-coder"):
        coder.CodingAgentTool().run(task="build the page", project_path="/tmp/proj-y")
    assert seen["env"].get("VAF_TOOL_CALL_ID") == "call-coder"
    assert seen["parent"] is None and "VAF_TOOL_CALL_ID" not in os.environ, \
        "the parent's own environment must never carry a step id (Rule 4.5)"

    # No call current: nothing is set, the child's pictures go to the turn strip.
    seen.clear()
    coder.CodingAgentTool().run(task="build the page", project_path="/tmp/proj-y")
    assert "VAF_TOOL_CALL_ID" not in seen["env"]


def test_the_module_pins_one_purpose():
    from vaf.core.web_interface import FILE_PURPOSES

    assert FILE_PURPOSES == frozenset({"check"})
    assert "purpose" in inspect.signature(
        __import__("vaf.core.web_interface", fromlist=["x"]).notify_file_created).parameters
