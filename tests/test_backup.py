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
import os
import sqlite3
import tarfile
from pathlib import Path

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

    def _fake_dump(staging, container=None):
        dump = staging / "postgres" / "database.dump"
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_bytes(b"PGDMP stand-in")
        (staging / "postgres" / "globals.sql").write_text("CREATE ROLE vaf;", encoding="utf-8")
        return ([("postgres/database.dump", dump),
                 ("postgres/globals.sql", staging / "postgres" / "globals.sql")],
                {"database": "vaf_memory", "role": "vaf"})

    monkeypatch.setattr(backup, "_dump_postgres", _fake_dump)
    # No test reaches the machine's Docker: a restore's safety backup takes the volumes, and
    # on this workstation that read the real ones through carrier containers, while a CI
    # runner without the database container failed the backup (measured: five tests red on
    # every runner, green here). The volume lane has tests of its own with a fake docker.
    monkeypatch.setattr(backup, "_collect_volumes", lambda staging, warnings, container=None: ([], []))
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
    # A retyped key with a mixed-case body is invalid Bech32 as typed; verify reads it the way
    # restore does (upper-cased), so a key verify accepts also restores. MUTATION: parse it
    # as typed in verify_backup - red.
    retyped = install["recovery"][:15] + install["recovery"][15:].lower()
    assert backup.verify_backup(result.path, retyped).ok
    garbage = backup.verify_backup(result.path, "not a key")
    assert not garbage.ok and "not a recovery key" in garbage.problems[0]

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


def test_every_backup_command_but_restore_asks_the_terminal_door(monkeypatch, tmp_path):
    """A backup holds every key of the machine, encrypted to a key this machine has: create,
    verify and list ask the door. Restore proves the person itself - with the recovery key
    when the admin password is gone, which is exactly what the door would ask for.
    MUTATION: ask the door for restore too, or for nothing - red."""
    from typer.testing import CliRunner

    import vaf.cli.gate as gate
    from vaf.cli.cmd import backup as backup_cmd
    from vaf.core.processes import AGENT_COMMAND_ENV
    ran = []        # the runner turns any exception into exit code 1: count the calls
    monkeypatch.setattr(backup, "create_backup", lambda *a, **k: ran.append(1))
    runner = CliRunner()
    # The real door, from the agent's own command: refused before any password is asked.
    monkeypatch.setenv(AGENT_COMMAND_ENV, "1")
    assert runner.invoke(backup_cmd.app, ["create"]).exit_code == 1 and ran == []
    monkeypatch.delenv(AGENT_COMMAND_ENV)
    asked = []

    def _door(force=False):
        asked.append(1)
        return False

    monkeypatch.setattr(gate, "require_admin_password", _door)
    assert runner.invoke(backup_cmd.app, ["create"]).exit_code == 1 and asked == [1] and ran == []

    import vaf.core.instance as instance
    monkeypatch.setattr(gate, "_stored_hash", lambda: ("", False))      # a new machine
    monkeypatch.setattr(gate, "is_interactive", lambda: True)
    monkeypatch.setattr(instance, "find_service", lambda: None)
    monkeypatch.setattr(backup_cmd.typer, "confirm", lambda *a, **k: False)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "AGE-SECRET-KEY-1X")
    monkeypatch.setattr(backup, "read_manifest", lambda path, identity=None: {"host": "h"})
    archive = tmp_path / "x.tar.age"
    archive.write_bytes(b"x")
    out = runner.invoke(backup_cmd.app, ["restore", str(archive), "--recovery-key"])
    assert asked == [1], "restore with the recovery key never asks the admin password"
    assert "Backup of h" in out.output



@pytest.mark.parametrize("stored, unreadable, opens_here, asked_password", [
    ("hash", False, True, False),        # this machine's own recovery key
    ("hash", False, False, True),        # a key of anybody's: the admin password decides
    ("", True, False, True),             # an unreadable keyring counts as an account
])
def test_a_recovery_key_restore_over_an_admin_account_is_this_machines_key(
        monkeypatch, tmp_path, stored, unreadable, opens_here, asked_password):
    """Anyone can write a backup to a key of their own, so a key that opens the backup
    proves nothing about the machine it replaces. Where an admin account exists, the key
    must open this machine's recovery file, or the admin password is asked.
    MUTATION: drop the check - a foreign key restores over the admin's machine - red."""
    from typer.testing import CliRunner

    import vaf.cli.gate as gate
    import vaf.core.instance as instance
    from vaf.cli.cmd import backup as backup_cmd
    asked = []

    def _door(force=False):
        asked.append(force)
        return False

    monkeypatch.setattr(gate, "require_admin_password", _door)
    monkeypatch.setattr(gate, "_stored_hash", lambda: (stored, unreadable))
    monkeypatch.setattr(gate, "is_interactive", lambda: True)
    monkeypatch.setattr(instance, "find_service", lambda: None)
    monkeypatch.setattr(recovery_kit, "unwrap_with_secret",
                        lambda secret: b"d" * 32 if opens_here else None)
    monkeypatch.setattr(backup_cmd.typer, "confirm", lambda *a, **k: False)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "AGE-SECRET-KEY-1X")
    monkeypatch.setattr(backup, "read_manifest", lambda path, identity=None: {"host": "h"})
    archive = tmp_path / "x.tar.age"
    archive.write_bytes(b"x")
    out = CliRunner().invoke(backup_cmd.app, ["restore", str(archive), "--recovery-key"])
    if asked_password:
        assert asked == [True] and out.exit_code == 1 and "Backup of h" not in out.output
    else:
        assert asked == [] and "Backup of h" in out.output


@pytest.mark.parametrize("args, stored, opens_here, proof", [
    ([], "hash", True, "the admin password"),
    (["--recovery-key"], "hash", True, "this machine's recovery key"),
    (["--recovery-key"], None, False, "the recovery key (no admin account here)"),
])
def test_the_restore_names_the_proof_it_took(monkeypatch, tmp_path, args, stored, opens_here,
                                            proof):
    """The security event says how the person proved it was them. MUTATION: pass no proof,
    or the wrong one - red."""
    from types import SimpleNamespace

    from typer.testing import CliRunner

    import vaf.cli.gate as gate
    import vaf.core.instance as instance
    from vaf.cli.cmd import backup as backup_cmd
    given = []
    monkeypatch.setattr(gate, "require_admin_password", lambda force=False: True)
    monkeypatch.setattr(gate, "_stored_hash", lambda: (stored, False))
    monkeypatch.setattr(gate, "is_interactive", lambda: True)
    monkeypatch.setattr(instance, "find_service", lambda: None)
    monkeypatch.setattr(recovery_kit, "unwrap_with_secret",
                        lambda secret: b"d" * 32 if opens_here else None)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "AGE-SECRET-KEY-1X")
    monkeypatch.setattr(backup, "read_manifest", lambda path, identity=None: {"host": "h"})
    monkeypatch.setattr(backup, "restore_backup", lambda path, **kw: given.append(kw["proof"])
                        or SimpleNamespace(files=1, volumes=0, rewrapped=[], pre_restore=None,
                                           warnings=[], incomplete=""))
    archive = tmp_path / "x.tar.age"
    archive.write_bytes(b"x")
    out = CliRunner().invoke(backup_cmd.app, ["restore", str(archive), "--yes", *args])
    assert out.exit_code == 0, out.output
    assert given == [proof]


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


# ── restore ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def target(install, monkeypatch):
    """A restore target: the database container answers, the database step is recorded."""
    from vaf.core import containers
    calls = {"pg": 0}
    monkeypatch.setattr(containers, "container_state", lambda name: "running")
    from vaf.core import environments
    monkeypatch.setattr(environments.EnvironmentManager, "readopt", lambda self: {})
    monkeypatch.setattr(backup, "_restore_postgres",
                        lambda staging, manifest, container: calls.__setitem__("pg", calls["pg"] + 1))
    install["calls"] = calls
    return install


def _encrypt_tar(path, members, manifest=None):
    """An age file holding a tar of ``members`` (name -> bytes), for archives a real
    backup would never write."""
    import hashlib

    import pyrage
    raw = io.BytesIO()
    checksums = {}
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        items = [("manifest.json", json.dumps(manifest or {"format": backup.BACKUP_FORMAT,
                                                           "vaf_version": "0.0.1"}).encode())]
        items += list(members.items())
        for name, data in items:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            checksums[name] = hashlib.sha256(data).hexdigest()
        data = json.dumps(checksums).encode()
        info = tarfile.TarInfo("checksums.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    path.write_bytes(pyrage.encrypt(raw.getvalue(), [backup.machine_identity().to_public()]))
    return path


def test_a_restore_puts_back_exactly_the_backup_and_keeps_this_machines_key(target):
    """The round trip: back up, change the installation, restore. The backed-up entries come
    back as they were, what was added since is gone, entries that are not VAF's stay, and
    the machine key on THIS machine is never replaced: the stores are re-wrapped under it.
    MUTATION: place the backup's key file, or skip the clearing - red."""
    home = target["roots"]["app"]
    (home / "secure_store.kek").write_text("the backed-up machine's key", encoding="utf-8")
    made = backup.create_backup(target["out"], include_volumes=False)
    (home / "sessions" / "web_chat-1.json").write_text("changed", encoding="utf-8")
    (home / "sessions" / "web_chat-new.json").write_text("added later", encoding="utf-8")
    (home / "secure_store.kek").write_text("this machine's key", encoding="utf-8")

    result = backup.restore_backup(made.path, pre_restore=False)

    assert (home / "sessions" / "web_chat-1.json").read_text() == '{"messages": ["hello"]}'
    assert not (home / "sessions" / "web_chat-new.json").exists()
    assert (home / "vm-backups" / "dump.sql.gz").exists(), "not VAF's, never touched"
    assert (target["roots"]["projects"] / "ab12cd34" / "web_chat-1" / "notes.md").exists()
    assert target["calls"]["pg"] == 1
    assert (home / "secure_store.kek").read_text() == "this machine's key"
    assert result.files >= 5


def test_a_backup_from_another_machine_opens_under_this_machines_key(target, tmp_path):
    """The stores travel sealed under the OLD machine key; on the new machine every one is
    re-wrapped under ITS key and opens. MUTATION: drop the re-wrap from restore_backup - red."""
    import base64
    import secrets as _secrets

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    old_kek, dek = _secrets.token_bytes(32), _secrets.token_bytes(32)
    nonce = _secrets.token_bytes(12)
    wrap = json.dumps({"v": 1, "kdf": "raw", "nonce": base64.b64encode(nonce).decode(),
                       "wrapped": base64.b64encode(AESGCM(old_kek).encrypt(nonce, dek, None)).decode()})
    payload_nonce = _secrets.token_bytes(12)
    enc = payload_nonce + AESGCM(dek).encrypt(payload_nonce, b'{"openai": "sk-from-the-old-machine"}', None)
    archive = _encrypt_tar(tmp_path / "old.tar.age", {
        "keys/machine_key": old_kek,
        "files/data/probe_store.key.json": wrap.encode(),
        "files/data/probe_store.enc": enc,
        "postgres/database.dump": b"PGDMP",
        "postgres/globals.sql": b"",
    })
    result = backup.restore_backup(archive, pre_restore=False)
    assert "probe_store" in result.rewrapped
    from vaf.core.secure_store import SecureBlobStore
    store = SecureBlobStore("probe_store", target["roots"]["data"] / "probe_store.enc")
    assert store.load_strict() == {"openai": "sk-from-the-old-machine"}



@pytest.mark.parametrize("fails", [False, True])
def test_a_restore_gives_the_recovery_key_this_machines_key(install, target, monkeypatch, fails):
    """The restored recovery wrap carries the backup's machine key, and the stores are sealed
    under this machine's now: the wrap is refreshed with the restored keyring's key, and a
    failure is said, not swallowed. MUTATION: drop the refresh from restore_backup - red."""
    from vaf.core import data_files, data_keyring
    from vaf.core.platform import Platform
    data, app = install["roots"]["data"], install["roots"]["app"]
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: data))
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: app))
    # The "a ring existed here" marker belongs to the ring it was written for (conftest
    # rule): left at the test's default, a key minted there - by this test's fixtures or a
    # background thread of an earlier one - made this ring look vanished (seen once, under
    # the hostile environment).
    monkeypatch.setattr(data_keyring, "_established_marker", lambda: data / "data_keys.established")
    data_keyring.reset_ring()
    data_files.reset_key_cache()
    calls = []

    def _refresh(dek):
        calls.append(dek)
        if fails:
            raise OSError("disk full")
        return True

    try:
        backup.machine_identity()
        made = backup.create_backup(install["out"], include_volumes=False)
        monkeypatch.setattr(recovery_kit, "refresh_recovery_wrap", _refresh)
        result = backup.restore_backup(made.path, pre_restore=False)
        assert calls == [data_keyring._ring()._get_dek(create=False)] and calls[0]
        assert any("renew-recovery-key" in w for w in result.warnings) is fails
    finally:
        data_keyring.reset_ring()
        data_files.reset_key_cache()



def test_a_backup_that_cannot_be_opened_is_reported_not_waited_on(target, tmp_path):
    """The decrypting worker opened the file before it took the pipe: a file that was gone
    left the pipe's write end open, and the reader waited on it forever - `vaf backup verify`
    of a mistyped path hung. MUTATION: open the file before the pipe again - red."""
    import threading
    gone = tmp_path / "gone.tar.age"
    calls = {"verify": lambda: backup.verify_backup(gone),
             "manifest": lambda: backup.read_manifest(gone),
             "restore": lambda: backup.restore_backup(gone, pre_restore=False)}
    for name, call in calls.items():
        out = {}

        def _run():
            try:
                out["result"] = call()
            except backup.BackupError as e:
                out["error"] = str(e)

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(20)
        assert not worker.is_alive(), f"{name} waits forever on a file that is not there"
        assert "error" in out or not out["result"].ok, name



@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits (harden_dir is a no-op there)")
def test_a_named_folder_keeps_its_permissions_and_a_new_one_is_owner_only(install, tmp_path):
    """`--to ~/Documents` made the person's folder owner-only as a side effect. A folder the
    backup creates is owner-only; one that was there keeps its mode, and the backup file in
    it is owner-only either way. MUTATION: harden every destination again - red."""
    import stat
    existing = tmp_path / "Documents"
    existing.mkdir(mode=0o755)
    os.chmod(existing, 0o755)
    made = backup.create_backup(existing, include_volumes=False)
    assert stat.S_IMODE(existing.stat().st_mode) == 0o755
    assert stat.S_IMODE(made.path.stat().st_mode) == 0o600
    fresh = tmp_path / "new" / "backups"
    backup.create_backup(fresh, include_volumes=False)
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_the_working_folders_keep_their_modes_and_the_rest_comes_back_owner_only(target):
    """Every file came back owner-only, so a script in a working folder lost its executable
    bit. The working folders keep their modes (no write bit for others); VAF's own stores
    stay owner-only. MUTATION: archive 0o600 for every file again - red."""
    import stat
    script = target["roots"]["projects"] / "ab12cd34" / "web_chat-1" / "build.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(script, 0o777)
    config = target["roots"]["app"] / "config.json"
    os.chmod(config, 0o644)
    made = backup.create_backup(target["out"], include_volumes=False)
    backup.restore_backup(made.path, pre_restore=False)
    assert stat.S_IMODE(script.stat().st_mode) == 0o755
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_a_failing_role_restore_stops_the_restore(tmp_path, monkeypatch):
    """psql runs past a failing statement and exits 0, so the exit code was never looked
    at; a psql that cannot connect exits non-zero, and no role then has the backup's password
    hash: the restored config could not connect. MUTATION: ignore the exit code - red."""
    import subprocess as sp
    (tmp_path / "postgres").mkdir()
    (tmp_path / "postgres" / "globals.sql").write_bytes(b"CREATE ROLE vaf;")
    (tmp_path / "postgres" / "database.dump").write_bytes(b"PGDMP")
    monkeypatch.setattr(backup, "_wait_for_postgres", lambda role, container=None: None)
    monkeypatch.setattr(backup, "_psql", lambda *a, **k: None)
    monkeypatch.setattr(backup, "_docker_exe", lambda: "docker")

    def _run(args, **kw):
        failed = "psql" in args and "pg_restore" not in args
        return sp.CompletedProcess(args, 2 if failed else 0, b"", b"could not connect" if failed else b"")

    monkeypatch.setattr(backup.subprocess, "run", _run)
    with pytest.raises(backup.BackupError) as err:
        backup._restore_postgres(tmp_path, {"postgres": {"role": "vaf", "database": "vaf_memory"}},
                                 "vaf-memory-db")
    assert "roles" in str(err.value) and "could not connect" in str(err.value)


def test_a_restore_is_a_security_event_with_its_proof(target, monkeypatch):
    """Every account, key and chat of the machine was replaced: the security log says so,
    with the proof the person gave. MUTATION: drop the event, or its proof - red."""
    from vaf.core import security_events
    seen = []
    monkeypatch.setattr(security_events, "log_security_event",
                        lambda kind, **kw: seen.append((kind, kw)))
    made = backup.create_backup(target["out"], include_volumes=False)
    backup.restore_backup(made.path, pre_restore=False, proof="the admin password")
    assert [k for k, _ in seen] == ["backup_restored"]
    detail = seen[0][1]["detail"]
    assert detail.startswith("proof: the admin password")
    assert seen[0][1]["path"] == made.path.name


def test_a_restore_that_stops_half_way_is_a_security_event_too(target, monkeypatch):
    """Half replaced is replaced, and it is not shown as restored: its own kind, labelled as
    such in the Logs window. MUTATION: only log the complete ones, or log the half-way one as
    `backup_restored` - red."""
    from vaf.core import security_events
    seen = []
    monkeypatch.setattr(security_events, "log_security_event",
                        lambda kind, **kw: seen.append((kind, kw)))
    made = backup.create_backup(target["out"], include_volumes=False)

    def _boom(*a, **k):
        raise backup.BackupError("The database did not take the dump.")

    monkeypatch.setattr(backup, "_restore_postgres", _boom)
    with pytest.raises(backup.BackupError):
        backup.restore_backup(made.path, proof="this machine's recovery key")
    assert [k for k, _ in seen] == ["backup_restore_failed"]
    detail = seen[0][1]["detail"]
    assert detail.startswith("The database did not take the dump")
    assert "proof: this machine's recovery key" in detail and "safety: vaf-backup-" in detail


@pytest.mark.parametrize("readopt, with_project, kind", [
    ({"failed": ["e1", "e2"]}, True, "backup_restore_failed"),
    # Left to the reaper all the same: its folder is not on this machine.
    ({"missing_folder": ["e1"]}, True, "backup_restore_failed"),
    ("raises", True, "backup_restore_failed"),
    # Nothing to recreate: a readopt that cannot ask docker loses nothing.
    ("raises", False, "backup_restored"),
])
def test_a_restore_whose_environments_did_not_come_back_is_not_completed(
        target, tmp_path, monkeypatch, readopt, with_project, kind):
    """Everything is in place but project environments the reaper deletes at the next start:
    the person is told to restore again, and the log must not say "restored". A backup that
    holds none is complete. MUTATION: audit without what is missing, leave missing folders
    out, or count a readopt error on a backup without environments - red."""
    from vaf.core import environment_image, environments, security_events
    seen = []
    monkeypatch.setattr(security_events, "log_security_event",
                        lambda kind, **kw: seen.append((kind, kw)))
    monkeypatch.setattr(environment_image, "ensure_image", lambda: "vaf-env-image")

    def _readopt(self):
        if readopt == "raises":
            raise RuntimeError("docker did not answer")
        return readopt

    monkeypatch.setattr(environments.EnvironmentManager, "readopt", _readopt)
    members = {"postgres/database.dump": b"PGDMP", "postgres/globals.sql": b""}
    if with_project:
        members["files/vaf/environments/e1.json"] = json.dumps(
            {"id": "e1", "kind": "project", "volume": "vaf-env-vol-e1"}).encode()
    archive = _encrypt_tar(tmp_path / "envs.tar.age", members)
    result = backup.restore_backup(archive, pre_restore=False, proof="the admin password")
    assert [k for k, _ in seen] == [kind]
    # The result says the same as the log, which is what the terminal reports.
    assert bool(result.incomplete) == (kind == "backup_restore_failed")
    if kind == "backup_restore_failed":
        assert seen[0][1]["detail"].startswith(result.incomplete)
        assert result.incomplete.startswith("sandbox environments not recreated")
        assert any("sandbox environments" in w or "project environments" in w
                   for w in result.warnings)


@pytest.mark.parametrize("incomplete", ["", "sandbox environments not recreated: 1"])
def test_the_terminal_does_not_call_an_incomplete_restore_done(monkeypatch, tmp_path,
                                                                incomplete):
    """`vaf backup restore` printed "Restored" and exited 0 while it warned that environments
    would be deleted at the next start. MUTATION: ignore `incomplete` - red."""
    from types import SimpleNamespace

    from typer.testing import CliRunner

    import vaf.cli.gate as gate
    import vaf.core.instance as instance
    from vaf.cli.cmd import backup as backup_cmd
    monkeypatch.setattr(gate, "require_admin_password", lambda force=False: True)
    monkeypatch.setattr(gate, "_stored_hash", lambda: ("hash", False))
    monkeypatch.setattr(gate, "is_interactive", lambda: True)
    monkeypatch.setattr(instance, "find_service", lambda: None)
    monkeypatch.setattr(backup, "read_manifest", lambda path, identity=None: {"host": "h"})
    monkeypatch.setattr(backup, "restore_backup", lambda path, **kw: SimpleNamespace(
        files=3, volumes=0, rewrapped=[], pre_restore=None, warnings=[], incomplete=incomplete))
    archive = tmp_path / "x.tar.age"
    archive.write_bytes(b"x")
    out = CliRunner().invoke(backup_cmd.app, ["restore", str(archive), "--yes"])
    if incomplete:
        assert out.exit_code == 1, out.output
        assert "not completely" in out.output and "Restored:" not in out.output
        assert "Start VAF again" not in out.output
    else:
        assert out.exit_code == 0, out.output
        assert "Restored:" in out.output


def test_the_restore_event_keeps_the_origin_inside_the_logged_200_characters(monkeypatch):
    """The log keeps 200 characters of `detail`. With the longest proof the terminal gives, a
    long failure, a long host name and a dated safety backup, the origin of the backup (host,
    time, version) is still inside them. MUTATION: drop any one field's bound - red."""
    from vaf.core import security_events
    seen = []
    monkeypatch.setattr(security_events, "log_security_event",
                        lambda kind, **kw: seen.append(kw))
    backup._audit_restore(
        Path("vaf-backup-20261019T140000Z.tar.age"),
        {"host": "h" * 80, "created_at": "2026-10-19T14:00:00+00:00", "vaf_version": "9" * 40},
        "the admin password (a foreign recovery key)" + "!" * 40,
        Path("vaf-backup-20261019T150000Z-and-a-name-someone-gave-it.tar.age"),
        failure="x" * 300)
    detail = seen[0]["detail"]
    assert len(detail) <= 200, len(detail)
    assert detail.endswith("2026-10-19T14:00 VAF " + "9" * 10)


def test_a_restore_event_is_never_swallowed_by_the_throttle(monkeypatch, tmp_path):
    """The same backup restored twice within seconds is two changes to the machine, not a
    burst. MUTATION: take the restore kinds out of the unthrottled set - red."""
    from vaf.core import security_events
    monkeypatch.setattr(security_events, "get_app_log_dir", lambda: tmp_path)
    monkeypatch.setattr(security_events, "_last_emit", {})
    for _ in range(2):
        security_events.log_security_event("backup_restored", path="vaf-backup-x.tar.age",
                                           detail="proof: the admin password")
    lines = [ln for f in tmp_path.glob("security_events_*.jsonl")
             for ln in f.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2, lines


@pytest.mark.parametrize("failure", [OSError("No space left on device"),
                                     backup.BackupError("pg_restore failed: boom")])
def test_a_restore_that_stops_half_way_says_so_and_where_the_state_before_is(
        target, monkeypatch, failure):
    """Past the clearing, a failure left a machine holding part of the backup, and an
    unexpected one reached the person as a traceback, without the safety backup's place.
    MUTATION: let the error through unchanged - red."""
    made = backup.create_backup(target["out"], include_volumes=False)

    def _boom(*a, **k):
        raise failure

    monkeypatch.setattr(backup, "_restore_postgres", _boom)
    with pytest.raises(backup.BackupError) as err:
        backup.restore_backup(made.path)
    text = str(err.value)
    assert "half way" in text and str(failure).rstrip(".") in text
    assert "pre-restore" in text and "vaf backup restore" in text


@pytest.mark.parametrize("changes", [True, False])
def test_a_restore_that_replaces_the_recovery_key_says_which_key_opens_it_now(
        target, monkeypatch, changes):
    """The recovery file comes from the backup: after a restore from another machine, or of
    a backup older than a renewal, the key this machine had opens nothing. MUTATION: drop
    the comparison - red."""
    made = backup.create_backup(target["out"], include_volumes=False)
    holder = {"recipient": "age1thismachine"}
    monkeypatch.setattr(recovery_kit, "recipient", lambda: holder["recipient"])
    place = backup._place_files

    def _place(staging):
        if changes:
            holder["recipient"] = "age1thebackups"
        return place(staging)

    monkeypatch.setattr(backup, "_place_files", _place)
    result = backup.restore_backup(made.path, pre_restore=False)
    named = [w for w in result.warnings if "age1thismachine" in w]
    assert bool(named) is changes
    if changes:
        assert "age1thebackups" in named[0] and "renew-recovery-key" in named[0]



@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="POSIX permission bits, which root ignores")
def test_a_backup_does_not_quietly_lack_a_folder_it_cannot_read(install):
    """A working folder the backup could not read was left out and the backup reported
    success. MUTATION: let the walk pass over unreadable directories again - red."""
    locked = install["roots"]["projects"] / "ab12cd34" / "locked"
    locked.mkdir()
    (locked / "work.py").write_text("print(1)", encoding="utf-8")
    os.chmod(locked, 0)
    try:
        with pytest.raises(backup.BackupError) as err:
            backup.create_backup(install["out"], include_volumes=False)
        assert "locked" in str(err.value)
        assert not list(install["out"].glob("*.tar.age")), "nothing is left behind"
    finally:
        os.chmod(locked, 0o700)


@pytest.mark.parametrize("name", ["../escape.txt", "files/app/../../escape.txt", "/etc/passwd",
                                  "files/elsewhere/x", "volumes/not-a-vaf-volume.tar",
                                  "files\\app\\config.json"])
def test_an_archive_with_a_foreign_entry_restores_nothing(target, tmp_path, name):
    """A restore unpacks into the machine's own folders; a name outside the shape a VAF
    backup has is a foreign or tampered archive. MUTATION: drop the name check - red."""
    archive = _encrypt_tar(tmp_path / "evil.tar.age", {name: b"x"})
    before = sorted(p.name for p in target["roots"]["app"].iterdir())
    with pytest.raises(backup.BackupError):
        backup.restore_backup(archive, pre_restore=False)
    assert sorted(p.name for p in target["roots"]["app"].iterdir()) == before
    assert not (tmp_path / "escape.txt").exists()


def test_a_backup_from_a_newer_vaf_is_refused_before_anything_changes(target, tmp_path):
    archive = _encrypt_tar(tmp_path / "new.tar.age", {"postgres/globals.sql": b""},
                           manifest={"format": backup.BACKUP_FORMAT, "vaf_version": "99.0"})
    with pytest.raises(backup.BackupError) as err:
        backup.restore_backup(archive, pre_restore=False)
    assert "newer" in str(err.value)
    assert target["calls"]["pg"] == 0


def test_the_current_state_is_backed_up_before_a_restore(target):
    """A wrong restore must be undoable. MUTATION: skip the safety backup - red."""
    made = backup.create_backup(target["out"], include_volumes=False)
    result = backup.restore_backup(made.path)
    assert result.pre_restore is not None and result.pre_restore.exists()
    assert backup.verify_backup(result.pre_restore).ok


def test_restoring_needs_a_person_and_a_stopped_vaf(monkeypatch, tmp_path):
    """It replaces every account and key: never from the agent's shell or a script, never
    under a running VAF. MUTATION: drop any of the three checks - red."""
    from typer.testing import CliRunner

    import vaf.cli.gate as gate
    import vaf.core.instance as instance
    from vaf.cli.cmd import backup as backup_cmd
    from vaf.core.processes import AGENT_COMMAND_ENV
    ran = []
    monkeypatch.setattr(backup, "restore_backup", lambda *a, **k: ran.append(1) or
                        backup.RestoreResult({}, 0, [], 0, None))
    monkeypatch.setattr(backup, "read_manifest", lambda path, identity=None: {})
    monkeypatch.setattr(gate, "_stored_hash", lambda: ("a stored hash", False))
    monkeypatch.setattr(gate, "require_admin_password", lambda force=False: True)
    archive = tmp_path / "x.tar.age"
    archive.write_bytes(b"x")
    runner = CliRunner()

    def _run():
        return runner.invoke(backup_cmd.app, ["restore", str(archive), "--yes"])

    monkeypatch.setattr(gate, "is_interactive", lambda: True)
    monkeypatch.setattr(instance, "find_service", lambda: None)
    monkeypatch.setenv(AGENT_COMMAND_ENV, "1")
    assert _run().exit_code == 1 and not ran
    monkeypatch.delenv(AGENT_COMMAND_ENV)
    monkeypatch.setattr(gate, "is_interactive", lambda: False)
    assert _run().exit_code == 1 and not ran
    monkeypatch.setattr(gate, "is_interactive", lambda: True)
    monkeypatch.setattr(instance, "find_service", lambda: object())
    out = _run()
    assert out.exit_code == 1 and "running" in out.output and not ran
    monkeypatch.setattr(instance, "find_service", lambda: None)
    assert _run().exit_code == 0 and ran == [1], "with all three in place it runs"


# ── review findings ─────────────────────────────────────────────────────────────────

def test_the_configured_backup_folder_stays_out_when_writing_elsewhere(install, monkeypatch):
    """`--to` another folder, while `backup_dir` sits inside a root (VAF_Projects counts
    everything as data): the earlier backups there must not ride along. MUTATION: exclude
    only the destination - red."""
    from vaf.core.config import Config
    home_backups = install["roots"]["projects"] / "my-backups"
    home_backups.mkdir()
    (home_backups / "vaf-backup-20260101T000000Z.tar.age").write_bytes(b"an earlier backup")
    real_get = Config.get
    monkeypatch.setattr(Config, "get", classmethod(
        lambda cls, k, d=None: str(home_backups) if k == "backup_dir" else real_get(k, d)))
    result = backup.create_backup(install["out"], include_volumes=False)
    assert not any("my-backups" in name for name, _ in _members(result.path))


def test_plaintext_in_transit_stays_on_this_machine_and_out_of_the_backup(install, monkeypatch):
    """The database dump, SQLite copies, volume tars and the machine key are staged before
    they are encrypted. In `dest` they sat on whatever that is (a USB stick, a network mount,
    where a deleted copy is recoverable); now in an owner-only folder of the local data
    directory, which the walk must not take. MUTATION: stage in `dest`, or drop the staging
    from the exclusions - red."""
    from vaf.core.platform import Platform
    data = install["roots"]["data"]
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: data))
    seen = {}
    fake_dump = backup._dump_postgres

    def _spy(staging, container=None):
        seen["staging"] = staging
        return fake_dump(staging, container)

    monkeypatch.setattr(backup, "_dump_postgres", _spy)
    crashed = data / ".backup-staging-crashed"
    (crashed / "postgres").mkdir(parents=True)
    (crashed / "postgres" / "database.dump").write_bytes(b"plaintext from a crashed run")
    import os as _os
    _os.utime(crashed, (0, 0))
    result = backup.create_backup(install["out"], include_volumes=False)
    assert not crashed.exists(), "a crashed run's plaintext is removed"
    assert seen["staging"].parent == data and not backup._inside(seen["staging"], install["out"])
    assert not any(".backup-staging" in s for s in result.skipped)
    assert not any(".backup-staging" in name for name, _ in _members(result.path))
    assert not any(p.name.startswith(".backup-staging") for p in data.iterdir())


def test_the_sandbox_environments_come_back_for_a_backup_without_volumes(target, monkeypatch):
    """An environment that mounts a host folder has no volume; its record must get its
    container back all the same, or the reaper clears it. MUTATION: call readopt only when
    the backup has volume tars - red."""
    from vaf.core import environments
    calls = []
    monkeypatch.setattr(environments.EnvironmentManager, "readopt",
                        lambda self: calls.append(1) or {})
    made = backup.create_backup(target["out"], include_volumes=False)
    backup.restore_backup(made.path, pre_restore=False)
    assert calls == [1]


def test_the_safety_backup_still_opens_after_the_keyring_was_replaced(target, monkeypatch):
    """The safety backup is encrypted to this machine's identity, which lives in the keyring
    the restore replaces with the backup's. The former identity is kept as a previous one.
    MUTATION: drop _keep_previous_identity - the safety backup no longer opens - red."""
    from pyrage import x25519

    from vaf.core.data_keyring import set_data_secret
    real_place = backup._place_files

    def _place_and_replace(staging):
        placed = real_place(staging)
        set_data_secret(backup.IDENTITY_SECRET, str(x25519.Identity.generate()))
        return placed

    monkeypatch.setattr(backup, "_place_files", _place_and_replace)
    made = backup.create_backup(target["out"], include_volumes=False)
    result = backup.restore_backup(made.path)
    assert result.pre_restore is not None
    check = backup.verify_backup(result.pre_restore)
    assert check.ok, check.problems


def test_an_interrupted_volume_read_never_leaves_docker_cp_blocked(tmp_path, monkeypatch):
    """After a read that stopped half-way, docker cp may be blocked writing into a full
    pipe; reading its stderr or waiting on it first would hang. MUTATION: wait before
    closing its stdout - red."""
    events = []

    class _Out(io.BytesIO):
        def close(self):
            events.append("stdout closed")
            super().close()

    class _Proc:
        def __init__(self, *a, **k):
            self.stdout = _Out(b"this is not a tar stream" * 100)
            self.stderr = io.BytesIO(b"")

        def wait(self, timeout=None):
            events.append("wait")
            return 0

        def kill(self):
            events.append("kill")

    monkeypatch.setattr(backup.subprocess, "Popen", _Proc)
    monkeypatch.setattr(backup, "_docker_exe", lambda: "docker")
    with pytest.raises(Exception):
        backup._docker_cp_tar("vaf-backup-carrier-ab12cd34", "/v", tmp_path / "x.tar")
    assert events.index("stdout closed") < events.index("wait")


def test_the_kit_carries_the_machine_key_also_under_a_passphrase(monkeypatch):
    """A store wrapped under the machine key before the passphrase came is upgraded only
    when it is next opened; until then the machine key opens it. MUTATION: return None
    when a passphrase is set - red."""
    from vaf.core import secure_store
    from vaf.core.recovery_kit import _machine_kek_for_kit
    key = secure_store._config_kek(create=True)
    secure_store.set_passphrase("a passphrase")
    try:
        assert _machine_kek_for_kit() == key
    finally:
        secure_store.set_passphrase(None)


def test_a_file_removed_while_the_backup_runs_is_skipped_and_named(install, monkeypatch):
    """A chat deleted between the walk and the archive used to fail the whole backup; the
    size is also taken from the file that is read, not from a stat before it. MUTATION:
    stat before opening again - red."""
    real = backup._collect_files

    def _collect(*a, **k):
        members = real(*a, **k)
        gone = install["roots"]["app"] / "sessions" / "deleted-meanwhile.json"
        return members + [("files/vaf/sessions/deleted-meanwhile.json", gone)]

    monkeypatch.setattr(backup, "_collect_files", _collect)
    result = backup.create_backup(install["out"], include_volumes=False)
    assert result.verified
    assert any("deleted-meanwhile" in w for w in result.warnings)
    assert not any("deleted-meanwhile" in n for n, _ in _members(result.path))


def test_restoring_a_keyring_file_reads_the_new_one_and_keeps_the_safety_backup(install, target,
                                                                                monkeypatch):
    """The keyring FILES are replaced on disk: the restored secrets must be what VAF reads
    afterwards - the file-store key too, which the process holds in memory - and the safety
    backup (encrypted to the identity in the replaced keyring) must still open.
    MUTATION: drop _keep_previous_identity, or the reset of the file-store key - red."""
    import base64
    import os

    from pyrage import x25519

    from vaf.core import data_files, data_keyring
    from vaf.core.data_keyring import peek_data_secret, set_data_secret
    from vaf.core.platform import Platform
    data, app = install["roots"]["data"], install["roots"]["app"]
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: data))
    # The file-store key looks for sealed files before it mints one: in this installation,
    # not in whatever home the rest of the suite left sealed chats in.
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: app))
    monkeypatch.setattr(data_keyring, "_established_marker", lambda: data / "data_keys.established")
    data_keyring.reset_ring()
    data_files.reset_key_cache()
    try:
        backup.machine_identity()
        set_data_secret("probe", "from the backup")
        chat = install["roots"]["app"] / "sessions" / "sealed.json"
        chat.write_bytes(data_files.encrypt_bytes(b"the backup's chat"))
        saved_identity = peek_data_secret(backup.IDENTITY_SECRET)
        made = backup.create_backup(install["out"], include_volumes=False)
        this_machine = str(x25519.Identity.generate())
        set_data_secret(backup.IDENTITY_SECRET, this_machine)
        set_data_secret("probe", "from this machine")
        set_data_secret("file_store_encryption_key", base64.b64encode(os.urandom(32)).decode())
        data_files.reset_key_cache()
        data_files.encrypt_bytes(b"this machine writes")      # its key is now held in memory

        result = backup.restore_backup(made.path, identity=saved_identity)

        assert peek_data_secret("probe") == "from the backup"
        assert peek_data_secret(backup.IDENTITY_SECRET) == saved_identity
        assert data_files.decrypt_bytes(chat.read_bytes()) == b"the backup's chat"
        check = backup.verify_backup(result.pre_restore)
        assert check.ok, check.problems
    finally:
        data_keyring.reset_ring()
        data_files.reset_key_cache()


def test_a_restore_of_project_environments_without_their_image_changes_nothing(target, tmp_path,
                                                                              monkeypatch):
    """Without the environment image no container can be recreated, and the reaper would
    delete the restored records and volumes at the next start. MUTATION: drop the preflight
    - the restore goes ahead - red."""
    from vaf.core import environment_image
    record = json.dumps({"id": "e1", "kind": "project", "volume": "vaf-env-vol-e1"}).encode()
    archive = _encrypt_tar(tmp_path / "envs.tar.age", {
        "files/vaf/environments/e1.json": record,
        "postgres/database.dump": b"PGDMP", "postgres/globals.sql": b""})
    monkeypatch.setattr(environment_image, "ensure_image", lambda: None)
    before = sorted(p.name for p in target["roots"]["app"].iterdir())
    with pytest.raises(backup.BackupError) as err:
        backup.restore_backup(archive, pre_restore=False)
    assert "nothing was changed" in str(err.value)
    assert sorted(p.name for p in target["roots"]["app"].iterdir()) == before
    assert target["calls"]["pg"] == 0
