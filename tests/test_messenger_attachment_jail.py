# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Regression: outgoing messenger attachments are confined to the caller's own data.

The four messenger senders (send_telegram, send_discord, send_whatsapp, send_to_user)
took a model-supplied file_path, asked only the static half of ``is_safe_path`` and
declared no ``file_access`` - so NO per-user jail installed on any lane, and a non-admin
tenant could attach (= exfiltrate) any file the static checks allow, including another
tenant's tree. Only send_mail was confined.

The fix is the declaration, not a hand-roll: ``file_access = "write"`` plus ``user_role``
in ``identity_kwargs`` (BaseTool wraps run() in user_jail). "write" and not "read" on
purpose - the mode names the ROOT SET, and "read" would make skill files shared by OTHER
users attachable to an outgoing message.

Also pinned here: send_whatsapp hands the bridge the UNRESOLVED path. Its old code
resolved AFTER the check and never re-checked, so a symlink was vetted as the link and
sent as the target; the shared rule now vets the real target itself, and the tool must
not undo that by resolving late again.
"""
import os
import sys
import types
from pathlib import Path

import pytest

from vaf.tools.send_discord import SendDiscordTool
from vaf.tools.send_telegram import SendTelegramTool
from vaf.tools.send_to_user import SendToUserTool
from vaf.tools.send_whatsapp import SendWhatsAppTool

# Synthetic scope (public-repo hygiene: never a real scope UUID).
TENANT = "deadbeef-0000-0000-0000-000000000000"

SENDERS = [SendTelegramTool, SendDiscordTool, SendWhatsAppTool, SendToUserTool]


def _install_fake_module(monkeypatch, name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)


# ── The declaration is the fix: pin it per tool ──────────────────────────────

@pytest.mark.parametrize("cls", SENDERS, ids=[c.name for c in SENDERS])
def test_every_messenger_sender_declares_the_write_jail(cls):
    """Strictly stronger than a source grep: this asserts the CLASS OBJECT the
    dispatcher instantiates carries the declaration and the wrapped run()."""
    assert cls.file_access == "write", (
        f"{cls.name} must declare file_access='write' - 'read' would widen the root set "
        "to skill folders shared by other users, i.e. make them sendable out"
    )
    assert {"user_scope_id", "user_role"} <= set(cls.identity_kwargs), (
        f"{cls.name} declares file_access but not the identity that resolves it"
    )
    assert getattr(cls.run, "_vaf_jailed", False), (
        f"{cls.name}.run is not wrapped - the declaration did not install the jail"
    )


# ── Through run(): a tenant cannot send files outside their own tree ─────────

def _telegram_seams(monkeypatch, calls):
    # Captured BEFORE vaf.core.session is faked below: compute_user_jail imports
    # get_user_projects_root from there, and swallowing it would fail the jail
    # CLOSED - the deny test would pass for the wrong reason. The real helper
    # reads the patched Platform.documents_dir at call time.
    from vaf.core.session import get_user_projects_root

    def fake_direct(chat_id, text, *, voice_lang=None, file_path=None):
        calls["chat_id"] = chat_id
        calls["file_path"] = file_path
        return True, ""

    monkeypatch.setattr(
        "vaf.core.messaging_connections.get_telegram_chat_id",
        lambda user_scope_id, username: "12345",
    )
    monkeypatch.setattr("vaf.core.telegram_reply.has_telegram_reply_callback", lambda: False)
    monkeypatch.setattr("vaf.api.telegram_bridge.send_telegram_message_direct", fake_direct)
    _install_fake_module(monkeypatch, "vaf.core.outbound_sanitizer",
                         sanitize_outgoing_message=lambda text: text)

    class FakeSession:
        def __init__(self, id, name):
            self.id = id
            self.name = name
            self.messages = []

        def add_message(self, role, content):
            self.messages.append((role, content))

    class FakeSessionManager:
        def load(self, session_id, restore_state=False):
            raise FileNotFoundError

        def save(self, session, sync_state=False):
            return None

    _install_fake_module(monkeypatch, "vaf.core.session",
                         SessionManager=FakeSessionManager, Session=FakeSession,
                         get_user_projects_root=get_user_projects_root)
    _install_fake_module(monkeypatch, "vaf.core.user_notifications",
                         append_notification=lambda *args, **kwargs: None)


def test_a_tenant_cannot_attach_a_file_outside_their_own_tree(monkeypatch, tmp_path):
    """The deny half, through the real run(): the refusal must come from the per-user
    jail (the file sits under tmp, which every static check allows) and the bridge
    must never see the path."""
    from vaf.core.platform import Platform

    base = tmp_path.resolve()
    monkeypatch.setattr(Platform, "documents_dir", classmethod(lambda cls: base))
    outside = base / "elsewhere" / "report.pdf"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"%PDF-1.4")

    calls = {}
    _telegram_seams(monkeypatch, calls)

    out = SendTelegramTool().run(
        message="here is the report",
        file_path=str(outside),
        user_scope_id=TENANT,
        user_role="user",
        username="alice",
    )
    assert "outside your own data" in out
    assert calls == {}, "the bridge must not be reached with a jailed path"


def test_a_tenants_own_file_still_goes_out(monkeypatch, tmp_path):
    """The allow half of the pair: inside VAF_Projects/<uid8> the jail lets the
    attachment through and the bridge receives the path."""
    from vaf.core.platform import Platform

    base = tmp_path.resolve()
    monkeypatch.setattr(Platform, "documents_dir", classmethod(lambda cls: base))
    own = base / "VAF_Projects" / "deadbeef"
    own.mkdir(parents=True)
    mine = own / "report.pdf"
    mine.write_bytes(b"%PDF-1.4")

    calls = {}
    _telegram_seams(monkeypatch, calls)

    out = SendTelegramTool().run(
        message="here is the report",
        file_path=str(mine),
        user_scope_id=TENANT,
        user_role="user",
        username="alice",
    )
    assert out == "Message and document report.pdf sent to the user via Telegram."
    assert calls["file_path"] == str(mine)


# ── send_whatsapp hands the bridge the UNRESOLVED path ───────────────────────

def test_whatsapp_passes_the_link_path_not_its_target(monkeypatch, tmp_path):
    """A legitimate in-tree symlink goes out under its own name. Resolving late would
    hand the bridge the target AFTER the check ran - the old ordering bug; killing
    this pin means reintroducing .resolve() on the vetted path."""
    base = tmp_path.resolve()
    real = base / "report.pdf"
    real.write_bytes(b"%PDF-1.4")
    alias = base / "latest.pdf"
    os.symlink(real, alias)

    seen = {}

    def fake_send(username, chat_jid, text, **kwargs):
        seen.update(kwargs)
        return "Message sent via WhatsApp."

    monkeypatch.setattr(
        "vaf.core.messaging_connections.get_whatsapp_chat_jid",
        lambda user_scope_id, username: "491761234567@s.whatsapp.net",
    )
    monkeypatch.setattr("vaf.api.whatsapp_bridge.send_whatsapp_with_confirmation", fake_send)
    _install_fake_module(monkeypatch, "vaf.core.outbound_sanitizer",
                         sanitize_outgoing_message=lambda text: text)
    _install_fake_module(monkeypatch, "vaf.core.user_notifications",
                         append_notification=lambda *args, **kwargs: None)

    out = SendWhatsAppTool().run(
        message="latest report",
        file_path=str(alias),
        username="admin",
    )
    assert out == "Message sent via WhatsApp."
    assert seen["document_path"] == str(alias)
    assert seen["document_path"] != str(real)
