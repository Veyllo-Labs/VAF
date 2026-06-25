# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Stage-3 brake of the pending-task auto-continue gate.

Before the agent auto-continues through its task list, it must NOT do so if its final reply is a
question that needs the user. The foreground Web UI has no tool signal for that (the question is
plain text), so Agent._reply_needs_user classifies the text with a tiny validation LLM and falls
back to a last-line "?" heuristic. These tests pin that decision logic in isolation; the inline gate
in chat_step just calls this method.
"""
import vaf.core.config as cfgmod
from vaf.core.agent import Agent


def _bare_agent(*, has_backend: bool) -> Agent:
    """An Agent shell without the heavy __init__ — only the attributes _reply_needs_user reads."""
    a = Agent.__new__(Agent)
    a.use_server = False
    a.api_backend = None
    a.llm = object() if has_backend else None
    return a


def test_empty_reply_never_needs_user():
    a = _bare_agent(has_backend=False)
    assert a._reply_needs_user("") is False
    assert a._reply_needs_user("   \n  ") is False


def test_no_backend_falls_back_to_last_line_heuristic():
    a = _bare_agent(has_backend=False)
    # Question on the last line -> needs the user
    assert a._reply_needs_user("Here is the plan.\nWhich file do you mean?") is True
    # A "?" only on an earlier line is ignored — the last line decides
    assert a._reply_needs_user("Should I continue?\nDone, moving on.") is False
    # No question at all -> safe to auto-continue
    assert a._reply_needs_user("Step done. Moving to the next one.") is False


def test_classifier_yes_brakes(monkeypatch):
    a = _bare_agent(has_backend=True)
    monkeypatch.setattr(a, "_run_validation_llm", lambda *x, **k: "YES")
    # A real question carrying no "?" — the heuristic would miss it, the classifier catches it
    assert a._reply_needs_user("Sag mir bitte, welche Datei gemeint ist.") is True


def test_classifier_no_continues(monkeypatch):
    a = _bare_agent(has_backend=True)
    monkeypatch.setattr(a, "_run_validation_llm", lambda *x, **k: "NO")
    # Rhetorical "?" — the classifier overrides the heuristic and lets it keep going
    assert a._reply_needs_user("Done. Pretty neat, right?") is False


def test_classifier_garbage_falls_back_to_heuristic(monkeypatch):
    a = _bare_agent(has_backend=True)
    monkeypatch.setattr(a, "_run_validation_llm", lambda *x, **k: "maybe-ish")
    assert a._reply_needs_user("All set.\nWhich one next?") is True   # heuristic: last line has ?
    assert a._reply_needs_user("All set. Next one now.") is False


def test_classifier_error_falls_back_to_heuristic(monkeypatch):
    a = _bare_agent(has_backend=True)

    def boom(*x, **k):
        raise RuntimeError("backend down")

    monkeypatch.setattr(a, "_run_validation_llm", boom)
    assert a._reply_needs_user("Okay.\nReady to proceed?") is True
    assert a._reply_needs_user("Okay. Proceeding now.") is False


def test_classifier_disabled_uses_heuristic_and_never_calls_llm(monkeypatch):
    def fake_get(cls, key, default=None):
        if key == "autocontinue_question_classifier_enabled":
            return False
        return default

    monkeypatch.setattr(cfgmod.Config, "get", classmethod(fake_get))
    a = _bare_agent(has_backend=True)
    calls = {"n": 0}

    def counting_llm(*x, **k):
        calls["n"] += 1
        return "YES"

    monkeypatch.setattr(a, "_run_validation_llm", counting_llm)
    assert a._reply_needs_user("Done.\nWhich file?") is True    # heuristic decides
    assert a._reply_needs_user("Done. Next.") is False
    assert calls["n"] == 0  # classifier must be skipped entirely when disabled
