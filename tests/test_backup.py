# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Backups (vaf/core/backup.py): what goes in, what never does, and who can open them.

The test that matters is the round trip on the real format: write a backup of a small
fake installation, open it with this machine's key AND with the recovery key, and find
every file byte for byte. The database dump is the one part replaced by a stand-in here;
the restore round trip against a real Postgres container is a live measurement, recorded
in docs/setup/BACKUP_RESTORE.md.
"""
import io
import json
import sqlite3
import tarfile

import pytest

from vaf.core import backup, data_stores, recovery_kit


@pytest.fixture
def install(tmp_path, monkeypatch):
    """A small installation in five fake roots, a stand-in database dump, and a recovery
    wrap of its own."""
    home = tmp_path / "dotvaf"
    roots = {"app": home, "vaf": home, "data": tmp_path / "data", "config": tmp_path / "config",
             "projects": tmp_path / "projects"}
    for path in roots.values():
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(data_stores, "roots", lambda: roots)
    monkeypatch.setattr(data_stores, "projects_dir", lambda: roots["projects"])
    files = {
        home / "config.json": '{"memory_db_url": "postgresql://vaf_app:x@localhost/vaf_memory"}',
        home / "sessions" / "web_chat-1.json": '{"messages": ["hello"]}',
        home / "context_archive" / "c.json": "derived",
        home / "vm-backups" / "dump.sql.gz": "not ours",
        roots["data"] / "VAF-BackThisUp.md": "AGE-SECRET-KEY-1 plaintext",
        roots["data"] / "logs" / "security_events_2026-10-16.jsonl": '{"kind": "login_failed"}',
        roots["data"] / "logs" / "web_2026-10-16.log": "diagnostics",
        roots["config"] / "trust" / "default.json": "{}",
        roots["projects"] / "ab12cd34" / "web_chat-1" / "notes.md": "the person's notes",
    }
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _fake_dump(staging):
        dump = staging / "postgres" / "database.dump"
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_bytes(b"PGDMP stand-in")
        (staging / "postgres" / "globals.sql").write_text("CREATE ROLE vaf;", encoding="utf-8")
        return ([("postgres/database.dump", dump),
                 ("postgres/globals.sql", staging / "postgres" / "globals.sql")],
                {"database": "vaf_memory", "role": "vaf"})

    monkeypatch.setattr(backup, "_dump_postgres", _fake_dump)
    wrap = tmp_path / "recovery.json"
    monkeypatch.setattr(recovery_kit, "recovery_wrap_path", lambda: wrap)
    identity = recovery_kit.create_recovery_wrap(b"d" * 32, b"k" * 32)
    out = tmp_path / "backups"
    return {"roots": roots, "files": files, "recovery": identity, "out": out}


def _members(path, identity=None):
    import pyrage
    from pyrage import x25519
    ident = x25519.Identity.from_str(identity) if identity else backup.machine_identity()
    plain = pyrage.decrypt(path.read_bytes(), [ident])
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r") as tar:
        return [(m.name, tar.extractfile(m).read() if m.isfile() else None)
                for m in tar.getmembers()]


def test_a_backup_opens_with_this_machine_and_with_the_recovery_key(install):
    """The round trip on the real format. MUTATION: drop the recovery recipient - the
    recovery key no longer opens it - red."""
    result = backup.create_backup(install["out"], include_volumes=False)
    assert result.verified and result.recipients == ["this machine", "the recovery key"]
    assert backup.verify_backup(result.path).ok
    by_key = backup.verify_backup(result.path, install["recovery"])
    assert by_key.ok, by_key.problems
    from pyrage import x25519
    stranger = backup.verify_backup(result.path, str(x25519.Identity.generate()))
    assert not stranger.ok

    names = dict(_members(result.path, install["recovery"]))
    home = install["roots"]["app"]
    assert names["files/vaf/sessions/web_chat-1.json"] == b'{"messages": ["hello"]}'
    assert names["files/app/config.json"].startswith(b'{"memory_db_url"')
    assert names["files/projects/ab12cd34/web_chat-1/notes.md"] == b"the person's notes"
    assert names["files/data/logs/security_events_2026-10-16.jsonl"]
    assert names["postgres/database.dump"] == b"PGDMP stand-in"
    _ = home


def test_the_archive_is_plain_age_and_tar_with_the_manifest_first(install):
    """Openable without VAF (`age -d | tar x`): the format tag first, the checksums last."""
    result = backup.create_backup(install["out"], include_volumes=False)
    members = _members(result.path)
    assert members[0][0] == "manifest.json" and members[-1][0] == "checksums.json"
    manifest = json.loads(members[0][1])
    assert manifest["format"] == backup.BACKUP_FORMAT
    assert manifest["postgres"]["database"] == "vaf_memory"
    checksums = json.loads(members[-1][1])
    assert set(checksums) == {name for name, _ in members[:-1]}


def test_what_must_not_leave_the_machine_never_does(install):
    """The recovery note is a plaintext key; derived data, logs and other people's files
    are not VAF's to carry. MUTATION: take NEVER rows, or enter unknown folders - red."""
    result = backup.create_backup(install["out"], include_volumes=False)
    names = {name for name, _ in _members(result.path)}
    assert not any("VAF-BackThisUp" in n for n in names)
    assert not any("context_archive" in n or "web_2026" in n or "vm-backups" in n for n in names)
    assert any("vm-backups" in s for s in result.skipped), "an unknown folder is reported"


def test_a_database_in_use_is_captured_consistent(install):
    """A SQLite store in WAL mode keeps recent writes in its -wal file until a checkpoint;
    copying the main file alone loses them. MUTATION: copy the file instead of SQLite's
    online backup - the row is gone - red."""
    db = install["roots"]["data"] / "channel_messages.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("CREATE TABLE m (t TEXT)")
    con.execute("INSERT INTO m VALUES ('still in the wal')")
    con.commit()
    try:
        result = backup.create_backup(install["out"], include_volumes=False)
    finally:
        con.close()
    members = dict(_members(result.path))
    assert "files/data/channel_messages.db-wal" not in members
    copy = install["out"] / "restored.db"
    copy.write_bytes(members["files/data/channel_messages.db"])
    assert sqlite3.connect(copy).execute("SELECT t FROM m").fetchall() == [("still in the wal",)]


def test_no_database_no_backup(install, monkeypatch):
    """A backup without the accounts and memories would be trusted and restore nothing.
    MUTATION: turn the missing container into a warning - red."""
    from vaf.core import containers
    monkeypatch.undo()
    monkeypatch.setattr(data_stores, "roots", lambda: install["roots"])
    monkeypatch.setattr(data_stores, "projects_dir", lambda: install["roots"]["projects"])
    monkeypatch.setattr(containers, "container_state", lambda name: None)
    with pytest.raises(backup.BackupError) as err:
        backup.create_backup(install["out"], include_volumes=False)
    assert "memory database" in str(err.value)
    assert not list(install["out"].glob("*.tar.age")) and not list(install["out"].glob(".*partial"))


def test_a_changed_backup_is_caught(install):
    """Storage corruption is caught by the .sha256 beside the file, and without it by age's
    own authentication. MUTATION: skip the sidecar check - the first half goes red."""
    result = backup.create_backup(install["out"], include_volumes=False)
    data = bytearray(result.path.read_bytes())
    data[len(data) // 2] ^= 0x01
    result.path.write_bytes(bytes(data))
    first = backup.verify_backup(result.path)
    assert not first.ok and ".sha256" in first.problems[0]
    result.path.with_name(result.path.name + ".sha256").unlink()
    assert not backup.verify_backup(result.path).ok


def test_the_backup_folder_is_never_inside_its_own_backup(install):
    """Pointed into a root (VAF_Projects counts everything as data), it must not swallow
    its earlier backups. MUTATION: drop the exclusion - red."""
    inside = install["roots"]["projects"] / "backups"
    first = backup.create_backup(inside, include_volumes=False)
    second = backup.create_backup(inside, include_volumes=False)
    names = {name for name, _ in _members(second.path)}
    assert not any(first.path.name in n or "backups/" in n for n in names)


def test_browser_caches_stay_out_of_a_profile(tmp_path, monkeypatch):
    """A profile's caches, and the Workspace folder that mirrors the person's own files, are
    most of its size (243 MB measured) and come back on their own. MUTATION: copy the stream
    unfiltered, or keep the top-level folders - red."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, data in (("v/.config/chromium/Default/Cookies", b"c"),
                           ("v/.config/chromium/Default/Cache/data_0", b"x" * 100),
                           ("v/.config/chromium/Default/Code Cache/js/a", b"y"),
                           ("v/.cache/fontconfig/x", b"z"),
                           ("v/Workspace/web_chat-1/upload.pdf", b"mirror")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

    class _Proc:
        def __init__(self, *a, **k):
            self.stdout = io.BytesIO(raw.getvalue())
            self.stderr = io.BytesIO(b"")

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(backup.subprocess, "Popen", _Proc)
    monkeypatch.setattr(backup, "_docker_exe", lambda: "docker")
    out = tmp_path / "profile.tar"
    backup._docker_cp_tar("vaf-backup-carrier-ab12cd34", "/v", out,
                          drop=backup._BROWSER_CACHES, drop_top=backup._BROWSER_NOT_PROFILE)
    with tarfile.open(out) as tar:
        assert tar.getnames() == ["v/.config/chromium/Default/Cookies"]


def test_the_backup_group_sits_behind_the_terminal_door():
    """A backup holds every key of the machine, encrypted to a key this machine has.
    MUTATION: register the group without the door - red."""
    from pathlib import Path
    main = (Path(__file__).resolve().parents[1] / "vaf" / "main.py").read_text(encoding="utf-8")
    line = next(ln for ln in main.splitlines() if 'name="backup"' in ln)
    following = main[main.index(line):main.index(line) + 300]
    assert "callback=_terminal_door" in following


def test_volumes_are_taken_by_name_read_only_even_without_their_container(tmp_path, monkeypatch):
    """A personal browser's container is recreated while its profile volume stays; taken by
    container, a profile whose container is gone was missing from the backup (measured: one
    of three profiles on a live machine). MUTATION: list containers instead of volumes, or
    mount the volume writable - red."""
    from types import SimpleNamespace

    from vaf.core import containers
    calls = []

    def _docker(args, timeout=60, **kw):
        calls.append(args)
        if args[:1] == ["inspect"]:
            return SimpleNamespace(returncode=0, stdout="pgvector/pgvector:pg16\n", stderr="")
        if args[:2] == ["volume", "ls"]:
            prefix = args[-1].split("=", 1)[1]
            names = {"vaf-env-vol-": "vaf-env-vol-e1\n",
                     "vaf-browser-profile-": "vaf-browser-profile-ab12cd34ef56\n"}[prefix]
            return SimpleNamespace(returncode=0, stdout=names, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def _cp(container, path, out, drop=frozenset(), drop_top=frozenset()):
        out.write_bytes(b"tar")
        return 1

    monkeypatch.setattr(containers, "docker", _docker)
    monkeypatch.setattr(backup, "_docker_cp_tar", _cp)
    warnings = []
    members, info = backup._collect_volumes(tmp_path, warnings)
    assert [arc for arc, _ in members] == ["volumes/vaf-env-vol-e1.tar",
                                           "volumes/vaf-browser-profile-ab12cd34ef56.tar"]
    creates = [a for a in calls if a[:1] == ["create"]]
    assert len(creates) == 2 and all(any(x.endswith(":/v:ro") for x in a) for a in creates)
    assert len([a for a in calls if a[:2] == ["rm", "-f"]]) == 2, "every carrier is removed"
    assert not warnings
