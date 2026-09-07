# Calendar

VAF has its own calendar. Appointments live in a per-user store, the agent's calendar tools
and the Web UI read and write that store, the contact book links its events to it, and the
reminders of its events fire from the automation scheduler. A connected Google Calendar or
Microsoft Outlook account is a **sync source**: its events are pulled into the store on a
schedule, and events created in VAF are written back into it. The integration uses the
**same OAuth credentials as Email** (Gmail/Outlook); there are no separate calendar
credentials.

## Architecture

| Layer | Code | Job |
|---|---|---|
| Store | `vaf/core/calendar_store.py` | One SQLite file per user scope, `Platform.data_dir()/scopes/<scope>/calendar.db`. Events, per-account sync state, the user's settings. Fail-closed: a store always belongs to an explicit scope. |
| Sync | `vaf/core/calendar_sync.py` | Push, pull, deletion detection and conflict rule per account; the supervisor that sweeps every `calendar_sync_interval_minutes`; the reminder tick. |
| Supervisor base | `vaf/core/sync_supervisor.py` | The sweep, the parallel cap, per-account crash isolation, the deduplicated "sync now" lane and the once-per-process start, shared with the mail supervisor. |
| Provider client | `vaf/core/calendar_client.py` | Google Calendar API v3 and Microsoft Graph behind one normalised event shape, paged listing, writes in a named zone, `AuthError` on a dead token. |
| Time grammar | `vaf/core/user_time.py` | `parse_user_datetime` reads every time a user or the model types in the user's zone; `format_user_datetime` and `format_user_date` print them in the user's format. |
| Tools | `vaf/tools/calendar.py` | `list_calendar_events`, `create_calendar_event`, `update_calendar_event`, `delete_calendar_event` on the store. |
| API | `vaf/api/calendar_routes.py` | Events, sync, settings and status for the Web UI. |
| Web UI | `web/components/AutomationCalendarModal.tsx`, `web/components/connections/CalendarDashboard.tsx` | The calendar window; the sync settings. |

OAuth is unchanged: `vaf/core/oauth_pkce.py` requests mail and calendar scopes for Gmail
(`https://www.googleapis.com/auth/calendar`) and Microsoft (`Calendars.ReadWrite`), the
redirect URI stays `/api/email/oauth/callback`, tokens live in the credential store. If the
calendar scopes were added after a user connected Gmail or Outlook, that user reconnects
the account once (Settings > Connections > Email).

## The event model

An event row carries: `id` (uuid), `title`, `description`, `location`, `start_ts` and
`end_ts` (unix instants), `all_day` with `start_date` and `end_date` (ISO dates, the end
exclusive, the way both providers count), `tz` (the IANA zone the event was created in),
`status` (`confirmed` or `cancelled`), `source` (`vaf`, `gmail`, `microsoft`),
`account_id`, `external_id`, `external_calendar_id`, `external_updated`, `etag`,
`recurring_master_id`, `sync_state`, `push_attempts`, `last_error`, `contact_ids`
(VAF's own link to the contact book), `created_by` (`user`, `agent`, `sync`),
`reminder_minutes`, `reminder_fired_at`, `reminder_missed_at`, `link` (the event's page at
the provider) and `legacy_id` (the contact-book event a migrated row came from).

`sync_state` is one of `local_only` (no account), `synced`, `pending_push` (a local
change owed to the provider), `pending_delete` (deleted here, the provider not yet told;
readers do not see the row any more) and `push_failed` (parked after five failed
attempts, retried after the next local edit).

Times are read through one grammar (`parse_user_datetime`): ISO 8601 with an offset is
taken as given, a naive ISO time or `YYYY-MM-DD HH:MM` is the user's zone, a bare
`YYYY-MM-DD` is an all-day event, a bare `HH:MM` is today. They are printed in the
user's zone and format everywhere in the lane; the event's own `tz` decides all-day
boundaries and the wall clock a push sends.

## Sync

The supervisor starts once per process from the web server's startup hook (guarded, because
that hook runs once per uvicorn server under TLS) and sweeps every calendar account of every
scope on `calendar_sync_interval_minutes`. One account sync, in this order:

1. **Push.** Every row in `pending_push` or `pending_delete` for the account is written to
   the account's primary calendar (create, update, delete). A success stores `external_id`,
   `external_updated`, `etag` and `link`; a failure counts against the one event and parks
   it as `push_failed` after the cap. Off when `calendar_sync_push_enabled` is false: the
   rows stay pending and go out once the flag is on again.
2. **Pull.** The window `[now - calendar_sync_past_days, now + calendar_sync_future_days]`
   is read completely (a failed page aborts the sync for this account, nothing is treated as
   deleted) and folded in with `upsert_external`: a new event is created, a changed one
   updated, a cancelled instance marked `cancelled` (its reminder off). **The newer change
   wins:** a local edit still waiting to be pushed is kept when it is newer than the
   provider's copy, and the push carries it over.
3. **Deletions.** A mirrored event inside the window that a complete pull no longer returned
   was deleted at the provider and is removed locally, with its reminder. Rows with a change
   still to push are never removed by this step.

A 401 marks the account `needs_reconsent` with the error text; it is not retried until the
stored token record changes (a re-consent or a refresh). Every other failure is recorded on
the account (`last_error`) and retried on the next sweep. After a sweep that changed rows the
supervisor tells the user's browsers (`calendar_changed`, below).

**Write-through.** A local write (route or tool) marks the event `pending_push` when it has
an account, tells the browser to refetch, and asks the running supervisor to push that
account now (`calendar_sync.after_local_change`). In a process without the supervisor (the
CLI, an automation started from `vaf automation start`) the request is a no-op and the web
server's next sweep pushes; this is a named boundary, not a gap.

**Which account.** A new event goes into the account the caller named, else the user's
**push target** (settings), else the first connected calendar account; without one it stays
internal. `internal_only` keeps it out of every connected calendar. A named account that is
not connected is refused ("No calendar account connected for ...") rather than silently
replaced.

**Account lifecycle.** A calendar-safe mail delete keeps the account entry (with
`mail_enabled=False`) and the shared token, so the calendar keeps syncing it. An account
removed from the configuration altogether is detached by the next sweep: its events stay,
read-only and local, its state row goes, and a push target pointing at it is cleared. The
per-account switch in the settings pauses the sync without detaching.

**Concurrency.** The sweep and a user's "sync now" are serialised per account by a lock, so
one pending row is never pushed twice (two creates at the provider). Rows in
`pending_delete` are invisible to readers and to the tools.

### Named boundaries

One external calendar per account (the primary); incremental sync tokens (Google
`syncToken`, Graph delta) are not used, the window is pulled each sweep; attendees and
invitations are not sent (`contact_ids` is VAF's own link); VAF creates single events and
does not author series (imported series arrive as instances, editable one by one); provider
push notifications need a public URL and are not used; Apple/CalDAV is not a source;
`calendar.db` is owner-only by file mode like the other scope databases, not blob-encrypted
(see [ENCRYPTION_AT_REST.md](../security/ENCRYPTION_AT_REST.md)).

## Reminders

An event with `reminder_minutes` is reminded of by `fire_due_calendar_reminders()`, which
rides the automation scheduler loop (`vaf/core/automation.py`) next to the one-shot
reminders of `vaf/core/reminders.py` and is the same narrow lane: stored data, a text
composed deterministically in the user's language (title, time in the user's format,
location, linked contacts), delivered through `send_to_main_messenger` and shown in the
Web UI bell. No agent run, no tools. Within six hours after the reminder time it is still
delivered (the backend may have been down); older ones are marked missed with an honest
notification. Cancelled events and rows waiting for their deletion stay silent. Reminders
fire wherever the scheduler singleton runs (the web server, `vaf automation start`), for
every scope that has a calendar on disk. The default reminder is a per-user setting in the
store (default 15 minutes, 0 for none); a mirrored event gets none unless asked.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `calendar_sync_interval_minutes` | `5` | Minutes between two sweeps. Read per sweep. |
| `calendar_sync_past_days` | `30` | The pull window's reach into the past. |
| `calendar_sync_future_days` | `365` | The pull window's reach into the future. |
| `calendar_sync_push_enabled` | `True` | Write VAF-created changes into the connected calendar. The kill switch for outbound writes. |

All four are instance policy and admin-only (see [CONFIG_SCHEMA.md](../setup/CONFIG_SCHEMA.md)).
The OAuth client ids and secrets are the email ones.

## Agent tools

| Tool | Description |
|------|-------------|
| `list_calendar_events` | Events in a range from the store, offline. Optional `time_min`, `time_max` (ISO 8601, `YYYY-MM-DD HH:MM` or `YYYY-MM-DD`, a bare end date meaning the end of that day; default now to 7 days ahead), `contact_id`, `provider` or `account_id` (only events mirrored from there), `max_results`. Prints time, location, source and id. |
| `create_calendar_event` | Required `summary`, `start` (a bare date makes an all-day event). Optional `end` (default one hour), `description`, `location`, `contact` or `contact_id` (an ambiguous name is asked back, not guessed), `reminder_minutes` (default the user's setting, 0 for none), `internal_only`, `provider` or `account_id` (mirror into that account). |
| `update_calendar_event` | Required `event_id`. Optional `summary`, `start` (a move keeps the duration), `end`, `description`, `location`, `reminder_minutes` (0 removes it). |
| `delete_calendar_event` | Required `event_id`. A mirrored event is removed from the connected calendar by the next sync. |

The tools receive `user_scope_id` and `username` from the agent and work on the caller's
own calendar. They need no connected account; "No calendar account connected for ..." is
returned only when a provider or account was named and is not connected, which is the
phrase the capability classifier (`vaf/core/context.py`) knows. The Front Office keeps no
calendar tool; a contact's upcoming events reach the agent through the contact block. The
router heuristics in `vaf/core/agent.py` are unchanged: calendar words route to these
tools, standalone reminders stay with `schedule_reminder`.

## REST API (Web UI)

All routes work on the caller's calendar; another scope's event id is not found.

- `GET /api/calendar/status`: `google_available` and `microsoft_available` as before, plus
  `has_calendar`, `accounts` (each with `account_id`, `email`, `provider`, `enabled`,
  `last_sync_at`, `last_error`, `needs_reconsent`), `settings` (`push_target`,
  `default_reminder_minutes`) and `sync` (`interval_minutes`, `push_enabled`,
  `supervisor_running`). Logged to `logs/backend.log` with `[CALENDAR]`.
- `GET /api/calendar/events?time_min&time_max&contact_id&include_cancelled`: the events of
  the range (default the next 7 days) with `start` and `end` as ISO strings in the user's
  zone; `account` names the account new events would go to.
- `POST /api/calendar/events`: `title`, `start`, optional `end`, `all_day`, `description`,
  `location`, `contact_ids`, `reminder_minutes` (absent means the user's default),
  `internal_only`, `account_id`.
- `PATCH /api/calendar/events/{id}`: the same fields; a moved start keeps the duration, an
  all-day move derives its end.
- `DELETE /api/calendar/events/{id}`: answers `pending_delete: true` for a mirrored event.
- `POST /api/calendar/sync?account_id`: syncs the scope's accounts now, in a worker thread,
  and reports per account.
- `PUT /api/calendar/settings`: `push_target`, `default_reminder_minutes`, `accounts`
  (`[{account_id, enabled}]`); answers with the status.
- `POST /api/calendar/ensure-daily-check-automation`: idempotent; creates the "Daily
  calendar check" automation for a scope with a connected account **or** events in the
  store, so an internal-only calendar gets it too. Its prompt lists the next 24 to 48 hours
  through `list_calendar_events` and schedules one-shot reminders through
  `schedule_reminder` (see [AUTOMATIONS.md](../platform/AUTOMATIONS.md)).

The `calendar_changed` WebSocket frame (see
[WEBUI_WEBSOCKET_FLOW.md](../web-ui/WEBUI_WEBSOCKET_FLOW.md)) is sent to the user's browsers
after every local write and after a sweep that changed rows; it carries no payload, the
views refetch.

## Web UI

- **The calendar window** ("Kalender", the sidebar footer button and Settings > Automations)
  shows the month with the VAF calendar's events (blue dots, on past days too) beside the
  automations (grey dots, today and later); a legend above the grid names the two colours.
  The day view is one grid of 24 hour rows with an all-day row on top and a sticky header
  that names the two lanes: "Termine" (calendar icon, with the hint that a click on an hour
  creates an appointment) and "Automatisierungen" (lightning icon), because two columns of
  empty cells do not say which is which. The events lane is in the middle (blocks span
  their duration and sit side by side when they overlap; a click on an empty hour creates
  an event there, and the hovered cell says "+ Termin"); the automations lane on the right
  is tinted grey all the way down, its chips carry the lightning icon, and an empty hovered
  cell says "+ Automatisierung" (a click creates an automation, as before). On today the
  current hour is framed in red across all three columns, with the current time under the
  hour label, the way the automation calendar always marked it.
  The event popup edits title, date, start and end, all-day, location, description, linked
  contacts, the reminder and, on creation, whether the event is also written into the
  connected calendar; a mirrored event shows its source, its push state and a link to open
  it at the provider; deletion asks first. The to-do list and the notes are unchanged.
- **The Calendar Dashboard** (the gear on the Google Calendar or Outlook card, Settings >
  Connections) is the sync settings: the connected accounts with last sync, error or "sign in
  again", the switch per account, which account new events go to, the default reminder,
  "sync now", the sync interval, the admin's push kill switch when it is off, and the
  upcoming events of the VAF calendar with their source. A button opens the calendar window.
- **The Calendar Setup Wizard** is unchanged: connecting Gmail or Outlook is the email OAuth
  flow, and the connections panel refreshes when it closes.
- The contact book's events are calendar events (see [CONNECTIONS.md](CONNECTIONS.md)):
  an event added from a contact carries that contact in `contact_ids`, and the contact's
  "Anstehend" card reads the store.

## Related

- [CONNECTIONS.md](CONNECTIONS.md): the Calendar section and OAuth setup.
- [AUTOMATIONS.md](../platform/AUTOMATIONS.md): the calendar window, the scheduler loop, the reminder lanes.
- [USER_ISOLATION.md](../security/USER_ISOLATION.md): the per-scope store.
- [CONFIG_SCHEMA.md](../setup/CONFIG_SCHEMA.md): the four keys.
- [TOOLS_CATALOG.md](../agents/TOOLS_CATALOG.md): the tool rows.
