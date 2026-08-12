# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Persisted artifacts carry a format identity, and readers honor it.

Three seams, one rule each:
- fs_map cache: a cache file without the current schema tag (foreign tool, older
  build) is DISCARDED and rebuilt, never misread as current.
- handoff bundles: new bundles are written with a 'format' tag; bundles from
  before the tag have none and MUST keep loading (open handoffs survive updates).
- timeline hash chain: new files link their first event to the versioned seed;
  files written before the versioned seed start with bare GENESIS and must still
  verify. A wrong first link or a broken mid-chain link fails verification.
"""
import json

import vaf.core.handoff_bundle as hb
from vaf.api.logs_routes import _verify_chain
from vaf.core.fs_map import CachedFilesystemMap
from vaf.core.log_helper import (
    LEGACY_CHAIN_SEED,
    TIMELINE_CHAIN_SEED,
    _timeline_hash,
    _timeline_prev_hash,
)
from vaf.core.platform import Platform

SCOPE = "0a0b0c0d-0000-4000-8000-000000000002"


# ── fs_map cache schema ──────────────────────────────────────────────────────

def _cache_map(tmp_path):
    return CachedFilesystemMap(cache_file=tmp_path / "fs_cache.json")


def test_fs_cache_roundtrip_carries_schema(tmp_path):
    m = _cache_map(tmp_path)
    m.map = {"os": m.os_type, "scanned_at": __import__("time").time(), "locations": {}}
    m.save_to_cache()
    on_disk = json.loads((tmp_path / "fs_cache.json").read_text(encoding="utf-8"))
    assert on_disk["schema"] == CachedFilesystemMap.CACHE_SCHEMA
    fresh = _cache_map(tmp_path)
    assert fresh.load_from_cache() is True


def test_fs_cache_without_schema_is_discarded(tmp_path):
    """A pre-tag or foreign cache file (valid JSON, right OS, fresh) must NOT load."""
    m = _cache_map(tmp_path)
    stale = {"os": m.os_type, "scanned_at": __import__("time").time(), "locations": {}}
    (tmp_path / "fs_cache.json").write_text(json.dumps(stale), encoding="utf-8")
    assert m.load_from_cache() is False


def test_fs_cache_with_foreign_schema_is_discarded(tmp_path):
    m = _cache_map(tmp_path)
    foreign = {"schema": "somebody-elses-2", "os": m.os_type,
               "scanned_at": __import__("time").time(), "locations": {}}
    (tmp_path / "fs_cache.json").write_text(json.dumps(foreign), encoding="utf-8")
    assert m.load_from_cache() is False


# ── handoff bundle format tag ────────────────────────────────────────────────

def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path))


def test_new_bundles_carry_the_format_tag(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    b = hb.create(SCOPE, history=None, summary="s", question="q")
    assert b["format"] == "bundle-1-3a8969"
    loaded = hb.load(SCOPE, b["id"])
    assert loaded and loaded["format"] == "bundle-1-3a8969"


def test_pre_tag_bundles_still_load(monkeypatch, tmp_path):
    """Bundles written before the tag have no 'format' key; load() must accept them."""
    _isolate(monkeypatch, tmp_path)
    b = hb.create(SCOPE, history=None, summary="s", question="q")
    path = tmp_path / "handoff_bundles" / SCOPE / f"{b['id']}.json"
    from vaf.core import data_files
    data = data_files.read_json(path)
    del data["format"]
    # Written as PLAINTEXT on purpose: a pre-tag bundle also predates encryption,
    # so this exercises both tolerances at once.
    path.write_bytes(json.dumps(data).encode("utf-8"))
    loaded = hb.load(SCOPE, b["id"])
    assert loaded is not None and "format" not in loaded


# ── timeline chain seed ──────────────────────────────────────────────────────

def _chain(seed):
    """Two well-formed events linked seed -> ev1 -> ev2."""
    ev1 = {"ts": "2026-01-01T00:00:00", "type": "tool_start", "prev_hash": seed}
    ev1["hash"] = _timeline_hash(ev1)
    ev2 = {"ts": "2026-01-01T00:00:01", "type": "tool_end", "prev_hash": ev1["hash"]}
    ev2["hash"] = _timeline_hash(ev2)
    return [ev1, ev2]


def test_format_identities_are_pinned_literals():
    """These values are part of the on-disk format. Changing one silently invalidates
    or orphans existing user artifacts (caches rebuild - fine; chains stop matching
    their legacy-accept - not fine), so a change must be a deliberate, tested
    migration, not a drive-by edit."""
    assert TIMELINE_CHAIN_SEED == "GENESIS:4ad9c39d"
    assert LEGACY_CHAIN_SEED == "GENESIS"
    assert CachedFilesystemMap.CACHE_SCHEMA == "fsmap-v3-aee248"
    from vaf.core.trust import TRUST_FORMAT
    assert TRUST_FORMAT == "trust-2-b17c4e"


def test_chain_with_versioned_seed_verifies():
    assert _verify_chain(_chain(TIMELINE_CHAIN_SEED)) is True


def test_legacy_chain_with_bare_genesis_still_verifies():
    assert _verify_chain(_chain(LEGACY_CHAIN_SEED)) is True


def test_chain_with_unknown_seed_fails():
    assert _verify_chain(_chain("GENESIS:ffffffff")) is False


def test_broken_middle_link_fails():
    events = _chain(TIMELINE_CHAIN_SEED)
    events[1]["prev_hash"] = "tampered"
    events[1]["hash"] = _timeline_hash(events[1])
    assert _verify_chain(events) is False


def test_empty_file_starts_a_chain_at_the_versioned_seed(tmp_path):
    assert _timeline_prev_hash(tmp_path / "missing.jsonl") == TIMELINE_CHAIN_SEED
    empty = tmp_path / "timeline_2026-01-01.jsonl"
    empty.write_text("", encoding="utf-8")
    assert _timeline_prev_hash(empty) == TIMELINE_CHAIN_SEED
