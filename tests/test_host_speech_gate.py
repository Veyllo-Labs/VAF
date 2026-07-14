# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Host-speech gate: only an agent constructed with host_audio=True may play
audio on the host machine's speakers (final answer TTS, thinking fillers).

Every other lane (headless web/channel queue, automations, thinking runs,
gateway, vaf run -p, embedders) is fail-closed by the __init__ default.
Behavioral pins: a spy SpeechManager must never be reached without the flag.
"""
import inspect

import vaf.core.speech as speech_mod
from vaf.core.agent import Agent


class _SpySpeechManager:
    def __init__(self):
        self.speak_calls = 0

    def is_tts_enabled(self):
        return True

    def _clean_markdown(self, text):
        return text

    def speak(self, text, lang="auto"):
        self.speak_calls += 1


def _bare(host_audio: bool) -> Agent:
    a = Agent.__new__(Agent)
    a._host_audio_allowed = host_audio
    a.config = {"language": "de"}  # skip detection paths in _speak/_speak_filler
    return a


def test_init_default_is_fail_closed():
    assert inspect.signature(Agent.__init__).parameters["host_audio"].default is False


def test_default_agent_never_speaks(monkeypatch):
    spy = _SpySpeechManager()
    monkeypatch.setattr(speech_mod, "get_speech_manager", lambda: spy)
    a = _bare(host_audio=False)
    a._speak("hello there")
    assert spy.speak_calls == 0


def test_background_and_thinking_agents_never_speak(monkeypatch):
    spy = _SpySpeechManager()
    monkeypatch.setattr(speech_mod, "get_speech_manager", lambda: spy)
    for run_kind, background in (("automation", True), ("thinking", False)):
        a = _bare(host_audio=False)
        a._run_kind = run_kind
        a._background_run = background
        a._speak("hello there")
        a._speak_filler("thinking")
    assert spy.speak_calls == 0


def test_interactive_cli_agent_speaks(monkeypatch):
    spy = _SpySpeechManager()
    monkeypatch.setattr(speech_mod, "get_speech_manager", lambda: spy)
    a = _bare(host_audio=True)
    a._speak("hello there")
    assert spy.speak_calls == 1


def test_filler_gated_without_flag(monkeypatch):
    spy = _SpySpeechManager()
    monkeypatch.setattr(speech_mod, "get_speech_manager", lambda: spy)
    a = _bare(host_audio=False)
    a._speak_filler("thinking")
    assert spy.speak_calls == 0


def test_filler_speaks_with_flag(monkeypatch):
    spy = _SpySpeechManager()
    monkeypatch.setattr(speech_mod, "get_speech_manager", lambda: spy)
    a = _bare(host_audio=True)
    a._speak_filler("thinking")
    assert spy.speak_calls == 1
