# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The set-level reader for Whare Wananga training runs, and the route on it.

Why it exists: jobs.get_status/is_running both make the caller name the tool
first. The tools modal header is exactly the caller that cannot - four lanes can
start a run (the web route, the eager worker, the retrain drain, the teacher) -
so without this reader the browser would have to poll once per installed tool.

Two properties are easy to lose again and are pinned here:
  * finished jobs are rewritten in place and NEVER removed from _jobs, so an
    unfiltered read returns a history rather than a work list;
  * get_status hands out a shallow copy whose `events` list is the same object
    the worker thread keeps appending to, so an aggregate must not carry it.
"""
import asyncio

import pytest

import vaf.core.web_server as ws
import vaf.whare_wananga.jobs as jobs


@pytest.fixture
def job_table(monkeypatch):
    """An isolated job table; the real one is a module global shared by threads."""
    table = {}
    monkeypatch.setattr(jobs, "_jobs", table)
    return table


def _running(tool, **extra):
    return {"tool": tool, "state": "running", "attempt": 2, "hits": 1, "fails": 0,
            "phase": "learn", "round": 0, "max_rounds": 3, "validate": None,
            "started_at": 1000.0, "events": [{"kind": "probe"}], **extra}


def test_only_running_jobs_are_reported(job_table):
    job_table["send_telegram"] = _running("send_telegram")
    job_table["read_file"] = _running("read_file", state="done")
    job_table["web_search"] = _running("web_search", state="error")

    assert [r["tool"] for r in jobs.active_runs()] == ["send_telegram"]


def test_a_run_disappears_when_it_finishes_in_place(job_table):
    job_table["memory_search"] = _running("memory_search")
    assert len(jobs.active_runs()) == 1
    # How a run actually ends: the entry is rewritten, not deleted.
    job_table["memory_search"]["state"] = "done"
    assert jobs.active_runs() == []


def test_the_live_event_list_never_rides_along(job_table):
    job_table["bash"] = _running("bash")
    (run,) = jobs.active_runs()
    assert "events" not in run
    # And the rest is a copy, so a reader cannot reach into the worker's dict.
    run["phase"] = "tampered"
    assert job_table["bash"]["phase"] == "learn"


def test_the_fields_the_header_renders_are_present(job_table):
    job_table["send_email"] = _running("send_email")
    (run,) = jobs.active_runs()
    for field in ("tool", "state", "phase", "attempt", "started_at"):
        assert field in run, field


def test_concurrent_runs_are_all_reported(job_table):
    # start_training refuses a second run of the SAME tool only, so two different
    # tools training at once is a real state the header has to render.
    job_table["a"] = _running("a")
    job_table["b"] = _running("b")
    assert {r["tool"] for r in jobs.active_runs()} == {"a", "b"}


def test_the_reader_is_on_the_package_facade():
    from vaf.whare_wananga import active_runs
    assert active_runs is jobs.active_runs


def test_route_consumes_the_primitive_instead_of_filtering_again(job_table):
    # Rule 0c inversion guard: the route must not grow its own copy of the
    # "which of these is running" rule. Same input, same answer, by construction.
    job_table["send_discord"] = _running("send_discord")
    job_table["list_dir"] = _running("list_dir", state="done")

    payload = asyncio.run(ws.whare_wananga_active_runs())
    assert payload["ok"] is True
    assert payload["runs"] == jobs.active_runs()
    assert [r["tool"] for r in payload["runs"]] == ["send_discord"]


def test_route_answers_with_an_empty_list_when_nothing_is_training(job_table):
    payload = asyncio.run(ws.whare_wananga_active_runs())
    assert payload == {"ok": True, "runs": []}
