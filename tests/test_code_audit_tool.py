# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The main agent's code_audit tool (vaf/tools/code_audit.py): it reads only what the
calling account may read, and it ends in a question, never in a change. The model is the
fake reviewer of test_code_audit.py.
"""
import shutil
import subprocess

import pytest

from test_code_audit import BUGGY, OFF_BY_ONE, _Model
from vaf.core import code_audit as ca
from vaf.tools.code_audit import CodeAuditTool

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

TENANT = "ab12cd34-0000-0000-0000-000000000000"


def _repo(path):
    path.mkdir(parents=True)
    for args in (["init", "-q"], ["config", "user.email", "t@example.org"],
                 ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    (path / "app.py").write_text("def total(items):\n    return sum(items)\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=path, check=True, capture_output=True)
    (path / "app.py").write_text(BUGGY, encoding="utf-8")
    return path


@pytest.fixture
def projects(tmp_path, monkeypatch):
    import vaf.tools.filesystem as fs
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    fs._shared_room_roots_cache.clear()
    root = tmp_path / "Documents" / "VAF_Projects"
    return {"own": _repo(root / "ab12cd34" / "shop"), "other": _repo(root / "ffff0000" / "bank")}


@pytest.fixture
def model(monkeypatch):
    holder = {"model": _Model([OFF_BY_ONE])}
    monkeypatch.setattr(ca, "ask_via_complete", lambda **kw: holder["model"])
    return holder


def _call(path, **extra):
    return CodeAuditTool().run(project_path=str(path), user_scope_id=TENANT, user_role="user",
                               **extra)


def test_another_accounts_project_is_refused_before_anything_is_read(projects, model):
    """MUTATION: drop the jail question - the other account's code goes to the provider."""
    out = _call(projects["other"])
    assert out.startswith("Error:") and "outside the folders" in out
    assert model["model"].seen == []


def test_findings_end_in_a_question_with_the_fix_prompt(projects, model):
    """The agent is told to ask before anything changes, and given the fix prompt to hand
    to coding_agent on a yes. MUTATION: return the report alone."""
    out = _call(projects["own"])
    assert "The loop skips the first item" in out
    assert "ASK whether the coding agent should fix them" in out
    assert f"project_path={str(projects['own'])!r}" in out
    assert ca.UNTRUSTED_PREAMBLE in out


def test_a_clean_change_asks_nothing(projects, model):
    model["model"] = _Model([])
    out = _call(projects["own"])
    assert ": COMPLETE (" in out and "0 verified finding(s)." in out and "ASK whether" not in out


def test_the_fix_prompt_reaches_the_agent_whole_and_bounded(projects, model):
    """The chat caps a tool result at 2,000 characters by cutting its middle, which would
    remove the question and the fix prompt. MUTATION: drop result_is_deliverable, or stop
    bounding the prompt."""
    from vaf.core.tool_dispatch import ToolCaller

    # Thirty findings in thirty places, far enough apart not to merge into one.
    (projects["own"] / "big.py").write_text(
        "".join(f"VALUE_{i} = {i}\n\n\n\n" for i in range(30)), encoding="utf-8")
    many = [dict(OFF_BY_ONE, file="big.py", title=f"Problem number {i}",
                 evidence=f"VALUE_{i} = {i}", explanation="x" * 1500) for i in range(30)]
    model["model"] = _Model(many)
    caller = ToolCaller({"code_audit": CodeAuditTool()}, user_scope_id=TENANT, user_role="user",
                        gate_enabled=False)
    out = caller.execute("code_audit", {"project_path": str(projects["own"])})
    assert "ASK whether the coding agent should fix them" in out
    assert "Output Truncated" not in out
    assert len(out) < 30_000 and "left out of this prompt" in out


def test_a_bad_scope_is_an_error(projects, model):
    assert _call(projects["own"], scope="everything").startswith("Error:")


def test_the_tool_never_changes_the_code(projects, model):
    _call(projects["own"])
    assert (projects["own"] / "app.py").read_text(encoding="utf-8") == BUGGY


@pytest.fixture
def router_agent(monkeypatch, tmp_path):
    """A chat agent whose router call fails, so the keyword matches are its answer."""
    from vaf.core.platform import Platform
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    from vaf.core.agent import Agent
    a = Agent(register_signals=False, run_kind="chat",
              config_overrides={"provider": "openai", "api_key_openai": "sk-test"})

    def no_router(*args, **kwargs):
        raise RuntimeError("router unavailable")

    a.api_backend.chat_completion = no_router
    return a


@pytest.mark.parametrize("words, hinted", [
    ("Mach bitte ein Code Review von meinem Projekt", True),
    ("Prüf den Code im Ordner shop", True),
    ("Zeig mir eine Vorschau, also ein preview, der Seite", False),
    ("Schick mir das Audit-Log von gestern", False),
    ("Überprüfe bitte meine Mails", False),
])
def test_the_router_hint_names_code_not_any_review(router_agent, words, hinted):
    """"review" sits inside "preview", "audit" inside "audit log". MUTATION: put the bare
    words back in the keyword list."""
    assert ("code_audit" in router_agent._route_tools(words)) is hinted

