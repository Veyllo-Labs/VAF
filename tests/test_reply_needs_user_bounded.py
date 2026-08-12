# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The ask-classifier cannot hold a finished turn hostage.

`_reply_needs_user` runs after the final answer is already on the user's screen and
before the turn closes - the stop button stays lit until it returns. It is one LLM call
for eight output tokens, and on the API lane it inherited the client's 600-second
streaming read timeout. Live incident 2026-08-10: the reply stood on screen at 10:56:20,
QUEUE_CHAT_END came at 11:02:03, and the 343 seconds in between produced not one log
line - the cause was only attributable by eliminating everything else in the window.

Two properties are pinned: a hanging backend falls back to the "?"-heuristic within the
bound instead of inheriting a ten-minute timeout, and the call is no longer silent (it
logs its verdict and duration, so the NEXT slow classifier is a log line, not a
forensics project).
"""
import threading
import time
from types import SimpleNamespace

from vaf.core.agent import Agent


class _HangingBackend:
    """chat_completion that blocks far longer than the classifier's bound."""

    def __init__(self, hang_s: float):
        self.hang_s = hang_s
        self.release = threading.Event()

    def chat_completion(self, **kwargs):
        # Wait, but wake immediately when the test tears down.
        self.release.wait(self.hang_s)
        return iter(["YES"])


def _agent_with(backend) -> SimpleNamespace:
    fake = SimpleNamespace(use_server=False, api_backend=backend, llm=None)
    fake._run_validation_llm = Agent._run_validation_llm.__get__(fake)
    fake._reply_needs_user = Agent._reply_needs_user.__get__(fake)
    return fake


def test_a_hanging_classifier_falls_back_to_the_heuristic(monkeypatch):
    """The bound, not the wall clock, decides: with the timeout mutated away this test
    would sit for the backend's full hang and fail on duration."""
    import vaf.core.agent as agent_mod

    backend = _HangingBackend(hang_s=120.0)
    fake = _agent_with(backend)
    # Shrink the bound for the test: patch the classifier's timeout via the shared
    # validation entry point (the production value is 12 s; the CONTRACT is bounded-ness).
    orig = Agent._run_validation_llm

    def fast_bound(self, messages, max_tokens=150, timeout_s=None):
        return orig(self, messages, max_tokens=max_tokens, timeout_s=0.3)

    monkeypatch.setattr(agent_mod.Agent, "_run_validation_llm", fast_bound)
    fake._run_validation_llm = agent_mod.Agent._run_validation_llm.__get__(fake)

    t0 = time.perf_counter()
    # Reply ends without "?" in the last line -> heuristic says False.
    verdict = fake._reply_needs_user("Alles erledigt. Die Datei liegt im Ordner.")
    took = time.perf_counter() - t0
    backend.release.set()

    assert verdict is False, "the heuristic verdict must stand in for the hung classifier"
    assert took < 5.0, (
        f"the classifier held the turn for {took:.1f}s - the timeout is gone and the "
        f"600-second client default is back in charge of the stop button"
    )


def test_the_heuristic_still_sees_a_real_question(monkeypatch):
    import vaf.core.agent as agent_mod

    backend = _HangingBackend(hang_s=120.0)
    fake = _agent_with(backend)
    orig = Agent._run_validation_llm

    def fast_bound(self, messages, max_tokens=150, timeout_s=None):
        return orig(self, messages, max_tokens=max_tokens, timeout_s=0.3)

    monkeypatch.setattr(agent_mod.Agent, "_run_validation_llm", fast_bound)
    fake._run_validation_llm = agent_mod.Agent._run_validation_llm.__get__(fake)

    verdict = fake._reply_needs_user("Ich kann das tun.\nSoll ich die Datei jetzt loeschen?")
    backend.release.set()
    assert verdict is True, "a trailing '?' must keep arming the ask-first latch"


def test_a_fast_classifier_answer_is_used_and_logged(monkeypatch):
    import vaf.core.agent as agent_mod

    class _Fast:
        def chat_completion(self, **kwargs):
            return iter(["NO"])

    lines = []
    monkeypatch.setattr(agent_mod, "append_domain_log",
                        lambda domain, msg: lines.append((domain, msg)))
    fake = _agent_with(_Fast())
    # A questioning last line, but the model says NO - the model outranks the heuristic.
    verdict = fake._reply_needs_user("Fertig. Brauchst du noch etwas?")
    assert verdict is False
    assert any("ASK_CLASSIFIER" in m for _, m in lines), (
        "the classifier ran silently again - the 343-second class of hang would once "
        "more be invisible in every log"
    )