# Backup and restore

What VAF keeps, where it keeps it, and what a backup has to carry so that a restore
on a new machine opens everything again.

## What VAF keeps where

VAF writes into five places on disk. Which entry of which place holds what is
decided in ONE list, `STORES` in `vaf/core/data_stores.py`; everything that has to
know (the backup, its report of what it skipped) reads that list instead of a copy.

| Root | On Linux | What lives there |
|------|----------|------------------|
| `app` | `~/.vaf` (`$VAF_CONFIG_DIR` in Docker mode) | `config.json`, the machine key (`secure_store.kek`), the per-user identity folders, the TLS authority |
| `vaf` | `~/.vaf` | chats (`sessions/`), automations, reminders, skills, workflows, rooms (`a2a/`), each account's SSH and FTP folders, sandbox environment records |
| `data` | `~/.local/share/vaf` | the keyring (`data_keys.enc`) and the other encrypted stores (`*.enc` + `*.key.json`), the recovery wrap, the message, mail, calendar and contact databases, the spend ledger, the security event log |
| `config` | `~/.config/vaf` | the tool grants per account (`trust/`) |
| `projects` | `~/Documents/VAF_Projects` | the people's own working folders |

On macOS `data` and `config` are both `~/Library/Application Support/vaf`; on
Windows they are `%LOCALAPPDATA%\vaf` and `%APPDATA%\vaf`.

Each top-level entry is one of:

| Kind | Meaning | Backed up |
|------|---------|-----------|
| `key` | key material; without it the ciphertext beside it is noise | yes |
| `data` | what people made or decided | yes |
| `recreatable` | comes back by signing in or pairing again, at a cost (the WhatsApp link, the TLS authority) | yes |
| `audit` | the security event log | yes |
| `derived` | caches and indexes VAF rebuilds | no |
| `runtime` | locks, pids, queues, markers of a running process; restoring them would replay work | no |
| `log` | diagnostics | no |
| `never` | must not leave the machine: the recovery note (a plaintext key) and plaintext legacy copies of keys | no |

An entry on disk that the list does not know is reported, not taken and not
silently dropped. `tests/test_data_stores.py` fails when the code joins a new
literal name onto one of the roots without a row in the list, so a new store
cannot ship without somebody deciding whether it is backed up.

Not files, and therefore not rows: Postgres (accounts, sessions, memories in the
Docker volume `vaf_memory_pgdata`), the Docker volumes of the project sandbox
environments and of the personal browsers, and credentials kept in the operating
system's keyring (`EXTERNAL` in the same module).

**Named boundary:** the working memory under the launch directory
(`<cwd>/.vaf/main`) is per-session scratch state that the garbage collector
removes by age; it is not a root.

## The recovery key

The recovery key (`VAF-BackThisUp.md`) is the offline half of a restore: it opens
the keyring AND the machine key under which the credential stores are sealed. See
[ENCRYPTION_AT_REST.md](../security/ENCRYPTION_AT_REST.md#the-recovery-key).

## Writing a backup

```
vaf backup create                 # into the backup folder
vaf backup create --to /mnt/usb   # somewhere else
vaf backup verify <file>          # open it and check every file
vaf backup verify <file> --recovery-key
vaf backup list
```

`vaf/core/backup.py` writes ONE file, `vaf-backup-<UTC time>.tar.age`, with a
`.sha256` beside it, into `backup_dir` (default `<data dir>/backups`, owner-only).
The `vaf backup` group sits behind the terminal door: a backup holds every key of
the machine, encrypted to a key this machine has, so the agent's own commands do
not write or read one; a script without a terminal passes as everywhere.
`restore` is the one command that does not go through the door, because it proves
the person itself (see [Restoring](#restoring)).

It is a tar archive (POSIX/pax) encrypted with [age](https://age-encryption.org),
the format the recovery key already uses. In an emergency it opens without VAF:

```
age -d -i key.txt vaf-backup-<time>.tar.age | tar x
```

Inside, in this order:

| Member | What |
|--------|------|
| `manifest.json` | format tag `vafbackup-1-5e2c90`, VAF version, the roots as they were, what was included and skipped, the warnings |
| `files/<root>/<path>` | every entry the list above marks as backed up. SQLite databases are copied with SQLite's own online backup, so a database in use (WAL mode) is captured consistent, and their `-wal`/`-shm` files are not taken |
| `postgres/database.dump` | `pg_dump -Fc` of the memory database, run inside the `vaf-memory-db` container as the owner role (row-level security shows the app role no rows). A stopped container is started for it and stopped again |
| `postgres/globals.sql` | the roles with their password hashes, so the connection strings in the restored `config.json` fit the restored database |
| `volumes/vaf-env-vol-<id>.tar` | the `/workspace` of each sandbox environment that keeps its files in a volume |
| `volumes/vaf-browser-profile-<hash>.tar` | each personal browser's profile, without its caches, the components Chromium downloads again, and the Workspace and Downloads folders (they mirror the person's own files) |
| `checksums.json` | the SHA-256 of every member above |

The parts that are plaintext before they are encrypted (the database dump, the
SQLite copies, the volume tars, the machine key) are staged in an owner-only
folder of the local data directory, never in the destination: that can be a USB
stick or a network mount, where a deleted copy is still recoverable. A staging
folder left by a run that crashed is removed by the next one after a day. Neither
the destination nor the configured backup folder is ever taken into a backup,
also when one of them sits inside a root.

Volumes are read by NAME through a carrier container that mounts them read-only
and is never started: a personal browser's container is recreated while its
profile volume stays, so going by container lost one of three profiles on a live
machine.

**A backup without the database is not written.** If there is no `vaf-memory-db`
container, or `pg_dump` fails, the backup fails and leaves nothing behind: a
backup that silently lacks the accounts and memories would be trusted and restore
nothing.

Each file is opened before its entry is written, and the size in the archive is
the size of the open file: a file replaced in between cannot give the entry one
size and the read another. A file deleted while the backup runs (a chat removed
in that moment) is left out and named in the warnings; the rest of the backup
stands.

Every backup is verified right after it is written: decrypted with this machine's
key and every member compared with its checksum. `vaf backup verify` does the
same later; the `.sha256` beside the file catches a change in storage before
anything is decrypted.

Measured on a desktop install: 430 MB, 9424 files, about 18 seconds.

### Who can open it

The file is encrypted to public keys, so nothing that decrypts it is needed to
write it - what lets a scheduled backup run unattended:

- **this machine** (`backup_identity` in the data keyring, created on first
  use): every backup is verified with it, and a restore on the same machine
  needs nothing typed. Whoever holds this machine holds its live data anyway.
- **the recovery key** (its public half, `recovery_kit.recipient()`): the key you
  keep off the machine opens the backup on a new one. A version 1 recovery key
  has no public half; the backup then says that only this machine can open it,
  and `vaf secure renew-recovery-key` fixes that. A backup opens with the
  recovery key it was written to: after a renewal, the earlier ones still need
  the old key (or this machine), so write a new one.

### Not in the backup (named boundaries)

- Credentials kept in the operating system's keyring (mail, cloud and GitHub
  sign-ins on a desktop that has one). The backup says so when there are any. On a
  machine without an OS keyring (a server with no desktop session) they are kept
  in the encrypted files, which are backed up.
- A sandbox environment that mounts a host folder keeps its files in that folder:
  inside `VAF_Projects` they are backed up with it, anywhere else they are the
  person's own folder.
- Entries in VAF's folders that are not in the list (old copies, debris, other
  tools' files) are listed as skipped, never taken.

## Restoring

```
vaf backup restore <file>                  # on the machine the backup came from
vaf backup restore <file> --recovery-key   # on a new machine
```

It replaces everything VAF keeps on this machine with the backup's, so it asks
for a person at the terminal (never the agent's own commands, never a script)
and for proof: the admin password of this machine, or the recovery key, which is
also what opens the backup on a machine that never saw it. On a machine that
already has an admin account, the recovery key counts only when it opens THIS
machine's recovery file; otherwise the admin password is asked. A key that merely
opens the backup proves nothing about the machine, since anyone can write a backup
to a key of their own. It does not go through
the terminal door the rest of the group uses: the door asks for the admin
password, which may be exactly what is gone, and on a new machine without an
account it offers to create one that the restore then replaces. It refuses the
agent's own commands and a script itself. VAF must be stopped;
the database container must exist (on a new machine, start VAF's services once).
It shows what the backup is (host, time, VAF version, what it holds) and asks
before it starts.

`restore_backup` in `vaf/core/backup.py` then works in this order, each step only
after the one before succeeded:

1. **Open and check.** Every member is decrypted, its name checked against the
   shape a VAF backup has (no absolute path, no `..`, only the known prefixes and
   volume names, only regular files; anything else refuses the whole archive),
   and its checksum compared. Nothing lands from a backup that does not verify.
2. **Version.** A backup written by a newer VAF is refused; update first. When the
   backup holds project sandbox environments, the image they run on must be here
   (it is built when it is missing). Without it the restore refuses before
   anything changes: the records would land with nothing to recreate their
   containers, and the reaper would delete them, volumes included, at the next
   start.
3. **Safety backup.** The current state is backed up into
   `<backup folder>/pre-restore/` (`--no-safety-backup` skips it), so a wrong
   restore can be undone. It is encrypted to this machine's backup identity, which
   lives in the keyring the restore replaces: the former identity is therefore kept
   as a previous one (`backup_identities_previous`), and this machine opens the
   safety backup afterwards as well as the backups made with the restored keyring.
4. **Files.** Every entry of the roots that is backed up, derived or runtime is
   removed and the backup's files are placed. Logs, entries that are not VAF's,
   the backup folder and THIS machine's key stay.
5. **Keys.** This machine's key is never replaced: every store the backup's
   machine key sealed is re-wrapped under it (`rewrap_from_kek`), which is why a
   backup carries that key (`keys/machine_key`). The keyring on disk is now the
   backup's, so what the running restore read before it is dropped from memory
   (the file-store key above all) before anything reads the keyring again. The
   restored recovery wrap still carries the backup's machine key, so it is
   rebuilt with this machine's key for the same recovery key, as
   `vaf secure recover` does (see [ENCRYPTION_AT_REST.md](../security/ENCRYPTION_AT_REST.md));
   if that fails, the warnings say to renew the recovery key.
6. **Database.** The roles from `globals.sql` (their password hashes, so the
   restored `config.json` connects), then a fresh database from the dump.
7. **Sandbox environments.** On every restore, with volumes or without (an
   environment that mounts a host folder has none), `EnvironmentManager.readopt()`
   gives each project environment its container back (see [SANDBOXING.md](../security/SANDBOXING.md)).
   A restored record is data from a file: the host folder it names passes the same
   checks as a new environment's (no home directory, no system folder) and is
   stored resolved; a refused one counts as failed. Failed and missing ones are
   named in the warnings with what follows from them: VAF deletes them at its next
   start unless the restore is run again first.
8. **Volumes.** Each is emptied and filled from the backup with its owners
   (`docker cp -a`); an environment volume is then handed to this machine's
   environment user. A browser profile keeps its Workspace, Downloads and cache
   folders, which the backup deliberately did not carry.

Measured on a desktop install, restored into an empty home with a fresh database
container: 9417 files placed, five encrypted stores re-wrapped under the new
machine's key, the keyring (13 keys), the API keys and stored credentials open,
a chat with 66 messages decrypts, the database holds both accounts and 836
memories, a memory decrypts, and both account records pass their seal, so the
accounts can sign in.
