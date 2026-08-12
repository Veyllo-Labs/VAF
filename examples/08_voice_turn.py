# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Drive a voice turn yourself - the pipeline VAF's own live call runs on.

This is the embedder shape for a car assistant or a home speaker: YOU own the
microphone, the transport and the TTS; the engine owns the decision - noise
gate, STT, speaker verification, the reflex policy, the first-layer reply and
the delegate decision - and hands you ONE TurnOutcome per utterance.

Two seams make this example self-contained:
- `transcribe` is injected (a scripted STT here; plug in your recognizer),
  so the example needs neither the speech extra nor a microphone.
- TTS is simply `print()` - the outcome carries text + language; speaking
  it is the embedder's half.

The reply layer runs on your configured provider (like every engine call),
so a provider key (or the local model) must be set up - see EMBEDDING.md.
"""
import io
import struct
import wave

from vaf import VoiceTurnEngine


def tiny_wav(seconds: float = 1.2) -> bytes:
    """A synthetic 16 kHz mono WAV loud enough to pass the noise gate."""
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


def my_stt(wav_bytes: bytes, **kwargs) -> tuple:
    """Your recognizer goes here. The contract: (text, detected_lang)."""
    return ("Hallo! Kannst du mir kurz sagen, was du alles kannst?", "de")


def main() -> None:
    # One engine per call. The state dict is yours to hold; a NEW call gets a
    # fresh dict and a fresh engine - never merge an old one.
    state = {"history": [], "lang": "de", "scope": "example-scope",
             "session": "call-1", "chime_recent": []}
    engine = VoiceTurnEngine(
        state,
        transcribe=my_stt,
        lane_speaks=lambda lang: True,   # which languages YOUR tts can speak
    )

    outcome = engine.turn(tiny_wav(), session_id="call-1",
                          main_busy=False, pending_task="", username="alice")

    if outcome.error:
        # busy_local | no_speech | llm_failed - the call stays open, you just
        # keep listening (VAF's own frontend does exactly that).
        print(f"[no reply: {outcome.error}]")
        return

    if outcome.flags.get("silent"):
        # The utterance was not addressed at the agent (side talk on an open
        # mic). It is kept as room context; nothing is spoken.
        print("[agent chose to stay silent]")
        return

    # TTS is YOURS: outcome.reply in outcome.tts_lang. VAF's handler uses
    # per-variant timeouts here (30 s short lines, 60 s chime, 130 s reply).
    print(f"AGENT ({outcome.tts_lang}): {outcome.reply}")

    if outcome.delegate:
        # The engine DECIDED real work is needed; enqueueing it is the
        # caller's job (VAF's handler feeds its TaskQueue at this point).
        print(f"[delegate to your worker: {outcome.delegate!r}]")

    engine.end()   # call over: clears the rolling transcript


if __name__ == "__main__":
    main()
