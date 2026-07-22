# Security Dashboard (Logs Overview)

This document is the design-doc home for the security dashboard subsystem: the
admin-only aggregation backend under `/api/security`, the always-on security
event log it reads from, and the skill protection lifecycle it controls. The
user-facing surface is the "Overview" view of the Logs window (see
[WEB_UI.md](../web-ui/WEB_UI.md)).

---

## Purpose and the honesty rule

The dashboard answers one question for the admin: "is anything unprotected or
under attack right now?" It aggregates the protection modules (sandbox,
firewall/LAN perimeter, Docker segmentation, user isolation, channels,
guardrails, skills) into per-module states plus a worst-of roll-up rendered by
the frontend.

**Honesty rule (non-negotiable):** every status is derived from a live probe
(`docker inspect`, config reads, the live tool registry, real DB queries),
never from compose files or static claims. Absent data is reported as absent
(`"state": "nodata"` or a `null` block), never as a green default. A collector
that cannot measure must say so; a dashboard that shows green when it did not
look is worse than no dashboard.

Status derivations are pure functions over probe results (`derive_*` in
`vaf/api/security_routes.py`), so they are unit-testable without Docker
(pattern borrowed from `vaf/core/display_platform.py`).

---

## GET /api/security/overview (the aggregator)

Defined in `vaf/api/security_routes.py`. Admin-gated via
`Depends(require_admin)` (from `vaf/api/user_routes.py`); in genuine
single-user desktop mode a tokenless localhost caller is floored to admin by
the auth middleware, so the desktop window keeps working. Blocking probes
(docker subprocesses, file walks) run in the threadpool; DB metrics are async.
Every block degrades independently: one failing collector nulls its own block
and never takes the others down.

| Block | Collector | States | What it measures |
|-------|-----------|--------|------------------|
| `sandbox` | `collect_sandbox_status` -> `derive_sandbox_status` | ok / warn | Live `docker inspect` of the `vaf-sandbox` container: running state plus the actually-enforced hardening (`cap_drop_all`, `no_new_privileges`, memory/CPU limits, `isolated_network` == only `vaf-sandbox-network`). `warn` when the docker daemon is down: sandboxed execution is BLOCKED, fail-closed (see [SANDBOXING.md](SANDBOXING.md)). |
| `firewall` | `collect_firewall_status` | ok | LAN perimeter: `local_network_enabled` / `local_network_firewall_enabled` flags plus today's counts of blocked-access events (`ip_blocked`, `unauthenticated_blocked`, `token_rejected`, `ws_rejected`) and failed logins (`login_failed`, `twofa_failed`). Blocks happening keeps the state ok: that is the firewall working. |
| `firewall.docker` | `collect_docker_isolation` | ok / warn / null | The inner firewall: Docker network segmentation across all VAF containers, independent of LAN mode. `warn` when any published port binds off loopback (`0.0.0.0`/`::` is LAN exposure) ; also checks the sandbox is off the internal `vaf-network`. `null` when docker is unavailable (no phantom green). |
| `isolation` | `get_admin_isolation_metrics` (vaf/memory/database.py) + `collect_workspace_metrics` | metrics / null | Per-scope memory/chunk counts, DB size, a live RAG latency probe, and per-user workspace folder sizes (bounded walk, `truncated` marks lower bounds). See "The owner DB lane" below. |
| `channels` | `collect_channels_status` | ok / warn | Messenger ingress perimeter (telegram/whatsapp/discord): enabled state, paired sender counts, ingress policy mode, and today's `channel_rejected` counts. `warn` when any ENABLED channel runs in permissive mode; rejections happening is the perimeter working. |
| `guardrails` | `collect_guardrails_status` | ok | Gate flags from config, a permission-level inventory of the LIVE agent tool registry, and the persisted trust store (trusted dirs, allow-always tools). Deliberate call: `channel_tools_unrestricted=true` is surfaced as a warning row in the popup but never ambers the module, because it is the shipped default and a banner that is amber on every install teaches alarm fatigue. |
| `skills` | `collect_skills_status` -> `derive_skills_status` | ok / warn / critical | Scan-level counts across all installed skills plus today's skill events. `critical` when a high-level skill is installed and not quarantined, `warn` on medium. An acknowledged medium is excluded from the banner state (`effective_worst`) but still shown truthfully in the counts and donut. |

The response also carries `security_latest_ts` (timestamp of today's newest
security event) so the frontend badge can be unread-based.

### The owner DB lane

`get_admin_isolation_metrics` runs on the owner engine, which bypasses RLS.
This is acceptable only because it returns cross-scope METADATA (counts and
sizes, never memory content), and its only caller is the admin-gated overview
route. Usernames are attached server-side from the auth DB and scope ids are
truncated to 8 characters for display; the full scope UUIDs never leave the
backend. This aggregate must never be exposed on a per-user route (see
[USER_ISOLATION.md](USER_ISOLATION.md)).

### Known gap: ephemeral sandbox network

When the persistent `vaf-sandbox` container is not running, execution falls
back to ephemeral containers (`DockerSandbox` in `vaf/tools/sandbox.py`).
These carry the same memory/CPU limits, but they are started without a
`--network` flag, i.e. on Docker's default bridge, NOT on the isolated
`vaf-sandbox-network` and not network-less. The sandbox module still reports
`ok`/`ephemeral_on_demand` because execution remains container-enforced; do
not document the ephemeral path as `--network none`.

---

## The security event log

`vaf/core/security_events.py` is the append-only audit log of blocked and
rejected access attempts: who tried to reach VAF and was turned away. Two
sinks are written together per event:

- `security_events_<date>.jsonl`: structured source of truth, served by
  `GET /api/security/events`.
- `security_<date>.log`: human-readable mirror. The Logs window's file rail
  lists domains from `<domain>_<date>.log` automatically, so it appears there
  as the `security` domain without extra wiring (ISO timestamp first, so the
  line parser renders the timestamp column).

Contract rules:

- **Never log secrets**: no passwords, no 2FA codes, no tokens. Usernames and
  IPs are fine; the reader endpoints are admin-only.
- **Never raises**: logging must not be able to break the request path.
- **Per-source flood throttle**: repeated identical `(kind, ip, username,
  channel)` events within 5 seconds are dropped, so an attacker hammering an
  endpoint cannot grow the log unboundedly, while distinct senders never
  swallow each other's events. The throttle map is bounded.
- **Always on**: independent of `debug_logs_enabled`. Rejected access attempts
  are audit signal, not debug noise.

Event kinds and emit sites:

| Kind | Meaning | Emit site |
|------|---------|-----------|
| `ip_blocked` | Request from outside the allowed LAN ranges (403) | `vaf/auth/middleware.py` |
| `unauthenticated_blocked` | LAN request without a token (401) | `vaf/auth/middleware.py` |
| `token_rejected` | LAN request with an invalid/expired token (401) | `vaf/auth/middleware.py` |
| `login_failed` | Wrong username/password on `/api/auth/login` | `vaf/api/auth_routes.py` |
| `twofa_failed` | Wrong/expired 2FA code or temp token | `vaf/api/auth_routes.py` |
| `ws_rejected` | Rejected NETWORK WebSocket handshake (IP/token); trusted-localhost paths do not emit | `vaf/core/web_server.py` (`_emit_sec_ws`) |
| `channel_rejected` | Unauthorized messenger sender dropped at ingress; `channel` carries the platform, `username` the sender id | `vaf/api/telegram_bridge.py`, `whatsapp_bridge.py`, `discord_bridge.py` |
| `skill_blocked` | HIGH scan result stopped a skill install/update | `vaf/skills/scanner.py emit_skill_security_event`, called from the `create_skill`/`update_skill` tools and the WebUI editor/zip import |
| `skill_override` | Admin explicitly accepted a HIGH result (install override or quarantine restore) | Same pipeline + `security_routes.py` restore |
| `skill_scan_alert` | Periodic re-scan found a worsened risk level (below high) | `vaf/skills/rescan.py` |
| `skill_quarantined` | Skill quarantined (auto on worsened-to-high, or manual isolate) | `vaf/skills/rescan.py`, `security_routes.py` isolate |
| `skill_removed` | Quarantined skill deleted from the dashboard | `security_routes.py` delete |

Unknown kinds pass through; the list is an informational contract for
consumers, not a validation gate.

Read endpoints (both admin-gated):

- `GET /api/security/events?date=YYYY-MM-DD&limit=N`: structured events for a
  day, oldest first. Backs the firewall detail popup.
- `GET /api/security/alert-count`: cheap poll returning today's event count and
  the newest timestamp. Every entry in the log is a rejected/blocked/failed
  attempt, so all count. Drives the sidebar unread dot.

---

## Skill protection lifecycle

The full skill system is documented in [SKILLS.md](../agents/SKILLS.md); this
section covers the protection lane the dashboard owns.

1. **Install-time gate.** Every path that lands a skill on disk scans it first
   (`create_skill`/`update_skill` tools, the WebUI editor, zip import via
   `skills_registry.import_skill_zip`). A HIGH result blocks the install
   unless an admin explicitly overrides; both outcomes are mirrored into the
   security event log (`skill_blocked` / `skill_override`).
2. **Periodic re-scan.** `vaf/skills/rescan.py` closes the post-install gap
   (files edited on disk, synced bundles, tampering): every
   `skills_rescan_interval_hours` (default 5, `0` disables) a daemon thread
   re-scans every installed skill folder, updates the manifest scan block on
   change, and emits `skill_scan_alert` when a level worsened. Worsening to
   HIGH auto-quarantines the skill (`skill_quarantined`). The worker is armed
   idempotently from the FastAPI startup hook (which runs twice in TLS mode)
   and persists a `last_rescan.json` summary for the dashboard.
3. **Quarantine gate.** `skills_registry.get_visible_skill_ids_for_user`
   skips quarantined entries before any `shared_with` logic runs: a
   quarantined skill is invisible to EVERY agent path (prompt list,
   `list_skills`, `read_skill`, `use_skill`, router), including admin agent
   sessions. Resolution happens only in the dashboard, never by the agent.

### Resolution endpoints

All are admin-gated via `require_admin`; two additionally require the current
admin's TOTP code (`_verify_admin_totp`: 403 on a wrong code, 400 when 2FA is
not configured or the caller is the tokenless localhost floor, which has no
user record to verify against).

| Endpoint | Method | Admin TOTP | Effect |
|----------|--------|------------|--------|
| `/api/security/skills/{id}/scan` | GET | no | Live re-scan of the installed folder so the popup can show per-rule findings (the manifest stores only the aggregate). |
| `/api/security/skills/{id}/delete` | POST | no | Delete a quarantined skill entirely. |
| `/api/security/skills/{id}/isolate` | POST | no | Manually quarantine an installed skill (e.g. an override-installed high). |
| `/api/security/skills/{id}/acknowledge` | POST | yes | Mark a MEDIUM skill as reviewed-and-kept: it stays visible and truthfully medium, but stops ambering the banner. Refused for high. |
| `/api/security/skills/{id}/restore` | POST | yes | False-positive resolution: lift a quarantine, re-exposing the skill to the agent. |

The asymmetry is deliberate (owner mandate): silencing a warning
(acknowledge) or re-exposing a flagged skill (restore) is a security decision,
so a stolen admin session alone must not suffice; the second factor is
required. Deleting or isolating a skill only reduces risk, so no second
factor is needed there.

---

## Frontend surface

The dashboard renders as the "Overview" view of the Logs window
(`OverviewPane` in `web/components/NotificationsModal.tsx`), which is the
default view when the window opens: a hero worst-of roll-up over the module
rows, each opening a detail popup, plus the skills donut with the resolution
actions above. For admins the frontend polls `GET /api/security/alert-count`
every 60 seconds; a red dot appears on the sidebar Logs button while the
newest security event is newer than the per-browser seen marker (localStorage
key `vaf_logs_seen_ts`), and opening the security log marks everything seen.
Layout, colors, and the full window structure are documented in
[WEB_UI.md](../web-ui/WEB_UI.md).

---

## Testing

CI-guarded contracts (run with the repo venv):

- `tests/test_security_overview_route.py`: sandbox status derivation semantics
  (ok running/ephemeral, warn on docker down, honest hardening booleans from
  the live inspect payload).
- `tests/test_security_events.py`: dual sink, parseable log-line format, flood
  throttle, no-secrets field surface, firewall-module derivations.
- `tests/test_skills_rescan.py`: post-install tampering surfaces (manifest
  updated to high, `skill_scan_alert` raised, clean sweep changes nothing,
  summary persisted).
- `tests/test_skills_dashboard_e2e.py`: installing a medium/high skill through
  the real scanner + registry flips the skills state to warn/critical.
- `tests/test_supervisor_scoping.py`: the supervised-units panel's backing API
  is caller-scoped (non-admins only see and cancel their own sessions' units).
- `tests/test_thinking_status_route.py`: the background-agent panel's snapshot
  is read-only, admin-gated, and carries only whitelisted request fields.
