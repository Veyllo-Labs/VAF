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

| Situation | Protected? |
|---|---|
| Disk removed, or the machine powered off and stolen | **Depends on where the master key is.** With the default file backend, only as far as the disk encryption underneath reaches (`/home` on LUKS, BitLocker, FileVault). With `secure_store_kek_backend = "keyring"` the key is protected by your login password and the disk alone is useless - but that mode needs VAF to start from your desktop session. |
| A backup, a copied directory, a support archive, cloud sync | **Yes** - the files are ciphertext and the key is not among them. |
| Another local account on the same machine | **Yes** - directory modes 0700, file modes 0600, and the key is not readable by them. |
| The machine is running and someone has your session | **No.** The shield is open from the inside; that is what makes unattended operation possible. |
| Code running AS you (a hostile skill, a compromised dependency) | **No.** Same reason. |
| Someone who knows the admin password | **No** for the terminal and the web UI; that is authentication, not encryption. |

## Where the keys live

```
~/.vaf/secure_store.kek  (mode 0600, the default)   <- protected by disk encryption
   (opt-in: OS keyring entry vaf / secure_store_kek, protected by your LOGIN password;
    only for installs that start VAF inside the desktop session - a tray started by a
    supervisor script cannot reach it, and a key it cannot read is a lockout)
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

`data_keys.enc`, `data_keys.key.json` and the master key (the OS keyring entry or
`~/.vaf/secure_store.kek`). **Any one of them missing means the data is gone** -
there is no recovery path and no vendor who can help, which is the honest price
of local encryption.

## What is encrypted

| Store | Location | State |
|---|---|---|
| Chat sessions | `~/.vaf/sessions/*.json` | AES-256-GCM |
| Context archives (pre-compression snapshots) | `~/.vaf/context_archive/` | AES-256-GCM |
| Handoff bundles | `~/.vaf/handoff_bundles/<scope>/` | AES-256-GCM |
| Sub-agent queue and task payloads | `~/.vaf/subagent_queue/` | AES-256-GCM |
| Working memory, user intent, team state | `<cwd>/.vaf/main/` | AES-256-GCM |
| Memory rows and chunk text | Postgres | AES-256-GCM (unchanged) |
| Mail bodies | `mail.db` | AES-256-GCM (unchanged) |
| Credentials (mail, cloud, API keys) | `<data_dir>/*.enc` | Envelope (unchanged) |
| User profile cache | `~/.vaf/user_profile_cache/` | AES-256-GCM (unchanged) |

File format: `VAFENC1:` ‖ 12-byte nonce ‖ ciphertext. A file WITHOUT that prefix
is plaintext and is read as-is, which is what lets chats written before this
existed keep opening forever.

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
| Browser sessions | `~/.vaf/browser_sessions/<scope>/` | Live site cookies and auth tokens | Playwright is handed a PATH and reads the file itself, so it needs decrypt-to-temp and re-encrypt around the run |

Each is self-contained. None of them is on the chat path, which is why the chat
path went first.

## Optional: the switch, and the two embedder modes

`file_encryption_enabled` (default `true`) decides what NEW writes look like.
**Reading never depends on it.** That gives three properties:

1. plaintext chats from before this feature keep opening, forever;
2. turning it off writes plaintext again, and files already encrypted still open;
3. an embedder picks per deployment - encrypt the end user's chats, or don't,
   because their own storage already does.

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
`context_archive_max_age_days`, and deletes orphaned sub-agent payloads. It also
REPORTS, without touching, files that hold readable copies and that no VAF code
created: a database dump under `~/.vaf/vm-backups/`, and old config backups.

**Downgrade:** `~/.vaf/config.json.pre-keyring.bak` is written once, before the
first key is removed from config.json. Restoring it puts the keys back where an
older release looks for them.
