# Email Client Architecture

Design doc for VAF's email subsystem. Read this BEFORE changing any mail-related
file (`vaf/core/email_transport.py`, `vaf/core/email_sync_store.py`,
`vaf/core/email_accounts.py` (route-independent account-config SSOT: get/save config,
get_account, sender-category rules),
`vaf/api/email_routes.py`, `vaf/core/oauth_pkce.py`, `vaf/core/credential_store.py`,
`vaf/tools/mail_*.py` / `send_mail.py` / `read_mail.py` / `find_mail.py` /
`label_mail.py` / `mark_mail_answered.py` / `list_email_accounts.py`,
`web/app/mail/page.tsx` (three-pane `MailClientView`),
`web/components/connections/MailClient.tsx` (window modal),
`web/components/connections/MailDashboard.tsx`, `EmailSetupWizard.tsx`).

Related docs: [CONNECTIONS.md](CONNECTIONS.md) (connection/channel model,
email account setup), [CONFIG_SCHEMA.md](../setup/CONFIG_SCHEMA.md) (email keys),
[USER_ISOLATION.md](../security/USER_ISOLATION.md) (scoping rules).

Status: section "Current architecture" describes the v1 code. Section "Target
architecture (v2)" records the decided design; this doc is updated as each
phase ships. SHIPPED so far (phase 1, read-only, default-off behind
`mail_engine_v2_enabled`): the `vaf/mail/` engine core - per-scope store
(store.py, schema v1 with FTS5 contentless-delete and encrypted zstd raw
blobs), parser.py, RFC 4549 sync engine (sync.py) with Gmail X-GM capture,
imap_client.py factory, service.py (nh3 sanitizer + attachment serving),
supervisor.py (sweep + IDLE watchers + E3 new-mail hook + legacy artifact
import via migrate.py), /api/mail routes (mail_routes.py), the three-pane mail
client (conversation view, sandboxed-iframe HTML with remote-image blocking) -
rendered as an in-app WINDOW MODAL (`MailClient.tsx`, same 95vw x 90vh chrome as
the other connection dashboards) opened from the Connections "Email" tile; the
`/mail` route renders the same `MailClientView` full-height as a direct-URL
fallback -, and the legacy-shape bridge (tool_bridge.py) that switches the
seven agent tools, the legacy /api/email message routes and MailDashboard to
the v2 store with the same flag.
Phase 2 SHIPPED (write path): local-first verbs (read/unread, star, archive,
trash-only delete) with a durable op queue (store.py ops table, writeback.py
executor: STORE/MOVE with COPY+EXPUNGE fallback/APPEND, attempts-capped),
compose.py (reply/reply-all/forward quoting, format=flowed, RFC 5322 threading
headers), undo-send outbox (client-delay model, restart-safe via the
supervisor sweep, fast-path delivery task in mail_routes), Sent-APPEND for
plain IMAP accounts, write REST endpoints, compose UI with undo snackbar, and
four new agent tools (reply_mail, forward_mail, archive_mail, delete_mail)
with the full Rule-2 registry sweep (kwargs tuples, thinking _SENT_TOOLS,
front-office exclusion) guarded by tests/test_mail_config_and_jail_guards.py.
Mailbox writes replay only when mail_engine_write_enabled is on.
Phase 3 SHIPPED (auth): IMAP-capable OAuth lanes - Google re-consent with the
scope UNION (mail.google.com + calendar, one token, calendar survives),
Microsoft via a separate "microsoft_imap" token record (outlook.office.com
resource; the Graph token keeps serving calendar), /api/email/oauth/start
accepts imap=true, successful re-consent stamps imap_ready on the account and
the supervisor picks it up; German provider presets (GMX, web.de, T-Online,
outlook.de) added to IMAP_SMTP_DEFAULTS.
Phase 4 partially SHIPPED: remote-image opt-in via the SSRF-guarded,
image-only /api/mail/image-proxy (per-view opt-in; per-sender persistence is
future polish), security events (mail_high_risk_send_blocked,
mail_image_proxy_blocked). STILL OPEN: WS delta events (UI polls 60s),
Gmail X-GM-MSGID cross-folder dedup (All Mail stays lazy-tier until then),
server-synced drafts, per-sender remote-image persistence, mobile UI pass
per MOBILE_UI.md, and the legacy-stack teardown (possible only after
existing accounts completed re-consent).

## Current architecture (v1)

The current integration is an account-connection layer plus a lightweight
synced-inbox viewer, not a full mail client.

### Components

- `vaf/core/email_transport.py`: three provider dialects dispatched by the
  account's `provider` field: `imap` (imaplib IMAP4_SSL + smtplib SMTP),
  `gmail` (Gmail REST API, OAuth2 Bearer), `microsoft` (Microsoft Graph,
  OAuth2 Bearer). Fetch is a stateless header poll (IMAP sequence numbers, not
  UIDs; no UIDVALIDITY tracking); folders are opened `readonly=True`; no flag
  writeback, no IDLE, no received-attachment handling. Bodies are fetched live
  per view and reduced to plain text (`_html_to_plain` regex strip); HTML is
  never rendered. Send builds MIME with attachments/cc/bcc (Bcc envelope-only
  for SMTP, header included for Gmail raw send) and In-Reply-To/References
  threading headers.
- `vaf/core/email_sync_store.py`: one SQLite file per user
  (`data_dir/scopes/<user_scope_id>/email_sync.db` for network users, legacy
  `users/<username>/`, root file for the local admin). Single table
  `email_messages` (envelope fields + 4 KB `body_snippet`, PK
  `(username, account_id, folder, message_id)`), WAL mode, 90-day retention
  delete on every sync. Search is `LIKE` over subject/from only.
- `vaf/api/email_routes.py`: REST under `/api/email` (OAuth start/callback,
  account CRUD/test/verify, per-account sync capped at 200 messages, message
  list/search/body, categories + sender rules). Every data endpoint depends on
  `_get_current_user`; store access goes through
  `store_candidates_for_mail` which returns exactly ONE (username, scope)
  candidate (no cross-user fallback). A 30-minute server loop auto-syncs
  INBOX for accounts with `auto_sync_enabled` (`web_server.py`).
- `vaf/core/oauth_pkce.py`: Authorization Code + PKCE (S256) for Google and
  Microsoft. Single-use state file (0600, 10-min TTL) carrying the
  code_verifier and initiating user; callback actor binding in network mode
  (`oauth_session_binding.py`); token refresh with rotation support.
  Scopes today are Gmail-API/Graph scopes (`gmail.readonly`, `gmail.send`,
  `Mail.Read`, `Mail.Send`, plus calendar) - NOT IMAP scopes. Calendar shares
  these tokens (see USER_ISOLATION.md).
- `vaf/core/credential_store.py`: two-tier storage - OS keyring (service
  `vaf-email`) preferred, AES-256-GCM envelope-encrypted fallback file via
  `secure_store.py`. Key naming: `email:{provider}:{scope}:{account_id}`;
  non-admin scopes never fall back to admin/legacy keys.
- Agent tools (`vaf/tools/`): `mail_inbox`, `read_mail`, `find_mail`,
  `send_mail`, `label_mail`, `mark_mail_answered`, `list_email_accounts`,
  shared helpers in `mail_utils.py`. The agent stamps `username` +
  `user_scope_id` into tool kwargs at dispatch (`agent.py`) and the workflow
  engine does the same (`workflows/engine.py`); tools never trust
  model-provided identity.
- Web UI: `MailDashboard.tsx` (INBOX list, category chips, subject/sender search,
  plain-text detail view) and `EmailSetupWizard.tsx` (OAuth/IMAP connect wizard).
  With the v2 client shipped, the Connections "Email" tile now opens the v2
  `MailClient` window; `MailDashboard` is reached from that window's "Accounts"
  button and serves as the account-management surface until account CRUD is ported
  into the client. All requests ride the Next.js catch-all proxy with
  cookie/authorization forwarding.

### Safety layers (must survive any rebuild)

- TLS enforced and not disableable: `ssl.create_default_context()` for IMAPS /
  SMTPS / STARTTLS; port 465 implicit TLS, otherwise mandatory STARTTLS.
- SSRF guard: `assert_safe_remote_host` before every IMAP/SMTP connect;
  private hosts only via admin-only `email_allow_private_hosts`.
- Credentials never in config, never returned to tools/agents; account ids
  masked in logs (`_mask_account`: first 3 chars + `***`; never log full
  provider response bodies).
- Phishing visibility split (inbound prompt-injection defense): suspicious
  messages are hidden from agent mail tools while the dashboard still shows
  them with a warning (`mail_utils.py`; config keys
  `email_agent_phishing_filter_enabled` / `_score_threshold` /
  `_trusted_sender_domains`, all admin-only). Note: scoring sees only
  subject/snippet/sender today; v2 must make it body-aware.
- High-risk outbound gate in `send_mail` (exec-impersonation to free-mail,
  high-risk request language, attachment-exfiltration wording, coercive
  urgency) requiring an explicit confirm re-call. Word lists live ONLY in
  `mail_utils.py` (`_FREE_MAIL_DOMAINS`, `_EXEC_IMPERSONATION_WORDS`);
  `send_mail.py` imports them - do not create copies.
- Attachment sending resolves paths under the shared per-user filesystem
  jail (`compute_user_jail` in `vaf/tools/filesystem.py`, same mechanism as
  LibrarianTool/WriteFileTool): a non-admin user cannot attach files outside
  their own data. Symlinks are resolved at check time and the real path is
  re-checked. The native sender (send_mail tool, P2.3) reads the attachment BYTES
  inside the jail window and embeds them in the MIME, so the check-vs-read swap
  race is closed for every imap_ready account; only the shrinking non-imap_ready
  OAuth delegate tail still reads paths in the transport (removed in P7). Guarded
  by `tests/test_mail_config_and_jail_guards.py`.
- Rate limiting: failed IMAP credential tests feed the per-IP login limiter.

### User isolation (v1 rules, unchanged in v2)

- Account config: `email_config_by_scope[user_scope_id]` (preferred),
  `email_config` only for the local admin, `email_config_by_user` legacy.
  Non-admin config reads expose only the caller's own scope slice.
- Store: one SQLite file per scope; every query filters on the single allowed
  (username, scope) candidate.
- Sharp edge: the v1 transport functions default `username`/`user_scope_id`
  to None, which resolves to the LOCAL ADMIN's config (fails open). Every
  route/tool must thread the caller's identity explicitly. The v2 service API
  is fail-closed instead (see below).

### Known v1 limitations (why v2 exists)

No UID-based incremental sync, no push (IDLE), no offline bodies, no received
attachments, no HTML rendering, no flags/read-state, no threads, no folder
discovery, no full-text search, no compose/reply UI, no outbox/drafts, hard
90-day retention, blocking IO inside async handlers, per-view full IMAP
logins.

## Target architecture (v2) - decided

Decisions (owner-approved): IMAP-uniform engine (Gmail-API/Graph transports
retired after migration); email stays OUT of the messaging channel model
(`KNOWN_CHANNELS`) and `send_to_user` cannot deliver via email; no new-mail
automation trigger in v1 of the client, but the sync engine emits internal
new-mail events so a trigger can be added later; message bodies are encrypted
at rest via the secure_store DEK; body-cache retention defaults to 12 months
(headers kept indefinitely), configurable.

### Engine (`vaf/mail/`, new package)

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
  buggy - use defensively). Gmail modeled natively via X-GM-EXT-1
  (X-GM-MSGID dedup, X-GM-THRID threads, X-GM-LABELS, X-GM-RAW search);
  Archive = remove INBOX label, Delete = MOVE to `[Gmail]/Trash`, never rely
  on the account's auto-expunge setting. MOVE with COPY+EXPUNGE fallback;
  SPECIAL-USE folder discovery with a localized well-known-name fallback
  table. New mail: one IDLE connection pinned to INBOX (re-issued ~every
  25 min; dead socket means resync now; periodic NOOP safety net for the
  Office365 IDLE bug) plus a STATUS sweep of other folders every 2-5 min.
  Maximum two connections per account. Folder tiering: INBOX eager
  (headers+bodies), Sent/Drafts/Archive headers eager + bodies lazy, other
  folders on open.
- Workers: a MailSyncSupervisor asyncio task in the web backend replaces the
  30-minute loop; one crash-isolated worker per account (synchronous
  IMAPClient driven via asyncio.to_thread), restartable individually so one
  broken account never stalls others. CLI-only mode runs no workers
  (on-demand sync remains). Writes go through the durable op queue: UI/tools
  write the local DB immediately and enqueue idempotent operations (flag,
  move, append, delete) replayed against the server using the `server_flags`
  diff.
- Native send (`vaf/mail/sender.py`, mail v2-only port P2): one delivery core for
  every account. Dispatch by `(provider, imap_ready)`: `imap` -> SMTP password;
  `gmail`/`microsoft` AND `imap_ready` -> SMTP SASL XOAUTH2 (Gmail union token,
  Microsoft `microsoft_imap` token - the same lanes the IMAP client uses); an
  OAuth account not yet `imap_ready` falls onto a documented
  `email_transport.send_mail` REST/Graph delegate (a shrinking strangler tail
  removed in P7; each fall emits a distinct countable log line). Delivery uses
  stdlib `smtplib` (every caller is synchronous - agent tool run / OpExecutor
  drain via `asyncio.to_thread` - so no event-loop bridge is needed and the SMTP
  conversation is driven step by step for honest hand-off classification). A
  `handed_off` flag flips at the DATA command: a failure before it is
  transient/permanent, at or after it is ambiguous (parked, never re-sent). Bcc
  is stripped from the delivered wire and rides the SMTP envelope only;
  `normalize_recipients` lives in `vaf/mail/addressing.py`. All four senders
  (send_mail/reply_mail/forward_mail tools + `writeback._op_send`) route here.
- Libraries: IMAPClient (BSD-3) as the IMAP driver, stdlib `smtplib` for SMTP
  submission (the native sender; aiosmtplib remains a declared dependency but is
  unused - a synchronous send path fits every caller), stdlib `email` with
  `policy.default` for parsing (per-message error boundary: a malformed message
  must never abort a folder sync), nh3 (MIT) for HTML sanitization, zstandard for
  blob compression. aioimaplib is
  rejected (GPL-3.0). bleach is EOL - never adopt it.
- Auth lanes: (1) password / app password (GMX, web.de, T-Online, iCloud,
  Yahoo, consumer Gmail) with provider presets and help texts; (2) Microsoft
  OAuth-IMAP via XOAUTH2 (`https://outlook.office.com/IMAP.AccessAsUser.All`
  + `SMTP.Send` + `offline_access`; basic auth is retired); (3) Google
  OAuth-IMAP via XOAUTH2 (`https://mail.google.com/`, restricted scope;
  app password is the recommended first choice in the wizard). Existing v1
  OAuth tokens carry API scopes unusable for IMAP: migration requires
  re-consent with a scope union so calendar keeps working; legacy accounts
  stay readable through the v1 transport until re-consent.

### API and Web UI

- REST under `/api/email/*` (existing endpoints stay functional until the new
  UI reaches parity; strangler rollout behind `mail_engine_v2_enabled` and a
  separate `mail_engine_write_enabled` flag). New endpoints for folders,
  threads, messages, bodies, attachments, compose, drafts, ops. Object
  vocabulary follows JMAP (RFC 8621) naming for Mailbox/Thread/Email shapes,
  but VAF does NOT implement the JMAP wire protocol (deliberate
  no-overengineering decision).
- WebSocket deltas ride the existing event pipeline: per-account monotonic
  `state` counter; `email_state` + `email_delta` events; a client that
  detects a gap resyncs via REST. Events are scoped at the EMIT site
  (`push_update_to_user`), never broadcast. Every new event field must be
  forwarded explicitly in `web/app/page.tsx` (known dropped-field bug class)
  and covered by a CI guard.
- HTML rendering: sanitize server-side with nh3 at the trust boundary (strip
  scripts/handlers/external references, resolve `cid:` parts from the store),
  render into a sandboxed `srcdoc` iframe with CSP `script-src 'none'`.
  Remote images blocked by default; per-sender opt-in loads them through a
  server-side image proxy (reusing the SSRF guard). Blocked trackers and
  quarantined phishing mails are logged to the security event log.
- UI: the mail client is an in-app window modal (`MailClient`, three-pane
  responsive layout, conversation view, quick actions, search with operators and
  folder/everywhere scope, compose with reply/reply-all/forward quoting,
  server-synced drafts, undo-send via a client-side outbox delay that
  survives restarts) opened from the Connections "Email" tile - same window chrome
  as the other dashboards, NOT a standalone full-screen route (the `/mail` URL is
  only a direct-access fallback rendering the same `MailClientView`). Desktop and
  mobile per [WEB_UI.md](../web-ui/WEB_UI.md) / [MOBILE_UI.md](../web-ui/MOBILE_UI.md).
  MailDashboard remains as the account-management surface until account CRUD is
  ported into the client, then is removed.

### Agent tools contract

The seven v1 tools keep their names, signatures, and output shapes; they read
the v2 store internally. New verbs (reply/forward with quoting, move/archive/
trash-only delete, attachment listing/reading) are added as NEW tools; the
destructive ones are NOT added to the front-office allow-list. The phishing
filter becomes body-aware (bodies are local in v2); untrusted mail content in
tool outputs is clearly delimited.

### Fail-closed scoping (v2 service API)

Every v2 service call takes explicit `(username, user_scope_id)`. In network
mode a missing scope raises; there is no silent fallback to the local admin.
Deletion lifecycle is explicit: removing an account deletes its rows, blobs,
FTS entries, queued ops, and credentials; deleting a user removes the scope
directory and all credential keys.

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
