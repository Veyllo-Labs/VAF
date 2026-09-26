# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A task's run log is never a task.

MEASURED LIVE: the automation list showed a nameless daily automation at 06:00 that could not
be deleted - the delete button did nothing, and it was back after every restart. It was
`<id>.runs.json`, the run log of a one-time automation that had been deleted when its run ended
and then logged that run: every reader globbed `*.json`, loaded the log as a task with every
field defaulted, and invented a new random id on each load, so the list could never name it to
delete it. The scheduler armed it for 06:00 with an empty prompt. And every automation that has
run keeps such a log beside its task file, so each one would have grown a phantom twin.

MUTATION: let the loader glob `*.json` again and the first test goes red; let append_run_log
write beside a missing task and the orphan test goes red; accept a record without an id and
the id test goes red.
"""
import json

import pytest

import vaf.core.automation as auto
from vaf.core.automation import AutomationManager, AutomationTask
from vaf.core.platform import Platform


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    d = tmp_path / "automations"
    d.mkdir()
    return d


def _write_task(directory, **kw):
    task = AutomationTask(**{"id": "a1b2c3d4", "name": "Wetter", "frequency": "daily",
                             "time": "07:15", **kw})
    (directory / f"{task.id}.json").write_text(json.dumps(task.to_dict()), encoding="utf-8")
    return task


def test_a_run_log_beside_its_task_is_not_loaded_as_a_second_task(store):
    task = _write_task(store)
    auto.append_run_log(store / f"{task.id}.json", status="success", started_at="2026-09-26T07:15:00",
                        duration_seconds=3.0, summary="ok")
    assert (store / f"{task.id}.runs.json").exists(), "the setup is real: the log exists"
    loaded = AutomationManager(storage_dir=str(store)).list()
    assert [t.id for t in loaded] == [task.id], [(t.id, t.name, t.time) for t in loaded]
    # By name, not only because a log has no id: a later log format may well carry one.
    assert [p.name for p in auto._task_files(store)] == [f"{task.id}.json"]


def test_an_orphaned_run_log_is_no_task_anywhere(store):
    (store / "deadbeef.runs.json").write_text(json.dumps({"format": "autorun-1-7c41d9", "runs": []}),
                                              encoding="utf-8")
    manager = AutomationManager(storage_dir=str(store))
    assert manager.list() == []
    assert auto._slot_occupancy(store) == {}, "a log must not take a 06:00 slot either"
    (manager.trash_dir / "deadbeef.runs.json").write_text("{}", encoding="utf-8")
    assert manager.list_trash() == []


def test_a_run_is_not_logged_for_a_task_that_is_gone(store):
    """A one-time task is deleted when its run ends, before the outcome is recorded."""
    auto.append_run_log(store / "a1b2c3d4.json", status="success", started_at="2026-09-26T07:15:00",
                        duration_seconds=3.0)
    assert not (store / "a1b2c3d4.runs.json").exists()


def test_a_record_without_an_id_is_not_a_task(store):
    (store / "noid.json").write_text(json.dumps({"name": "x", "time": "06:00"}), encoding="utf-8")
    assert AutomationManager(storage_dir=str(store)).list() == []
