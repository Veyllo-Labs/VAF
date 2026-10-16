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
