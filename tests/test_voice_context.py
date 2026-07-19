# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Rolling transcript buffer (vaf/core/voice_context.py) - bounded + isolated.

Pins the reflex-system store contract: per (scope, session) isolation, retention by
age and count, an LRU cap over keys, bounded digest, and the never-raise degradation.
"""
import vaf.core.voice_context as vc


def _fresh():
    with vc._LOCK:
        vc._BUFFERS.clear()


def test_record_and_recent_order():
    _fresh()
    vc.record("s1", "sess", "erste", label="self", verdict="respond_now")
    vc.record("s1", "sess", "zweite", label="other", verdict="store_only")
    got = vc.recent("s1", "sess")
    assert [e[3] for e in got] == ["erste", "zweite"]
    assert got[0][1] == "self" and got[1][1] == "other"


def test_empty_and_whitespace_skipped():
    _fresh()
    vc.record("s1", "sess", "   ")
    vc.record("s1", "sess", "")
    assert vc.recent("s1", "sess") == []


def test_isolation_per_scope_and_session():
    _fresh()
    vc.record("alice", "a", "geheim-a")
    vc.record("bob", "a", "geheim-b")
    vc.record("alice", "b", "andere-session")
    assert [e[3] for e in vc.recent("alice", "a")] == ["geheim-a"]
    assert [e[3] for e in vc.recent("bob", "a")] == ["geheim-b"]
    assert [e[3] for e in vc.recent("alice", "b")] == ["andere-session"]


def test_retention_by_age():
    _fresh()
    import time
    now = time.time()
    # recent() prunes relative to the real clock, so anchor the timestamps to now.
    vc.record("s", "x", "alt", ts=now - vc._MAX_AGE_S - 5)
    vc.record("s", "x", "neu", ts=now)
    got = vc.recent("s", "x")
    assert [e[3] for e in got] == ["neu"]


def test_since_scopes_to_post_engagement_talk():
    """`since` returns only entries at/after a wall-clock ts - used to give an engaged
    guest ONLY the group talk after engagement, never the owner's earlier private 1:1."""
    _fresh()
    import time
    now = time.time()
    vc.record("s", "x", "private 1:1", label="self", ts=now - 300)   # pre-engagement
    vc.record("s", "x", "hallo zusammen", label="self", ts=now - 30)  # post-engagement
    vc.record("s", "x", "merhaba", label="other", ts=now - 20)
    vc.record("s", "x", "hos geldiniz", label="agent", ts=now - 10)
    since = now - 60
    got = [e[3] for e in vc.recent("s", "x", n=12, since=since)]
    assert got == ["hallo zusammen", "merhaba", "hos geldiniz"]
    assert "private 1:1" not in vc.digest("s", "x", since=since)
    # no `since` still returns everything (unchanged default)
    assert len(vc.recent("s", "x", n=12)) == 4


def test_max_entries_cap():
    _fresh()
    for i in range(vc._MAX_ENTRIES + 25):
        vc.record("s", "x", f"u{i}")
    got = vc.recent("s", "x", n=100000)
    assert len(got) <= vc._MAX_ENTRIES
    assert got[-1][3] == f"u{vc._MAX_ENTRIES + 24}"   # newest kept


def test_lru_cap_over_keys():
    _fresh()
    for i in range(vc._MAX_KEYS + 10):
        vc.record(f"scope{i}", "x", "hi")
    with vc._LOCK:
        assert len(vc._BUFFERS) <= vc._MAX_KEYS


def test_digest_is_bounded_and_labelled():
    _fresh()
    for i in range(40):
        vc.record("s", "x", "x" * 300, label="other")
    d = vc.digest("s", "x", n=12, per_item=160, total_cap=1200)
    assert len(d) <= 1200
    assert "[other]" in d


def test_clear_drops_the_session():
    _fresh()
    vc.record("s", "x", "hi")
    vc.clear("s", "x")
    assert vc.recent("s", "x") == []


def test_recent_zero_returns_nothing_not_everything():
    """n<=0 means 'no entries' - NOT the whole buffer ([-0:] == [:] trap)."""
    _fresh()
    for i in range(5):
        vc.record("s", "x", f"u{i}")
    assert vc.recent("s", "x", n=0) == []
    assert vc.recent("s", "x", n=-3) == []
    assert vc.digest("s", "x", n=0) == ""
    assert len(vc.recent("s", "x", n=3)) == 3   # positive n still works


def test_read_refreshes_lru_so_active_key_survives_eviction():
    """A key that is actively READ (polled by the policy while the owner is briefly
    silent) must not be evicted just because it stopped receiving new utterances."""
    _fresh()
    vc.record("A", "s", "keep-me")
    for i in range(vc._MAX_KEYS + 20):
        vc.record(f"k{i}", "s", "x")
        vc.recent("A", "s")            # each read refreshes A's recency
    got = vc.recent("A", "s")
    assert got and got[0][3] == "keep-me"
