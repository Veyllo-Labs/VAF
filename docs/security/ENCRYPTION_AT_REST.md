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

**Out-of-profile files are the Windows gap that remains.** The per-chat working
memory lives under the directory VAF was started from (`<cwd>/.vaf/main`), and
when that is outside your profile (`C:\VAF`, `D:\VAF`) its ACL does not cover it:
other local accounts can read it there. Its files are encrypted like the chats,
so what they see is ciphertext and file names. The compose password file used to
be the second such path, `<checkout>/.env`; it is `~/.vaf/compose.env` now, inside
the profile, and an older `.env` loses its `REDIS_PASSWORD` line at the next stack
start (`compose_env_file` in `vaf/core/service_stack.py`).

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
        +-- browser_vnc_secret           the password of the browser's live view (KasmVNC),
        |                                handed to compose through ~/.vaf/compose.env
        +-- admin_password_hash          Argon2 hash, so the terminal can verify offline
        +-- account_seal_key             seals the account rows in Postgres, so a row changed
        |                                in the database alone grants nothing
        |                                (vaf/auth/account_seal.py)
        +-- account_seal_adopted         the existing accounts were sealed once
        +-- admin_established            this installation has had an admin (the web
        |                                setup stays closed)
        +-- channel_<name>_<field>       a messaging channel's login (the Telegram and
        |                                Discord bot tokens), vaf/core/channel_secrets.py
        +-- a2a_signing_root_key         every room's signing key is derived from it and
        |                                never stored (vaf/core/a2a/signing.py)
        +-- mail_case_root_key           signs the case stamps on mail the agent sends
        |                                (vaf/mail/case_token.py)
        +-- backup_identity              this machine's backup key: every backup is
        |                                encrypted to it (vaf/core/backup.py)
        +-- backup_identities_previous   the backup keys this machine held before a restore
        |                                replaced its keyring, so their backups still open
        +-- mcp_server.<name>.env        a local MCP server's environment (its API keys)
        +-- mcp_server.<name>.token      a remote MCP server's access token,
        |                                vaf/core/mcp_secrets.py
        +-- mcp_server.<name>.oauth.<account>
        |                                one account's sign-in at a remote MCP server
        |                                (tokens, expiry, the client it registered),
        |                                vaf/core/mcp_oauth.py
        +-- mcp_server.<name>.oauth_client_secret
                                         the secret of a client registered by hand
```

`mcp_servers.json` keeps only the NAMES of a server's env variables. A value written there
by hand (a documented way to add a server) moves into the ring the next time the file is
loaded, and so do a `token`, an `Authorization: Bearer` header or an `oauth_client_secret`; the
Settings page receives the names and whether a token or a client secret is stored, never a
value. `vaf secure status` names a server
whose secret is still in the file.

`~/.vaf/config.json` holds **no key material and no messenger login**. It used to
hold all of it, which made every "encrypted" store equivalent to chmod protection. The
bot tokens were the last to leave: they are a field inside a channel's block
(`telegram_config.bot_token`) rather than a key of their own, so the top-level move
missed them, and the config API sent that block, token included, to every admin's
browser. A token found in config.json (pasted there by hand, which stays a valid way to
set a bot up, or left by an older release) moves into the ring on the next read. No
pre-keyring backup is written for it: that backup is a plaintext copy of config.json,
and a bot token can be re-issued by the platform, so a downgrade costs one re-entry of
the token rather than a second plaintext copy of it.

Check any installation with:

```
vaf secure status
```

It prints locations, never values, and names anything still left in config.json.

### The recovery key

When the keyring is created, VAF writes **`VAF-BackThisUp.md` to your Desktop**
(the key directory if there is no Desktop). It contains a recovery key: an `age`
identity (`AGE-SECRET-KEY-1...`, the X25519 key of the published age file
format, with a bech32 checksum, so a mistyped character is reported instead of
taken for the wrong file). Beside the keyring, `data_keys.recovery.json` is
encrypted TO that key (`vaf/core/recovery_kit.py`, format tag
`recovery-2-3d8a51`) and holds two keys: the keyring's data key AND the machine
key (KEK) under which every other encrypted store is sealed - the provider API
keys, the credentials stored for commands, and the mail, cloud and GitHub sign-ins
where they are kept in a file (a machine without an OS keyring, such as a server).
Sign-ins kept in the OS keyring are protected by your login instead and are not
in the kit: on a new machine they are entered again. The key store keeps only the
PUBLIC half of the identity; the private one is written into the note alone, which
sits on this machine until you move it off and delete it here.

Both keys, because measured: the first kit wrapped the data key alone (scrypt
over a base64 secret), so a recovery brought the chats and memories back and lost
every API key, mail password and stored credential with the old machine. Such a
version 1 kit keeps working for what it covers; `vaf secure status` and
`vaf security doctor` (`recovery_key_partial`) say what it does not, and
`vaf secure renew-recovery-key` replaces it (a person at the terminal, the admin
password, never from the agent's own commands; the old key stops opening the
key store). Backups written before the renewal are encrypted to the old key and
this machine, so only those open them: run `vaf backup create` afterwards for a
backup the new key opens. If the new key cannot be stored, the old note is put
back and the old key keeps working. Stores sealed under `VAF_MASTER_PASSPHRASE` instead of a machine key
open with that passphrase on any machine; the kit does not replace it.

One encoding, deliberately. An earlier draft also printed 24 words from a
home-made 64-word list and called them 256 bits - six bits per word is 144, there
was no checksum, and two encodings of one secret double the transcription and
leak surface.

With the recovery key plus the key directory (`data_keys.enc`,
`data_keys.recovery.json`, and every other store's `*.enc` together with its
`*.key.json`, which holds that store's key) you can open your data on a new
machine - no OS keyring, no old computer, no password:

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
recovery key plus the key directory, which re-wraps the data key and every store
the old machine key sealed under the new machine key. Lose the backup AND the recovery key and the data is gone -
no vendor can help, which is the honest price of local encryption.

One consequence of a whole-home backup: on macOS, Time Machine's default job
includes `$HOME`, and so does any `rsync -a ~` on Linux, so those backups carry
the key directory alongside the ciphertext. The backup is then only as protected
as the medium it lands on. That is a reason to encrypt the backup disk, not a
reason to skip the backup - losing the keys is the unrecoverable direction.

## What is encrypted

| Store | Location | State |
|---|---|---|
| Chat sessions, and the archived ones | `~/.vaf/sessions/*.json`, `~/.vaf/sessions/archive/<scope>/*.json` | AES-256-GCM |
| Context archives (pre-compression snapshots) | `~/.vaf/context_archive/` | AES-256-GCM |
| Handoff bundles | `~/.vaf/handoff_bundles/<scope>/` | AES-256-GCM |
| Sub-agent queue and task payloads | `~/.vaf/subagent_queue/` | AES-256-GCM |
| Working memory, user intent, team state | `<cwd>/.vaf/main/` | AES-256-GCM |
| Browser sessions (live site cookies, auth tokens) | `~/.vaf/browser_sessions/<scope>/` | AES-256-GCM; the agent lane stages a decrypted 0600 temp beside the store for the duration of a run (browser_use reads and auto-saves the path itself) and folds it back encrypted - the construction the audit named |
| Memory rows and chunk text | Postgres | AES-256-GCM (unchanged) |
| Mail bodies | `mail.db` | AES-256-GCM (unchanged) |
| Credentials | Provider API keys (`provider_keys.enc`) and a person's credentials for commands (`user_secrets.enc`): always `<data_dir>/*.enc`. Mail, cloud and GitHub sign-ins: the OS keyring where one is reachable, otherwise `email_credentials.enc`, `cloud_credentials.enc`, `github_credentials.enc` in `<data_dir>` | Envelope (unchanged) for the files; the OS keyring encrypts under your login |
| User profile cache | `~/.vaf/user_profile_cache/` | AES-256-GCM (unchanged) |
| Word suggestions learned from what a person types | `~/.vaf/autosuggest.json` (machine owner), `~/.vaf/autosuggest/<scope>.json` | AES-256-GCM; a plaintext corpus from before opens and is encrypted on its next write |
| Agent rooms (A2A): the room, its log, members, read positions, invitations | `~/.vaf/a2a/rooms/<room>/` | AES-256-GCM |
| An account's SSH key (`vaf/core/ssh.py`) | `~/.vaf/ssh/<account>/id_ed25519` | OpenSSH's own key encryption (bcrypt KDF, 64 rounds); the passphrase is in `user_secrets.enc` (envelope) under the namespace `ssh`, and reaches OpenSSH only through its prompt, never a file or a command line |

File format: `VAFENC1:` ‖ 12-byte nonce ‖ ciphertext. A file WITHOUT that prefix
is plaintext and is read as-is for as long as the store still tolerates
plaintext (see the switch below), which is what lets chats written before this
existed keep opening.

## What is deliberately NOT encrypted

Named, not hidden - each of these is a decision with a reason:

- **An account's SSH `known_hosts`** (`~/.vaf/ssh/<account>/known_hosts`, 0600 in a
  0700 folder). OpenSSH reads it as a file on every connection, and it holds server names
  and their PUBLIC keys, no secret. Its public key (`id_ed25519.pub`) is plaintext for the
  same reason: it is meant to be handed out.

- **An account's confirmed FTP servers** (`~/.vaf/ftp/<account>/servers.json`, 0600 in a
  0700 folder). Server names, how each certificate is trusted, and a certificate's SHA-256
  fingerprint where it is pinned: no secret. The FTP password is not in it; it is a
  credential in `user_secrets.enc`, used by NAME.

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
  plaintext, under the data directory. A credential in a tool's arguments is
  masked before it is written (`secret_args`, and any key named like one), and a
  credential the person hands over in a chat is replaced in the debug logs of that
  day and the day before once the agent stored it (`forget_secrets.scrub_recent_logs`).

## Still plaintext, and measured

These were found by the audit and are NOT done. They are listed with their size
so the next round starts from a number rather than a memory:

| Store | Location | Holds | Why it is still open |
|---|---|---|---|
| Channel messages | `<data_dir>/channel_messages.db` (the local admin), `<data_dir>/scopes/<scope>/channel_messages.db` per account, `<data_dir>/users/<name>/channel_messages.db` (legacy per-name lane) | Full WhatsApp/Telegram/Discord message bodies, sender ids | Column-level AEAD plus moving one `LIKE` search into Python (`channel_message_store.py:330`) |
| Email sync (legacy lane) | `<data_dir>/email_sync.db`, and the same per account under `scopes/<scope>/` and `users/<name>/` | Subject, sender, body snippet | Same, plus one legacy `LIKE` fallback (`email_sync_store.py:396`) |
| Calendar | `<data_dir>/scopes/<scope>/calendar.db` | Event titles, descriptions, locations, contact links, the per-account sync state | Owner-only file modes like the other scope databases (`calendar_store.py`); a blob key would join `mail_store_encryption_key` |
| Speaker profiles | `~/.vaf/speaker_profiles/<scope>/` | Voice biometrics (`.npy` centroids) and enrolled names | Twelve numpy/JSON I/O sites; the arrays need a binary wrapper, not the text helper |
| The BROWSER's own profile, inside the container | docker volume `vaf-browser-profile-<hash>`, mounted at `/home/browser` | The live cookie database and any password Chromium itself saved, for the sites that per-user browser is logged into | Chromium encrypts these with `os_crypt`, and with no keyring in the container it falls back to the `basic` backend, whose key is a hardcoded constant - so it is plaintext to anyone who can read the volume. Closing it means giving the container a keyring (gnome-keyring plus libsecret, unlocked at start with a per-scope passphrase), not a change on VAF's side. NOT the same thing as `~/.vaf/browser_sessions/` in the table above: that is VAF's own store and it IS encrypted |
| The WhatsApp login | `~/.vaf/users/<name>/whatsapp/` | Baileys' Signal-protocol state, one file per key: `creds.json` with the identity key, pre-keys, sessions, sender keys, app-state keys | Owner-only file modes, not encryption: the bridge starts Node with umask 077 and `whatsapp_auth.harden_auth_dir` fixes older files (at every Node start, at the start of a QR link, and at VAF startup for every linked account, switched on or not) (measured before: the directory 0755 and 379 of 381 files 0644). Encrypting them means giving `wa-bridge.js` an auth-state store of its own instead of Baileys' `useMultiFileAuthState`, which reads and writes these files directly |
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
- **Interactive terminal** (`vaf run`, the TUI, and the whole `vaf session`,
  `inbox`, `outbox`, `secrets`, `ssh`, `ftp` and `env` groups, plus
  `vaf users list`, `sessions` and `sign-out`): asks for the admin password, verified offline against the
  hash mirrored into the keyring, so a stopped database cannot lock you out.
  `vaf session export/search/list/load` prints the very chats the encryption
  protects, so gating only `vaf run` would lock the front door and leave the
  window open. `cli_password_gate` turns it off.
- **Never asked**: `vaf run -p`, the tray, the headless runner, sub-agent
  spawns, the workflow engine, automations, and any non-tty stdin. They are
  already inside the shield, and prompting there would mean an unattended
  machine stops working after a reboot.
- **Refused: the agent's own commands.** A command VAF starts for the agent
  (`host_bash`, `python_exec`, a background command) has no terminal either,
  and "no terminal" let it through, so any account's agent could run
  `vaf session export` or `vaf outbox send` as the machine owner. Each such
  command now carries a mark in its environment (`agent_env` in
  `vaf/core/processes.py`), and the door, and `vaf users reset-password` and `token`,
  refuse it before anything else, whatever `cli_password_gate` or
  `VAF_SKIP_PASSWORD_GATE` say: the agent is never the person at the terminal,
  and it has tools of its own that know whose turn it is. NAMED BOUNDARY: a
  command that deliberately clears its environment is not stopped by the mark;
  what an account with `host_bash` can do on this computer is an account
  permission ([SANDBOXING.md](SANDBOXING.md)).
- **Not behind the door, deliberately**: `vaf security`, `secure`, `server`,
  `memory`, `automation` and the rest of the groups not listed above. A door
  in front of them would not take anything away from the one caller it is
  about, the agent's shell: an account that runs a command there has
  `host_bash`, which reaches the same files, the same database and `crontab`
  directly. What decides that is who gets `host_bash`, not which group asks.
  `vaf a2a` must stay open in any case: it is the door foreign agents walk
  through ([A2A_PROTOCOL.md](../agents/A2A_PROTOCOL.md)).
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
| Success Recovery key set up (file: data_keys.recovery.json); it opens the key
          store and every password store.
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

The way back after a reinstall or a disk swap. Put the key directory back
(`data_keys.enc`, `data_keys.recovery.json`, and every other store's `*.enc`
with its `*.key.json`), restore your data, then:

```
$ vaf secure recover
Recovery key: <paste from VAF-BackThisUp.md>
| Success Recovered. Keys available again: file_store_encryption_key, ...
```

It unwraps the data key and the old machine key with the recovery key, re-wraps
the data key under the NEW machine's key, and then re-wraps every store in the
key directory that the old machine key sealed and the new one does not open
(`rewrap_from_kek` in `vaf/core/secure_store.py`; it names them), so normal
unattended operation resumes and the recovery key is not needed again. Last, the
recovery wrap gets the NEW machine key in place of the old one, which now opens
nothing (`refresh_recovery_wrap` in `vaf/core/recovery_kit.py`). It is encrypted
to the recovery key's public half, so this needs no secret and the same key
keeps working: without it, the next machine loss would bring back the key store
and skip every password store. If it fails, the command says so and
`vaf secure renew-recovery-key` writes a new key. A version 1 key recovers the key store and says that the password stores stay
sealed. `--key <value>` skips the prompt for scripted restores; the
value then lands in your shell history, which is why it is not the default.

### `vaf secure rotate-db`

Replaces the published default Postgres password (`vaf_dev_secret`) with a fresh
random one and writes the new DSN into the config. Deliberately a command and
not a startup step: rotating the password of a database the app is mid-connection
with, and half-succeeding, locks you out of your own memories. It verifies the
new credentials before persisting them, and refuses to change anything if the
rotation fails. If the role's password was changed but the new connection does not
verify, nothing is saved and the error names the new connection string, the only
copy of the new password, so the operator can get back in. On a server it is a provisioning step as well: `vaf server
provision` runs the same rotation (`vaf/core/db_credentials.py`, which also holds
the one check for the published passwords that `vaf secure status` and the stack
start's warning read) whenever either role still has its published password.

The two halves of a rotation live in different places and only work together: the
role's password is in the database volume (`vaf_memory_pgdata`), the connection string
that carries it is in `config.json`. A backup made with `vaf backup` takes both (the
roles with their password hashes, and the config). Anything that replaces only one of
them locks VAF out of its memories: an older `config.json` put back by hand, or a
database volume recreated with `docker compose down -v`, which starts again with the
published passwords. The way back from a recreated volume is in the "Reset Database"
section of [MEMORY_SYSTEM.md](../memory/MEMORY_SYSTEM.md).

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
how a repair silently never runs), VAF re-writes plaintext files of the encrypted
file stores as ciphertext (the files matched by the `sealed` globs of the one list of
stores, `vaf/core/data_stores.py`, which the check for existing ciphertext before a
key is minted reads as well; the credential files and the profile cache have writers
of their own and are not part of it), tightens directory modes, prunes context
archives past
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
half applies whether or not the first is on. `at_rest_migration.run_once` hardens the
stores that hold user-authored content or a record of when somebody was active, and whose
files the sweep does not encrypt (the `harden` column of `vaf/core/data_stores.py`):
`~/.vaf/automations`, `~/.vaf/automation_planner`, `~/.vaf/reminders`, `~/.vaf/logs`,
`<data_dir>/logs`, the thinking workspaces in `<data_dir>/workspaces`, and
`~/.vaf/user_profile_cache` (under `VAF_CONFIG_DIR` in Docker mode; its files are encrypted
by their own writer, with the memory key). Directories become 0700 and files 0600, on
POSIX; on Windows this is the documented no-op.

The rest are hardened and not encrypted for a reason worth stating rather than leaving to be
inferred: their writers use plain `json.load` and `json.dump` instead of the `data_files`
seam, so encrypting them would lock their own loaders out - an automation the scheduler
could no longer read is a feature that breaks silently on the next start. Bringing them
under encryption means converting those writers first, which is its own change.

Measured before this existed: `~/.vaf/sessions` was 0700 with 0600 files while
`~/.vaf/automations` beside it was 0755 with 0644 files. An automation's prompt is
user-authored natural language, the same content class as a chat message, so a second
local account could read what the first had asked its agent to do, and when.

