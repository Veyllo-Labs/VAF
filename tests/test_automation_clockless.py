# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One automation record with no readable clock, and what it may cost the others.

The answer has to be: nothing. A record is data that arrived from a file or a caller, and
the model coerces it at the boundary rather than trusting it. Measured before this file
existed: one record whose `time` could not be parsed made `list()` raise, and `list()` is
what the Web UI automations list, the CLI listing and the thinking-mode start gate all
read. So a single bad file took every automation surface down for every user, and the
create path admits exactly such a record, because the interval check steps aside whenever
the time has no colon in it.
"""
import json
from datetime import datetime

import pytest

from vaf.core.automation import (AutomationManager, AutomationTask,
                                 get_next_automation_run_utc)
from vaf.core.platform import Platform


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    root = tmp_path / "automations"
    root.mkdir()
    return root


def _task(**kw):
    base = dict(id="a1", name="Wetter", prompt="Wetterbericht holen", frequency="daily",
                time="07:15", enabled=True)
    base.update(kw)
    return AutomationTask(**base)


def _write(root, task):
    (root / f"{task.id}.json").write_text(json.dumps(task.to_dict()), encoding="utf-8")


@pytest.mark.parametrize("time", ["", "morgens", "25:00", "07", "7:60", None])
def test_a_record_with_no_readable_clock_has_no_next_run(time):
    """MUTATION: parse the time without the guard, or let the calendar's refusal escape.

    Each value is a different way of not being a clock: absent, a word, an hour that does
    not exist, one half, a minute that does not exist. None of them is a reason to raise.
    """
    task = _task(time=time)
    assert task.next_run_datetime is None
    assert task.next_run_iso == ""
    assert task.next_run_label == "-"


def test_a_frequency_this_version_does_not_know_has_no_next_run():
    """MUTATION: answer `now` for an unknown frequency.

    "Now" sorted such a record first and told the thinking-mode gate that something was
    due this instant, every time it asked. A frequency with no clock rule has no next
    clock run, and the honest answer is none.
    """
    assert _task(frequency="on_message").next_run_datetime is None


def test_a_readable_clock_still_answers():
    task = _task(time="07:15")
    when = task.next_run_datetime
    assert isinstance(when, datetime) and (when.hour, when.minute) == (7, 15)
    assert task.next_run_label.endswith(" 07:15")
    assert task.next_run_iso.startswith(when.isoformat()[:16])


def test_one_clockless_record_does_not_take_the_listing_down(store):
    """MUTATION: sort by next_run_datetime alone.

    The listing is read by every surface. It must answer with the readable records
    first and the unreadable one last, and the next create must still be checkable.
    """
    _write(store, _task(id="ok", time="07:15"))
    _write(store, _task(id="bad", time="morgens"))
    manager = AutomationManager(storage_dir=str(store))

    assert [t.id for t in manager.list()] == ["ok", "bad"], "the clockless record sorts last"
    can, _why = manager.check_can_create_automation("09:00", "daily")
    assert can


def test_the_thinking_mode_gate_ignores_a_clockless_record(store):
    """MUTATION: take min() over every record.

    The gate asks "when is the next automation due" to keep out of its way. A record
    with no clock is not due, and it must neither raise nor answer for the others.
    """
    _write(store, _task(id="bad", time="morgens"))
    assert get_next_automation_run_utc(None) is None, "nothing is due, and nothing raised"

    _write(store, _task(id="ok", time="07:15"))
    when = get_next_automation_run_utc(None)
    assert when is not None and (when.hour, when.minute) == (7, 15)
