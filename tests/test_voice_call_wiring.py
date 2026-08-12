# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The voice block's OUTER door must admit every branch it contains.

The live call's websocket branches sit two levels deep: one `elif type in (...)`
admits the voice family, and inside it one `elif type == "..."` per message. A
branch whose type is missing from the outer tuple is unreachable - and it reads
as working code to every later reader, which is exactly how the streaming
endpointer shipped: the browser streamed microphone PCM (`voice_call_chunk`) and
mute toggles (`voice_call_reset`) that the server discarded without a trace,
while the release notes announced the feature.

The class is what matters, not the two names: a source-level guard pins door and
branches to each other, and a behavioral test drives a chunk through the real
websocket entry point and proves the endpointer was fed.
"""
import asyncio
import re
from pathlib import Path

import pytest

import vaf.core.web_server as ws


def _voice_block_source() -> str:
    """The voice branch body: from the outer door to the next same-level elif."""
    src = Path(ws.__file__).read_text(encoding="utf-8")
    start = src.index('elif type in ("voice_call_start"')
    rest = src[start:]
    # the next message-type branch at the SAME indentation (16 spaces) ends it
    nxt = re.search(r"\n {16}elif type ", rest[10:])
    return rest[: 10 + nxt.start()] if nxt else rest


def test_every_voice_branch_is_admitted_by_the_outer_door():
    """Mutation: drop a type from the tuple - red. This is the guard the
    live defect needed; the branch existed and was simply never reached."""
    block = _voice_block_source()
    door = re.search(r'elif type in \((.*?)\):', block, re.S).group(1)
    admitted = set(re.findall(r'"(voice_call_\w+)"', door))
    handled = set(re.findall(r'elif type == "(voice_call_\w+)"', block))
    handled |= set(re.findall(r'if type == "(voice_call_start)"', block))

    missing = handled - admitted
    assert not missing, (
        f"unreachable voice branches (handled inside, not admitted by the outer "
        f"door): {sorted(missing)}")
    # and the door promises nothing it cannot serve
    assert "voice_call_chunk" in admitted and "voice_call_reset" in admitted


def test_a_streamed_chunk_actually_reaches_the_endpointer(monkeypatch, tmp_path):
    """End to end through the REAL websocket entry point: start a call, send one
    PCM chunk, and the per-call endpointer must have been fed. Mutation: remove
    voice_call_chunk from the outer tuple - red (fed stays empty)."""
    import base64
    import jwt as _jwt

    import vaf.auth.crypto as crypto
    import vaf.core.voice_turn as vt
    from vaf.core.config import Config

    scope = "ab12cd34"
    monkeypatch.setattr(crypto, "get_jwt_secret", lambda: "test-secret")
    token = _jwt.encode(
        {"sub": "u1", "user_scope_id": scope, "username": "alice", "role": "admin",
         "session_id": None},
        "test-secret", algorithm="HS256")
    monkeypatch.setattr(ws, "_ws_client_ip", lambda w: "127.0.0.1")

    _cfg = {"local_network_enabled": False, "default_language": "de",
            "speaker_id_enabled": False}
    real_get = Config.get
    monkeypatch.setattr(Config, "get",
                        classmethod(lambda cls, k, d=None: _cfg.get(k, real_get(k, d))))

    # The lane is armed via the documented seam, not by faking the model.
    fed = []

    class _Recorder:
        def feed_pcm(self, pcm):
            fed.append(pcm)
            return None          # neither "end" nor "hold": nothing is sent back

        def reset(self):
            fed.append("reset")

    monkeypatch.setattr(vt, "make_endpointer", lambda: _Recorder())
    monkeypatch.setattr(vt, "lane_status",
                        lambda: {"available": True, "exclusive": False,
                                 "dedicated_model": ""})
    monkeypatch.setattr(vt, "greeting_for", lambda *a, **kw: "Hallo!")
    monkeypatch.setattr(vt, "build_chat_digest", lambda *a, **kw: "")
    monkeypatch.setattr(vt, "prewarm_speaker", lambda scope: False)
    monkeypatch.setattr(vt, "prepare_dedicated_model_async", lambda *a, **kw: None)
    monkeypatch.setattr(vt, "subagents_hold_model", lambda: False)

    class _Speech:
        @staticmethod
        def get_instance():
            class _M:
                @staticmethod
                def synthesize_audio(*a, **kw):
                    return None

                @staticmethod
                def call_lane_speaks(lang):
                    return True
            return _M()

    import vaf.core.speech as speech
    monkeypatch.setattr(speech.SpeechManager, "get_instance", _Speech.get_instance)

    pcm = base64.b64encode(b"\x00\x01" * 480).decode("ascii")
    frames = [
        {"type": "voice_call_start", "ui_lang": "de"},
        {"type": "voice_call_chunk", "pcm": pcm},
        {"type": "voice_call_reset"},
        {"type": "voice_call_end"},
    ]

    # Same socket shape the call baseline drives with: the handshake calls
    # send_text, and the loop ends on a real WebSocketDisconnect - a socket
    # missing either looks like a handler defect (measured, cost one debug round).
    import json as _json

    from fastapi import WebSocketDisconnect

    class _Sock:
        def __init__(self):
            self.sent = []
            self._frames = [_json.dumps(f) for f in frames]
            self.cookies, self.headers = {}, {}
            self.client = type("C", (), {"host": "127.0.0.1", "port": 1234})()

        async def accept(self):
            return None

        async def receive_text(self):
            if self._frames:
                return self._frames.pop(0)
            raise WebSocketDisconnect(code=1000)

        async def send_json(self, payload):
            self.sent.append(payload)

        async def send_text(self, payload):
            self.sent.append(_json.loads(payload))

        async def close(self, code: int = 1000, reason: str = ""):
            raise AssertionError(f"handshake rejected: {code} {reason}")

    sock = _Sock()
    asyncio.run(ws.websocket_endpoint(sock, token=token))
    assert any(p.get("type") == "voice_call_started" for p in sock.sent), \
        f"the call never started; got {[p.get('type') for p in sock.sent]}"

    assert fed, "the streamed PCM never reached the endpointer (branch unreachable)"
    assert fed[0] == b"\x00\x01" * 480
    assert "reset" in fed, "voice_call_reset never reached the endpointer"


if __name__ == "__main__":   # pragma: no cover
    pytest.main([__file__])
