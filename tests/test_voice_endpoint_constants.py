# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The endpointing constants exist once, on both sides of the wire.

The same "how much trailing silence ends an utterance" decision was hand-rolled three
times: the browser listen loop (VoiceCallLayer.tsx SILENCE_MS), an inline literal in the
CLI speech lane (speech.py), and vaf/core/voice_vad.py - the clean primitive, which for
months had tests and zero callers while the copies drifted around it. This guard makes
the drift a CI failure instead of an archaeology find (CLAUDE.md Rule 2: prefer a CI
guard over a prose rule).

Source-level pins on purpose: the frontend constant lives in TypeScript, so importing
both sides is not an option, and the numbers ARE the contract.
"""
import re
from pathlib import Path

from vaf.core import voice_vad

_REPO = Path(__file__).resolve().parent.parent
_LAYER = _REPO / "web" / "components" / "VoiceCallLayer.tsx"


def _frontend_const(name: str) -> float:
    text = _LAYER.read_text(encoding="utf-8")
    m = re.search(rf"^const {name} = (\d+)", text, re.M)
    assert m, f"{name} not found in VoiceCallLayer.tsx - renamed? The guard must follow."
    return float(m.group(1))


def test_silence_window_matches_across_the_wire():
    assert _frontend_const("SILENCE_MS") == voice_vad._SILENCE_MS, (
        "the browser and the server-side TurnDetector disagree on when an utterance "
        "ends - the fourth hand-rolled copy is being born. Change BOTH or neither."
    )


def test_min_speech_matches_across_the_wire():
    assert _frontend_const("MIN_SPEECH_MS") == voice_vad._MIN_SPEECH_MS


def test_max_utterance_matches_across_the_wire():
    assert _frontend_const("MAX_UTTER_MS") == voice_vad._MAX_UTTER_MS
