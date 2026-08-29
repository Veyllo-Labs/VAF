# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""What a background run may honestly say about a user's existing automations.

The record carries `last_run`, and `_stamp_successful_run` writes it ONLY on success. So a task
that errors every morning and a task whose machine was switched off for a week leave byte-identical
records. Every finding here stays inside that limit, and the tests pin the limit itself: no finding
may claim a failure, an error, or anything about output quality.

Findings are computed in code, not read out of an automation's prompt by a model: a prompt reads
like evidence while saying nothing about how the job behaved."""
from datetime import datetime, timedelta

from vaf.core.automation import AutomationTask, review_findings

NOW = datetime(2026, 8, 29, 12, 0, 0)


def _task(**kw):
    base = dict(id="a1", name="Wetter", prompt="Wetterbericht fuer Berlin holen und senden",
                frequency="daily", time="07:15", enabled=True,
                created_at=(NOW - timedelta(days=30)).isoformat())
    base.update(kw)
    return AutomationTask(**base)


def _kinds(tasks):
    return {f["kind"] for f in review_findings(tasks, now=NOW)}


# ── the findings the record actually supports ─────────────────────────────────────────────

def test_never_completed_is_reported():
    assert "never_completed" in _kinds([_task(last_run=None)])


def test_a_young_task_is_given_time():
    """One missed occurrence is noise. Two periods is an observation."""
    young = _task(last_run=None, created_at=(NOW - timedelta(hours=6)).isoformat())
    assert "never_completed" not in _kinds([young])


def test_no_recent_success_is_reported():
    stale = _task(last_run=(NOW - timedelta(days=9)).isoformat())
    assert "no_recent_success" in _kinds([stale])


def test_a_task_that_ran_this_morning_is_quiet():
    fresh = _task(last_run=(NOW - timedelta(hours=5)).isoformat())
    assert review_findings([fresh], now=NOW) == []


def test_a_once_task_that_ran_is_done_not_stale():
    """ONCE has no period, so 'overdue' is meaningless for it."""
    once = _task(frequency="once", last_run=(NOW - timedelta(days=400)).isoformat())
    assert "no_recent_success" not in _kinds([once])


def test_disabled_and_old_is_reported_and_nothing_else():
    """A disabled task produces exactly one finding: it is off. Everything else - overdue, dead
    output path - would be nonsense about a job that is not scheduled to run at all."""
    off = _task(enabled=False, created_at=(NOW - timedelta(days=90)).isoformat(),
                last_run=(NOW - timedelta(days=200)).isoformat(), output_path="/nope/out.md")
    assert _kinds([off]) == {"disabled_and_old"}


def test_a_recently_disabled_task_is_not_called_forgotten():
    off = _task(enabled=False, created_at=(NOW - timedelta(days=3)).isoformat())
    assert review_findings([off], now=NOW) == []


def test_slot_collision_is_reported():
    a = _task(id="a1", name="Wetter", time="07:15")
    b = _task(id="a2", name="Kalender", time="07:15", prompt="Kalender pruefen und Termine melden")
    assert "slot_collision" in _kinds([a, b])


def test_near_duplicate_prompts_are_reported():
    a = _task(id="a1", name="Wetter frueh", time="07:15",
              prompt="Hole den Wetterbericht fuer Berlin und schicke ihn mir zusammengefasst")
    b = _task(id="a2", name="Wetter spaet", time="08:20",
              prompt="Hole den Wetterbericht fuer Berlin und schicke ihn mir kurz zusammengefasst")
    assert "near_duplicate" in _kinds([a, b])


def test_different_prompts_are_not_flagged():
    a = _task(id="a1", time="07:15", prompt="Hole den Wetterbericht fuer Berlin und sende ihn")
    b = _task(id="a2", time="08:20", prompt="Pruefe meinen Kalender und melde anstehende Termine")
    assert "near_duplicate" not in _kinds([a, b])


def test_dead_output_path_is_reported(tmp_path):
    gone = _task(output_path=str(tmp_path / "does-not-exist" / "out.md"),
                 last_run=(NOW - timedelta(hours=5)).isoformat())
    assert "dead_output_path" in _kinds([gone])


def test_live_output_path_is_quiet(tmp_path):
    f = tmp_path / "out.md"
    f.write_text("x", encoding="utf-8")
    ok = _task(output_path=str(f), last_run=(NOW - timedelta(hours=5)).isoformat())
    assert review_findings([ok], now=NOW) == []


# ── the limit itself ──────────────────────────────────────────────────────────────────────

def test_no_finding_claims_a_failure():
    """`last_run` is written only on success, so the record cannot distinguish 'it errored' from
    'the machine was off'. A finding that used the language of failure would be a claim the data
    does not carry."""
    tasks = [
        _task(id="a1", last_run=None),
        _task(id="a2", last_run=(NOW - timedelta(days=9)).isoformat()),
        _task(id="a3", enabled=False, last_run=(NOW - timedelta(days=200)).isoformat()),
        _task(id="a4", output_path="/nope/out.md", last_run=(NOW - timedelta(hours=5)).isoformat()),
    ]
    forbidden = ("fail", "error", "broken", "crash", "fehler", "kaputt", "abgestuerzt",
                 "too slow", "bad output", "useless")
    for f in review_findings(tasks, now=NOW):
        text = f"{f['kind']} {f['detail']}".lower()
        for word in forbidden:
            assert word not in text, f"finding {f['kind']!r} claims more than the record supports: {f['detail']!r}"


def test_finding_keys_are_stable_for_dedup_across_runs():
    """De-duplication across runs keys on the FINDING, not the phrasing - otherwise the same
    observation is re-sent every run and starves the rungs below it."""
    t = _task(last_run=None)
    first = review_findings([t], now=NOW)
    second = review_findings([t], now=NOW + timedelta(hours=3))
    assert [f["key"] for f in first] == [f["key"] for f in second]
    assert first[0]["key"] == "a1:never_completed"


def test_no_automations_no_findings():
    assert review_findings([], now=NOW) == []


# ── the recorded run log (what closes the "errored vs switched off" gap) ──────────────────

def test_run_log_roundtrip(tmp_path):
    from vaf.core.automation import append_run_log, load_run_log
    task_file = tmp_path / "a1.json"
    append_run_log(task_file, status="success", started_at=NOW.isoformat(), duration_seconds=2.4)
    append_run_log(task_file, status="error", started_at=NOW.isoformat(), duration_seconds=0.2,
                   summary="Error: provider unreachable")
    log = load_run_log(task_file)
    assert [r["status"] for r in log] == ["success", "error"]
    assert log[1]["summary"].startswith("Error:")
    assert log[0]["seconds"] == 2.4


def test_run_log_is_bounded(tmp_path):
    from vaf.core.automation import append_run_log, load_run_log, _RUN_LOG_MAX
    task_file = tmp_path / "a1.json"
    for i in range(_RUN_LOG_MAX + 20):
        append_run_log(task_file, status="success", started_at=NOW.isoformat(), duration_seconds=i)
    log = load_run_log(task_file)
    assert len(log) == _RUN_LOG_MAX
    assert log[-1]["seconds"] == float(_RUN_LOG_MAX + 19)   # newest kept, oldest dropped


def test_run_log_carries_the_format_tag(tmp_path):
    import json
    from vaf.core.automation import append_run_log, _RUN_LOG_FORMAT
    task_file = tmp_path / "a1.json"
    append_run_log(task_file, status="success", started_at=NOW.isoformat(), duration_seconds=1)
    on_disk = json.loads((tmp_path / "a1.runs.json").read_text(encoding="utf-8"))
    assert on_disk["format"] == _RUN_LOG_FORMAT


def test_a_log_without_the_tag_still_loads(tmp_path):
    """A store that refuses to read what an earlier version wrote turns an upgrade into data loss."""
    import json
    from vaf.core.automation import load_run_log
    (tmp_path / "a1.runs.json").write_text(
        json.dumps({"runs": [{"status": "success", "at": NOW.isoformat(), "seconds": 1}]}),
        encoding="utf-8")
    assert len(load_run_log(tmp_path / "a1.json")) == 1


def test_a_corrupt_log_reads_as_empty_not_as_a_crash(tmp_path):
    from vaf.core.automation import load_run_log
    (tmp_path / "a1.runs.json").write_text("{not json", encoding="utf-8")
    assert load_run_log(tmp_path / "a1.json") == []


def test_repeated_errors_become_sayable_once_the_log_exists():
    """The payoff: with the record alone this task looks identical to one whose machine was off."""
    t = _task(last_run=(NOW - timedelta(days=9)).isoformat())
    errors = [{"status": "error", "at": (NOW - timedelta(days=d)).isoformat(), "seconds": 1}
              for d in (3, 2, 1)]
    kinds = {f["kind"] for f in review_findings([t], now=NOW, run_logs={"a1": errors})}
    assert "repeated_errors" in kinds
    # and without the log, the same task yields only the weaker, honest observation
    assert "repeated_errors" not in _kinds([t])


def test_a_single_error_is_not_a_pattern():
    t = _task(last_run=(NOW - timedelta(hours=5)).isoformat())
    mixed = [{"status": "success", "at": NOW.isoformat(), "seconds": 1},
             {"status": "error", "at": NOW.isoformat(), "seconds": 1}]
    assert review_findings([t], now=NOW, run_logs={"a1": mixed}) == []
