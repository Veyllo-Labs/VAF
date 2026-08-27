# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""What the at-rest shield does on Windows and macOS, proven from Linux.

The round was designed and run on Linux only, and two of its assumptions are
POSIX assumptions. Neither can be tested by running the code as-is here, so
each test injects the foreign platform's behaviour at the exact seam where it
differs, and each states the mutation that turns it red.

**Renaming onto an open file.** POSIX always allows it; Windows MoveFileEx
fails with a sharing violation while any other handle is open on the
destination without FILE_SHARE_DELETE, which `open()` does not request. Every
store in the round writes through `_atomic_write_bytes`, so one unretried
failure is a lost chat save or a lost keyring write - and the holders are all
transient (Defender scanning the file just written, the Search indexer, a
concurrent reader).

**The recovery kit's write order.** The note is the only copy of the secret
that opens the wrap, and every way it can fail is platform-shaped: a Desktop
macOS TCC has denied, a OneDrive-redirected Desktop that is not materialised, a
read-only disk. Writing the wrap first left an unopenable file behind and a
guard that considers the job done forever.
"""
import json
import os

import pytest

from vaf.core import recovery_kit, secure_store


# ── renaming onto a file another process holds open ─────────────────────────────

def test_a_windows_sharing_violation_is_retried_not_lost(tmp_path, monkeypatch):
    """MUTATION: call os.replace directly again and this goes red.

    Simulates the Windows failure by making the first two replaces raise
    PermissionError, which is what CPython surfaces for a sharing violation.
    """
    target = tmp_path / "chat.json"
    calls = {"n": 0}
    real_replace = os.replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(32, "The process cannot access the file")
        return real_replace(src, dst)

    monkeypatch.setattr(secure_store, "_on_windows", lambda: True)
    monkeypatch.setattr(secure_store.os, "replace", flaky)
    monkeypatch.setattr(secure_store.time, "sleep", lambda *_a: None)

    secure_store._atomic_write_bytes(target, b"the chat")

    assert target.read_bytes() == b"the chat"
    assert calls["n"] == 3, "the write must have been retried, not abandoned"


def test_a_permanent_windows_denial_still_raises(tmp_path, monkeypatch):
    """The retry must not turn a real permission problem into a silent no-op."""
    target = tmp_path / "chat.json"

    def always_denied(src, dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(secure_store, "_on_windows", lambda: True)
    monkeypatch.setattr(secure_store.os, "replace", always_denied)
    monkeypatch.setattr(secure_store.time, "sleep", lambda *_a: None)

    with pytest.raises(PermissionError):
        secure_store._atomic_write_bytes(target, b"the chat")

    assert not target.exists()
    leftovers = list(tmp_path.glob(".tmp-*"))
    assert not leftovers, f"the temp file was left behind: {leftovers}"


def test_posix_does_not_retry(tmp_path, monkeypatch):
    """A PermissionError on POSIX is a real one - retrying would only delay it."""
    calls = {"n": 0}

    def denied(src, dst):
        calls["n"] += 1
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(secure_store, "_on_windows", lambda: False)
    monkeypatch.setattr(secure_store.os, "replace", denied)

    with pytest.raises(PermissionError):
        secure_store._atomic_write_bytes(tmp_path / "x.json", b"y")

    assert calls["n"] == 1


# ── the recovery kit's write order ──────────────────────────────────────────────

def test_an_unwritable_desktop_leaves_no_orphaned_wrap(tmp_path, monkeypatch):
    """The macOS TCC / OneDrive case: the note cannot be written.

    MUTATION: persist the wrap before the note (the original order) and this
    goes red - the wrap survives, nothing can open it, and the guard below
    treats the recovery key as created for the rest of the installation's life.
    """
    monkeypatch.setattr(recovery_kit, "recovery_wrap_path",
                        lambda: tmp_path / "data_keys.recovery.json")

    def denied():
        raise PermissionError(13, "Operation not permitted")   # what TCC surfaces

    monkeypatch.setattr(recovery_kit, "kit_path", denied)

    assert recovery_kit.ensure_recovery_kit(b"k" * 32) is None
    assert not recovery_kit.recovery_wrap_path().exists(), (
        "a wrap with no key to open it is worse than no wrap: the guard would "
        "never create one again"
    )


def test_the_next_start_retries_after_an_unwritable_desktop(tmp_path, monkeypatch):
    """The point of leaving nothing behind: it must actually heal."""
    monkeypatch.setattr(recovery_kit, "recovery_wrap_path",
                        lambda: tmp_path / "data_keys.recovery.json")
    monkeypatch.setattr(recovery_kit, "kit_path",
                        lambda: (_ for _ in ()).throw(PermissionError(13, "denied")))

    recovery_kit.ensure_recovery_kit(b"k" * 32)          # the denied start

    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    monkeypatch.setattr(recovery_kit, "kit_path",
                        lambda: desktop / recovery_kit.KIT_FILENAME)

    note = recovery_kit.ensure_recovery_kit(b"k" * 32)   # the next start

    assert note is not None and note.exists()
    secret = next(l.strip() for l in note.read_text().split("## Your recovery key")[1]
                  .split("##")[0].splitlines() if l.startswith("    ") and l.strip())
    assert recovery_kit.unwrap_with_secret(secret) == b"k" * 32


@pytest.mark.skipif(os.name == "nt", reason="chmod cannot restrict read access on Windows")
def test_the_note_is_never_world_readable_even_briefly(tmp_path, monkeypatch):
    """It is a plaintext master key on the Desktop: 0600 from creation, not after.

    MUTATION: go back to write_text() + harden_path() and this goes red on any
    machine whose umask leaves the default mode group- or world-readable.

    Skipped rather than weakened on Windows, where the observed mode was 666:
    `os.chmod` there honours only the read-only flag, so no mode assertion can
    hold. What protects the file on that platform is the profile ACL, which VAF
    neither sets nor can verify - which is precisely why the master key does not
    live in a file there. Asserting a laxer mode everywhere would have thrown
    away the guarantee on the two platforms that do keep it.
    """
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    monkeypatch.setattr(recovery_kit, "recovery_wrap_path",
                        lambda: tmp_path / "data_keys.recovery.json")
    monkeypatch.setattr(recovery_kit, "kit_path",
                        lambda: desktop / recovery_kit.KIT_FILENAME)

    seen = []
    real_chmod = os.chmod

    def watched(path, mode, *a, **kw):
        # Record the mode the file had BEFORE anyone narrowed it.
        try:
            seen.append(oct(os.stat(path).st_mode)[-3:])
        except OSError:
            pass
        return real_chmod(path, mode, *a, **kw)

    monkeypatch.setattr(secure_store.os, "chmod", watched)
    recovery_kit.ensure_recovery_kit(b"k" * 32)

    assert seen, "harden_path never ran - the test proves nothing"
    assert all(m == "600" for m in seen), (
        f"the note existed with a wider mode before it was narrowed: {seen}")


def test_the_wrap_still_guards_against_a_second_kit(tmp_path, monkeypatch):
    """The write-once property must survive the reordering.

    The user is told to move the note off the machine, so its absence is the
    SUCCESS case - regenerating on absence would invalidate the copy they filed
    away. The guard therefore still tests the wrap.
    """
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    monkeypatch.setattr(recovery_kit, "recovery_wrap_path",
                        lambda: tmp_path / "data_keys.recovery.json")
    monkeypatch.setattr(recovery_kit, "kit_path",
                        lambda: desktop / recovery_kit.KIT_FILENAME)

    first = recovery_kit.ensure_recovery_kit(b"k" * 32)
    assert first is not None
    (desktop / recovery_kit.KIT_FILENAME).unlink()        # the user filed it away

    assert recovery_kit.ensure_recovery_kit(b"k" * 32) is None
    assert not (desktop / recovery_kit.KIT_FILENAME).exists()


def test_a_failed_wrap_write_does_not_claim_success(tmp_path, monkeypatch):
    """The mirror case: note written, wrap not. It must heal, not lie."""
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    monkeypatch.setattr(recovery_kit, "recovery_wrap_path",
                        lambda: tmp_path / "data_keys.recovery.json")
    monkeypatch.setattr(recovery_kit, "kit_path",
                        lambda: desktop / recovery_kit.KIT_FILENAME)
    monkeypatch.setattr(recovery_kit, "persist_recovery_wrap",
                        lambda doc: (_ for _ in ()).throw(OSError("disk full")))

    assert recovery_kit.ensure_recovery_kit(b"k" * 32) is None
    assert not recovery_kit.recovery_wrap_path().exists()


def test_the_wrap_document_is_built_without_touching_disk(tmp_path, monkeypatch):
    """The split the ordering depends on: building must persist nothing."""
    monkeypatch.setattr(recovery_kit, "recovery_wrap_path",
                        lambda: tmp_path / "data_keys.recovery.json")

    secret, doc = recovery_kit.build_recovery_wrap(b"k" * 32)

    assert not recovery_kit.recovery_wrap_path().exists()
    assert set(doc) >= {"v", "kdf", "salt", "nonce", "wrapped"}
    recovery_kit.persist_recovery_wrap(doc)
    assert recovery_kit.unwrap_with_secret(secret) == b"k" * 32
    assert json.loads(recovery_kit.recovery_wrap_path().read_text())["kdf"] == "scrypt"


# ── where a NEW master key goes, per platform ───────────────────────────────────

def test_windows_puts_the_master_key_in_the_credential_manager(monkeypatch):
    """On Windows a key FILE is protected by nothing we do.

    os.chmod cannot restrict read access there (CPython: only the read-only flag
    is honoured, "All other bits are ignored"), so a 0600 KEK file is 0600 in
    name only. The Credential Manager is DPAPI-backed, per user, and VAF's own
    Windows autostart is the user's Startup folder, so the tray always runs in
    that user's logon session - the Linux objection (a supervisor-started tray
    with no session bus) has no counterpart.

    MUTATION: return "file" unconditionally and this goes red.
    """
    monkeypatch.setattr(secure_store, "_on_windows", lambda: True)
    assert secure_store._default_kek_backend() == "keyring"


def test_posix_keeps_the_key_in_a_file(monkeypatch):
    """chmod is real on Linux and macOS, and both keyrings can lock us out.

    Linux was measured: a supervisor-started tray produced 295 failed key
    resolutions against a locked keyring. macOS binds a Keychain item's ACL to
    the requesting binary, so an interpreter upgrade re-prompts or refuses.
    """
    monkeypatch.setattr(secure_store, "_on_windows", lambda: False)
    assert secure_store._default_kek_backend() == "file"


@pytest.mark.parametrize("on_windows", [True, False])
@pytest.mark.parametrize("choice", ["file", "keyring"])
def test_an_explicit_choice_beats_the_platform_default(monkeypatch, on_windows, choice):
    from vaf.core.config import Config

    real = Config.get
    monkeypatch.setattr(secure_store, "_on_windows", lambda: on_windows)
    monkeypatch.setattr(Config, "get", classmethod(
        lambda cls, k, d=None: choice if k == "secure_store_kek_backend" else real(k, d)))

    assert secure_store._preferred_kek_backend() == choice


def test_an_unknown_value_falls_back_to_the_platform_default(monkeypatch):
    """A typo in the config must not silently mean "no backend"."""
    from vaf.core.config import Config

    real = Config.get
    monkeypatch.setattr(secure_store, "_on_windows", lambda: True)
    monkeypatch.setattr(Config, "get", classmethod(
        lambda cls, k, d=None: "kyering" if k == "secure_store_kek_backend" else real(k, d)))

    assert secure_store._preferred_kek_backend() == "keyring"


def test_the_shipped_default_is_auto():
    """"auto" is what lets the platform decide; a concrete value here would pin
    every platform to whatever the author's machine happened to be."""
    from vaf.core.config import Config

    assert Config.DEFAULTS["secure_store_kek_backend"] == "auto"


# ── the compose password does not live in the working tree ──────────────────────

def test_the_compose_password_is_written_beside_the_other_secrets(tmp_path, monkeypatch):
    """Not into the checkout: on Windows the installation directory is often
    outside the profile, where the drive-root ACL grants every local account
    read and chmod cannot fix it - and editors treat a root .env as project
    configuration to inject into terminals.

    MUTATION: write `<project_root>/.env` again and this goes red.
    """
    from vaf.core import service_stack
    from vaf.core.config import Config

    monkeypatch.setattr(Config, "APP_DIR", tmp_path / "home_vaf")
    monkeypatch.setattr("vaf.memory.cache.redis_password", lambda: "s3cret-token")
    project = tmp_path / "checkout"
    project.mkdir()

    service_stack._write_compose_env_file(project)

    written = service_stack.compose_env_file()
    assert written == tmp_path / "home_vaf" / "compose.env"
    assert "REDIS_PASSWORD=s3cret-token" in written.read_text()
    assert not (project / ".env").exists(), "the secret was written into the checkout"


def test_an_older_repo_env_file_loses_only_our_line(tmp_path, monkeypatch):
    """The file may carry variables that belong to the user."""
    from vaf.core import service_stack
    from vaf.core.config import Config

    monkeypatch.setattr(Config, "APP_DIR", tmp_path / "home_vaf")
    monkeypatch.setattr("vaf.memory.cache.redis_password", lambda: "new-token")
    project = tmp_path / "checkout"
    project.mkdir()
    (project / ".env").write_bytes(b"MY_OWN=keepme\nREDIS_PASSWORD=old-token\nOTHER=alsokeep\n")

    service_stack._write_compose_env_file(project)

    left = (project / ".env").read_bytes()
    assert b"REDIS_PASSWORD" not in left
    assert b"MY_OWN=keepme" in left and b"OTHER=alsokeep" in left


def test_a_repo_env_file_that_was_only_ours_is_removed(tmp_path, monkeypatch):
    """Otherwise the editor keeps offering to inject an empty file."""
    from vaf.core import service_stack
    from vaf.core.config import Config

    monkeypatch.setattr(Config, "APP_DIR", tmp_path / "home_vaf")
    monkeypatch.setattr("vaf.memory.cache.redis_password", lambda: "new-token")
    project = tmp_path / "checkout"
    project.mkdir()
    (project / ".env").write_bytes(b"REDIS_PASSWORD=old-token\n")

    service_stack._write_compose_env_file(project)

    assert not (project / ".env").exists()


def test_an_unreadable_keyring_removes_the_password_file(tmp_path, monkeypatch):
    """A stale line would start Redis WITH a password while the client sends
    none - NOAUTH on every cache call, with only a warning in a log.

    BOTH secrets have to be unreadable for the file to go: it carries the
    browser stream credential too now, and the writer works per key so an
    unreadable keyring drops only the line it could not produce."""
    from vaf.core import service_stack
    from vaf.core.config import Config

    monkeypatch.setattr(Config, "APP_DIR", tmp_path / "home_vaf")
    (tmp_path / "home_vaf").mkdir()
    (tmp_path / "home_vaf" / "compose.env").write_text("REDIS_PASSWORD=stale\n")
    monkeypatch.setattr("vaf.memory.cache.redis_password", lambda: "")
    monkeypatch.setattr("vaf.core.browser_interactive.browser_vnc_secret", lambda: "")
    project = tmp_path / "checkout"
    project.mkdir()

    service_stack._write_compose_env_file(project)

    assert not service_stack.compose_env_file().exists()


# ── the terminal door cannot be walked around by redirecting output ─────────────

def test_redirecting_output_does_not_open_the_terminal_door(monkeypatch):
    """`vaf session export <id> > chat.txt` used to skip the password entirely.

    The gate required stdin AND stdout to be ttys, so anyone at a real terminal
    could disable it by redirecting - on the exact command group the gate was
    added to protect. getpass talks to the terminal directly, so a redirected
    stdout never stopped the question from being askable.

    MUTATION: require stdout.isatty() again and this goes red.
    """
    from vaf.cli import gate

    class _Tty:
        def isatty(self):
            return True

    class _Pipe:
        def isatty(self):
            return False

    monkeypatch.setattr(gate.sys, "stdin", _Tty())
    monkeypatch.setattr(gate.sys, "stdout", _Pipe())

    assert gate.is_interactive() is True


def test_a_script_feeding_stdin_is_still_not_prompted(monkeypatch):
    """The case the check is actually for: no human to answer."""
    from vaf.cli import gate

    class _Tty:
        def isatty(self):
            return True

    class _Pipe:
        def isatty(self):
            return False

    monkeypatch.setattr(gate.sys, "stdin", _Pipe())
    monkeypatch.setattr(gate.sys, "stdout", _Tty())

    assert gate.is_interactive() is False


# ── every interactive lane migrates, not just two of the three ──────────────────

def test_every_interactive_lane_runs_the_at_rest_migration():
    """The default terminal lane skipped it while the other two did not.

    A repair wired into some lanes is the defect this repository has shipped
    before, and it is invisible from inside any single lane. The check is
    textual on purpose: the alternative is booting three full terminal apps,
    and what actually went wrong was a missing call site, not a broken call.

    MUTATION: delete the run_once() call from any of the three and this goes
    red, naming the lane.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    lanes = {
        "modern / classic CLI (vaf run)": root / "vaf/cli/cmd/run.py",
        "the default TUI app lane": root / "vaf/cli/tui_app/agent_bridge.py",
        "web and tray startup": root / "vaf/core/web_server.py",
    }
    missing = [name for name, path in lanes.items()
               if "at_rest_migration import run_once" not in path.read_text(encoding="utf-8")]

    assert not missing, f"these start lanes never run the at-rest migration: {missing}"


def test_the_headless_wait_loop_does_not_depend_on_a_posix_only_call():
    """`vaf start` used to exit within seconds on Windows.

    The loop called signal.pause(), which does not exist there. The resulting
    AttributeError is not in the except tuple, so it propagated out of the main
    thread and every daemon thread died with it - including the uvicorn thread
    that hosts the startup hooks, so nothing the service was started for ever
    ran. Nobody saw it because the default Windows start is the tray, not this.

    MUTATION: call signal.pause() unguarded again and this goes red.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "vaf/tray.py").read_text(encoding="utf-8")
    block = src.split("Headless mode active")[1][:900]

    assert "getattr(sig_module, \"pause\", None)" in block, (
        "the headless wait loop calls a POSIX-only function without a guard")
    assert "_time.sleep" in block, "no portable fallback for platforms without signal.pause"
