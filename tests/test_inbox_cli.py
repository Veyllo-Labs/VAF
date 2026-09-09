# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf inbox list` (vaf/cli/cmd/inbox.py): the inbox rows from the terminal, as the machine
owner, behind the same door as `vaf session`, read-only."""
import json
import re
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vaf.cli.cmd import inbox as inbox_cmd
from vaf.core import channel_message_store as store
from vaf.core.platform import Platform

SCOPE = "11111111-2222-3333-4444-555555555555"
MAIN = Path(__file__).resolve().parent.parent / "vaf" / "main.py"


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    import vaf.core.config as cfg_mod
    cfg = {"whatsapp_config": {"whitelist": [], "reply_window_hours": 72}, "telegram_config": {}, "discord_config": {}}
    monkeypatch.setattr(cfg_mod.Config, "get", classmethod(lambda cls, key, default=None: cfg.get(key, default)))
    import vaf.core.contacts_store as contacts
    monkeypatch.setattr(contacts, "front_office_endpoints", lambda username=None, user_scope_id=None, channel="whatsapp": set())
    monkeypatch.setattr(contacts, "is_local_admin_caller", lambda username, user_scope_id: False)
    import vaf.core.web_interface as wi
    monkeypatch.setattr(wi, "notify_inbox_changed", lambda scope: None)
    store._reset_announce_state()   # cancels the timers an earlier test left behind; the dicts stay the module's own
    import vaf.core.session as session_mod
    monkeypatch.setattr(session_mod, "_room_rows", lambda scope: [])
    monkeypatch.setattr(inbox_cmd, "_identity", lambda: ("alice", SCOPE))
    now = time.time() - 3600
    store.append_message("alice", "+491700000042", "passt Donnerstag?", ts=now - 900, user_scope_id=SCOPE, chat_name="Alice")
    store.append_message("alice", "+491700000043", "hi", ts=now - 800, user_scope_id=SCOPE)
    store.append_message("alice", "+491700000043", "hello", direction="out", ts=now - 700, user_scope_id=SCOPE)


def test_list_prints_one_json_object_per_conversation_newest_first(world):
    result = CliRunner().invoke(inbox_cmd.app, ["list", "--json"])
    assert result.exit_code == 0, result.output
    rows = [json.loads(line) for line in result.output.strip().splitlines()]
    assert [r["key"] for r in rows] == ["whatsapp:+491700000043", "whatsapp:+491700000042"]
    assert rows[1]["waits"] and rows[1]["name"] == "Alice" and rows[0]["answered_by_agent"]
    waiting = CliRunner().invoke(inbox_cmd.app, ["list", "--json", "--view", "waits"])
    assert [json.loads(l)["key"] for l in waiting.output.strip().splitlines()] == ["whatsapp:+491700000042"]


def test_the_bulk_flag_is_off_unless_given(world, monkeypatch):
    seen = []
    from vaf.core import inbox as inbox_mod
    real = inbox_mod.list_conversations
    monkeypatch.setattr(inbox_mod, "list_conversations", lambda *a, **kw: seen.append(kw.get("include_bulk")) or real(*a, **kw))
    assert CliRunner().invoke(inbox_cmd.app, ["list", "--json"]).exit_code == 0
    assert CliRunner().invoke(inbox_cmd.app, ["list", "--json", "--bulk"]).exit_code == 0
    assert seen == [False, True]


def test_the_table_carries_the_counts_and_the_waits_reason(world):
    result = CliRunner().invoke(inbox_cmd.app, ["list"])
    assert result.exit_code == 0, result.output
    assert "2 conversations" in result.output and "1 wait for you" in result.output
    assert "unanswered" in result.output and "Alice" in result.output


def test_the_group_sits_behind_the_terminal_door_and_has_no_write_command():
    src = MAIN.read_text(encoding="utf-8")
    assert re.search(r'add_typer\(inbox\.app, name="inbox"[^)]*callback=_terminal_door', src, re.S), \
        "the inbox prints chats and must sit behind the same door as vaf session"
    names = {c.name for c in inbox_cmd.app.registered_commands}
    assert names == {"list"}


def test_an_unknown_channel_or_view_is_refused_instead_of_silently_widened(world):
    """MUTATION: drop the two checks in vaf/cli/cmd/inbox.py and `--channel fax` prints every
    channel's rows with exit code 0 (list_conversations falls back to all channels and to the
    "all" view for names it does not know)."""
    result = CliRunner().invoke(inbox_cmd.app, ["list", "--json", "--channel", "fax"])
    assert result.exit_code == 1 and "fax" in result.output and "whatsapp" in result.output, result.output
    assert not [line for line in result.output.splitlines() if line.startswith("{")], "no rows for a channel that does not exist"
    result = CliRunner().invoke(inbox_cmd.app, ["list", "--json", "--view", "sideways"])
    assert result.exit_code == 1 and "sideways" in result.output and "waits" in result.output, result.output
    assert CliRunner().invoke(inbox_cmd.app, ["list", "--json", "--channel", "whatsapp", "--view", "waits"]).exit_code == 0
