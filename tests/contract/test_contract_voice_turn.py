# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The embedder contract for the voice turn engine.

Drives `vaf.VoiceTurnEngine` the way a stranger's car assistant would: imported
from the FACADE, no Agent, no web server, no speech extra, no microphone - the
STT arrives through the documented `transcribe` seam and the reply layer is the
only thing faked (an embedder brings a provider; this test must not need one).
If any of those were required, the export would be product code wearing a
library's name.
"""
import io
import struct
import wave

import vaf


def _wav(seconds: float = 1.2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        n = int(16000 * seconds)
        w.writeframes(b"".join(
            struct.pack("<h", 12000 if (i // 40) % 2 == 0 else -12000)
            for i in range(n)))
    return buf.getvalue()


def test_a_stranger_can_run_a_voice_turn_from_the_facade(monkeypatch):
    """Facade import, injected STT, faked reply layer - one decided outcome."""
    import vaf.core.voice_agent as va

    monkeypatch.setattr(va, "is_exclusive", lambda: False)
    monkeypatch.setattr(va, "voice_reply",
                        lambda *a, **kw: {"reply": "Gerne!", "delegate": None,
                                          "silent": False})

    # EXACTLY the five initial keys EMBEDDING.md promises - if the engine ever
    # requires a sixth, that is a contract change, not a detail: this test is
    # the stranger who followed the doc to the letter.
    state = {"history": [], "lang": "de", "scope": "embedder-scope",
             "session": "call-1", "chime_recent": []}
    engine = vaf.VoiceTurnEngine(
        state,
        transcribe=lambda wav, **kw: ("hallo wie geht es dir", "de"),
        lane_speaks=lambda lang: True,
        subagents_busy=lambda: False,
    )
    out = engine.turn(_wav(), session_id="call-1", main_busy=False,
                      pending_task="", username="alice")

    assert isinstance(out, vaf.TurnOutcome)
    assert out.kind == "reply" and out.error is None
    assert out.reply == "Gerne!" and out.tts_lang == "de"
    assert out.delegate is None
    # The engine wrote the exchange into the state the embedder holds:
    assert [h["role"] for h in state["history"]] == ["user", "assistant"]
    engine.end()


def test_the_outcome_contract_fields_exist():
    """The documented TurnOutcome fields are the promise (EMBEDDING.md)."""
    out = vaf.TurnOutcome(kind="silent")
    for field in ("kind", "error", "user_text", "speaker_label", "reply",
                  "tts_lang", "tts_follow", "flags", "delegate", "speaker_ok",
                  "active_s", "marks"):
        assert hasattr(out, field), f"documented field {field!r} vanished"


def test_the_engine_never_speaks_or_enqueues():
    """The division of labor is the contract: TTS and the delegation enqueue
    belong to the embedder. The engine module must not import the speech
    manager or the task queue at module level - and its module level must stay
    pure stdlib, or the slim base grows a dependency by accident."""
    import pathlib
    import vaf.core.voice_turn as vt

    src = pathlib.Path(vt.__file__).read_text(encoding="utf-8")
    # Usage, not prose: the module docstring legitimately DESCRIBES that TTS and
    # the enqueue stay outside - the guard must only fire on actual calls/imports.
    assert "synthesize_audio(" not in src, "TTS crept into the engine"
    assert "TaskQueue(" not in src and "import task_queue" not in src and \
        "from vaf.core.task_queue" not in src, "the enqueue belongs to the caller"
    for line in src.splitlines():
        if line.startswith(("import ", "from ")) and "__future__" not in line:
            assert "vaf" not in line, f"top-level import breaks the slim base: {line}"
