# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The one list of what VAF keeps where (vaf/core/data_stores.py).

A backup is only as complete as the list it reads. The list used to be half-copies in
three places, and a measured inventory found 19 doc statements that no longer matched the
code. The guard that matters is the static one: every literal name the code joins onto a
root must have a row, so a new store cannot ship without somebody deciding whether it is
backed up.
"""
import re
from pathlib import Path

import pytest

from vaf.core import data_stores as ds

ROOT = Path(__file__).resolve().parents[1]

_JOINS = {
    "vaf": [r"Platform\.vaf_dir\(\)\s*/\s*[\"']([^\"']+)[\"']",
            r"Path\.home\(\)\s*/\s*[\"']\.vaf[\"']\s*/\s*[\"']([^\"']+)[\"']"],
    "data": [r"Platform\.data_dir\(\)\s*/\s*[\"']([^\"']+)[\"']"],
    "config": [r"Platform\.config_dir\(\)\s*/\s*[\"']([^\"']+)[\"']"],
    "app": [r"Config\.APP_DIR\s*/\s*[\"']([^\"']+)[\"']"],
}


def _joined_names():
    found = set()
    for path in (ROOT / "vaf").rglob("*.py"):
        if path.name == "data_stores.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for root, patterns in _JOINS.items():
            for pattern in patterns:
                for match in re.finditer(pattern, text):
                    found.add((root, match.group(1), path.relative_to(ROOT).as_posix()))
    return found


def test_every_name_the_code_joins_onto_a_root_has_a_row():
    """MUTATION: delete any row the code still writes (`sessions` is not literal here, but
    `automations`, `spend` or `trust` are) - red, naming the file that writes it."""
    found = _joined_names()
    assert len(found) >= 60, f"the scan found only {len(found)} names; it is broken"
    missing = sorted(f"{root}/{name} ({where})" for root, name, where in found
                     if ds.classify(name, [s for s in ds.STORES if s.root == root]) is None)
    assert not missing, "unclassified stores:\n" + "\n".join(missing)


def test_the_rows_are_well_formed():
    roots = {"app", "vaf", "data", "config", "projects"}
    for row in ds.STORES:
        assert row.root in roots, row
        assert row.kind in ds.KINDS, row
        if row.owner.startswith("vaf/") or row.owner == "install.sh":
            assert (ROOT / row.owner).exists(), f"stale owner: {row}"
    assert ds.BACKED_UP <= ds.KINDS


@pytest.mark.parametrize("root,rel,kind", [
    ("data", "logs", ds.LOG),
    ("data", "logs/security_events_2026-10-16.jsonl", ds.AUDIT),
    ("data", "logs/web_2026-10-16.log", ds.LOG),
    ("vaf", "sessions/web_chat-1.json", ds.DATA),
    ("vaf", "a2a/rooms/r1/room.json", ds.DATA),
    ("vaf", "a2a/session/r1/inbox.ndjson", ds.RUNTIME),
    ("data", "VAF-BackThisUp.md", ds.NEVER),
    ("data", "provider_keys.enc", ds.KEY),
    ("data", "provider_keys.key.json", ds.KEY),
    ("app", "users/alice/whatsapp/creds.json", ds.RECREATABLE),
    ("app", "users/alice/soul.md", ds.DATA),
    ("app", "config.json.pre-keyring.bak", ds.NEVER),
    ("projects", "ab12cd34/web_chat-1/notes.md", ds.DATA),
])
def test_the_most_specific_row_decides(root, rel, kind):
    """A nested row wins over its parent: the security log inside `logs`, the WhatsApp login
    inside a user's folder. MUTATION: take the first match instead of the deepest - red."""
    row = ds.classify(rel, [s for s in ds.STORES if s.root == root])
    assert row is not None and row.kind == kind, (rel, row)


@pytest.fixture
def fake_roots(tmp_path, monkeypatch):
    home = tmp_path / "dotvaf"
    roots = {"app": home, "vaf": home, "data": tmp_path / "data", "config": tmp_path / "config",
             "projects": tmp_path / "projects"}
    for path in roots.values():
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ds, "roots", lambda: roots)
    monkeypatch.setattr(ds, "projects_dir", lambda: roots["projects"])
    files = {
        home / "config.json": "{}",
        home / "secure_store.kek": "k",
        home / "sessions" / "web_chat-1.json": "{}",
        home / "context_archive" / "c.json": "{}",
        home / "logs" / "vaf_run.log": "x",
        home / "vm-backups" / "dump.sql.gz": "x",
        roots["data"] / "logs" / "security_events_2026-10-16.jsonl": "{}",
        roots["data"] / "logs" / "web_2026-10-16.log": "x",
        roots["data"] / "VAF-BackThisUp.md": "AGE-SECRET-KEY-1",
        roots["config"] / "trust" / "default.json": "{}",
        roots["projects"] / "ab12cd34" / "web_chat-1" / "notes.md": "n",
    }
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return roots


def _walked(include_projects=True):
    out = {}
    for root, path, row in ds.walk(include_projects=include_projects):
        out[path] = row
    return out


def test_the_walk_takes_what_is_backed_up_and_reports_the_rest(fake_roots):
    """app and vaf are one folder on a normal install: both halves of the list describe it,
    once. An unknown folder is reported as it is and never entered. MUTATION: enter unknown
    directories, or drop the AUDIT row below `logs` - red."""
    walked = _walked()
    home, data = fake_roots["app"], fake_roots["data"]
    taken = {p for p, row in walked.items() if ds.backed_up(row)}
    assert taken == {
        home / "config.json", home / "secure_store.kek", home / "sessions" / "web_chat-1.json",
        data / "logs" / "security_events_2026-10-16.jsonl",
        fake_roots["config"] / "trust" / "default.json",
        fake_roots["projects"] / "ab12cd34" / "web_chat-1" / "notes.md",
    }
    assert walked[home / "vm-backups"] is None, "an unknown folder is reported, not entered"
    assert home / "vm-backups" / "dump.sql.gz" not in walked
    assert walked[home / "context_archive"].kind == ds.DERIVED
    assert walked[data / "VAF-BackThisUp.md"].kind == ds.NEVER
    assert sum(1 for p in walked if p.parent == home and p.name == "config.json") == 1


def test_the_working_folders_can_be_left_out(fake_roots):
    walked = _walked(include_projects=False)
    assert not any(fake_roots["projects"] in p.parents for p in walked)


def test_a_link_is_never_followed(fake_roots, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("not ours", encoding="utf-8")
    link = fake_roots["app"] / "sessions" / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform cannot create a symlink here")
    walked = _walked()
    assert link in walked and outside / "secret.txt" not in walked
