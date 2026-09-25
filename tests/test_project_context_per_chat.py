# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A chat's VAF.md comes from that chat's project folder, never from the server's directory.

The loader read `os.getcwd()`. In the terminal that is the person's own directory, which is
right. In the web server it is wherever `vaf start` ran: that folder's VAF.md went into every
chat of every person, a chat's own project never counted, the walk went up to the filesystem
root, and the section was then carried from turn to turn by whichever chat had built it.

Pinned here: the runner lane reads the folder the runner hands over for THIS chat and never
climbs above the person's own projects root; the terminal lane keeps its working directory;
the section is resolved again every turn, so the next chat does not inherit the last one's.

MUTATION: put back "preserve the PROJECT CONTEXT part of the old prompt" in chat_step and the
two-chats test goes red; drop `stop_at` in `_project_context_block` and the shared-root test
goes red; read `os.getcwd()` for the runner lane and the no-project test goes red.
"""
import json
from pathlib import Path

import pytest

from vaf.core.platform import Platform
from vaf.core.project_context import find_project_context_file


def test_the_walk_never_climbs_above_its_ceiling(tmp_path):
    (tmp_path / "VAF.md").write_text("above")
    inner = tmp_path / "home" / "proj"
    inner.mkdir(parents=True)
    assert find_project_context_file(inner) == tmp_path / "VAF.md"
    assert find_project_context_file(inner, stop_at=tmp_path / "home") is None
    (tmp_path / "home" / "VAF.md").write_text("mine")
    assert find_project_context_file(inner, stop_at=tmp_path / "home") == tmp_path / "home" / "VAF.md"
    assert find_project_context_file(tmp_path, stop_at=tmp_path / "home") is None, "outside it"


@pytest.fixture
def agent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.setattr(Platform, "documents_dir", staticmethod(lambda: tmp_path / "docs"))
    from vaf.core.agent import Agent
    a = Agent(register_signals=False, run_kind="chat",
              config_overrides={"provider": "openai", "api_key_openai": "sk-test"})
    a.init_chat()
    return a


def _project(root: Path, text: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "VAF.md").write_text(text, encoding="utf-8")
    return root


def test_the_runner_lane_reads_this_chats_project(agent, tmp_path):
    _project(tmp_path, "SERVER DIRECTORY")                     # where the server was started
    agent._current_chat_source = "web"
    agent._chat_project_dir = ""
    assert agent._project_context_block() == "", "no project, no VAF.md - not the server's"
    proj = _project(tmp_path / "docs" / "VAF_Projects" / "ab12cd34" / "chat1" / "site", "SITE RULES")
    agent._chat_project_dir = str(proj)
    block = agent._project_context_block()
    assert "SITE RULES" in block and "SERVER DIRECTORY" not in block


def test_the_runner_starts_as_a_runner_lane():
    """Its first prompt is built before any task sets a source: without this the server's own
    directory was read once, and a turn that does not rebuild the prompt kept it."""
    import inspect
    import vaf.core.headless_runner as hr
    src = inspect.getsource(hr)
    at = src.index('agent = Agent(verbose=False, register_signals=False, run_kind="chat")')
    first = src[at:at + 600]
    assert first.index('agent._current_chat_source = "web"') < first.index("agent.init_chat()")


def test_the_runner_lane_stops_at_the_persons_own_root(agent, tmp_path):
    root = tmp_path / "docs" / "VAF_Projects"
    _project(root, "EVERYONE")                                  # the shared projects root
    proj = root / "ab12cd34" / "chat1"
    proj.mkdir(parents=True)
    agent._current_chat_source = "telegram"
    agent._chat_project_dir = str(proj)
    assert agent._project_context_block() == ""
    _project(root / "ab12cd34", "MY OWN")
    assert "MY OWN" in agent._project_context_block()


def test_the_terminal_lane_keeps_its_working_directory(agent, tmp_path):
    _project(tmp_path, "MY REPO")
    agent._current_chat_source = None
    assert "MY REPO" in agent._project_context_block()


def test_a_session_switch_forgets_the_last_chats_folder(agent, tmp_path, monkeypatch):
    # The switch also points the process-wide session context at the new chat; that is not
    # this test's subject and must not outlive it.
    import vaf.core.subagent_ipc as ipc
    monkeypatch.setattr(ipc, "set_current_session_id", lambda sid: None)
    agent._chat_project_dir = str(tmp_path)
    agent.current_session_id = "green123456"
    agent.load_session_context("red654321")
    assert agent._chat_project_dir == ""


class _Model:
    def __init__(self):
        self.prompts = []

    def __call__(self, messages=None, **kw):
        if kw.get("tools"):
            self.prompts.append(str((messages or [{}])[0].get("content") or ""))
        yield "Fertig."


def test_the_next_chat_does_not_inherit_the_last_ones(agent, tmp_path):
    base = tmp_path / "docs" / "VAF_Projects" / "ab12cd34"
    alpha = _project(base / "chat-a", "ALPHA RULES")
    bravo = _project(base / "chat-b", "BRAVO RULES")
    model = _Model()
    agent.api_backend.chat_completion = model
    agent._current_chat_source = "web"
    agent._chat_project_dir = str(alpha)
    agent.chat_step(user_input="Hallo", stream_callback=lambda t: None)
    assert "ALPHA RULES" in agent.history[0]["content"]
    agent._chat_project_dir = str(bravo)
    agent.chat_step(user_input="Und jetzt?", stream_callback=lambda t: None)
    prompt = agent.history[0]["content"]
    assert "BRAVO RULES" in prompt and "ALPHA RULES" not in prompt
    assert json.dumps(model.prompts[-1]).count("PROJECT CONTEXT") == 1


def test_a_link_out_of_the_ceiling_is_not_read(tmp_path):
    """A VAF.md that is a link to someone else's file would be read for the person who made
    it. MUTATION: drop the resolved-path check in find_project_context_file - red."""
    import os
    secret = tmp_path / "owner" / "notes.md"
    secret.parent.mkdir()
    secret.write_text("OWNER ONLY")
    mine = tmp_path / "tenant" / "proj"
    mine.mkdir(parents=True)
    try:
        os.symlink(secret, mine / "VAF.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    assert find_project_context_file(mine, stop_at=tmp_path / "tenant") is None
    (mine / "own.md").write_text("mine")
    (mine / "VAF.md").unlink()
    os.symlink(mine / "own.md", mine / "VAF.md")
    assert find_project_context_file(mine, stop_at=tmp_path / "tenant") == mine / "VAF.md"


def test_the_file_read_is_the_file_checked(tmp_path, monkeypatch):
    """A link swapped between the check and the read must not be read: the loader checks the
    file it has OPEN. Simulated by an open that lands on a foreign file while the path still
    resolves inside the tree. MUTATION: check the path and not the open file - red."""
    import builtins
    from vaf.core.project_context import load_project_context
    foreign = tmp_path / "owner" / "notes.md"
    foreign.parent.mkdir()
    foreign.write_text("OWNER ONLY")
    mine = _project(tmp_path / "tenant" / "proj", "MINE")
    real_open = builtins.open

    def swapped(file, *a, **kw):
        if str(file).endswith("VAF.md"):
            return real_open(foreign, *a, **kw)
        return real_open(file, *a, **kw)

    assert load_project_context(mine, stop_at=tmp_path / "tenant").content == "MINE"
    monkeypatch.setattr(builtins, "open", swapped)
    assert load_project_context(mine, stop_at=tmp_path / "tenant") is None
