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


# ── fields, tags, bulk, created ──────────────────────────────────────────────────

def test_new_fields_are_written_normalised_and_readable(scratch):
    c = cs.create_contact("Lena Example", "alice", user_scope_id=SCOPE_A, company="  Studio Example GmbH ",
                          role="Geschaeftsfuehrerin", tags="Fotografie, berlin , FOTOGRAFIE, ,angebot offen", source="agent")
    assert c["company"] == "Studio Example GmbH" and c["role"] == "Geschaeftsfuehrerin"
    assert c["tags"] == ["Fotografie", "berlin", "angebot offen"]          # deduped case-insensitively, first spelling kept
    assert c["source"] == "agent" and c["created_at"] > 1_700_000_000
    plain = cs.create_contact("Plain", "alice", user_scope_id=SCOPE_A)
    assert plain["tags"] == [] and plain["source"] == "manual" and plain["company"] is None
    # update: a list, a comma string, null, and an unknown key
    up = cs.update_contact(c["id"], "alice", user_scope_id=SCOPE_A, tags=["a", "b", "A"], company="", role="CEO")
    assert up["tags"] == ["a", "b"] and up["company"] is None and up["role"] == "CEO"
    assert cs.update_contact(c["id"], "alice", user_scope_id=SCOPE_A, tags="x, y")["tags"] == ["x", "y"]
    assert cs.update_contact(c["id"], "alice", user_scope_id=SCOPE_A, tags=None)["tags"] == []
    assert "bogus" not in cs.update_contact(c["id"], "alice", user_scope_id=SCOPE_A, bogus="z")


def test_normalize_tags_limits():
    assert cs._normalize_tags(None) == [] and cs._normalize_tags(42) == [] and cs._normalize_tags("") == []
    assert cs._normalize_tags("a" * 60) == ["a" * cs.TAG_MAX_LENGTH]
    assert len(cs._normalize_tags(",".join(str(i) for i in range(50)))) == cs.TAG_MAX_COUNT
    assert cs._normalize_tags("  spaced   out  ") == ["spaced out"]


def test_status_and_tag_values_come_from_one_helper(scratch):
    a = cs.create_contact("A", "alice", user_scope_id=SCOPE_A, tags="vip, berlin")
    cs.create_contact("B", "alice", user_scope_id=SCOPE_A, tags="berlin")
    cs.update_contact(a["id"], "alice", user_scope_id=SCOPE_A, status="warm friend")
    assert cs.contact_tag_values("alice", user_scope_id=SCOPE_A) == ["berlin", "vip"]     # most frequent first
    assert cs.contact_status_values("alice", user_scope_id=SCOPE_A) == list(cs.CONTACT_STATUS_DEFAULTS) + ["warm friend"]
    assert cs.contact_tag_values("bob", user_scope_id=SCOPE_B) == []


def test_bulk_update_and_delete_touch_only_this_users_records(scratch):
    a = cs.create_contact("A", "alice", user_scope_id=SCOPE_A, tags="old")
    b = cs.create_contact("B", "alice", user_scope_id=SCOPE_A)
    other = cs.create_contact("Other", "bob", user_scope_id=SCOPE_B)
    n = cs.update_contacts_bulk([a["id"], b["id"], other["id"], "missing"], "alice", user_scope_id=SCOPE_A,
                                status="lead", add_tags="vip, old", remove_tags=["OLD"])
    assert n == 2
    back_a = cs.get_contact_by_id(a["id"], "alice", user_scope_id=SCOPE_A)
    back_b = cs.get_contact_by_id(b["id"], "alice", user_scope_id=SCOPE_A)
    assert back_a["status"] == "lead" and back_a["tags"] == ["vip"]                      # a tag in both lists ends up removed
    assert back_b["status"] == "lead" and back_b["tags"] == ["vip"]
    assert cs.get_contact_by_id(other["id"], "bob", user_scope_id=SCOPE_B).get("status") is None
    assert cs.update_contacts_bulk([a["id"]], "alice", user_scope_id=SCOPE_A) == 0            # nothing requested
    assert cs.update_contacts_bulk([a["id"]], "alice", user_scope_id=SCOPE_A, status=None) == 1
    assert cs.get_contact_by_id(a["id"], "alice", user_scope_id=SCOPE_A)["status"] is None
    # delete: foreign and unknown ids are ignored, the other scope keeps its record
    assert cs.delete_contacts([a["id"], other["id"], "missing"], "alice", user_scope_id=SCOPE_A) == 1
    assert cs.get_contact_by_id(a["id"], "alice", user_scope_id=SCOPE_A) is None
    assert cs.get_contact_by_id(other["id"], "bob", user_scope_id=SCOPE_B) is not None
    assert cs.delete_contacts([], "alice", user_scope_id=SCOPE_A) == 0
    # A tenant without a file cannot bulk-touch the admin's book (the isolation fix, seen from here).
    from vaf.core.config import get_local_admin_scope_id, get_local_admin_username
    admin = cs.create_contact("Admin Friend", get_local_admin_username(), user_scope_id=get_local_admin_scope_id())
    assert cs.update_contacts_bulk([admin["id"]], "carol", user_scope_id="77777777-8888-9999-0000-111111111111", status="x") == 0
    assert cs.delete_contacts([admin["id"]], "carol", user_scope_id="77777777-8888-9999-0000-111111111111") == 0
    assert not (scratch / "data" / "scopes" / "77777777-8888-9999-0000-111111111111" / "contacts.json").exists()


def test_contact_created_falls_back_to_the_oldest_channel_link():
    assert cs.contact_created({"id": "x"}) is None
    assert cs.contact_created({"created_at": 1000.0, "source": "agent"}) == {"ts": 1000.0, "source": "agent"}
    assert cs.contact_created({"created_at": 1000.0}) == {"ts": 1000.0, "source": "manual"}
    legacy = {"links": {"telegram": {"linked_at": 3000.0}, "whatsapp": {"linked_at": 2000.0, "last_seen_ts": 9000.0}}}
    assert cs.contact_created(legacy) == {"ts": 2000.0, "source": "whatsapp"}


def test_sync_stamps_new_records_but_never_rewrites_existing_ones(scratch):
    existing = cs.create_contact("Known", "alice", user_scope_id=SCOPE_A, whatsapp_phone="+491700000001", source="manual")
    cs.sync_channel_contacts("whatsapp", [
        {"endpoint": "+491700000001", "display_name": "Known", "last_seen_ts": 5000.0},
        {"endpoint": "+491700000002", "display_name": "Fresh Person", "last_seen_ts": 6000.0},
    ], "alice", user_scope_id=SCOPE_A)
    by_name = {c["name"]: c for c in cs.list_contacts("alice", user_scope_id=SCOPE_A)}
    assert by_name["Known"]["source"] == "manual" and by_name["Known"]["created_at"] == existing["created_at"]
    fresh = by_name["Fresh Person"]
    assert fresh["source"] == "whatsapp" and fresh["tags"] == [] and fresh["created_at"] == fresh["links"]["whatsapp"]["linked_at"]
    assert cs.contact_created(fresh) == {"ts": fresh["created_at"], "source": "whatsapp"}


# ── message store: stats and paging ──────────────────────────────────────────────

def _seed_chat(username, chat_id, scope, n, start=1_700_000_000.0, channel="whatsapp"):
    for i in range(n):
        store.append_message(username, chat_id, f"msg {i}", direction="out" if i % 3 == 0 else "in",
                             message_id=f"m{i}", user_scope_id=scope, channel=channel, ts=start + i * 60)


def test_store_exists_and_chat_stats_never_create_a_database(scratch):
    assert store.store_exists("alice", SCOPE_A) is False
    assert store.chat_stats("alice", ["+491700000042"], user_scope_id=SCOPE_A, channel="whatsapp") == \
        {"count": 0, "out_count": 0, "first_ts": None, "last_ts": None}
    assert not (scratch / "data" / "scopes" / SCOPE_A / "channel_messages.db").exists()
    assert store.chat_stats("alice", [], user_scope_id=SCOPE_A) == {"count": 0, "out_count": 0, "first_ts": None, "last_ts": None}


def test_chat_stats_count_both_keys_skip_tombstones_and_tolerate_null_content_type(scratch):
    _seed_chat("alice", "+491700000042", SCOPE_A, 6)                       # out at i=0,3 -> 2 out
    _seed_chat("alice", "999@lid", SCOPE_A, 2, start=1_700_100_000.0)     # out at i=0 -> 1 out
    store.mark_deleted("alice", "+491700000042", "m5", user_scope_id=SCOPE_A) if hasattr(store, "mark_deleted") else None
    conn = store._get_conn("alice", SCOPE_A)
    conn.execute("UPDATE channel_messages SET content_type = NULL WHERE message_id = 'm1'")
    conn.commit(); conn.close()
    s = store.chat_stats("alice", ["+491700000042", "999@lid"], user_scope_id=SCOPE_A, channel="whatsapp")
    assert s["out_count"] == 3 and s["first_ts"] == 1_700_000_000.0 and s["last_ts"] == 1_700_100_060.0
    assert s["count"] in (7, 8)                                               # 8 rows, minus the tombstone when mark_deleted exists
    # no rows for these ids in an existing store: zeros, not NULLs
    assert store.chat_stats("alice", ["+490000000000"], user_scope_id=SCOPE_A) == {"count": 0, "out_count": 0, "first_ts": None, "last_ts": None}
    # another scope is another file
    assert store.chat_stats("alice", ["+491700000042"], user_scope_id=SCOPE_B)["count"] == 0


def test_get_chat_messages_before_ts_is_an_inclusive_cursor(scratch):
    _seed_chat("alice", "+491700000042", SCOPE_A, 5)
    rows = store.get_chat_messages("alice", "+491700000042", limit=10, user_scope_id=SCOPE_A, before_ts=1_700_000_120.0)
    assert [r["body"] for r in rows] == ["msg 2", "msg 1", "msg 0"]


# ── mail: the per-address query ──────────────────────────────────────────────────

@pytest.fixture
def pinned_mail_key():
    import os
    import vaf.mail.crypto as mail_crypto
    old = mail_crypto._cached_key
    mail_crypto._cached_key = os.urandom(32)
    yield
    mail_crypto._cached_key = old


def _mail(message_id, subject, from_addr, to_addrs, ts, body="body", cc=""):
    from vaf.mail.parser import ParsedMessage
    return ParsedMessage(message_id=message_id, subject=subject, from_addr=from_addr, to_addrs=to_addrs,
                         cc_addrs=cc, date_ts=ts, body_text=body)


def _mail_store_with_bob(scope):
    from vaf.mail.store import MailStore
    s = MailStore(scope)
    apk = s.upsert_account("owner@example.com", "imap", "owner@example.com")
    inbox = s.upsert_folder(apk, "INBOX", special_use="\\Inbox", sync_tier="eager")
    sent = s.upsert_folder(apk, "Gesendet", special_use="\\Sent", sync_tier="headers")
    junk = s.upsert_folder(apk, "Spam", special_use="\\Junk", sync_tier="lazy")
    s.ingest_message(apk, inbox, 1, _mail("<in1@x>", "Angebot", "Bob <bob@example.com>", "owner@example.com", 1_700_000_100, "hi"))
    s.ingest_message(apk, sent, 2, _mail("<out1@x>", "Re: Angebot", "owner@example.com", "Bob <bob@example.com>", 1_700_000_200, ""))
    s.ingest_message(apk, inbox, 3, _mail("<cc1@x>", "Team", "carol@example.com", "owner@example.com", 1_700_000_300, "x", cc="bob@example.com"))
    s.ingest_message(apk, inbox, 4, _mail("<mention@x>", "News", "news@example.com", "owner@example.com", 1_700_000_400, "ask bob@example.com about it"))
    s.ingest_message(apk, junk, 5, _mail("<junk@x>", "Spam", "bob@example.com", "owner@example.com", 1_700_000_500, "spam"))
    s.ingest_message(apk, inbox, 6, _mail("<nodate@x>", "Undated", "bob@example.com", "owner@example.com", None, "?"))
    return s


def test_mail_store_messages_for_address_uses_headers_not_full_text(scratch, pinned_mail_key):
    from vaf.mail.store import MailStore
    assert MailStore.exists(SCOPE_A) is False
    s = _mail_store_with_bob(SCOPE_A)
    assert MailStore.exists(SCOPE_A) is True
    rows = s.messages_for_address("Bob@Example.com")
    assert [r["message_id"] for r in rows] == ["<cc1@x>", "<out1@x>", "<in1@x>"]      # newest first; junk, body-only and undated left out
    assert rows[1]["special_use"] == "\\Sent" and rows[1]["snippet"] == ""
    assert [r["message_id"] for r in s.messages_for_address("bob@example.com", before_ts=1_700_000_200)] == ["<out1@x>", "<in1@x>"]
    s.close()


def test_messages_for_address_merged_decides_direction_and_keeps_the_legacy_user_rule(scratch, pinned_mail_key, monkeypatch):
    from vaf.mail import tool_bridge
    s = _mail_store_with_bob(SCOPE_A)
    s.close()
    rows = tool_bridge.messages_for_address_merged("bob@example.com", None, 10, "alice", SCOPE_A)
    assert [(r["message_id"], r["direction"]) for r in rows] == [("<cc1@x>", "out"), ("<out1@x>", "out"), ("<in1@x>", "in")]
    assert all(isinstance(r["ts"], float) for r in rows)
    # A username-only caller never constructs the v2 store, whatever the admin scope holds.
    from vaf.mail import store as mail_store_module
    monkeypatch.setattr(mail_store_module.MailStore, "__init__", lambda *a, **k: (_ for _ in ()).throw(AssertionError("v2 touched")))
    assert tool_bridge.messages_for_address_merged("bob@example.com", None, 10, "alice", None) == []
    # ...and reads the legacy store when it has one.
    from vaf.core import email_sync_store as legacy
    legacy.init_store("alice", None)
    conn = legacy._get_conn("alice", None)
    conn.execute("INSERT INTO email_messages (username, account_id, folder, message_id, subject, from_addr, date_str, body_snippet, synced_at, message_date_iso) "
                 "VALUES ('', 'acct', 'INBOX', '<legacy@x>', 'Old mail', 'bob@example.com', 'Tue, 01 Sep 2026 10:00:00 +0200', 'old', '2026-09-01T08:00:00Z', '2026-09-01T08:00:00Z')")
    conn.execute("INSERT INTO email_messages (username, account_id, folder, message_id, subject, from_addr, date_str, body_snippet, synced_at, message_date_iso) "
                 "VALUES ('', 'acct', 'INBOX', '<undated@x>', 'No date', 'bob@example.com', '', 'x', '2026-09-01T08:00:00Z', NULL)")
    conn.commit(); conn.close()
    rows = tool_bridge.messages_for_address_merged("bob@example.com", None, 10, "alice", None)
    assert [r["message_id"] for r in rows] == ["<legacy@x>"] and rows[0]["direction"] == "in"
    assert abs(rows[0]["ts"] - 1_788_249_600.0) < 1                              # 2026-09-01T08:00:00Z


# ── the timeline ─────────────────────────────────────────────────────────────────

def test_contact_timeline_merges_every_source_newest_first(scratch, pinned_mail_key):
    c = cs.create_contact("Bob Example", "alice", user_scope_id=SCOPE_A, whatsapp_phone="0170 0000042", email="Bob@Example.com")
    _seed_chat("alice", "+491700000042", SCOPE_A, 3, start=1_700_000_000.0)
    _seed_chat("alice", "999@lid", SCOPE_A, 1, start=1_700_000_500.0)             # the agent's send, stored under the lid
    note = cs.add_contact_note(c["id"], "wants a demo", "alice", user_scope_id=SCOPE_A)
    ev = cs.add_contact_event(c["id"], "Demo", 4_000_000_000.0, "alice", user_scope_id=SCOPE_A, note="bring the deck")
    _mail_store_with_bob(SCOPE_A).close()
    contact = cs.get_contact_by_id(c["id"], "alice", user_scope_id=SCOPE_A)
    out = cs.contact_timeline(contact, "alice", SCOPE_A, lid_map={"999@lid": "+491700000042"})
    kinds = [(it["kind"], it.get("direction")) for it in out["items"]]
    assert kinds[:2] == [("event", None), ("note", None)]                            # attached just now
    assert ("created", None) in kinds and kinds.index(("created", None)) == 2
    bodies = [it["body"] for it in out["items"] if it["kind"] == "message"]
    assert bodies == ["msg 0", "msg 2", "msg 1", "msg 0"]                            # lid row (newest) then the three by number
    assert [it["ref"]["chat_id"] for it in out["items"] if it["kind"] == "message"][0] == "999@lid"
    mails = [(it["title"], it["direction"]) for it in out["items"] if it["kind"] == "mail"]
    assert mails == [("Team", "out"), ("Re: Angebot", "out"), ("Angebot", "in")]
    assert out["next_cursor"] is None
    ts_list = [it["ts"] for it in out["items"]]
    assert ts_list == sorted(ts_list, reverse=True)
    ev_item = next(it for it in out["items"] if it["kind"] == "event")
    assert ev_item["ref"]["when_ts"] == 4_000_000_000.0 and ev_item["body"] == "bring the deck" and ev_item["id"] == ev["id"]
    assert next(it for it in out["items"] if it["kind"] == "note")["id"] == note["id"]
    # tabs
    only_notes = cs.contact_timeline(contact, "alice", SCOPE_A, kinds={"note"})
    assert [it["kind"] for it in only_notes["items"]] == ["note"]


def test_contact_timeline_pages_exactly_through_a_long_chat_and_same_second_ties(scratch):
    c = cs.create_contact("Chatty", "alice", user_scope_id=SCOPE_A, whatsapp_phone="+491700000042")
    _seed_chat("alice", "+491700000042", SCOPE_A, 120, start=1_700_000_000.0)
    # a note written in the very second of message 60
    cs.add_contact_note(c["id"], "same second", "alice", user_scope_id=SCOPE_A)
    contact = cs.get_contact_by_id(c["id"], "alice", user_scope_id=SCOPE_A)
    contact["notes_log"][0]["ts"] = 1_700_000_000.0 + 60 * 60
    seen, cursor, pages = [], None, 0
    while True:
        out = cs.contact_timeline(contact, "alice", SCOPE_A, limit=25, cursor=cursor)
        seen.extend(out["items"]); pages += 1
        cursor = out["next_cursor"]
        if not cursor:
            break
        assert pages < 20
    ids = [it["id"] for it in seen]
    assert len(ids) == len(set(ids)) == 122                                           # 120 messages + note + created, nothing twice
    assert pages == 5
    assert [it["ts"] for it in seen] == sorted((it["ts"] for it in seen), reverse=True)
    assert cs.decode_timeline_cursor("garbage") is None and cs.decode_timeline_cursor("") is None


def test_contact_timeline_survives_a_broken_mail_lane_and_creates_no_store(scratch, monkeypatch):
    c = cs.create_contact("Quiet", "alice", user_scope_id=SCOPE_A, email="q@example.com", whatsapp_phone="+491700000042")
    from vaf.mail import tool_bridge
    monkeypatch.setattr(tool_bridge, "messages_for_address_merged", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("mail down")))
    out = cs.contact_timeline(c, "alice", SCOPE_A)
    assert [it["kind"] for it in out["items"]] == ["created"]
    assert not (scratch / "data" / "scopes" / SCOPE_A / "channel_messages.db").exists()
    assert not (scratch / "data" / "scopes" / SCOPE_A / "mail.db").exists()


def test_contact_timeline_and_stats_stay_inside_the_callers_scope(scratch):
    c = cs.create_contact("Bob", "alice", user_scope_id=SCOPE_A, whatsapp_phone="+491700000042")
    _seed_chat("alice", "+491700000042", SCOPE_A, 4)
    same_number_other_scope = {"id": "x", "name": "Bob", "channels": [{"type": "whatsapp", "value": "+491700000042"}]}
    assert [it["kind"] for it in cs.contact_timeline(same_number_other_scope, "bob", SCOPE_B)["items"]] == []
    assert cs.contact_activity_stats(same_number_other_scope, "bob", SCOPE_B)["messages"] == 0
    stats = cs.contact_activity_stats(c, "alice", SCOPE_A)
    assert stats["messages"] == 4 and stats["from_agent"] == 2 and stats["first_ts"] == 1_700_000_000.0
    assert stats["last_ts"] == 1_700_000_180.0 and set(stats["by_channel"]) == {"whatsapp"}
