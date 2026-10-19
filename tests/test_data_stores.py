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
import os
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


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="POSIX permission bits, which root ignores")
def test_a_directory_that_cannot_be_read_is_not_passed_over(fake_roots):
    """The walk skipped a directory it could not read, so a backup lacked it without a word
    (an unreadable FILE already failed the backup). MUTATION: suppress the error again - red."""
    locked = fake_roots["app"] / "sessions" / "locked"
    locked.mkdir()
    (locked / "chat.json").write_text("{}", encoding="utf-8")
    os.chmod(locked, 0)
    try:
        with pytest.raises(PermissionError):
            _walked()
    finally:
        os.chmod(locked, 0o700)


# ── how a store is protected at rest: the `sealed` and `harden` columns ──────────────────

# Modules that write through the encrypted seam without owning a sealed store, and why.
_SEAM_WRITERS_WITHOUT_A_ROW = {
    "vaf/core/main_persistence.py": "the working memory under the launch directory, which is "
                                    "not a root; the sweep adds it (sealed_trees(with_launch_dir))",
    "vaf/core/forget_secrets.py": "rewrites a file of another store in place, keeping whether "
                                  "it was encrypted",
    "vaf/core/at_rest_migration.py": "the sweep itself",
    "vaf/tools/browser_agent.py": "writes the store vaf/core/browser_interactive.py owns",
}
_SEAM_WRITE = re.compile(r"data_files\.write_(?:bytes|json|text)_atomic\(")


def test_every_writer_through_the_encrypted_seam_owns_a_sealed_store():
    """A store whose files are encrypted but which the sweep and the key probe do not know
    is plaintext forever after an upgrade, and invisible to the probe that decides whether a
    key may be minted. Rooms and word suggestions were exactly that. MUTATION: drop `sealed`
    from the a2a row - red."""
    sealed_owners = {s.owner for s in ds.STORES if s.sealed}
    writers = {path.relative_to(ROOT).as_posix() for path in (ROOT / "vaf").rglob("*.py")
               if _SEAM_WRITE.search(path.read_text(encoding="utf-8", errors="replace"))}
    assert len(writers) >= 8, f"the scan found only {sorted(writers)}; it is broken"
    missing = sorted(writers - sealed_owners - set(_SEAM_WRITERS_WITHOUT_A_ROW))
    assert not missing, "writes through the seam without a sealed store: " + ", ".join(missing)
    stale = sorted(set(_SEAM_WRITERS_WITHOUT_A_ROW) - writers)
    assert not stale, "no longer writes through the seam, drop it from the list: " + ", ".join(stale)


def test_a_protected_store_is_one_its_owner_names():
    """The sweep once hardened `~/.vaf/thinking_workspace`, a folder no code writes, while the
    real workspaces sat open under the data directory. A sealed or hardened row must be one
    its owner module names. MUTATION: rename the `workspaces` row to `thinking_workspace` -
    red."""
    ghosts = []
    for store in ds.STORES:
        if not (store.sealed or store.harden):
            continue
        source = (ROOT / store.owner).read_text(encoding="utf-8", errors="replace")
        if f'"{store.pattern}"' not in source:
            ghosts.append(f"{store.root}/{store.pattern} ({store.owner})")
    assert not ghosts, "protected stores their owner never names: " + ", ".join(ghosts)


def test_the_sealed_globs_stay_inside_their_store():
    """The sweep hardens each sealed entry and the probe reads its globs; a glob that reaches
    past its own entry would encrypt files of a store whose loader does not read ciphertext."""
    names = [s.pattern for s in ds.STORES if s.sealed]
    assert len(names) == len(set(names)), "two sealed rows share a name"
    for store in ds.STORES:
        for glob in store.sealed:
            assert glob == store.pattern or glob.startswith(store.pattern + "/"), (store, glob)


@pytest.mark.parametrize("rel", [
    "sessions/archive/ab12cd34/web_chat-1.json",
    "a2a/rooms/r1/log/peer-a/000001.json",
    "autosuggest/ab12cd34.json",
    "autosuggest.json",
])
def test_the_key_probe_finds_ciphertext_in_every_sealed_store(fake_roots, rel):
    """Before minting a file-store key, data_files asks whether ciphertext already exists. Its
    own list knew four folders; a machine whose only ciphertext was an archived chat, a room or
    a word list looked like a fresh install. MUTATION: give the probe its old list back - red."""
    from vaf.core import data_files
    path = fake_roots["vaf"] / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data_files.FILE_MAGIC + b"\0" * 32)
    assert data_files._encrypted_files_exist()


def test_a_launch_directory_with_foreign_ciphertext_does_not_block_the_key(
        fake_roots, tmp_path, monkeypatch):
    """The working memory under the launch directory can be another installation's (a
    project folder carried over, a checkout shared between homes). Counting it refused this
    installation its own file-store key, and every chat save failed; the suite measured it on
    a checkout with sealed working memory. MUTATION: probe the launch dir too - red."""
    from vaf.core import data_files
    launch = tmp_path / "project"
    (launch / ".vaf" / "main").mkdir(parents=True)
    (launch / ".vaf" / "main" / "working_memory.json").write_bytes(data_files.FILE_MAGIC + b"\0" * 32)
    monkeypatch.chdir(launch)
    (fake_roots["vaf"] / "sessions" / "web_chat-1.json").unlink()
    assert not data_files._encrypted_files_exist()


def test_a_store_list_that_cannot_be_read_never_means_a_fresh_install(monkeypatch):
    """`_key` mints a new file-store key when the probe says no ciphertext exists. A probe
    that answered False because it could not list the stores would mint over existing data.
    MUTATION: swallow the error and answer False again - red."""
    from vaf.core import data_files

    def _broken(**_kw):
        raise OSError("the store list could not be read")

    monkeypatch.setattr(ds, "sealed_trees", _broken)
    with pytest.raises(OSError):
        data_files._encrypted_files_exist()


def test_a_plaintext_archived_chat_keeps_the_store_tolerant(fake_roots):
    """The sweep closes plaintext reads once it finds none left. Archived chats were not in
    its list, so it closed them over a plaintext archive, and the archive view swallowed the
    refusal: the chat silently disappeared. MUTATION: drop the archive glob - red."""
    from vaf.core import at_rest_migration
    (fake_roots["vaf"] / "sessions" / "web_chat-1.json").unlink()
    assert not at_rest_migration._any_plaintext_left(ds.sealed_trees()), "the fixture is not clean"
    chat = fake_roots["vaf"] / "sessions" / "archive" / "ab12cd34" / "web_chat-1.json"
    chat.parent.mkdir(parents=True)
    chat.write_text('{"id": "web_chat-1"}', encoding="utf-8")
    assert at_rest_migration._any_plaintext_left(ds.sealed_trees())


def test_the_at_rest_doc_names_every_sealed_store():
    """ENCRYPTION_AT_REST.md is what a person reads to learn what is encrypted; the rooms and
    the archived chats were missing from its table while their files were encrypted. MUTATION:
    drop the rooms row from the doc - red."""
    doc = (ROOT / "docs" / "security" / "ENCRYPTION_AT_REST.md").read_text(encoding="utf-8")
    table = doc.split("\n## What is encrypted", 1)[1].split("\n## ", 1)[0]
    roots = {"vaf": "~/.vaf"}
    missing = [f"{roots[s.root]}/{s.pattern}" for s in ds.STORES
               if s.sealed and f"{roots[s.root]}/{s.pattern}" not in table]
    assert not missing, "sealed stores the doc's table does not name: " + ", ".join(missing)
