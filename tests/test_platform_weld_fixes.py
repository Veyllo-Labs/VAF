# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Platform-weld cleanup: the last prompt/heuristic surfaces that hardwired a
platform, the dead 'email' main_messenger value, and send_discord attachments.

Follow-ups from the channel-model audit: prompts and heuristics must never
hardwire a platform (the platform is user configuration, resolved at run time
by send_to_user), 'email' was storable but healed to None on every read (dead
value), and send_discord hid the attachment support its core sender always had.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from vaf.core.platform import Platform
from vaf.tools.send_discord import SendDiscordTool


@pytest.fixture(autouse=True)
def _isolate_side_effect_stores(monkeypatch, tmp_path):
    """SendDiscordTool.run() has REAL post-send bookkeeping (session file,
    channel-message store, activity notification). Only the network sender is
    mocked below, so without this isolation every suite run wrote a fake
    'Message sent via Discord / Hier dein Bericht' entry into the live
    Activity feed and channel store (268 rows by the time it was noticed -
    the owner reasonably asked whether we had been hacked)."""
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path / "vaf"))
    # SessionManager does NOT go through Platform: it defaults to
    # Path.home()/.vaf/sessions (session.py), so the outbound-session mirror
    # needs its own redirect or discord_42.json reappears in the real store.
    import vaf.core.session as sess
    real_init = sess.SessionManager.__init__

    def _init(self, storage_dir=None, state_registry=None):
        real_init(self, storage_dir=str(tmp_path / "sessions"), state_registry=state_registry)
    monkeypatch.setattr(sess.SessionManager, "__init__", _init)


# ── send_discord attachments ──────────────────────────────────────────────────

def test_send_discord_schema_has_file_path():
    assert "file_path" in SendDiscordTool.parameters["properties"]
    assert SendDiscordTool.parameters["required"] == ["message"]


def test_send_discord_passes_attachment_to_core_sender(monkeypatch, tmp_path):
    f = tmp_path / "report.pdf"
    f.write_text("x")
    calls = {}
    import vaf.core.messaging_connections as mc
    import vaf.core.discord_send as ds
    import vaf.core.config as cfg
    monkeypatch.setattr(mc, "get_discord_user_id", lambda scope, user: "42")
    monkeypatch.setattr(cfg.Config, "get", staticmethod(
        lambda k, d=None: {"bot_token": "t"} if k == "discord_config" else d))
    monkeypatch.setattr(ds, "send_discord_dm",
                        lambda token, uid, text, chunk=True, file_path=None:
                        calls.update(file_path=file_path, text=text) or True)
    out = SendDiscordTool().run(message="Hier dein Bericht", file_path=str(f))
    assert calls["file_path"] == str(f)
    assert "Message and document report.pdf sent to the user via Discord" in out


def test_send_discord_missing_file_refuses(monkeypatch):
    out = SendDiscordTool().run(message="x", file_path="/nonexistent/nope_77.pdf")
    assert "File not found" in out


# ── the last platform welds are gone (source guards) ─────────────────────────

def test_librarian_send_hint_is_channel_agnostic():
    import vaf.tools.librarian as lib
    src = Path(lib.__file__).read_text(encoding="utf-8")
    assert "To send via Telegram" not in src, "librarian hint hardwires Telegram again"
    assert "send_to_user(message=" in src


def test_channel_capabilities_use_ssot_map():
    import vaf.core.system_prompt as sp
    src = Path(sp.__file__).read_text(encoding="utf-8")
    assert 'send_tool = "send_whatsapp" if src ==' not in src, (
        "channel-capabilities regained the hardcoded platform ternary"
    )
    assert "CHANNEL_SEND_TOOLS.get(src, \"send_to_user\")" in src


def test_ask_once_guidance_teaches_send_to_user():
    import vaf.core.system_prompt as sp
    src = Path(sp.__file__).read_text(encoding="utf-8")
    assert "deliver with `send_to_user(message=" in src


def test_send_success_heuristic_has_no_bare_platform_names():
    import vaf.core.agent as agent_mod
    src = Path(agent_mod.__file__).read_text(encoding="utf-8")
    assert '"sent", "gesendet", "telegram", "mail"' not in src, (
        "the delegation send-success heuristic regained bare platform names - "
        "'Failed to send Telegram message' would count as success again"
    )
    assert '"sent to the user", "message sent", "gesendet", "delivered",' in src


# ── dead 'email' main_messenger value removed everywhere ─────────────────────

def test_email_gone_from_identity_enum_and_validators():
    from vaf.tools.user_identity import UpdateUserIdentityTool
    enum = UpdateUserIdentityTool.parameters["properties"]["main_messenger"]["enum"]
    assert "email" not in enum
    for mod_name in ("vaf.tools.user_identity", "vaf.core.system_prompt",
                     "vaf.core.messaging_connections"):
        mod = __import__(mod_name, fromlist=["_"])
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert '"whatsapp", "email")' not in src, f"{mod_name} regained the dead email value"


def test_front_office_mapping_covers_all_known_channels():
    # The mapping had TWO drifts: slack missing, dead email line present.
    import vaf.core.system_prompt as sp
    src = Path(sp.__file__).read_text(encoding="utf-8")
    for tool in ("send_telegram", "send_whatsapp", "send_discord", "send_slack"):
        assert src.count(f"call `{tool}(message=") >= 1, f"front-office EN mapping lost {tool}"
    assert "If `email` → call `send_mail" not in src
    assert "Steht dort `email`" not in src
