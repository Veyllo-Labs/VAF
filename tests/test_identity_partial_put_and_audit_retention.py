# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One incident, three defects: a wiped profile, a nameless log entry, a vanishing trail.

On 2026-08-05 the owner's stored profile lost `main_messenger`, `city`, `country`,
`timezone`, `date_format` and `time_format`. Nobody edited them. Closing the "what's new"
dialog after an update sends a single field to `PUT /user-identity`, and the route read
its payload with `data.dict()` - which, under Pydantic 2, returns EVERY model field and
fills the ones the client never sent with `None`. `if "city" in full_dict` is therefore
always true, so each branch wrote that `None` into the profile and logged it as
"Manual edit: updated city". The quiet-hours block a few lines below had already been
fixed for exactly this reason, with a comment naming the announcement dialog; the other
six fields were left, so every release fired it again.

Reconstructing that took file mtimes, prompt logs and session archives, because two more
things were missing:

- The route binds the username as a dependency, uses it to find the workspace, and then
  does not write it into the entry. A change log that records what but never who answers
  the wrong half of the question.
- `security_events_<date>.jsonl` and `security_<date>.log` share the app log directory and
  the dated-name convention with ordinary logs, so the garbage collector's 48-hour rule
  matched them. Measured while investigating: not one security log survived on the
  machine. An audit trail that erases itself after two days is not an audit trail, and
  its absence is invisible - deletion looks exactly like "nothing ever happened".

Each test below is the pin for one of those, and the empty-string cases matter as much as
the missing ones: clearing a field ON PURPOSE has to keep working, or the fix trades a
data-loss bug for a data-stuck bug.
"""
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from vaf.api.user_persona_routes import UserIdentityUpdate
from vaf.core.garbage_collector import DATED_LOG_PATTERN, is_security_log

_PROTECTED = ("main_messenger", "city", "country", "timezone", "date_format", "time_format")


# ── The data loss ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("field", _PROTECTED)
def test_a_field_that_was_not_sent_is_not_in_the_update_dict(field):
    """The membership test the route now uses. `data.dict()` would answer true for all of
    these on a payload that mentions none of them, which is precisely the defect."""
    payload = UserIdentityUpdate(**{"last_seen_announcement_version": "0.1.0a20"})
    assert field not in payload.model_dump(exclude_none=True)
    assert field in payload.model_dump(), (
        "if this ever stops holding, the bug shape changed and the comment in the route "
        "needs revisiting - the point is that the unfiltered dict DOES contain it"
    )


@pytest.mark.parametrize("field", _PROTECTED)
def test_clearing_a_field_on_purpose_still_reaches_the_route(field):
    """An empty string is not None, so `exclude_none` keeps it. Without this the fix would
    make fields impossible to clear, which is a worse bug than the one it replaces."""
    payload = UserIdentityUpdate(**{field: ""})
    sent = payload.model_dump(exclude_none=True)
    assert field in sent and sent[field] == ""


def test_the_route_does_not_gate_profile_fields_on_the_unfiltered_dict():
    """Source-level pin, because the runtime path needs a workspace and an auth dependency.
    `full_dict` may survive for `last_seen_announcement_version`, which is a system field
    that is explicitly allowed to be written from a partial payload - but no profile field
    may be gated on it again."""
    src = Path(__file__).resolve().parent.parent / "vaf" / "api" / "user_persona_routes.py"
    text = src.read_text(encoding="utf-8")

    # Every membership test against the unfiltered dict, not just the ones written with a
    # literal field name. Two of the six fields are checked through a LOOP variable
    # (`loc_key`, `dt_key`), so an earlier version of this test that only looked for
    # '"city" in full_dict' stayed green while the defect was reintroduced - found by
    # mutating the loop back and watching nothing turn red.
    import re as _re

    gates = _re.findall(r"(\S+) in full_dict", text)
    allowed = {'"last_seen_announcement_version"'}
    offenders = [g for g in gates if g not in allowed]
    assert not offenders, (
        f"profile fields gated on the unfiltered dict again: {offenders}. A payload that "
        f"omits them will wipe them, exactly as the announcement dialog did."
    )
    assert '"last_seen_announcement_version" in full_dict' in text, (
        "the one deliberate use is gone; if that was intended, update this test and say why"
    )


# ── The nameless entry ───────────────────────────────────────────────────────

def test_the_change_log_entry_records_who_and_what_really_happened():
    """Pins both halves: the username the route already has, and a verb that follows the
    outcome. "updated" was written even when a field ended up empty, which is how a
    wipe read like an edit."""
    src = Path(__file__).resolve().parent.parent / "vaf" / "api" / "user_persona_routes.py"
    text = src.read_text(encoding="utf-8")
    assert '"user": username' in text, "the change log still throws the caller away"
    assert "cleared" in text, "a wiped field must not be reported as 'updated'"


# ── The vanishing audit trail ────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "security_events_2026-08-05.jsonl",
    "security_2026-08-05.log",
])
def test_security_logs_are_recognised_as_an_audit_trail(name):
    assert DATED_LOG_PATTERN.match(name), "precondition: the GC sees these files at all"
    assert is_security_log(name)


@pytest.mark.parametrize("name", [
    "backend_2026-08-05.log",
    "queue_2026-08-05.log",
    "timeline_2026-08-05.jsonl",
    "prompt_2026-08-05.log",
])
def test_ordinary_logs_keep_the_short_retention(name):
    """The exemption has to stay narrow: everything else still goes after 48 hours, or the
    log directory grows without bound."""
    assert not is_security_log(name)


def test_the_retention_is_configurable_and_defaults_to_two_weeks():
    from vaf.core.config import Config

    assert Config.DEFAULTS["security_log_retention_days"] == 14


def test_a_security_log_survives_the_ordinary_cutoff(tmp_path):
    """The behaviour, not just the predicate: a file older than the 48-hour rule but
    younger than the audit retention must still be there afterwards."""
    from vaf.core.garbage_collector import GarbageCollector

    old_day = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
    audit = tmp_path / f"security_events_{old_day}.jsonl"
    plain = tmp_path / f"backend_{old_day}.log"
    audit.write_text("{}\n", encoding="utf-8")
    plain.write_text("noise\n", encoding="utf-8")

    gc = GarbageCollector()
    stats = {"deleted": 0, "freed_bytes": 0, "errors": 0}
    import vaf.core.log_helper as lh

    original = lh.get_app_log_dir
    lh.get_app_log_dir = lambda: tmp_path
    try:
        gc._clean_log_files(datetime.now() - timedelta(hours=48), stats)
    finally:
        lh.get_app_log_dir = original

    assert audit.exists(), "the audit trail was deleted by the ordinary 48-hour rule again"
    assert not plain.exists(), "ordinary logs must still be collected"
