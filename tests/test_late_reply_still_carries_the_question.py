# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""An answer that arrives late still reaches an agent that knows what was asked.

The background run asks the user something, chases it for a while, and then stops
chasing. Stopping the chase used to DELETE the record of the question, and the record
is the only thing that tells the main agent what a later message is answering. So a
user who came back after the chase had ended got an agent that behaved as if it had
never asked anything.

The live incident, read off the runtime files to the second: the question went to
Telegram, was escalated to the web chat at 11:49:50, the give-up deleted the waiting
record at 11:59:51, the user answered "ja bin ich was gibts?" at 12:31:55 - and the
reply landed on an agent with no question in hand, which answered "nothing much going
on". The request record still reads status "asked" with an empty user_reply.

The two halves are now separate lifetimes, and this file pins that:

- the CHASE (nudge at 3 min, one escalation, then stop) ends on schedule, so nothing
  keeps pestering the user or blocking background runs;
- the QUESTION is kept until `thinking_reply_wait_ttl_hours` - the TTL that already
  existed for a latch left behind by a crashed or disabled thinking mode - so the main
  agent's reply pickup can still frame the answer.

Mutation check for every test below: restore `clear_waiting_for_reply(user_scope_id)`
in the give-up branch of `_process_waiting_reply` and they fail.
"""
import time
from pathlib import Path

import pytest

import vaf.core.thinking_mode as tm
from vaf.core.platform import Platform

_REPO = Path(__file__).resolve().parents[1]
_AGENT = _REPO / "vaf" / "core" / "agent.py"
_THINKING = _REPO / "vaf" / "core" / "thinking_mode.py"

SCOPE = "ab12cd34-0000-4000-8000-000000000042"
QUESTION = "Wolltest du heute mit dem Commit weitermachen - laeuft das schon?"


def _src(path: Path) -> str:
    # CRLF-normalised: git can check this out with CRLF on the Windows CI runner.
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


@pytest.fixture
def store(monkeypatch, tmp_path):
    """Isolate both stores; the waiting latch lives in data_dir, requests in vaf_dir."""
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr("vaf.core.last_interaction.get_last_interaction", lambda scope: None)
    return tmp_path


def _ask(channel="web", escalated=False):
    tm.set_waiting_for_reply(
        SCOPE, username="admin", display_name="admin", question_text=QUESTION,
        request_id="req-1", session_id="sid-anchor", channel=channel, escalated_to_web=escalated,
    )


def _past_skip():
    """Minutes to elapse so the give-up window has passed, whatever `thinking_wait_skip_minutes` is."""
    from vaf.core.config import Config
    return float(Config.get("thinking_wait_skip_minutes", 40) or 40) + 1


def _elapse(minutes):
    """Move the question back in time so the given number of minutes has 'passed'."""
    data = tm._load_waiting()
    key = tm._key(SCOPE)
    data[key]["question_sent_at_ts"] = time.time() - minutes * 60
    tm._save_waiting(data)


def test_the_question_survives_the_give_up(store):
    """The incident itself: the chase ends at the skip window, the user answers long after."""
    _ask(channel="web")
    _elapse(_past_skip())

    assert tm._process_waiting_reply(SCOPE) == "allow_run"      # chase over

    _elapse(_past_skip() + 30)                                   # the user comes back much later
    w = tm.get_waiting_for_reply(SCOPE)
    assert w is not None, "the question was deleted - the main agent cannot frame the reply"
    assert w["question_text"] == QUESTION
    assert w["request_id"] == "req-1", "without the request id the answer is never recorded"
    assert w["session_id"] == "sid-anchor"


def test_the_kept_question_stops_the_chase(store, monkeypatch):
    """Keeping the record must not keep nudging: no nudge, no escalation, runs allowed."""
    nudges = []
    monkeypatch.setattr(tm, "_send_nudge",
                        lambda *a, **k: (nudges.append(a), True)[1])
    _ask(channel="web")
    _elapse(_past_skip())
    assert tm._process_waiting_reply(SCOPE) == "allow_run"
    assert tm.chase_is_active(tm.get_waiting_for_reply(SCOPE)) is False

    for _ in range(3):        # every later run passes through the same lane
        _elapse(_past_skip() + 20)
        assert tm._process_waiting_reply(SCOPE) == "allow_run"
    assert nudges == [], "a question we gave up chasing must never nudge again"


def test_a_kept_question_still_expires_with_the_ttl(store):
    """Kept is not immortal: past the TTL the latch is gone, so a fresh message days later
    is never framed as the answer to a forgotten question (the hijack this TTL exists for)."""
    _ask(channel="web")
    _elapse(_past_skip())
    assert tm._process_waiting_reply(SCOPE) == "allow_run"

    _elapse(13 * 60)          # default thinking_reply_wait_ttl_hours = 12
    assert tm.get_waiting_for_reply(SCOPE) is None
    assert tm._key(SCOPE) not in tm._load_waiting(), "expired latch must be cleared, not hidden"


def test_a_new_question_re_opens_the_chase(store):
    """The next question is chased normally - the ended chase must not stick to the slot."""
    _ask(channel="web")
    _elapse(_past_skip())
    assert tm._process_waiting_reply(SCOPE) == "allow_run"

    tm.set_waiting_for_reply(SCOPE, username="admin", question_text="Neue Frage?")
    w = tm.get_waiting_for_reply(SCOPE)
    assert tm.chase_is_active(w) is True
    assert w["question_text"] == "Neue Frage?"


def test_a_legacy_latch_without_the_field_is_chased_then_ended(store):
    """A record written before this field existed has no `chase_ended_at_ts` key at all. It must
    read as an ACTIVE chase (else the nudge/escalation lane skips it silently on upgrade)."""
    key = tm._key(SCOPE)
    tm._save_waiting({key: {
        "question_sent_at_ts": time.time() - _past_skip() * 60,
        "nudge_sent_at_ts": None,
        "username": "admin", "display_name": "admin",
        "question_text": QUESTION, "request_id": "req-old", "session_id": "sid-old",
    }})
    assert tm.chase_is_active(tm.get_waiting_for_reply(SCOPE)) is True
    assert tm._process_waiting_reply(SCOPE) == "allow_run"
    assert tm.chase_is_active(tm.get_waiting_for_reply(SCOPE)) is False
    assert tm.get_waiting_for_reply(SCOPE)["question_text"] == QUESTION


def test_the_dashboard_stops_saying_waiting_when_the_chase_ends(store):
    """The admin panel's line is "waiting for a reply (<channel>)". Once the chase is over that
    sentence is false, and the open question is still visible in the same panel's request list."""
    _ask(channel="web")
    assert (tm.thinking_status_snapshot().get(tm._key(SCOPE)) or {}).get("waiting") is not None

    _elapse(_past_skip())
    assert tm._process_waiting_reply(SCOPE) == "allow_run"
    snap = tm.thinking_status_snapshot().get(tm._key(SCOPE)) or {}
    assert snap.get("waiting") is None, "the panel would claim a wait that has ended"


# ── wiring: the two readers in the main agent ────────────────────────────────
# These cannot be exercised through chat_step in a unit test (it needs a model, a
# session and a backend), so the wiring is pinned at the source. Both mutations are
# real regressions: gate the pickup on an active chase and the late reply is blind
# again; drop the gate on the brake and a question nobody chases any more holds the
# agent's write actions for the whole TTL.

def test_the_reply_pickup_reads_the_record_without_a_chase_gate():
    src = _src(_AGENT)
    start = src.index("NUDGE KILLER")
    lane = src[start:src.index("self._thinking_reply_context = None", start)]
    assert "waiting = get_waiting_for_reply(_scope)" in lane, \
        "the reply pickup no longer reads the waiting record"
    assert "chase_is_active" not in lane, (
        "the reply pickup must NOT require an active chase - a late answer arrives after the "
        "chase has ended, which is the whole point of keeping the record"
    )


def test_the_ask_first_brake_requires_an_active_chase():
    src = _src(_AGENT)
    anchor = src.index("Ask-first invariant: latch")
    brake = src[anchor - 1500:anchor]
    assert "chase_is_active(get_waiting_for_reply(" in brake, (
        "the brake keys on the record's existence again: a question kept only so a late reply is "
        "understood would block write actions for the rest of the TTL"
    )


def test_the_give_up_branch_does_not_delete_the_question():
    """The regression in one line: the branch past the skip window must end the chase, not clear."""
    src = _src(_THINKING)
    body = src[src.index("def _process_waiting_reply"):src.index("def _get_known_scope_ids")]
    assert "end_reply_chase(user_scope_id)" in body
    assert "clear_waiting_for_reply(" not in body, (
        "giving up deletes the question again - a reply arriving later reaches an agent that "
        "does not know it ever asked"
    )
