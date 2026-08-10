# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""How VAF stores conversations, and the four decisions you make about it.

    venv/bin/python examples/08_session_storage_and_encryption.py

No model, no API key, no network: every step drives the session store directly
so you can see the bytes that land on disk. The four parts are the four
questions an embedder actually has to answer, in the order they come up:

  1. plaintext, one user        - the simplest thing that works
  2. plaintext, several users   - one installation, separate people
  3. encrypted                  - the same code, one config key different
  4. recovery                   - what happens when the machine is gone

Background: docs/security/ENCRYPTION_AT_REST.md and the "Encryption at rest"
section of docs/EMBEDDING.md.

## Which API this uses, and why

`SessionManager` is engine-internal (`vaf.core.session`, not the facade) and may
change between releases. It is used here because it needs no model, so every
step below is a fact you can see rather than a claim. In an application the
public path is the facade, and it stores into the same files with the same
encryption:

    agent = Agent(user_scope=<uuid>)        # ownership
    agent.run("...")
    sid = agent.save_session()              # write
    agent = Agent(user_scope=<uuid>, session=sid)   # resume

What is NOT on the facade is session ENUMERATION - listing and searching another
scope's chats is exactly the operation multi-tenant code gets wrong, so it is not
exported on speculation. If you need it, say so.
"""
import atexit
import os
import shutil
import tempfile
from pathlib import Path

# ── a throwaway HOME, set up BEFORE vaf is imported ─────────────────────────────
# Everything below mints keys and, in part 4, deletes one again. VAF resolves its
# config, key store and Desktop from the home directory at import time, so the
# sandbox has to exist first. Your own installation is never touched.
SANDBOX = Path(tempfile.mkdtemp(prefix="vaf-storage-example-"))
atexit.register(shutil.rmtree, SANDBOX, ignore_errors=True)
os.environ.update({
    "HOME": str(SANDBOX),
    "USERPROFILE": str(SANDBOX),                       # Windows
    "XDG_DATA_HOME": str(SANDBOX / ".local" / "share"),
    "XDG_CONFIG_HOME": str(SANDBOX / ".config"),
    "LOCALAPPDATA": str(SANDBOX / "AppData" / "Local"),  # Windows
    "VAF_LOG_DIR": str(SANDBOX / "logs"),
})
(SANDBOX / ".vaf").mkdir()
(SANDBOX / "Desktop").mkdir()   # so part 4 shows where the recovery note lands

import logging  # noqa: E402

from vaf.core.config import Config  # noqa: E402  (imported after the sandbox)
from vaf.core.session import SessionManager  # noqa: E402

# A first run logs "minted a new key" and "no KEK in config yet". Both are
# correct on a fresh installation and only get in the way of reading the output.
logging.getLogger("vaf").setLevel(logging.ERROR)

ALICE = "aaaa1111-2222-3333-4444-555555555555"
BOB = "bbbb1111-2222-3333-4444-555555555555"


def peek(path: Path, label: str) -> None:
    """Print the start of the file AS IT IS ON DISK - the only honest check."""
    head = path.read_bytes()[:48]
    print(f"      {label}: {head.decode('utf-8', 'replace')[:44]!r}")


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Plaintext, one user
# ═══════════════════════════════════════════════════════════════════════════════
def part1_plaintext(store: Path) -> None:
    """Readable JSON, one file per conversation. Still a supported mode.

    Choose it when your own layer already protects the data (an encrypted
    volume, a managed container, a database you control), or when the files are
    meant to be read by other tools. `file_encryption_enabled = false` is the
    switch; part 3 turns it back on.
    """
    print("\n1. PLAINTEXT, one user")
    Config.set("file_encryption_enabled", False)
    manager = SessionManager(storage_dir=str(store))

    session = manager.new(name="Support chat")
    session.add_message("user", "my licence key is ABCD-1234")
    session.add_message("assistant", "Noted.")
    path = manager.save(session)

    print(f"      saved {path.name}")
    peek(path, "on disk")

    # Reading back is the same call in every mode.
    print(f"      loaded: {manager.load(session.id).messages[0].content!r}")

    # This is how a UI builds its sidebar.
    for row in manager.list():
        print(f"      listed: {row['name']!r} ({row['message_count']} messages)")


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Plaintext, several users on one installation
# ═══════════════════════════════════════════════════════════════════════════════
def part2_isolation(store: Path) -> None:
    """Ownership is a value carried on the session, and there are TWO rules.

    Picking the wrong one is the classic multi-tenant bug, so they are named
    apart:

      list()        - what a scope may SEE listed. A session with no owner is
                      listed for everyone, because it predates isolation and
                      can only belong to the machine's own user.
      list_owned()  - what a scope OWNS. Both scopes must be non-empty and
                      equal, so an unowned session belongs to nobody. Use this
                      before you READ content, and everywhere a decision
                      depends on who the data belongs to.

    `iter_owned_sessions()` is the same strict rule as a generator; `search()`
    runs on it and therefore takes `user_scope_id` as a required argument.
    """
    print("\n2. PLAINTEXT, several users")
    manager = SessionManager(storage_dir=str(store))

    for scope, name, text in (
        (ALICE, "Alice: travel", "book me a flight to Lisbon"),
        (BOB, "Bob: taxes", "my tax number is 12/345/67890"),
        (None, "Legacy chat", "written before scopes existed"),
    ):
        session = manager.new(name=name, user_scope_id=scope)
        session.add_message("user", text)
        manager.save(session)

    print(f"      Alice may see: {[r['name'] for r in manager.list(user_scope_id=ALICE)]}")
    print(f"      Alice owns:    {[r['name'] for r in manager.list_owned(user_scope_id=ALICE)]}")
    print(f"      Bob owns:      {[r['name'] for r in manager.list_owned(user_scope_id=BOB)]}")
    print("      -> the legacy chat is listed for both and owned by neither")

    hits = manager.search("tax number", user_scope_id=ALICE)
    print(f"      Alice searching for Bob's content: {hits or 'nothing, as it should be'}")
    print(f"      Bob searching his own:             "
          f"{[h['session_name'] for h in manager.search('tax number', user_scope_id=BOB)]}")


# ═══════════════════════════════════════════════════════════════════════════════
# 3. The same thing, encrypted
# ═══════════════════════════════════════════════════════════════════════════════
def part3_encrypted(store: Path) -> None:
    """One config key. The calling code does not change at all.

    `file_encryption_enabled` (the product default is true) decides what NEW
    writes look like. A second setting, `allow_plaintext_at_rest`, decides
    whether a file WITHOUT the header is still accepted on read. Together they
    buy three properties:

      - a plaintext chat written before the switch keeps opening; the tolerance
        ends once a startup pass has re-written the store and found nothing
        plain left, and a file without the header is refused as a downgrade
        from then on;
      - turning the switch off writes plaintext again without stranding what is
        already encrypted (the key stays in the ring), and it reopens the
        tolerant read - a store that writes plaintext must be able to read it;
      - turning it on needs no migration step from you - re-saving a session
        encrypts it, and the product sweeps the store once at startup.

    The key is machine-held, so the agent keeps working unattended after a
    reboot. That is the deliberate trade: this protects the FILES when they
    move without the key (stolen disk, backup, cloud sync, another account),
    not against code already running as you.
    """
    print("\n3. ENCRYPTED")
    Config.set("file_encryption_enabled", True)
    manager = SessionManager(storage_dir=str(store))

    session = manager.new(name="Bank", user_scope_id=ALICE)
    session.add_message("user", "my wallet seed is apple banana cherry")
    path = manager.save(session)

    peek(path, "on disk")
    print(f"      'apple banana' present in the raw bytes: "
          f"{b'apple banana' in path.read_bytes()}")
    print(f"      'Bank' present in the raw bytes:         "
          f"{b'Bank' in path.read_bytes()}")
    print(f"      loaded: {manager.load(session.id).messages[0].content!r}")

    # A plaintext session written by an older version opens side by side.
    plain = manager.new(name="Older chat", user_scope_id=ALICE)
    plain.add_message("user", "written before encryption existed")
    Config.set("file_encryption_enabled", False)
    legacy_path = manager.save(plain)
    Config.set("file_encryption_enabled", True)
    peek(legacy_path, "legacy file")
    print(f"      both load: {[len(manager.load(i).messages) for i in (session.id, plain.id)]}")

    # Re-saving is the migration: one save, and the legacy file is ciphertext.
    manager.save(manager.load(plain.id))
    peek(legacy_path, "after re-save")


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Recovery
# ═══════════════════════════════════════════════════════════════════════════════
def part4_recovery(store: Path) -> None:
    """The part nobody thinks about until the machine is gone.

    A machine-held key means: reinstall the operating system, replace the disk,
    or lose the OS account, and the encrypted files are unreadable. No vendor
    can help, because nobody else has the key. So the first time VAF mints the
    key it also writes a RECOVERY KEY - a second wrapping of the same data key
    under a fresh 256-bit secret - and a note explaining it (VAF-BackThisUp.md
    on the Desktop, which the user is told to move off the machine).

    Recovering needs all three, and this is the sentence you owe your users:

      1. the recovery key,
      2. `data_keys.recovery.json`,
      3. `data_keys.enc`.

    The key alone is not enough, and neither are the files alone. The old
    machine key and its `data_keys.key.json` wrap are NOT on the list:
    `vaf secure recover` re-wraps the recovered data key under the NEW machine's
    key and writes that sibling itself - which is why the run below can delete
    the machine key and still get back in.
    """
    print("\n4. RECOVERY")
    from vaf.core import data_files, data_keyring, recovery_kit
    from vaf.core.secure_store import _kek_file_path, _kek_marker_path

    manager = SessionManager(storage_dir=str(store))
    session = manager.new(name="Important")
    session.add_message("user", "the safe combination is 42-17-8")
    manager.save(session)

    # VAF calls this itself the first time it mints a key; it is idempotent.
    data_keyring.ensure_recovery_kit()
    note = recovery_kit.kit_path()
    print(f"      note for the user: {note}")
    print(f"      recovery file:     {recovery_kit.recovery_wrap_path()}")

    # The user copies the key out of that note. The note is the only copy.
    secret = next(line.strip() for line in note.read_text().splitlines()
                  if line.startswith("    ") and len(line.strip()) > 40)
    print(f"      recovery key:      {secret[:12]}...  (256 bits, base64)")

    # The disaster: a new machine, so the machine key is gone. What is left is
    # ciphertext nobody can open - not the vendor, not you, not a support ticket.
    _kek_file_path().unlink(missing_ok=True)
    _kek_marker_path().unlink(missing_ok=True)
    data_files.reset_key_cache()
    data_keyring.reset_ring()
    try:
        manager.load(session.id)
        print("      UNEXPECTED: the chat opened without the key")
    except Exception as e:
        print(f"      without the machine key: {type(e).__name__}: {str(e)[:52]}...")

    # `vaf secure recover` is this. Called as a function so the example stays
    # in one process; on a real machine it prompts for the key instead.
    from vaf.cli.cmd import secure
    secure.recover(key=secret)
    data_files.reset_key_cache()

    print(f"      after recovery: {manager.load(session.id).messages[0].content!r}")


def main() -> None:
    print(f"Sandbox home: {SANDBOX}")
    part1_plaintext(SANDBOX / ".vaf" / "sessions")
    part2_isolation(SANDBOX / ".vaf" / "sessions")
    part3_encrypted(SANDBOX / ".vaf" / "sessions")
    part4_recovery(SANDBOX / ".vaf" / "sessions")
    print("\nOn a real installation, `vaf secure status` reports the same state:")
    print("where the master key lives, which keys the store holds, whether any")
    print("key material is still lying in config.json, and whether a recovery")
    print("key exists. See docs/security/ENCRYPTION_AT_REST.md.")


if __name__ == "__main__":
    main()
