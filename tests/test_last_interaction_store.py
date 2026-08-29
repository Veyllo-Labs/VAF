# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The last-interaction store must survive a corrupt/truncated file.

Live incident: a truncated last_interaction.json (a partial write left by a
test run against the real data dir) made json.loads raise inside the same
swallowed try that performs the write. Result: update_last_interaction()
silently stopped recording forever, get_idle_user_scope_ids() saw no idle
users, and thinking mode never ran again on that machine. The store must
treat a corrupt file as empty and heal it on the next write, like the other
per-user JSON stores (see thinking_requests._load/_save).
"""
import json

import vaf.core.last_interaction as li
import vaf.core.thinking_mode as tm
from vaf.core.platform import Platform


CORRUPT = '{"ab12cd34": {"ts": 17654'  # truncated mid-write, invalid JSON


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path))


def test_update_heals_corrupt_store(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    store = tmp_path / "last_interaction.json"
    store.write_text(CORRUPT, encoding="utf-8")

    li.update_last_interaction("green123456", source="web", preview="hello")

    data = json.loads(store.read_text(encoding="utf-8"))
    assert "green123456" in data
    entry = li.get_last_interaction("green123456")
    assert entry is not None
    assert entry["source"] == "web"


def test_corrupt_store_reads_as_missing_not_crash(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    store = tmp_path / "last_interaction.json"
    store.write_text(CORRUPT, encoding="utf-8")

    assert li.get_last_interaction("green123456") is None
    assert tm.get_idle_user_scope_ids(10) == []


def test_update_drops_non_dict_payload(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    store = tmp_path / "last_interaction.json"
    store.write_text('["not", "a", "dict"]', encoding="utf-8")

    li.update_last_interaction("green123456", source="cli", preview="")

    data = json.loads(store.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert "green123456" in data
