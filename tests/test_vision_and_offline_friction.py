# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""No setting, no detour: the agent can always look at an image, and the coder's shell says
what it cannot do before a build finds out.

Measured in a long build session: 162 of 168 file reads were the agent's own screenshots, looked
at in the middle of a task. Three frictions stood between VAF and that loop:

- the chat offered analyze_image only when the router picked it for the task, and the router
  picks tools for the task, not for checking a screenshot along the way;
- the coder resolved a relative image path against the CHAT's workspace, while every other
  file tool of the coder resolves against its project - its own screenshot was "not found";
- render_check named its screenshot by a bare file name that only the chat lane resolved;
- the coder's jailed shell advertised "npm install" while it has no network at all, so every
  dependency download failed there first, silently for the reason.

MUTATION: drop the analyze_image rider and the first test goes red; drop the coder's
image_path normalization and the source test goes red; drop the network hint in bash and its
test goes red.
"""
import json

import pytest

from vaf.core.platform import Platform


@pytest.fixture
def agent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    from vaf.core.agent import Agent
    a = Agent(register_signals=False, run_kind="chat",
              config_overrides={"provider": "openai", "api_key_openai": "sk-test"})
    a.current_session_id = "green123456"
    return a


def _turn(agent, monkeypatch, *, vision: bool):
    import vaf.core.vision_infer as vi
    monkeypatch.setattr(vi, "vision_available", lambda: vision)
    monkeypatch.setattr(agent, "_route_tools", lambda text: ["web_search"])

    def model(messages=None, **kw):
        yield "Fertig."

    agent.api_backend.chat_completion = model
    agent.chat_step(user_input="Bau die Seite und prüf sie", stream_callback=lambda t: None)
    return list(agent._active_tools or [])


def test_the_agent_can_always_look_when_something_can_see(agent, monkeypatch):
    assert "analyze_image" in _turn(agent, monkeypatch, vision=True)


def test_nothing_is_offered_that_could_only_refuse(agent, monkeypatch):
    assert "analyze_image" not in _turn(agent, monkeypatch, vision=False)


def test_the_coder_reads_its_own_images_from_its_project():
    import inspect
    import vaf.tools.coder as mod
    src = inspect.getsource(mod.CodingAgentTool.run)
    assert 'if fn_name == "analyze_image":' in src
    assert 'fn_args["image_path"] = os.path.join(base_dir, _ipath)' in src


def test_render_check_names_its_screenshot_by_its_full_path(monkeypatch, tmp_path):
    import vaf.core.session as session_mod
    import vaf.core.subagent_ipc as ipc
    from vaf.tools.render_check import RenderCheckTool
    monkeypatch.setattr(ipc, "get_current_session_id", lambda: "green123456")
    monkeypatch.setattr(session_mod, "get_session_workspace_dir",
                        lambda sid, create=False, **kw: tmp_path)
    tool = RenderCheckTool.__new__(RenderCheckTool)
    note = tool._save_screenshot("aGVsbG8=")
    shot = tmp_path / "render_check.jpg"
    assert shot.read_bytes() == b"hello"
    assert f"image_path='{shot}'" in note


def test_the_jailed_shell_says_it_has_no_network():
    from vaf.tools.bash import BashTool
    desc = BashTool.description
    assert "NO\nnetwork" in desc or "NO network" in desc
    assert 'bash(command="npm install")' not in desc
    assert "host_bash" in desc
    assert "timeout" in BashTool.parameters["properties"]


@pytest.mark.parametrize("output,hinted", [
    ("curl: (6) Could not resolve host: repo.maven.apache.org", True),
    ("[ERROR] Failed to execute goal: Could not transfer artifact org.papermc:paper-api", True),
    ("npm ERR! getaddrinfo EAI_AGAIN registry.npmjs.org", True),
    ("AssertionError: expected 3, got 4", False),
])
def test_a_download_that_failed_for_want_of_a_network_is_named(monkeypatch, tmp_path, output, hinted):
    import vaf.tools.workspace_exec as we
    from vaf.tools.bash import BashTool
    monkeypatch.setattr(we, "run_in_workspace", lambda ws, cmd, timeout=120: (1, "", output, "host-jail (bwrap)"))
    out = BashTool(base_dir=str(tmp_path)).run(command="./build.sh")
    assert ("has no network" in out) is hinted, out
    assert json.dumps(out)
