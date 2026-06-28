# Changelog

All notable changes to VAF are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and VAF aims to follow
[Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`, with PEP 440
prerelease suffixes such as `a0` / `b1` / `rc1`).

Each released version has a matching git tag `v<version>` and a GitHub Release.
To update an installed VAF, run `vaf update`.

## [Unreleased]

### Changed
- **License: relicensed from "MIT + Commons Clause v1.0" to a dual license — GNU
  AGPL-3.0-or-later (open source) plus a separate Commercial License.** `LICENSE` now
  carries the verbatim AGPL-3.0 text; see the new `LICENSING.md` (dual-license explanation,
  EN/DE) and `COMMERCIAL.md` (commercial/Enterprise terms). Building Plugins, Tools, and
  Workflows on top of VAF stays permission-free via an AGPL Section 7 additional permission.
  Contributor terms in `CONTRIBUTING.md` updated: contributions are accepted under the AGPL
  inbound plus a separate commercial-relicensing grant to Veyllo GmbH (so the dual-license
  model is enforceable), with a DCO `git commit -s` sign-off certifying origin. Source files
  now carry `SPDX-License-Identifier: AGPL-3.0-or-later` headers pointing to `LICENSING.md`.

### Added
- Embeddable library surface: `from vaf import Agent` (`docs/EMBEDDING.md`,
  `docs/ARCHITECTURE.md`); slim base install plus optional extras in `setup.py`.
- Entry-point tool discovery: third-party tools via the `vaf.tools` group.
- Tool input validation & repair before dispatch (`docs/agents/TOOL_INPUT_REPAIR.md`).
- Self-update: `vaf update check` / `vaf update`, an opt-in startup
  update-available hint, and a tag-triggered GitHub release workflow.
- Web search result cache: identical `web_search` queries are served from a
  short-lived file cache (default 15 min; `web_search_cache_enabled` /
  `web_search_cache_ttl_seconds`), skipping the providers and synthesis.
- Email subsystem hardening. **New config key `email_allow_private_hosts` (default
  `false`)**: IMAP/SMTP hosts that resolve to loopback / RFC-1918 private / link-local
  addresses (incl. the `169.254` metadata range) are refused as an SSRF guard unless this
  is enabled. IMAP/SMTP connections now verify TLS certificates against the system trust
  store (connect timeouts; port 465 uses implicit SMTP_SSL). `GET /api/config` redacts
  secret keys (`api_key_*`, `*_secret`, `*_password`, `memory_db_url`, `redis_url`,
  encryption keys, ...) for non-admin users; admins still receive everything.
  `POST /api/email/accounts/test` now requires authentication and is rate-limited (shared
  per-IP login limiter). OAuth PKCE state files (email + cloud) are written atomically with
  `0600` permissions, and token-endpoint errors are no longer logged verbatim.
- `send_mail` now supports `cc`, `bcc`, and reply threading via `in_reply_to` /
  `references`, with recipient-address validation.

### Fixed
- Filesystem alias resolution now matches only on a path boundary.
- `send_mail` no longer silently drops a single string attachment path.
- Mailbox authentication/connection failures now surface as an "authentication failed"
  error from `mail_inbox` / `read_mail` instead of an empty "no messages" result.
- Email headers (From/To/Subject) are now RFC 2047-decoded and message bodies are decoded
  with the part's declared charset (previously hardcoded UTF-8).
- Switching to an unowned/new session now resets the agent's current user scope/username,
  preventing cross-user identity bleed; UUID-scoped network users' mailboxes are now
  included in email auto-sync.
- Cloud storage OAuth (Google Drive etc.) now opens in the system browser instead of the
  embedded desktop webview, and its callback uses the same effective HTTPS proxy port as
  email (shared `vaf/network/oauth_redirect` helper) instead of an unreliable
  `request.base_url`, so connecting cloud accounts works on the Linux/macOS desktop.

<!--
Template for a new release (see docs/setup/RELEASING.md):

## [X.Y.Z] - YYYY-MM-DD
### Added
### Changed
### Fixed
### Removed
-->
