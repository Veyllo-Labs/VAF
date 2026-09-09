# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The WhatsApp window's Composer and the person's own send (POST /api/whatsapp/composer,
POST /api/whatsapp/send).

The Composer route is the mail route's twin on the shared lane: same frames, same
no-tools completion, the chat instead of the thread. The send route is the one place
the person writes from the agent's number by hand, so it pins that the message goes
out as the OWNER's (origin) and that a bridge refusal reaches the client as an error
rather than a silent success.
"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import vaf.api.whatsapp_routes as wr
import vaf.core.composer_lane as lane
from vaf.core import channel_message_store as store
from vaf.core.config import Config

SCOPE = "12345678-1234-1234-1234-123456789abc"


def _request(username="alice", scope=SCOPE):
    return SimpleNamespace(state=SimpleNamespace(user={"username": username, "user_scope_id": scope}))


def _row(body, direction="in", ts=1_700_000_000.0, name="Bob"):
    return {"chat_id": "+491700000001", "chat_name": name, "body": body, "direction": direction,
            "ts": ts, "content_type": "text", "channel": "whatsapp", "message_id": f"m{ts}",
            "sender_jid": "491700000001@s.whatsapp.net"}


@pytest.fixture
def composer(monkeypatch):
    """A chat in the store seam, a captured completion, the settings at their defaults."""
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, k, d=None: Config.DEFAULTS.get(k, d)))
    seen = {"reads": [], "knowledge": []}
    rows = [_row("Hallo, hast du morgen Zeit?", ts=1), _row("Klar, wann?", "out", ts=2),
            _row("So gegen 15 Uhr?", ts=3)]
    monkeypatch.setattr(store, "get_chat_messages",
                        lambda username, cid, limit=50, user_scope_id=None, **kw:
                        seen["reads"].append((username, cid, user_scope_id)) or list(rows))
    monkeypatch.setattr(lane, "local_model_is_cold", lambda: False)

    def _stream(messages, max_tokens, temperature, *, lane="?"):
        seen["messages"] = messages
        seen["lane"] = lane
        yield "Ja, 15 Uhr passt."

    monkeypatch.setattr(lane, "stream_completion", _stream)
    monkeypatch.setattr(lane, "knowledge",
                        lambda scope, instruction, fallback="", *, caller, chat_key=None:
                        seen["knowledge"].append((scope, instruction, fallback, caller, chat_key)) or "")
    return seen


def _run(body, request=None):
    async def _collect():
        resp = await wr.whatsapp_composer(request or _request(), wr.ComposerRequest(**body))
        return "".join([f async for f in resp.body_iterator])
    return asyncio.run(_collect())


def _fence(messages):
    from vaf.core.composer import CHAT
    return next(m["content"] for m in messages if m["content"].startswith(CHAT.fence_open))


# ── the Composer route ────────────────────────────────────────────────────────

def test_a_draft_reads_the_chat_of_the_requesting_user_and_streams_the_mail_frames(composer):
    out = _run({"chat_id": "+491700000001", "instruction": "sag zu", "chat_label": "Bob"})
    assert composer["reads"] == [("alice", "+491700000001", SCOPE)], "the store is read in the caller's scope"
    assert "event: meta" in out and '"own_included": 1' in out and '"included": 3' in out
    assert '"Ja, 15 Uhr passt."' in out and out.rstrip().endswith("event: end\ndata: {}")


def test_the_prompt_is_the_chat_profile_with_the_users_side_labelled(composer):
    from vaf.core.composer import CHAT
    _run({"chat_id": "+491700000001", "instruction": "sag zu"})
    msgs = composer["messages"]
    assert msgs[0]["content"].startswith(CHAT.intro), "the chat profile, not the mail one"
    fenced = _fence(msgs)
    assert f"from: {CHAT.own_label}" in fenced and "Klar, wann?" in fenced
    assert "from: Bob" in fenced and "So gegen 15 Uhr?" in fenced
    assert msgs[-1]["content"].startswith(CHAT.draft_operator) and "sag zu" in msgs[-1]["content"]
    assert "sag zu" not in fenced


def test_the_completion_is_booked_on_the_whatsapp_lane(composer):
    _run({"chat_id": "+491700000001"})
    assert composer["lane"] == "whatsapp"


def test_memory_is_keyed_on_the_instruction_with_the_chat_name_as_the_fallback(composer):
    _run({"chat_id": "+491700000001", "instruction": "confirm the day rate", "chat_label": "Bob"})
    _run({"chat_id": "+491700000001", "chat_label": "Bob"})
    assert composer["knowledge"] == [
        (SCOPE, "confirm the day rate", "Bob", "whatsapp_composer", "whatsapp_alice_491700000001"),
        (SCOPE, "", "Bob", "whatsapp_composer", "whatsapp_alice_491700000001"),
    ], "the Composer names this chat's namespace with the bridge's own session id"


def test_rewrite_reads_no_chat_and_carries_the_draft(composer):
    _run({"chat_id": "+491700000001", "mode": "rewrite", "draft": "ja passt", "instruction": "höflicher"})
    assert composer["reads"] == [], "rewrite works on the person's text and reads nothing"
    assert "<user_draft>\nja passt\n</user_draft>" in composer["messages"][-1]["content"]
    assert composer["knowledge"][-1][4] == "whatsapp_alice_491700000001", \
        "the person's preferences from this chat are as useful when rewriting"


def test_the_composer_lane_forwards_the_namespace_and_the_mail_lane_never_names_one(monkeypatch):
    from pathlib import Path

    from vaf.core import composer_lane
    from vaf.memory import rag

    seen = []
    # The lane is gated on memory_enabled; the defaults, not this machine's config, decide.
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, k, d=None: Config.DEFAULTS.get(k, d)))
    monkeypatch.setattr(rag, "turn_memory_context", lambda query, **kw: seen.append(kw) or "")
    composer_lane.knowledge(SCOPE, "q", "Bob", caller="whatsapp_composer", chat_key="whatsapp_alice_1")
    composer_lane.knowledge(SCOPE, "q", "Bob", caller="mail_composer")
    assert seen[0]["chat_key"] == "whatsapp_alice_1" and seen[1]["chat_key"] is None
    mail = (Path(__file__).resolve().parent.parent / "vaf" / "api" / "mail_routes.py").read_text(encoding="utf-8")
    assert "chat_key" not in mail, "a mail draft must never reach a contact's namespace"


def test_refusals(composer, monkeypatch):
    with pytest.raises(HTTPException) as ei:
        _run({"chat_id": "+491700000001", "mode": "rewrite"})
    assert ei.value.status_code == 422
    with pytest.raises(HTTPException) as ei:
        _run({"chat_id": "+491700000001", "mode": "summarise"})
    assert ei.value.status_code == 422
    with pytest.raises(HTTPException) as ei:
        _run({"chat_id": " "})
    assert ei.value.status_code == 400
    monkeypatch.setattr(Config, "get", classmethod(
        lambda cls, k, d=None: False if k == "mail_composer_enabled" else Config.DEFAULTS.get(k, d)))
    with pytest.raises(HTTPException) as ei:
        _run({"chat_id": "+491700000001"})
    assert ei.value.status_code == 403


def test_a_provider_failure_is_an_error_frame_not_an_empty_draft(composer, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("no backend")
        yield  # pragma: no cover
    monkeypatch.setattr(lane, "stream_completion", _boom)
    out = _run({"chat_id": "+491700000001"})
    assert "event: error" in out and "event: meta" in out


# ── the person's own send ─────────────────────────────────────────────────────

@pytest.fixture
def sender(monkeypatch):
    import vaf.api.whatsapp_bridge as wa
    seen = {}
    monkeypatch.setattr(wa, "is_bridge_running", lambda: True)

    def _send(username, chat_jid, text, **kw):
        seen.update(username=username, chat_jid=chat_jid, text=text, **kw)
        return seen.get("_answer", "Message sent via WhatsApp.")

    monkeypatch.setattr(wa, "send_whatsapp_with_confirmation", _send)
    return seen


def _send(body, request=None):
    return asyncio.run(wr.send_whatsapp_message(request or _request(), wr.SendRequest(**body)))


def test_the_send_goes_out_as_the_owner_from_the_callers_account(sender):
    out = _send({"chat_id": "+49 170 0000001", "text": "  Hi Bob  "})
    assert out == {"ok": True, "chat_id": "+49 170 0000001"}
    assert sender["username"] == "alice"
    assert sender["chat_jid"] == "491700000001@s.whatsapp.net"
    assert sender["text"] == "Hi Bob"
    assert sender["origin"] == "owner", "stored under OWNER_SENDER: no reply window opens"
    assert sender["allow_contact_send"] is True, "the person may write to anyone from their own number"


def test_a_lid_chat_is_addressed_as_it_is(sender):
    _send({"chat_id": "173642054922259@lid", "text": "hi"})
    assert sender["chat_jid"] == "173642054922259@lid"


def test_a_bridge_refusal_is_a_502_with_the_bridges_words(sender):
    sender["_answer"] = "WhatsApp could not deliver the message: not connected"
    with pytest.raises(HTTPException) as ei:
        _send({"chat_id": "+491700000001", "text": "hi"})
    assert ei.value.status_code == 502 and "not connected" in ei.value.detail


def test_send_refusals(sender, monkeypatch):
    import vaf.api.whatsapp_bridge as wa
    for body, code in (({"chat_id": "+491700000001", "text": "  "}, 400),
                       ({"chat_id": "", "text": "hi"}, 400),
                       ({"chat_id": "not a number", "text": "hi"}, 400),
                       ({"chat_id": "+491700000001", "text": "x" * 20001}, 413)):
        with pytest.raises(HTTPException) as ei:
            _send(body)
        assert ei.value.status_code == code, body
    assert "chat_jid" not in sender, "nothing was sent"
    monkeypatch.setattr(wa, "is_bridge_running", lambda: False)
    with pytest.raises(HTTPException) as ei:
        _send({"chat_id": "+491700000001", "text": "hi"})
    assert ei.value.status_code == 503
