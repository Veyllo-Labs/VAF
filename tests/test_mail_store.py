# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Mail engine v2 store tests: fail-closed scoping, ingest/FTS/threading,
RFC 4549 bookkeeping (UIDVALIDITY reset, flag resync, expunge), encrypted raw
roundtrip, retention, account deletion cascade. Fully isolated: tmp_path DB and
a pinned in-memory crypto key (no real config/stores are touched)."""
import os

import pytest

import vaf.mail.crypto as mail_crypto
from vaf.mail.parser import ParsedMessage, parse_message
from vaf.mail.store import MailStore, normalize_subject

_SCOPE = "12345678-1234-1234-1234-123456789abc"


@pytest.fixture(autouse=True)
def _pinned_crypto_key():
    old = mail_crypto._cached_key
    mail_crypto._cached_key = os.urandom(32)
    yield
    mail_crypto._cached_key = old


@pytest.fixture()
def store(tmp_path):
    s = MailStore(_SCOPE, base_dir=tmp_path)
    yield s
    s.close()


def _msg(message_id="<m1@example.com>", subject="Hello", refs=None, body="Hi there, invoice attached."):
    return ParsedMessage(message_id=message_id, subject=subject,
                         from_addr="Alice <alice@example.com>", to_addrs="bob@example.com",
                         date_ts=1_700_000_000, refs=refs or [], body_text=body)


def _setup(store):
    apk = store.upsert_account("bob@example.com", "imap", "bob@example.com")
    fpk = store.upsert_folder(apk, "INBOX", special_use="\\Inbox", sync_tier="eager")
    return apk, fpk


def test_fail_closed_scope():
    with pytest.raises(ValueError):
        MailStore("")
    with pytest.raises(ValueError):
        MailStore(None)  # type: ignore[arg-type]


def test_ingest_search_and_snippet(store):
    apk, fpk = _setup(store)
    pk = store.ingest_message(apk, fpk, 1, _msg(), server_flags=["\\Seen"])
    assert pk > 0
    hits = store.search("invoice")
    assert [h["id"] for h in hits] == [pk]
    msg = store.get_message(pk)
    assert msg["snippet"].startswith("Hi there")
    assert msg["flags"] == ["\\Seen"]
    # umlaut handling: diacritics-insensitive prefix search
    store.ingest_message(apk, fpk, 2, _msg("<m2@example.com>", subject="Überweisung fällig"))
    assert store.search("uberweisung")


def test_threading_reply_joins_and_out_of_order_merge(store):
    apk, fpk = _setup(store)
    root = store.ingest_message(apk, fpk, 10, _msg("<root@example.com>", "Plan"))
    reply = store.ingest_message(
        apk, fpk, 11, _msg("<r1@example.com>", "Re: Plan", refs=["<root@example.com>"]))
    t_root = store.get_message(root)["thread_id"]
    assert store.get_message(reply)["thread_id"] == t_root
    # out-of-order: two replies to a missing parent arrive first -> two threads
    a = store.ingest_message(
        apk, fpk, 20, _msg("<a@example.com>", "Re: Trip", refs=["<missing@example.com>"]))
    b_refs = ["<other@example.com>"]
    b = store.ingest_message(apk, fpk, 21, _msg("<b@example.com>", "Re: Trip", refs=b_refs))
    assert store.get_message(a)["thread_id"] != store.get_message(b)["thread_id"]
    # the missing parent arrives referencing nothing, but a and (via References
    # overlap) b now chain to it -> threads merge
    store.ingest_message(
        apk, fpk, 22,
        _msg("<missing@example.com>", "Trip", refs=["<other@example.com>"]))
    tids = {store.get_message(x)["thread_id"] for x in (a, b)}
    assert len(tids) == 1
    threads = store.list_threads()
    trip = [t for t in threads if "Trip" in (t["subject"] or "")]
    assert trip and trip[0]["message_count"] == 3


def test_gm_thrid_overrides_threading(store):
    apk, fpk = _setup(store)
    a = store.ingest_message(apk, fpk, 1, _msg("<x1@example.com>", "One"), gm_thrid="777")
    b = store.ingest_message(apk, fpk, 2, _msg("<x2@example.com>", "Completely different"),
                             gm_thrid="777")
    assert store.get_message(a)["thread_id"] == store.get_message(b)["thread_id"]


def test_uidvalidity_reset_clears_folder(store):
    apk, fpk = _setup(store)
    store.ingest_message(apk, fpk, 1, _msg())
    store.set_folder_state(fpk, uidvalidity=100, last_seen_uid=1)
    dropped = store.reset_folder(fpk, new_uidvalidity=200)
    assert dropped == 1
    assert store.list_messages() == []
    assert store.search("invoice") == []
    f = store.get_folder(apk, "INBOX")
    assert f["uidvalidity"] == 200 and f["last_seen_uid"] == 0


def test_flag_resync_and_expunge_detection(store):
    apk, fpk = _setup(store)
    store.ingest_message(apk, fpk, 1, _msg("<f1@example.com>"))
    store.ingest_message(apk, fpk, 2, _msg("<f2@example.com>", subject="Second"))
    changed = store.apply_server_flags(fpk, {1: ["\\Seen", "\\Flagged"]})
    assert changed == 1
    uid_map = store.message_uid_map(fpk)
    assert set(uid_map) == {1, 2}
    gone = store.remove_vanished(fpk, present_uids=[1])
    assert gone == 1
    assert set(store.message_uid_map(fpk)) == {1}


def test_raw_roundtrip_encrypted_and_compressed(store):
    apk, fpk = _setup(store)
    raw = b"Subject: Hello\r\nMessage-ID: <raw@example.com>\r\n\r\nBody " + b"x" * 500
    pk = store.ingest_message(apk, fpk, 5, _msg("<raw@example.com>"), raw=raw)
    assert store.get_message(pk)["body_state"] == "cached"
    assert store.get_raw(pk) == raw
    # on-disk blob must not contain the plaintext (encrypted at rest, E4)
    row = store._conn().execute("SELECT raw FROM message_raw WHERE message_pk=?", (pk,)).fetchone()
    assert b"Body" not in bytes(row["raw"])


def test_oversized_raw_is_not_cached(store):
    from vaf.mail.store import RAW_CACHE_MAX_BYTES
    apk, fpk = _setup(store)
    big = b"S" * (RAW_CACHE_MAX_BYTES + 1)
    pk = store.ingest_message(apk, fpk, 6, _msg("<big@example.com>"), raw=big)
    assert store.get_message(pk)["body_state"] == "too_large"
    assert store.get_raw(pk) is None


def test_retention_evicts_bodies_keeps_envelopes(store):
    apk, fpk = _setup(store)
    raw = b"Subject: Old\r\n\r\nOld body"
    pk = store.ingest_message(apk, fpk, 7, _msg("<old@example.com>"), raw=raw)
    evicted = store.evict_old_bodies(keep_days=1)  # date_ts=2023 -> older than 1 day
    assert evicted == 1
    msg = store.get_message(pk)
    assert msg is not None and msg["body_state"] == "none"
    assert store.get_raw(pk) is None


def test_delete_account_cascades(store):
    apk, fpk = _setup(store)
    store.ingest_message(apk, fpk, 1, _msg())
    assert store.delete_account("bob@example.com") is True
    assert store.list_accounts() == []
    assert store.list_messages() == []
    assert store.search("invoice") == []


def test_parser_error_boundary_and_charset_lies():
    # grossly malformed input must not raise
    p = parse_message(b"\xff\xfe not a mail at all")
    assert isinstance(p, ParsedMessage)
    # cp1252 bytes declared as latin-1 (Euro sign) decode readably
    raw = (b"Subject: Rechnung\r\nFrom: Shop <shop@example.com>\r\n"
           b"Content-Type: text/plain; charset=iso-8859-1\r\n\r\nPreis: 5\x80")
    p2 = parse_message(raw)
    assert "€" in p2.body_text
    # multipart with attachment + inline cid image
    mp = (b"Subject: Pics\r\nMessage-ID: <p@example.com>\r\nMIME-Version: 1.0\r\n"
          b"Content-Type: multipart/mixed; boundary=B\r\n\r\n"
          b"--B\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>see <img src=\"cid:i1\"></p>\r\n"
          b"--B\r\nContent-Type: image/png\r\nContent-ID: <i1>\r\n"
          b"Content-Transfer-Encoding: base64\r\n\r\niVBORw0KGgo=\r\n"
          b"--B\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; "
          b"filename=\"invoice.pdf\"\r\nContent-Transfer-Encoding: base64\r\n\r\nJVBERg==\r\n--B--\r\n")
    p3 = parse_message(mp)
    assert p3.has_attachments is True
    inline = [a for a in p3.attachments if a.is_inline]
    assert inline and inline[0].content_id == "i1"
    assert "see" in p3.body_text


def test_normalize_subject_strips_reply_prefixes():
    assert normalize_subject("Re: Re: AW: Fwd: Plan  B") == "plan b"
    assert normalize_subject("WG: Überweisung") == "überweisung"


def test_move_ghost_row_is_adopted_not_duplicated(store):
    """C3/T4: after a local move the row is uid-NULL in the destination; when the
    server copy arrives under a new uid, it must ADOPT the ghost (one row), not
    insert a duplicate."""
    apk, fpk = _setup(store)
    arch = store.upsert_folder(apk, "Archive", special_use="\\Archive")
    store.ingest_message(apk, fpk, 1, _msg("<g@example.com>", "Hi"))
    m = store.list_messages()[0]
    store.move_message_local(m["id"], arch)                       # -> uid NULL in Archive
    store.ingest_message(apk, arch, 55, _msg("<g@example.com>", "Hi"))   # server copy, new uid
    rows = store._conn().execute(
        "SELECT id, uid, folder_id FROM messages WHERE message_id='<g@example.com>'").fetchall()
    assert len(rows) == 1                                         # adopted, not duplicated
    assert rows[0]["uid"] == 55 and rows[0]["folder_id"] == arch
