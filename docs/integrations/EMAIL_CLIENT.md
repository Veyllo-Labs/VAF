# Email Client Architecture

Design doc for VAF's email subsystem. Read this BEFORE changing any mail-related
file: the engine package `vaf/mail/` (`store.py`, `sync.py`, `parser.py`,
`imap_client.py`, `service.py`, `sender.py`, `compose.py`, `writeback.py`,
`supervisor.py`, `tool_bridge.py`, `migrate.py`, `addressing.py`), the routes
`vaf/api/mail_routes.py` and `vaf/api/email_routes.py` (the shared OAuth + accounts
hub), `vaf/core/email_accounts.py` (route-independent account-config SSOT),
`vaf/core/oauth_pkce.py`, `vaf/core/credential_store.py`, the agent tools
(`vaf/tools/mail_*.py`, `send_mail.py`, `read_mail.py`, `find_mail.py`,
`label_mail.py`, `mark_mail_answered.py`, `list_email_accounts.py`,
`reply_mail.py`, `manage_mail.py`), and the web surfaces
`web/app/mail/page.tsx` (three-pane `MailClientView`),
`web/components/connections/MailClient.tsx` (window modal) and
`MailAccounts.tsx` (in-client account panel).

Related docs: [CONNECTIONS.md](CONNECTIONS.md) (connection/channel model,
email account setup), [CONFIG_SCHEMA.md](../setup/CONFIG_SCHEMA.md) (email keys),
[USER_ISOLATION.md](../security/USER_ISOLATION.md) (scoping rules).

## What this is

ONE mail client. `vaf/mail/` is a real IMAP engine - per-scope local store,
incremental UID sync, push via IDLE, offline bodies, threads, full-text search,
HTML rendering, a durable write queue and native SMTP submission - and it is the
only mail lane there is. The earlier integration (a header-poll viewer over three
provider dialects, with its own store, routes and UI) was removed; so was the
`mail_engine_v2_enabled` flag that used to switch between them. There is nothing
left to toggle and no second implementation to keep in sync.

`mail_engine_write_enabled` is the one remaining switch, and it is NOT a rollout
flag: it gates writes back to the MAIL SERVER (flag/move/append replay). It
defaults to off, so the engine stays read-only against mailboxes until an admin
enables it. Local-first state (read marks, stars, archive, categories) always
applies immediately; sending is never gated by it, because a queued mail must be
able to leave.

Decisions that shaped this (owner-approved): the engine is IMAP-uniform (the
Gmail-API/Graph transports are gone); email stays OUT of the messaging channel
model (`KNOWN_CHANNELS`), so `send_to_user` cannot deliver via email; there is no
new-mail automation trigger yet, but the sync engine emits internal new-mail events
so one can be added; message bodies are encrypted at rest via the secure_store DEK;
body-cache retention defaults to 12 months with headers kept indefinitely.

## Not built yet

Deliberately deferred, listed so nobody looks for them in the code:

- WebSocket delta events. The client polls every 60 s instead; the per-account
  `state` counter and the `email_state`/`email_delta` event shapes are designed
  (see "API and Web UI") but not wired.
- Gmail X-GM-MSGID cross-folder dedup - until it lands, `[Gmail]/All Mail` stays
  in the lazy sync tier.
- Server-synced drafts (compose is local until sent).
- Per-sender persistence of the remote-image opt-in (today it is per view).
- Mobile pass per [MOBILE_UI.md](../web-ui/MOBILE_UI.md).
- Body-aware phishing scoring: the scorer still sees only subject, snippet and
  sender, even though bodies are local now.
- Attachment listing/reading agent tools.
- Mailboxes where IMAP is administratively or contractually impossible (Microsoft
  365 F3/Kiosk licences, tenants with IMAP disabled) are NOT supported. The engine
  is IMAP-uniform by decision; such an account cannot be served.

## Architecture

### Engine (`vaf/mail/`)

- Store: one SQLite DB per user scope (`data_dir/scopes/<uuid>/mail.db`, WAL).
  Tables: `accounts`, `folders` (uidvalidity, uidnext, highestmodseq,
  special_use), `messages` (DB-assigned primary key; IMAP coordinates
  account/folder/UIDVALIDITY/UID stored as mutable server pointers, never as
  identity; flags + `server_flags` shadow copy for conflict diffs; References/
  In-Reply-To; thread id), `message_raw` (zstd-compressed RFC 822 blob up to
  ~256 KB, encrypted at rest; larger bodies fetched on demand via partial
  FETCH), `attachments` (metadata; payloads as files under the scope dir),
  `threads`, `ops` (durable operation queue), FTS5 external-content index
  (unicode61, remove_diacritics 2) populated in the same transaction as
  ingest, plus a `schema_version` table (lazy migrate-on-open). Invariant:
  every derived table (threads, FTS, counters) is rebuildable from raw
  messages + the server; reindex is a cheap command.
- Sync: RFC 4549 baseline (cache keyed on mailbox+UIDVALIDITY+UID;
  UIDVALIDITY change wipes the folder cache; new mail via
  `UID FETCH lastseen+1:*`; flag/expunge resync via windowed FLAGS fetches in
  ~100-UID batches). CONDSTORE/QRESYNC only as capability-gated accelerators
  (Office365, Yahoo, GMX/web.de, T-Online lack them; iCloud's QRESYNC is
  buggy - use defensively). Gmail is modeled natively via X-GM-EXT-1
  (X-GM-MSGID, X-GM-THRID threads, X-GM-LABELS, X-GM-RAW search);
  Archive = remove INBOX label, Delete = MOVE to `[Gmail]/Trash`, never relying
  on the account's auto-expunge setting. MOVE with a COPY+EXPUNGE fallback that
  is UIDPLUS-gated (without UIDPLUS the op is parked rather than risking a plain
  EXPUNGE); SPECIAL-USE folder discovery with a localized well-known-name
  fallback table. New mail: one IDLE connection pinned to INBOX (re-issued
  ~every 25 min; a dead socket means resync now; periodic NOOP safety net for
  the Office365 IDLE bug) plus a periodic sweep of the other folders. Folder
  tiering: INBOX eager (headers+bodies), Sent/Drafts/Archive headers eager with
  bodies lazy, every other folder ON OPEN - the client issues a one-time
  `POST /api/mail/sync/{account}?folder=<name>` when an opened folder comes back
  empty.
- Workers: a MailSyncSupervisor asyncio task in the web backend; one
  crash-isolated worker per account (synchronous IMAPClient driven via
  `asyncio.to_thread`), restartable individually so one broken account never
  stalls the others. It is the ONLY sync lane. CLI-only mode runs no workers
  (on-demand sync remains). Account selection: the sweep and the IDLE watchers
  poll only accounts passing `_wants_sync` - `enabled` AND `mail_enabled` (a
  calendar-safe mail delete clears it, and re-syncing would resurrect the
  messages the delete just purged) AND `auto_sync_enabled` (the per-account
  panel toggle; ignoring it would make switching auto-sync OFF *raise* the
  polling rate). The send drain deliberately runs over the wider `enabled`-only
  set, so a queued mail still leaves an account whose mailbox is no longer
  polled. Writes go through the durable op queue: UI/tools write the local DB
  immediately and enqueue idempotent operations (flag, move, append, delete)
  replayed against the server using the `server_flags` diff, with an atomic
  claim so two executors can never deliver the same send twice.
- Native send (`vaf/mail/sender.py`): one delivery core for every account.
  Dispatch by `(provider, imap_ready)`: `imap` -> SMTP password;
  `gmail`/`microsoft` AND `imap_ready` -> SMTP SASL XOAUTH2 (Gmail union token,
  Microsoft `microsoft_imap` token - the same lanes the IMAP client uses). An
  OAuth account that is not `imap_ready` has NO delivery lane and `send()`
  refuses PERMANENTLY with an error naming the fix (reconnect the account),
  logging a distinct NO_DELIVERY_LANE line. Refusing beats falling through to
  SMTP, which would attempt XOAUTH2 with a token that has no mail scope and turn
  a clear instruction into an opaque auth error; and 'permanent' beats
  'transient', which the outbox would retry five times and then park in silence.
  Delivery uses stdlib `smtplib` (every caller is synchronous - agent tool run or
  OpExecutor drain via `asyncio.to_thread` - so no event-loop bridge is needed and
  the SMTP conversation is driven step by step for honest hand-off
  classification). A `handed_off` flag flips at the DATA command: a failure before
  it is transient/permanent, at or after it is ambiguous (parked, never re-sent).
  The stored Sent bytes are sent verbatim, so the delivered Message-ID is
  byte-identical to the local copy and replies join the same thread. Bcc is
  stripped from the delivered wire and rides the SMTP envelope only;
  `normalize_recipients` lives in `vaf/mail/addressing.py`. All four senders
  (send_mail/reply_mail/forward_mail tools + `writeback._op_send`) route here.
- Libraries: IMAPClient (BSD-3) as the IMAP driver, stdlib `smtplib` for SMTP
  submission, stdlib `email` with `policy.default` for parsing (per-message error
  boundary: a malformed message must never abort a folder sync), nh3 (MIT) for
  HTML sanitization, zstandard for blob compression. aioimaplib is rejected
  (GPL-3.0). bleach is EOL - never adopt it.
- Auth lanes: (1) password / app password (GMX, web.de, T-Online, iCloud, Yahoo,
  consumer Gmail) with provider presets and help texts; (2) Google OAuth-IMAP via
  XOAUTH2 (`https://mail.google.com/`, a restricted scope) - the mail connect
  requests it up front as a UNION that still contains calendar, so ONE consent
  yields a working account and the shared token keeps Calendar alive;
  (3) Microsoft OAuth-IMAP via XOAUTH2
  (`https://outlook.office.com/IMAP.AccessAsUser.All` + `SMTP.Send` +
  `offline_access`; basic auth is retired). Microsoft needs TWO consents by
  construction: its IMAP/SMTP tokens live on the outlook.office.com resource and
  cannot be combined with Graph scopes in one token, and `calendar_client` reads
  the Graph record. VAF ships a Google client id; Microsoft requires an
  admin-configured one, and the account panel disables the button when it is
  missing (`GET /api/email/oauth-status`).

### API and Web UI

- `/api/mail/*` serves all mail data: status, folders, threads, messages, bodies,
  attachments, search, compose/send with undo, the op queue, the image proxy and
  account CRUD. Object vocabulary follows JMAP (RFC 8621) naming for
  Mailbox/Thread/Email shapes, but VAF does NOT implement the JMAP wire protocol
  (deliberate no-overengineering decision).
- `/api/email/*` is the shared OAuth + accounts hub ONLY: oauth start/callback/
  status plus account CRUD/test/verify. It is not mail-specific - the Calendar
  wizard mints its consent through the same `/oauth/start` (calendar_routes has no
  OAuth endpoint of its own) and the Connections tile and Calendar dashboard read
  `/accounts`, which is why the module outlived the mail teardown. The callback
  path is registered verbatim at Google and Azure, so renaming it by one character
  breaks sign-in for mail, calendar and cloud at once;
  `tests/test_email_routes_surface.py` pins the surviving path set as data.
- HTML rendering: sanitized server-side with nh3 at the trust boundary (scripts,
  handlers and dangerous URL schemes stripped, `cid:` parts resolved from the
  store), rendered into a sandboxed `srcdoc` iframe with CSP `script-src 'none'`.
  Remote images are blocked by default; an explicit opt-in loads them through the
  SSRF-guarded, image-only `/api/mail/image-proxy` (resolve once, pin the IP,
  ports 80/443 only). Blocked hosts and high-risk sends are logged to the security
  event log.
- WebSocket deltas (designed, not wired - see "Not built yet"): a per-account
  monotonic `state` counter with `email_state` + `email_delta` events, a client
  that detects a gap resyncing via REST, events scoped at the EMIT site
  (`push_update_to_user`) and never broadcast. Every new event field must be
  forwarded explicitly in `web/app/page.tsx` (known dropped-field bug class) and
  covered by a CI guard.
- UI: the mail client is an in-app WINDOW MODAL (`MailClient.tsx`, same 95vw x
  90vh chrome as the other connection dashboards) opened from the Connections
  "Email" tile - NOT a standalone full-screen route; the `/mail` URL is only a
  direct-access fallback rendering the same `MailClientView`. Three-pane layout:
  folder sidebar (special-use folders with unread/total counts, collapsible
  labels), conversation list, reader. The gear opens the in-client account panel
  (`MailAccounts.tsx`), which owns the whole account surface: list with
  IMAP-ready state, add an IMAP account (test then save, with host/port overrides
  for IMAP *and* SMTP), connect or reconnect Gmail/Microsoft through the shared
  OAuth hub, verify, label, auto-sync toggle, calendar-safe remove. Desktop and
  mobile per [WEB_UI.md](../web-ui/WEB_UI.md) /
  [MOBILE_UI.md](../web-ui/MOBILE_UI.md).

### Agent tools contract

Eleven tools: `mail_inbox`, `read_mail`, `find_mail`, `send_mail`, `reply_mail`,
`forward_mail`, `archive_mail`, `delete_mail`, `label_mail`,
`mark_mail_answered`, `list_email_accounts`. They keep the row/body output shapes
the earlier tools had and read the engine store internally. The destructive verbs
are deliberately NOT on the front-office allow-list. `delete_mail` is trash-only:
it MOVEs to the trash folder and never expunges.

The agent stamps `username` + `user_scope_id` into tool kwargs at dispatch
(`agent.py`) and the workflow engine does the same (`workflows/engine.py`); tools
never trust model-provided identity. Whether a caller may be served at all is
decided in ONE place, `mail_utils.mail_v2_active(store_username, user_scope_id)`:
the store is scope-keyed with no username dimension, so a legacy per-username
caller (username set, NO scope) has no store of its own and serving it would
resolve through the local admin scope and hand that caller the ADMIN's mailbox,
reads and writes. That caller is refused, matching the two layers that already
refuse it (`email_sync_store`'s `_legacy_user` branch and
`MailSyncSupervisor._collect_accounts`, which never syncs `email_config_by_user`
accounts). Such an install heals itself: the user reconnects the account once and
lands on a scope-keyed config. Guards: `tests/test_mail_tools_import_guard.py`
(no tool imports the FastAPI route module) and `tests/test_mail_tools_v2.py`.

`vaf/core/email_sync_store.py` and `vaf/mail/tool_bridge.py` survive on purpose:
`label_mail` and `mark_mail_answered` reach the engine store through the former's
dual-write, and the latter's merge keeps an account the engine does not sync
readable rather than blanking its mailbox.

### Fail-closed scoping

Every service call takes an explicit `user_scope_id`; `MailService` raises without
one and there is no silent fallback to the local admin. Account config resolves
`email_config_by_scope[user_scope_id]` first and never falls back across scopes;
`email_config` is the local admin's blob and `email_config_by_user` is a legacy
layout that is never served from the engine store. Deletion is explicit: removing
a mail account deletes its rows, blobs, FTS entries, queued ops and mail
credentials - but a shared gmail/microsoft OAuth token is NEVER revoked, because
Calendar resolves accounts by that provider; the entry survives with
`mail_enabled=False` so it disappears from the mail list while the calendar keeps
working. Deleting a user removes the scope directory and all credential keys.

## Safety layers (must survive any rebuild)

- TLS enforced and not disableable: `ssl.create_default_context()` for IMAPS /
  SMTPS / STARTTLS; port 465 implicit TLS, otherwise mandatory STARTTLS.
- SSRF guard: `assert_safe_remote_host` before every IMAP/SMTP connect;
  private hosts only via admin-only `email_allow_private_hosts`.
- Credentials never in config, never returned to tools/agents; account ids
  masked in logs (`_mask_account`: first 3 chars + `***`; never log full
  provider response bodies).
- Phishing visibility split (inbound prompt-injection defense): suspicious
  messages are hidden from agent mail tools while the human mail client still
  shows them with a warning. The v2 client re-surfaces this via
  `MailService.annotate_visibility`, which shims the v2 row fields
  (`from_addr`->`from`, `snippet`->`body_snippet`, `category`) into the SSOT
  scorer `mail_utils.annotate_messages_with_agent_visibility` and stamps
  `suspicious_for_agent` / `suspicious_reasons` onto every `/api/mail` read
  response (threads, thread detail, messages, search); the reader renders a
  warning banner and the conversation list a warning badge. Config keys
  `email_agent_phishing_filter_enabled` / `_score_threshold` /
  `_trusted_sender_domains` (all admin-only). Note: scoring sees only
  subject/snippet/sender today; making it body-aware remains open.
- Answered indicator: the store tracks `answered_at` (set when a reply is
  sent); `store.list_threads` exposes an `answered` count and the v2 client shows
  a reply marker on answered conversations and "Answered on {date}" in the reader,
  so a mail is not answered twice.
- Gmail-style categories: Gmail's inbox tabs
  are NOT labels - they are saved searches over hidden system categories, so
  `FETCH X-GM-LABELS` carries no tab at all and the original label mapping could
  never match (verified against a live mailbox: every ingested message was stamped
  `primary`). The category is therefore resolved with `SEARCH UID lo:hi X-GM-RAW
  "category:<tab>"`, one search per tab (promotions/social/updates/forums), scoped
  to the UID range being ingested and to the inbox, degrading to `primary` on any
  error. The v2 client shows a category chip on non-primary conversations and a
  relabel picker in the reader. Categories are applied on INSERT only, so a manual
  relabel is never overwritten by a later sync.
- Sender-rule learning on relabel (owner decision: every relabel learns a rule):
  `PATCH /api/mail/messages/{pk}/category` runs `relabel_and_learn` ->
  it relabels the one message, derives a sender pattern from its From header
  (`email_accounts.pattern_from_from_addr`, the one SSOT copy re-exported by
  email_routes), upserts a `sender_category_rules` entry
  (`email_accounts.upsert_sender_rule`), then backfills EVERY stored mail from that
  sender (`MailService.apply_sender_rules_backfill`, also exposed standalone at
  `POST /api/mail/messages/apply-sender-rules`). The response carries `updated`
  (count changed) so the client refreshes the list when the backfill touched more
  than the one mail. All of it is a LOCAL classification (nothing written to the
  mail server), normalized to lowercase/underscores/64-char cap, and NOT gated by
  `mail_engine_write_enabled`. The rule is ALSO applied at
  ingest (`ImapSyncEngine._apply_sender_rules`), where it overrides the
  provider tab - without that a learned rule only ever relabelled EXISTING mail
  and silently missed every new arrival, which is the opposite of what `label_mail`
  promises. Rules are read once per sync run and the matching itself stays in the
  config SSOT (`apply_sender_rules_to_category(..., rules=...)`).
- Connecting mail requests engine-capable scopes up front. For Google the
  mail connect passes `imap=true`, so ONE consent yields an account the engine can
  serve; the IMAP scope set is a union that still contains calendar, so the shared
  token keeps Calendar working. Without this every newly connected Google account
  was born unable to use the DEFAULT mail engine and needed a second consent round
  forever - a permanent state, not a migration step. Microsoft is different BY
  CONSTRUCTION: IMAP/SMTP tokens live on the outlook.office.com resource and cannot
  be combined with Graph scopes in one token, and `calendar_client` reads the Graph
  record - so the Microsoft mail connect keeps minting Graph first and the mail
  token is a second, unavoidable consent. There is therefore no "upgrade" concept
  in the UI: an account that cannot serve the engine offers the ordinary
  **Reconnect** action, which runs the same sign-in as connecting
  (`GET /api/email/oauth/start?provider=..&imap=true&account=<email>`). The account
  panel starts it itself so it works on the standalone `/mail` route where the setup
  wizard is not mounted. `account` becomes the OAuth `login_hint`, without which a
  multi-account user reconnects whichever mailbox the browser is signed in as (the
  identity comes back from the token, not the request). Consent completes in the
  system browser, so the panel polls and re-checks on window focus. A
  password/app-password account is IMAP-capable by definition and never carries
  `imap_ready`, so the UI gates that badge on the provider too.
- Silent-failure classes the single-lane design depends on. Each one fails without
  a word, and with no second lane to fall back to each would be permanent:
  - A send with no usable lane is classified `permanent`, not `transient`. A
    transient verdict is retried five times and then parked, and nothing read that
    state - the compose dialog reported success and the mail never left. The client
    now polls `GET /api/mail/ops` (which also returns `last_error` and the subject)
    and shows a banner for parked sends. The banner is ACTIONABLE
    (`POST /api/mail/ops/{id}/retry`, `DELETE /api/mail/ops/{id}`, both only valid
    on a `failed` op): a parked send that can be neither retried nor dismissed is a
    warning the user can never clear, which is exactly what the first live test hit
    - the parked op predated the XOAUTH2 fix, so a retry succeeds now.
  - The message list clears on a failed load and carries a header naming the folder
    it belongs to. Keeping the previous rows made a failed reload indistinguishable
    from the selected folder's real content (live test: the drafts folder appeared
    to contain inbox mail).
  - Folder unread/total badges are re-read on the 60-second refresh and after every
    action that changes them (opening a thread, archive/trash, sync). They were
    fetched ONCE with the status on mount, so every badge froze at whatever it was
    when the client opened - `list_folders` costs well under a millisecond for ~50
    folders, so there is no reason to fetch it only once. The Sync button also
    reports a refusal: the route answers 200 with `{ok: false}` for an account with
    no usable connection, which used to produce a spinner and nothing else.
  - `import_legacy_artifacts` is keyed PER ACCOUNT (`legacy_import_done:<id>`, JSON
    value carrying `attempts`; a bare-timestamp marker from the store-wide era still
    counts as done). It ran once per account but marked itself done store-wide, so
    the first account to sync consumed the marker and every later account lost its
    labels and answered markers. It is also NOT marked done while legacy rows have
    no counterpart in the store yet - the first sync is UID-bounded, so an older
    mail deserves another try - with `_MAX_ATTEMPTS` bounding rows that never
    arrive. It now also runs on the manual `POST /api/mail/sync/{id}`, not only on
    the supervisor sweep (a user with auto-sync off never got it).
  - Adding a password account for an address already connected via OAuth is
    REFUSED (`email_accounts.oauth_provider_for`): it would replace the entry, and
    `calendar_client` resolves calendars by exactly that provider, so the calendar
    lost the account without a word. The error points at Reconnect, which grants
    everything the engine needs.
- Lazy folders: `sync_account` covers the eager/headers tiers only; other
  folders sync ON OPEN. Nothing was requesting them, so every label stayed
  permanently empty - the client now issues a one-time
  `POST /api/mail/sync/{account}?folder=<name>` when an opened folder comes back
  empty, then re-reads.
- Account list in the client: `GET /api/mail/status` returns the UNION of the
  engine store's accounts and the configured mail accounts, with config-only entries
  marked `synced: false`. The client renders those with a "needs IMAP re-consent"
  hint that opens the account panel, instead of dropping them - listing only the
  store would make an account still awaiting re-consent look deleted.
- High-risk outbound gate in `send_mail` (exec-impersonation to free-mail,
  high-risk request language, attachment-exfiltration wording, coercive
  urgency) requiring an explicit confirm re-call. Word lists live ONLY in
  `mail_utils.py` (`_FREE_MAIL_DOMAINS`, `_EXEC_IMPERSONATION_WORDS`);
  `send_mail.py` imports them - do not create copies.
- Attachment sending resolves paths under the shared per-user filesystem
  jail (`compute_user_jail` in `vaf/tools/filesystem.py`, same mechanism as
  LibrarianTool/WriteFileTool): a non-admin user cannot attach files outside
  their own data. Symlinks are resolved at check time and the real path is
  re-checked. The native sender reads the attachment BYTES
  inside the jail window and embeds them in the MIME, so the check-vs-read swap
  race is closed for every account (the non-imap_ready delegate that used to read
  paths in the transport is gone). Guarded by
  `tests/test_mail_config_and_jail_guards.py`.
- Rate limiting: failed IMAP credential tests feed the per-IP login limiter.

## Registry copies checklist (CLAUDE.md Rule 2)

Any mail change that adds tools, config keys, or events must update ALL of:

1. Mail-tool kwargs-injection tuples: `agent.py` (search for the mail tool
   tuple in `execute_tool` dispatch) AND `workflows/engine.py` (same tuple).
2. `front_office_tools.py` allow-list (decide deliberately; destructive mail
   verbs stay out).
3. `thinking_mode.py` `_SENT_TOOLS` (any new outbound mail tool).
4. `context.py` pruning allow-list and `agent.py` `_NONPROGRESS_TOOLS`.
5. `agent.py` email keyword heuristics and send-success phrase list.
6. `config.py` DEFAULTS + admin gating (`GLOBAL_CONFIG_KEYS` /
   `GLOBAL_CONFIG_KEY_PREFIXES`; `email_agent_` and
   `email_allow_private_hosts` are admin-only) + CONFIG_SCHEMA.md row AND its
   key-count line (guarded by `tests/test_mail_config_and_jail_guards.py` and
   `tests/test_speech_config_schema_sync.py`).
7. `web/app/page.tsx` WebSocket field forwarding for every new mail event.
8. [CONNECTIONS.md](CONNECTIONS.md) email section and this document.

## License ledger (pattern sources; never copy code)

Architecture patterns studied: Thunderbird incl. Panorama (MPL-2.0),
Mailspring + Mailspring-Sync (GPL-3.0), Geary (LGPL-2.1+), Evolution/EDS
(LGPL-2.1), K-9 Mail / Thunderbird for Android (Apache-2.0), Nextcloud Mail
(AGPL-3.0), Roundcube (GPL-3.0+), Cypht (LGPL-2.1), jmap-perl (MIT), Dovecot
(MIT/LGPL-2.1), Notmuch and isync/mbsync (GPL, read-only inspiration).
Protocol algorithms come from IETF RFCs (freely implementable): 2177 (IDLE),
3676 (format=flowed), 4315 (UIDPLUS), 4549 (offline sync), 5256 (threading),
6154 (SPECIAL-USE), 6851 (MOVE), 7162 (CONDSTORE/QRESYNC), 8621 (JMAP data
model). Runtime dependencies (v2): IMAPClient BSD-3-Clause, aiosmtplib MIT,
nh3 MIT (ammonia MIT/Apache-2.0), zstandard BSD, optional DOMPurify
Apache-2.0/MPL dual. GPL/AGPL projects are pattern references only.
