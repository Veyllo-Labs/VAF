# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`reply_to_chat` answers on the chat a turn came from, and the runner's three plain-text
reply sites (the turn-error reply, the speaker-confirm ack, the sub-agent drain summary) go
through it instead of a chain per site."""
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from vaf.core import channel_reply
from vaf.core import headless_runner as hr

ROOT = Path(__file__).resolve().parents[1]
RUNNER = (ROOT / "vaf" / "core" / "headless_runner.py").read_text(encoding="utf-8")

SCOPE = "ab12cd34-0000-0000-0000-000000000000"
META = {
    "telegram": {"telegram_chat_id": "1001"},
    "discord": {"discord_channel_id": "2002"},
    "whatsapp": {"whatsapp_chat_jid": "4917000000000@s.whatsapp.net", "username": "alice",
                 "user_scope_id": SCOPE},
}


@pytest.fixture
def sent(monkeypatch):
    out = []
    import vaf.core.discord_reply as dr
    import vaf.core.telegram_reply as tr
    import vaf.core.whatsapp_reply as wr
    monkeypatch.setattr(tr, "send_telegram_reply",
                        lambda chat_id, text, *a, **k: out.append(("telegram", chat_id, text)) or True)
    monkeypatch.setattr(dr, "send_discord_reply",
                        lambda channel_id, text: out.append(("discord", channel_id, text)))
    monkeypatch.setattr(wr, "send_whatsapp_reply",
                        lambda username, jid, text, *a, **k: out.append(
                            ("whatsapp", jid, text, username, k.get("user_scope_id"))) or True)
    return out


@pytest.mark.parametrize("channel", sorted(META))
def test_each_channel_is_answered_by_its_own_address(sent, channel):
    assert channel_reply.reply_to_chat(channel, META[channel], "hi") is True
    address = next(v for k, v in META[channel].items() if k.endswith(("_id", "_jid")))
    assert sent[0][:3] == (channel, address, "hi")
    if channel == "whatsapp":
        assert sent[0][3:] == ("alice", SCOPE)


def test_no_address_no_text_no_send(sent):
    assert channel_reply.reply_to_chat("telegram", {"discord_channel_id": "2"}, "hi") is False
    assert channel_reply.reply_to_chat("telegram", META["telegram"], "") is False
    assert channel_reply.reply_to_chat("web", META["telegram"], "hi") is False
    assert sent == []
    # A stored session without `source` is answered by the address it carries.
    assert channel_reply.reply_to_chat(None, META["discord"], "hi", infer=True) is True
    assert sent == [("discord", "2002", "hi")]


@pytest.mark.parametrize("channel", sorted(META))
def test_the_turn_error_reply_goes_through_it(sent, channel):
    task = SimpleNamespace(source=channel, metadata=dict(META[channel]), session_id="s1")
    hr._send_turn_error_reply(task, RuntimeError("boom"), contact=None)
    assert [s[0] for s in sent] == [channel]


def _block(start: str, end: str) -> str:
    i = RUNNER.index(start)
    return RUNNER[i:RUNNER.index(end, i)]


def test_the_three_sites_call_the_primitive_and_hand_roll_nothing():
    """The ack and the drain run deep inside the queue loop; their source is held here: each
    calls `reply_to_chat`, and no per-channel sender is called there any more."""
    error_site = _block("def _send_turn_error_reply(", "\ndef ")
    ack_site = _block("_spc.try_consume_channel_reply(", "QUEUE_DONE session_id={task.session_id} (speaker_confirm)")
    drain_site = _block("Send subagent summary to", "Failed to send subagent summary")
    for site in (error_site, ack_site, drain_site):
        assert "reply_to_chat(" in site
        assert not re.search(r"send_(telegram|discord|whatsapp)_reply\(", site)
        assert "send_to_main_messenger(" not in site
    assert "infer=True" in drain_site
    # The session's source is copied for every chat channel, not by one `if` per channel.
    assert 'session.metadata["source"] = task.source' in RUNNER
    assert not re.search(r'session\.metadata\["source"\] = "(telegram|discord|whatsapp)"', RUNNER)


def test_the_speaker_ack_on_discord_answers_the_turns_channel(sent):
    assert channel_reply.reply_to_chat("discord", {**META["discord"], "user_scope_id": SCOPE},
                                       "ok") is True
    assert sent == [("discord", "2002", "ok")]
