# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A messenger's ingress policy decides who the AGENT answers, not what the OWNER may read
on their own bot or number. Telegram and Discord keep a rejected sender's message in the
channel store the way the WhatsApp bridge does (tests/test_whatsapp_outbound_model.py):
nothing runs on it, the dashboard and the inbox tools can show it. Discord keeps DMs only:
the bot sees every guild channel it sits in, and that is not the owner's mail."""
from types import SimpleNamespace

import pytest

from vaf.core import channel_message_store as store
from vaf.core.platform import Platform


@pytest.fixture
def scratch(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    import vaf.core.config as cfg_mod
    monkeypatch.setattr(cfg_mod.Config, "get", classmethod(lambda cls, key, default=None: {"channel_ingress_policy": None}.get(key, default)))
    return tmp_path


def _update(text=None, caption=None, message_id=7):
    return SimpleNamespace(effective_message=SimpleNamespace(text=text, caption=caption, message_id=message_id))


def test_telegram_drop_keeps_the_message_for_the_owner(scratch):
    from vaf.api import telegram_bridge as tg
    tg._drop_unauthorized_telegram("9001", "9001", "text", update=_update(text="hello?"))
    tg._drop_unauthorized_telegram("9001", "9001", "photo", update=_update(caption="look", message_id=8))
    tg._drop_unauthorized_telegram("9001", "9001", "voice", update=_update(message_id=9))
    rows = store.get_chat_messages("admin", "9001", channel="telegram")
    assert [(r["body"], r["direction"], r["content_type"], r["message_id"]) for r in rows] == [
        ("<voice>", "in", "voice", "9"), ("look", "in", "photo", "8"), ("hello?", "in", "text", "7")]
    # a drop without the update (the older call shape) stores nothing and raises nothing
    tg._drop_unauthorized_telegram("9002", "9002", "text")
    assert store.get_chat_messages("admin", "9002", channel="telegram") == []


def test_discord_keeps_a_strangers_dm_but_not_guild_chatter(scratch):
    from vaf.api import discord_bridge as dc
    assert dc._keep_rejected_discord_message("4242", "hey there", "m1", is_dm=True) is True
    assert dc._keep_rejected_discord_message("4242", "", "m2", is_dm=True) is True
    assert dc._keep_rejected_discord_message("4243", "guild noise", "m3", is_dm=False) is False
    rows = store.get_chat_messages("admin", "4242", channel="discord")
    assert [(r["body"], r["direction"]) for r in rows] == [("<message>", "in"), ("hey there", "in")]
    assert store.get_chat_messages("admin", "4243", channel="discord") == []
