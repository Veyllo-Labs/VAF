# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The contact book as a small CRM, framework half.

One phone primitive for the whole tree (the bridge, the dashboard routes and the contact
book used to carry four normalisers with two different rules), the store keys of a
contact per channel, the Front Office phone set as one function instead of six hand
copies, and the contact book's isolation: a tenant without a file must never read the
local admin's book.
"""
import pytest

from vaf.core import channel_message_store as store
from vaf.core import contacts_store as cs
from vaf.core.platform import Platform

SCOPE_A = "11111111-2222-3333-4444-555555555555"
SCOPE_B = "66666666-7777-8888-9999-000000000000"


@pytest.fixture
def scratch(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    return tmp_path


# ── the phone primitive ─────────────────────────────────────────────────────────

def test_phone_digits_canonical_reads_every_notation_the_same_way():
    assert cs.phone_digits_canonical("+49 176 1234567") == "491761234567"
    assert cs.phone_digits_canonical("0176 1234567") == "491761234567"            # trunk zero, 11 digits
    assert cs.phone_digits_canonical("0176 123456") == "49176123456"              # trunk zero, 10 digits
    assert cs.phone_digits_canonical("0049 176 1234567") == "491761234567"        # international prefix
    assert cs.phone_digits_canonical("0044 20 7946 0958") == "442079460958"       # never rewritten to 49
    assert cs.phone_digits_canonical("491761234567:3@s.whatsapp.net") == "491761234567"
    assert cs.phone_digits_canonical("12345678901234@lid") == ""                 # a LID is not a number
    assert cs.phone_digits_canonical("123456789-1234@g.us") == ""
    assert cs.phone_digits_canonical("") == "" and cs.phone_digits_canonical("abc") == ""
    assert cs._phone_digits_canonical is cs.phone_digits_canonical                # the old name still works


def test_whatsapp_store_key_is_the_plus_form_or_nothing():
    assert cs.whatsapp_store_key("+491761234567") == "+491761234567"
    assert cs.whatsapp_store_key("0176 1234 5678") == "+4917612345678"
    assert cs.whatsapp_store_key("0044 20 7946 0958") == "+442079460958"
    assert cs.whatsapp_store_key("491761234567@s.whatsapp.net") == "+491761234567"
    assert cs.whatsapp_store_key("123") is None
    assert cs.whatsapp_store_key("1234567890123456") is None                      # 16 digits
    assert cs.whatsapp_store_key("12345678901234@lid") is None


def test_routes_and_bridge_build_the_same_store_key_as_the_contact_book():
    from vaf.api import whatsapp_bridge as wa
    from vaf.api import whatsapp_routes as wr
    for value in ("+49 176 1234567", "0176 1234567", "491761234567@s.whatsapp.net", "0049 176 1234567", "++491761234567"):
        key = cs.whatsapp_store_key(value)
        assert key == "+491761234567", value
        assert wr._normalize_chat_id(value) == key
        assert wa._to_e164_display(value) == key
        assert wa._phone_digits_canonical(value) == cs.phone_digits_canonical(value) == "491761234567"
    # Not a number: the routes keep the raw id (groups are matched raw), the bridge has no key.
    assert wr._normalize_chat_id("12345678901234@lid") == "12345678901234@lid"
    assert wa._to_e164_display("12345678901234@lid") == ""
    assert wa._phone_digits_canonical("12345678901234@lid") == ""


# ── endpoints ────────────────────────────────────────────────────────────────────

def _contact(**extra):
    base = {
        "id": "c1", "name": "Bob Example",
        "channels": [
            {"type": "phone", "value": "0176 1234 5678"},
            {"type": "whatsapp", "value": "+49 176 1234 5678"},        # same number, other notation
            {"type": "telegram", "value": "12345"},
            {"type": "telegram", "value": "@bob"},
            {"type": "discord", "value": "777"},
            {"type": "email", "value": "Bob@Example.com"},
        ],
    }
    base.update(extra)
    return base


def test_contact_endpoints_are_store_keys_per_channel():
    ep = cs.contact_endpoints(_contact())
    assert ep == {"whatsapp": ["+4917612345678"], "telegram": ["12345"], "discord": ["777"], "email": ["bob@example.com"]}


def test_contact_endpoints_add_the_mapped_lid_only_when_asked():
    lid_map = {"999@lid": "+4917612345678", "888@lid": "+15550001111", "junk": "x"}
    assert cs.contact_endpoints(_contact(), lid_map=lid_map)["whatsapp"] == ["+4917612345678"]
    assert cs.contact_endpoints(_contact(), with_lids=True, lid_map=lid_map)["whatsapp"] == ["+4917612345678", "999@lid"]
    assert cs.contact_endpoints({"id": "x", "name": "n", "channels": []}, with_lids=True, lid_map=lid_map)["whatsapp"] == []


def test_front_office_endpoints_cover_only_contacts_who_may_reach_the_assistant(scratch):
    cs.create_contact("Fo Person", "alice", user_scope_id=SCOPE_A, whatsapp_phone="0176 1234 5678", allow_as_assistant_user=True)
    cs.create_contact("Quiet Person", "alice", user_scope_id=SCOPE_A, whatsapp_phone="+491700000042")
    cs.create_contact("Telegram Person", "alice", user_scope_id=SCOPE_A, telegram_user_id="4242", allow_as_assistant_user=True)
    assert cs.front_office_endpoints("alice", SCOPE_A, "whatsapp") == {"+4917612345678"}
    assert cs.front_office_endpoints("alice", SCOPE_A, "telegram") == {"4242"}
    assert cs.front_office_endpoints("bob", SCOPE_B, "whatsapp") == set()


def test_no_file_outside_the_contact_book_builds_whatsapp_keys_from_contact_values():
    """The Front Office phone set was hand-rolled six times (four dashboard sites, the
    bridge, the cross-chat filter), each with its own '+' rule. They all call the store now;
    a new hand copy is the drift this guard exists for."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "vaf"
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "contacts_store.py":
            continue
        if "_contact_whatsapp_values" in path.read_text(encoding="utf-8", errors="replace"):
            offenders.append(str(path.relative_to(root.parent)))
    assert offenders == [], offenders


# ── isolation ────────────────────────────────────────────────────────────────────

def test_a_tenant_without_a_file_never_reads_the_local_admins_book(scratch):
    from vaf.core.config import get_local_admin_scope_id, get_local_admin_username
    admin_user, admin_scope = get_local_admin_username(), get_local_admin_scope_id()
    admin_contact = cs.create_contact("Admin Friend", admin_user, user_scope_id=admin_scope, whatsapp_phone="+491700000001")
    assert (scratch / "data" / "contacts.json").exists()
    # The admin reaches the book by scope, by username, and with no identity at all.
    assert [c["name"] for c in cs.list_contacts(admin_user, user_scope_id=admin_scope)] == ["Admin Friend"]
    assert [c["name"] for c in cs.list_contacts(admin_user)] == ["Admin Friend"]
    assert [c["name"] for c in cs.list_contacts()] == ["Admin Friend"]
    # A tenant with no file of their own sees nothing, by scope or by username.
    assert cs.list_contacts("bob", user_scope_id=SCOPE_B) == []
    assert cs.list_contacts("bob") == []
    assert cs.get_contact_by_id(admin_contact["id"], "bob", user_scope_id=SCOPE_B) is None
    assert cs.update_contact(admin_contact["id"], "bob", user_scope_id=SCOPE_B, status="lead") is None
    assert cs.add_contact_note(admin_contact["id"], "leak", "bob", user_scope_id=SCOPE_B) is None
    assert cs.front_office_endpoints("bob", SCOPE_B, "whatsapp") == set()
    # And a tenant with an EMPTY file (the exact case the old fallback walked past).
    (scratch / "data" / "scopes" / SCOPE_B).mkdir(parents=True, exist_ok=True)
    (scratch / "data" / "scopes" / SCOPE_B / "contacts.json").write_text("[]", encoding="utf-8")
    assert cs.list_contacts("bob", user_scope_id=SCOPE_B) == []
    # The admin's record never moved.
    assert [c["name"] for c in cs.list_contacts(admin_user, user_scope_id=admin_scope)] == ["Admin Friend"]


def test_whatsapp_read_tools_read_the_scoped_store_the_bridge_writes(scratch):
    from vaf.tools.find_whatsapp_messages import FindWhatsAppMessagesTool
    from vaf.tools.read_whatsapp_chat import ReadWhatsAppChatTool
    store.append_message("alice", "+491700000042", "hello from the scoped store", direction="in",
                         user_scope_id=SCOPE_A, channel="whatsapp", ts=1_700_000_000.0)
    out = ReadWhatsAppChatTool().run(chat_id="+491700000042", username="alice", user_scope_id=SCOPE_A)
    assert "hello from the scoped store" in out
    out = FindWhatsAppMessagesTool().run(query="scoped store", username="alice", user_scope_id=SCOPE_A)
    assert "hello from the scoped store" in out
    # Another scope's file is another file.
    assert "No messages found" in ReadWhatsAppChatTool().run(chat_id="+491700000042", username="alice", user_scope_id=SCOPE_B)
