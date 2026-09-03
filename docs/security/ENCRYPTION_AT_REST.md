# Encryption at rest: the shield, and where it ends

VAF runs unattended. Automations fire, channels are answered and compaction runs
after a reboot with nobody at the keyboard, so the keys that open the stored data
are held by the MACHINE, not derived from a password someone types. That single
fact decides everything below, and it is stated first on purpose: a page about
encryption that does not say what its keys are protected by is marketing.

The model is a shield around the running instance:

- **Inside**, the agents work as they always did - they read chats, memory and
  their own working state without asking anyone for anything.
- **Outside**, every entry authenticates first: the web UI by login, the
  interactive terminal by the admin password, channels by their allowlists.
- **At rest**, the files and the database rows are ciphertext, so anything that
  moves the DATA without the machine's key gets nothing.

## What this protects against, and what it does not

The answer differs per operating system, because the mechanism does. Where a row
says "depends", the platform section below it says on what.

| Situation | Windows | macOS | Linux |
|---|---|---|---|
| Disk removed, or the machine powered off and stolen | **Yes** - the master key is in the Credential Manager, encrypted under your Windows login. The disk alone is useless. | **Depends** - the key is a file, so only as far as FileVault reaches. | **Depends** - the key is a file, so only as far as LUKS reaches. |
| A backup, a copied directory, a support archive | **Yes** for the data. **But** a whole-home backup that includes the key directory carries the key with it - see "back these up together". | Same, and Time Machine backs up `$HOME` by default, so the key travels with the data unless the backup disk is itself encrypted. | Same for any `rsync` of `$HOME`. |
| Cloud sync | **Yes for the chats.** **No for the recovery note** if OneDrive Known Folder Backup has your Desktop: the note is a plaintext key and it is uploaded. Move it off the machine, which the note tells you to do. | Same, with iCloud "Desktop and Documents". | Yes - nothing here is cloud-synced by default. |
| Another local account on the same machine | **Yes for the key** (it is not on disk at all) and **yes in practice for the files**, because they sit under your profile, whose ACL excludes other standard users. But **VAF does not set that ACL** and cannot: `chmod` on Windows only toggles the read-only flag. A local **administrator** can read everything. | **Yes** - `0700` directories and `0600` files, set by VAF and honoured. | **Yes** - same. |
| The machine is running and someone has your session | **No.** The shield is open from the inside; that is what makes unattended operation possible. | **No.** | **No.** |
| Code running AS you (a hostile skill, a compromised dependency) | **No.** Same reason. | **No.** | **No.** |
| Someone who knows the admin password | **No** for the terminal and the web UI; that is authentication, not encryption. | **No.** | **No.** |

### Why the mechanism differs

`os.chmod(path, 0o600)` is the whole of VAF's file containment, and on Windows it
does nothing: the CPython documentation states that only the read-only flag can
be set there and "all other bits are ignored", and because `0o600` carries the
write bit the call does not even set that. It raises no error, so nothing in the
process learns the hardening did not happen.

Two consequences, both deliberate. The master key defaults to the **Credential
Manager on Windows** instead of a file, because a file VAF cannot protect is the
worst place for the one piece of plaintext key material in the system - and
Windows autostart runs VAF from your own Startup folder, so the Credential
Manager is always reachable. On **Linux and macOS** the key stays a `0600` file:
`chmod` is real there, and both OS keyrings can lock the app out of its own data
(Linux measured: a tray started by a supervisor script has no session bus; macOS
binds a Keychain item to the requesting binary, so an interpreter upgrade
re-prompts or refuses).

**Out-of-profile files are the Windows gap that remains.** Two paths are not
under your profile and therefore not covered by its ACL: the `.env` holding the
Redis password, written next to the compose file in the installation directory,
and the per-chat working memory under the working directory. On Windows those
are readable by other local accounts. Neither contains chat text, and the Redis
instance they guard listens on localhost only, but the honest statement is that
they are not protected there.

## Where the keys live

```
THE MASTER KEY, per platform (secure_store_kek_backend = "auto"):
   Windows   OS keyring entry vaf / secure_store_kek        <- Credential Manager (DPAPI),
                                                               encrypted under your login
   Linux     ~/.vaf/secure_store.kek  (mode 0600)           <- protected by disk encryption
   macOS     ~/.vaf/secure_store.kek  (mode 0600)           <- protected by FileVault
   (Set the key to "file" or "keyring" to override. Reading finds a KEK wherever an
    earlier version put it, so an existing install is never relocated behind your back.)
        |
        v  wraps
   <data_dir>/data_keys.key.json                <- one wrapped DEK
        |
        v  opens
   <data_dir>/data_keys.enc                     <- the keyring: every at-rest key
        |
        +-- file_store_encryption_key    chats, archives, bundles, queue, working memory
        +-- memory_encryption_key        memory rows and chunk text in Postgres
        +-- mail_store_encryption_key    raw mail bodies in mail.db
        +-- github_credentials_key       the GitHub credential fallback file
        +-- local_network_jwt_secret     token signing (and, derived, the TOTP column)
        +-- redis_password               the cache that holds DECRYPTED memory
        +-- admin_password_hash          Argon2 hash, so the terminal can verify offline
```

`~/.vaf/config.json` holds **no key material**. It used to hold all of it, which
made every "encrypted" store equivalent to chmod protection.

Check any installation with:

```
vaf secure status
```

It prints locations, never values, and names anything still left in config.json.

### The recovery key

When the keyring is created, VAF writes **`VAF-BackThisUp.md` to your Desktop**
(the key directory if there is no Desktop). It contains a recovery key: 256 bits
of randomness as ONE base64 string, under which the same data key is wrapped a
second time (scrypt, stored beside the keyring as `data_keys.recovery.json`).

One encoding, deliberately. An earlier draft also printed 24 words from a
home-made 64-word list and called them 256 bits - six bits per word is 144, there
was no checksum, so a mistyped word was indistinguishable from the wrong backup
file, and two encodings of one secret double the transcription and leak surface.
If hand-transcription ever becomes the real scenario the answer is BIP-39, not a
private word list.

With the recovery key plus `data_keys.enc` and `data_keys.recovery.json` you can
open your data on a new machine - no OS keyring, no old computer, no password:

```
vaf secure recover
```

The key alone is not enough, and neither are the files alone; the note says so
rather than implying a guarantee it cannot keep. While that file sits on the
Desktop it is a plaintext key: move it somewhere else and delete it there.
`vaf secure status` keeps reminding you until you do.

### If the key store is gone

Once the first key exists, VAF records that this installation HAS a keyring
(`~/.vaf/data_keys.established`). From then on a missing `data_keys.enc` is
treated as a loss, not as a first run: VAF refuses to start a new key and points
at the recovery key instead. That distinction is the difference between "your
data is locked until you restore the backup" and "your data is gone", and it is
not obvious from the outside - a fresh key looks like a healthy start while
every encrypted row silently stops opening.

### Back these up together

`data_keys.enc`, `data_keys.key.json` and the master key (the OS keyring entry on
Windows, `~/.vaf/secure_store.kek` on Linux and macOS). **Without all three this
machine stops opening the store**, and the way back without that backup is `vaf secure recover`: the
recovery key plus `data_keys.enc` and `data_keys.recovery.json`, which re-wraps
the data key under the new machine key. Lose the backup AND the recovery key and the data is gone -
no vendor can help, which is the honest price of local encryption.

One consequence of a whole-home backup: on macOS, Time Machine's default job
includes `$HOME`, and so does any `rsync -a ~` on Linux, so those backups carry
the key directory alongside the ciphertext. The backup is then only as protected
as the medium it lands on. That is a reason to encrypt the backup disk, not a
reason to skip the backup - losing the keys is the unrecoverable direction.

## What is encrypted

| Store | Location | State |
|---|---|---|
| Chat sessions | `~/.vaf/sessions/*.json` | AES-256-GCM |
| Context archives (pre-compression snapshots) | `~/.vaf/context_archive/` | AES-256-GCM |
| Handoff bundles | `~/.vaf/handoff_bundles/<scope>/` | AES-256-GCM |
| Sub-agent queue and task payloads | `~/.vaf/subagent_queue/` | AES-256-GCM |
| Working memory, user intent, team state | `<cwd>/.vaf/main/` | AES-256-GCM |
| Browser sessions (live site cookies, auth tokens) | `~/.vaf/browser_sessions/<scope>/` | AES-256-GCM; the agent lane stages a decrypted 0600 temp beside the store for the duration of a run (browser_use reads and auto-saves the path itself) and folds it back encrypted - the construction the audit named |
| Memory rows and chunk text | Postgres | AES-256-GCM (unchanged) |
| Mail bodies | `mail.db` | AES-256-GCM (unchanged) |
| Credentials (mail, cloud, API keys) | `<data_dir>/*.enc` | Envelope (unchanged) |
| User profile cache | `~/.vaf/user_profile_cache/` | AES-256-GCM (unchanged) |

File format: `VAFENC1:` ‖ 12-byte nonce ‖ ciphertext. A file WITHOUT that prefix
is plaintext and is read as-is for as long as the store still tolerates
plaintext (see the switch below), which is what lets chats written before this
existed keep opening.

## What is deliberately NOT encrypted

Named, not hidden - each of these is a decision with a reason:

- **Embeddings** (`memories.embedding`, `chunks.embedding`). pgvector needs
  plaintext vectors to search; the codebase's own comment calls them
  "practically invertible back to text". Treat them as equivalent to the content
  they were derived from, and rely on the disk encryption underneath.
- **The mail FTS index, and mail subject/sender/snippet columns.** An index
  contains the vocabulary it indexes; encrypting it ends mail search.
- **Memory metadata** (`memories.meta`): titles, tags, source filenames and the
  LLM-written section summaries. Filtering runs on them. This is the largest
  remaining gap and the honest name for it is: the topic map of everything you
  ever learned is readable without a key.
- **Session workspaces** under `Documents/VAF_Projects` - the generated .docx,
  the code, the uploaded images. These are deliverables; encrypting them would
  mean you could no longer open your own files without VAF. Protected by
  directory modes and whatever full-disk encryption is beneath them.
- **Log previews.** Message text no longer goes to disk in full: the whole
  system prompt is off by default (`prompt_log_full_enabled`). Short previews
  (60 characters in the queue log, tool arguments in the timeline) remain, in
  plaintext, under the data directory.

## Still plaintext, and measured

These were found by the audit and are NOT done. They are listed with their size
so the next round starts from a number rather than a memory:

| Store | Location | Holds | Why it is still open |
|---|---|---|---|
| Channel messages | `<data_dir>/channel_messages.db` | Full WhatsApp/Telegram/Discord message bodies, sender ids | Column-level AEAD plus moving one `LIKE` search into Python (`channel_message_store.py:330`) |
| Email sync (legacy lane) | `<data_dir>/email_sync.db` | Subject, sender, body snippet | Same, plus one legacy `LIKE` fallback (`email_sync_store.py:396`) |
| Speaker profiles | `~/.vaf/speaker_profiles/<scope>/` | Voice biometrics (`.npy` centroids) and enrolled names | Twelve numpy/JSON I/O sites; the arrays need a binary wrapper, not the text helper |
| The BROWSER's own profile, inside the container | docker volume `vaf-browser-profile-<hash>`, mounted at `/home/browser` | The live cookie database and any password Chromium itself saved, for the sites that per-user browser is logged into | Chromium encrypts these with `os_crypt`, and with no keyring in the container it falls back to the `basic` backend, whose key is a hardcoded constant - so it is plaintext to anyone who can read the volume. Closing it means giving the container a keyring (gnome-keyring plus libsecret, unlocked at start with a per-scope passphrase), not a change on VAF's side. NOT the same thing as `~/.vaf/browser_sessions/` in the table above: that is VAF's own store and it IS encrypted |
| Legacy gzipped chats | `~/.vaf/sessions/*.json.gz` | Whole chat transcripts written by an older release | Gzip is its own container: the sweep skips `.gz` and the reader keys on the extension. Nothing writes new ones; a pre-existing file only keeps its extension when it is rewritten |

Read the browser rows together, because they are easy to confuse: VAF's own
per-scope cookie store left this list in the banking round, while the browser's
own profile inside the container did not. The first is what a login saved by
hand or by an agent is written to; the second is what the running Chromium
works with. On the shared container the profile is wiped at every change of
hands anyway; on a dedicated per-user instance it persists, which is exactly
where the weakness lives.

Browser sessions left this list in the banking round: the store is encrypted,
the migration sweeps pre-existing plaintext files, and the agent lane uses the
decrypt-to-temp construction this table used to prescribe (see the encrypted
table above).

Each is self-contained. Apart from the legacy `.gz` chats, which no current code
path creates, none of them is on the chat path - which is why the chat path went
first.

## Optional: the switches, and the two embedder modes

`file_encryption_enabled` (default `true`) decides what NEW writes look like.
`allow_plaintext_at_rest` (default `true`) decides whether a file WITHOUT the
`VAFENC1` header is still accepted on read. Together they are three ordered
states, not one switch:

1. **Migrating** (both on): new writes are ciphertext and plaintext chats from
   before this feature keep opening.
2. **Enforced**: once a startup pass has re-written everything and found nothing
   plain left, the sweep sets `allow_plaintext_at_rest` to `false` by itself.
   From then on a file without the header is refused as a downgrade instead of
   read, because a tolerant reader that never has to present a ciphertext
   defeats the AEAD.
3. **Plaintext by choice**: turning `file_encryption_enabled` off writes
   plaintext again AND reopens the tolerant read - enforcement is a statement
   about a fully encrypted store, so it cannot outlive that state. Files already
   encrypted still open, because the key stays in the ring. An embedder picks
   per deployment - encrypt the end user's chats, or don't, because their own
   storage already does.

`cross_chat_hint_enabled`, `cli_password_gate` and `prompt_log_full_enabled`
compose with it; see [CONFIG_SCHEMA.md](../setup/CONFIG_SCHEMA.md).

## The doors

- **Web UI**: unchanged - login, JWT, 2FA.
- **Interactive terminal** (`vaf run`, the TUI, and the whole `vaf session`
  group): asks for the admin password, verified offline against the hash
  mirrored into the keyring, so a stopped database cannot lock you out.
  `vaf session export/search/list/load` prints the very chats the encryption
  protects, so gating only `vaf run` would lock the front door and leave the
  window open. `cli_password_gate` turns it off.
- **Never asked**: `vaf run -p`, the tray, the headless runner, sub-agent
  spawns, the workflow engine, automations, and any non-tty stdin. They are
  already inside the shield, and prompting there would mean an unattended
  machine stops working after a reboot.
- **No account yet**: there is nothing to verify against, so the door has
  always let you through. It now offers to create the account on the spot
  (`vaf setup`, see [FIRST_RUN.md](../setup/FIRST_RUN.md)); declining, or a
  setup that fails, still lets you through - an offer must not make a fresh
  install harder to start than it was.

## The commands

Three subcommands, all under `vaf secure`. None of them prints a secret.

### `vaf secure status`

The whole picture in one screen: where the master key is, which keys the store
holds, whether anything is still lying in config.json, whether chats are being
encrypted, whether the database still uses the shipped default password, and
whether a recovery key exists. Run it after an install, after a restore, and any
time you are not sure what state a machine is in.

```
$ vaf secure status
| Info    Master key (KEK): file, owner-only (protected by disk encryption underneath)
| Info    Key store:        /home/user/.local/share/vaf/data_keys.enc
| Info    Keys inside:      file_store_encryption_key, memory_encryption_key, ...
| Success No key material left in config.json.
| Info    Chats and files:  encrypted at rest
| Success The memory database password is not the shipped default.
| Success Recovery key set up (file: data_keys.recovery.json).
| Warning The recovery note is still on this machine: /home/user/Desktop/VAF-BackThisUp.md
          - it is a key in plain text. Move it somewhere else and delete it here.

| Info    Back up together, or the data is unrecoverable:
          /home/user/.local/share/vaf/data_keys.enc, its .key.json sibling, and
          the master key (/home/user/.vaf/secure_store.kek).
```

The last line names the master key's ACTUAL location on that machine, not both
possibilities: a backup that misses the master key is indistinguishable from no
backup at all, so sending half the readers to a file that is not there would be
worse than saying nothing.

### `vaf secure recover`

The way back after a reinstall or a disk swap. Put `data_keys.enc` and
`data_keys.recovery.json` back into the key directory, restore your data, then:

```
$ vaf secure recover
Recovery key: <paste from VAF-BackThisUp.md>
| Success Recovered. Keys available again: file_store_encryption_key, ...
```

It unwraps the data key with the recovery key and re-wraps it under the NEW
machine's key, so normal unattended operation resumes and the recovery key is
not needed again. `--key <value>` skips the prompt for scripted restores; the
value then lands in your shell history, which is why it is not the default.

### `vaf secure rotate-db`

Replaces the published default Postgres password (`vaf_dev_secret`) with a fresh
random one and writes the new DSN into the config. Deliberately a command and
not a startup step: rotating the password of a database the app is mid-connection
with, and half-succeeding, locks you out of your own memories. It verifies the
new credentials before persisting them, and refuses to change anything if the
rotation fails.

```
$ vaf secure rotate-db --yes
| Success Database password rotated and verified. Restart VAF so every worker
          picks up the new credentials.
```

**Not in the web UI.** There is no encryption panel and no key page: `vaf secure
status` is the only report, and the Security dashboard does not show ring state.
Named boundary, not an oversight - the actions those rows would invite (rotate,
re-key, disable) are the ones that destroy data when a browser tab fires them by
accident, and nothing in the product needs them mid-session.

## Using this from your own code

Everything above is the product's behaviour. For the embedding surface - the two
storage modes, user isolation, and the recovery path shown end to end - see the
"Encryption at rest" section of [EMBEDDING.md](../EMBEDDING.md) and the runnable

```
venv/bin/python examples/08_session_storage_and_encryption.py
```

which builds a throwaway home, saves chats plaintext and encrypted, proves the
words are not in the bytes, and recovers a deleted machine key.

## Migration and retention

On every start (web/tray startup event AND the CLI start path - one lane only is
how a repair silently never runs), VAF re-writes plaintext files of the five file
stores as ciphertext, tightens directory modes, prunes context archives past
`context_archive_max_age_days`, and deletes orphaned sub-agent payloads. A pass
that re-writes everything and still finds nothing plain left sets
`allow_plaintext_at_rest` to `false` (the enforced state above); a single failed
file keeps the store tolerant rather than locking a record out. It also
REPORTS, without touching, files that hold readable copies and that no VAF code
created: a database dump under `~/.vaf/vm-backups/`, and old config backups.

**Downgrade:** `~/.vaf/config.json.pre-keyring.bak` is written once, before the
first key is removed from config.json. Restoring it puts the keys back where an
older release looks for them.

## Owner-only modes on the stores that are not encrypted

Encryption is one half of at-rest protection and file modes are the other, and the second
half applies whether or not the first is on. `at_rest_migration.run_once` hardens six
stores that hold user-authored content or a record of when somebody was active, and that
are NOT in the encrypting table: `automations`, `automation_planner`, `reminders`, `logs`,
`user_profile_cache` and `thinking_workspace`. Directories become 0700 and files 0600, on
POSIX; on Windows this is the documented no-op.

They are hardened and not encrypted for a reason worth stating rather than leaving to be
inferred: their writers use plain `json.load` and `json.dump` instead of the `data_files`
seam, so encrypting them would lock their own loaders out - an automation the scheduler
could no longer read is a feature that breaks silently on the next start. Bringing them
under encryption means converting those writers first, which is its own change.

Measured before this existed: `~/.vaf/sessions` was 0700 with 0600 files while
`~/.vaf/automations` beside it was 0755 with 0644 files. An automation's prompt is
user-authored natural language, the same content class as a chat message, so a second
local account could read what the first had asked its agent to do, and when.

