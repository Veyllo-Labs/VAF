# Calendar Integration

VAF provides Calendar Scheduling & Event Management via Google Calendar and Microsoft Outlook. The integration uses the **same OAuth credentials as Email** (Gmail/Outlook): there are no separate calendar credentials. Connect Gmail or Outlook in Settings → Connections → Email once, and the agent can list, create, update, and delete calendar events.

## Overview

- **Google Calendar**: Uses the same OAuth client and tokens as Gmail. Scopes include `https://www.googleapis.com/auth/calendar`.
- **Microsoft Outlook Calendar**: Uses the same OAuth client and tokens as Outlook/Mail. Scopes include `Calendars.ReadWrite`.
- **Per-User Isolation**: Calendar access is scoped by `user_scope_id` (and username); credentials and data follow the same isolation as email (see [USER_ISOLATION.md](USER_ISOLATION.md)).
- **Agent Tools**: `list_calendar_events`, `create_calendar_event`, `update_calendar_event`, `delete_calendar_event`.
- **Automation Calendar**: The "Automation Calendar" in the Web UI (scheduled automations, notes, todos) is separate. It is for VAF-internal scheduling only. This document describes **external** calendars (Google/Microsoft).

## Architecture

- **OAuth**: Extended in `vaf/core/oauth_pkce.py`: Gmail and Microsoft providers request mail + calendar scopes. Redirect URI is unchanged (`/api/email/oauth/callback`). Tokens are stored in the same credential store as email (`vaf/core/credential_store.py`).
- **Calendar client**: `vaf/core/calendar_client.py` uses `get_valid_access_token(account_id, provider, user_scope_id=...)` from `oauth_pkce` and calls:
  - **Google**: Calendar API v3 (`https://www.googleapis.com/calendar/v3/calendars/.../events`).
  - **Microsoft**: Graph API (`https://graph.microsoft.com/v1.0/me/calendar/calendarView` for list, `/me/calendar/events` for create, etc.).
- **Account selection**: Which account to use is taken from `email_config` / `email_config_by_scope` (same as email). The first connected Gmail or Microsoft account per user is used if the user does not specify `provider` or `account_id` in the tool.

## Configuration

- **No new config keys.** Calendar uses existing Email OAuth client IDs and secrets (`email_oauth_google_client_id`, `email_oauth_microsoft_client_id`, etc.). See [CONNECTIONS.md](CONNECTIONS.md) for how to register OAuth apps.
- **Enable Calendar in the OAuth app**:
  - **Google Cloud Console**: Enable the "Google Calendar API" for your project. The same redirect URI as email is used (`/api/email/oauth/callback`).
  - **Microsoft Azure**: The scope `Calendars.ReadWrite` is requested by VAF; ensure your app has the corresponding permission.
- **Re-authorization**: If you added calendar scopes after users already connected Gmail/Outlook, those users must reconnect (Settings → Connections → Email → remove and re-add the account) so the new scopes are granted.

## Agent Tools

| Tool | Description |
|------|-------------|
| `list_calendar_events` | List events in a time range. Parameters: optional `time_min`, `time_max` (ISO8601 or YYYY-MM-DD; default: now to 7 days ahead), optional `provider`, `account_id`, `calendar_id`, `max_results`. |
| `create_calendar_event` | Create an event. Required: `summary`, `start`, `end`. Optional: `description`, `provider`, `account_id`, `calendar_id`, `reminder_minutes`. |
| `update_calendar_event` | Update an event. Required: `event_id`. Optional: `summary`, `start`, `end`, `description`, `provider`, `account_id`, `calendar_id`. |
| `delete_calendar_event` | Delete an event. Required: `event_id`. Optional: `provider`, `account_id`, `calendar_id`. |

All tools receive `user_scope_id` and `username` from the agent (injected by the backend) so calendar access is always per-user.

## REST API (Web UI)

- **POST /api/calendar/ensure-daily-check-automation**: Idempotent. If the current user has a calendar connected and no automation named "Daily calendar check" exists, creates one (daily 08:00, default prompt). Returns `{ "ok": true, "created": true|false, "task_id"?: "..." }`. The frontend calls this when calendar status is connected and when the user opens the Automation calendar (footer button or Settings → Automations), so the Daily calendar check appears in the list even if Settings was not opened first.
- **GET /api/calendar/status**: Returns `{ "google_available": true/false, "microsoft_available": true/false }` for the current user. Status is derived from the **same** email config as the email API (`_get_email_config` from `email_routes`), so if Gmail/Outlook is connected for Email, the calendar is shown as connected. No token is used; it only reads config. Each call is logged to `logs/backend.log` with `[CALENDAR]` (always, not only when debug logs are enabled) for diagnostics.
- **GET /api/calendar/events**: Lists events for the current user. Query parameters `time_min` and `time_max` are optional (ISO8601 or YYYY-MM-DD); defaults are now to 7 days ahead. Uses the first connected Gmail or Microsoft account. The route depends on `Request` being injected for auth; the handler must use `request: Request` in the dependency so FastAPI injects it correctly (otherwise the API returns 422 "Field required").

## Web UI (Settings → Connections)

- **Calendar cards**: Google Calendar and Microsoft Outlook appear in the **Calendar** category. "Connected" state is determined by the calendar status API and by a **fallback**: if the status API has not been called or fails, the UI also treats the calendar as connected when the same provider (Gmail/Outlook) appears in the email accounts list from `GET /api/email/accounts`. Refreshing the connections panel (e.g. after closing the Email or Calendar setup wizard) triggers a refetch of both email accounts and calendar status.
- **Calendar Dashboard**: When a calendar is connected, clicking the settings (gear) icon on the Google Calendar or Microsoft Outlook card opens the **Calendar Dashboard** — a large modal similar to Mail and Cloud dashboards:
  - **Left sidebar**: List of connected calendar-capable accounts (Gmail/Outlook from email config), each with a link to open **Google Calendar** or **Outlook Calendar** in the browser. Option to add another account via Email.
  - **Main area**: Upcoming events from `GET /api/calendar/events` with a selectable range (next 7, 14, 30, or 60 days). Each event shows summary, start/end, optional description snippet, and a link to open the event in the provider’s calendar. Refresh button to reload events.
- **Calendar Setup Wizard**: If the calendar is not yet connected, the Connect button opens the Calendar Setup Wizard (intro + "Sign in with Google" / "Sign in with Microsoft"), which uses the Email OAuth flow. After signing in, the user can refresh status; closing the wizard or the Email "Manage your accounts" modal refreshes the connections panel so the calendar shows as connected when applicable.
- **Daily calendar check (automatic)**: When a calendar is connected, the frontend calls **POST /api/calendar/ensure-daily-check-automation** (on opening the Automation calendar or when calendar is connected in Settings). If the user does not already have a "Daily calendar check" automation, one is created with the current user's `user_scope_id`; it appears in **Settings → Automations** and in the Automation popup (and in `vaf automation list`). It runs daily at 08:00 by default; the user can change the time or disable it. The automation runs in the **task owner's scope** (see [AUTOMATIONS.md](AUTOMATIONS.md)), so calendar, memory, and messaging use that user's data. The prompt instructs the agent to: fetch the next 24–48 hours of events, analyze importance, and for important events either create one-off reminder automations (e.g. 30 minutes before via `create_automation`) or send an immediate reminder/prep via the user's main_messenger (e.g. `send_telegram`, `send_whatsapp`, `send_discord`, `send_slack`, or `send_mail` depending on User Identity). The agent is allowed to call `create_automation` from within this run so it can schedule reminders. See [AUTOMATIONS.md](AUTOMATIONS.md).

## Tool Router

The Tool Router in `vaf/core/agent.py` includes calendar heuristics so the agent can read, create, update, and delete calendar events reliably:

- **List & create:** When the user message contains words such as "calendar", "kalender", "event", "termin", "meeting", "reminder", "erinnerung", "appointment", "verabredung", "schedule", "termine", "was steht an", "upcoming", "meine termine", the router adds `list_calendar_events` and `create_calendar_event`.
- **Update:** For "termin ändern", "termin verschieben", "event update", "termin updaten", "meeting verschieben", "appointment change", "reschedule", the router adds `update_calendar_event`.
- **Delete:** For "termin löschen", "termin absagen", "event löschen", "event delete", "termin entfernen", "meeting absagen", "appointment cancel", the router adds `delete_calendar_event`.

## Implementation notes

- **Backend**: `vaf/api/calendar_routes.py` — calendar status and events endpoints. The dependency `_get_current_user(request: Request)` must declare `Request` so FastAPI injects it; otherwise the events endpoint returns 422 "Field required".
- **Frontend**: `web/components/connections/CalendarDashboard.tsx` — dashboard modal; `ConnectionsPanel.tsx` — calendar cards and open-dashboard/open-wizard behavior; i18n keys under `settings.calendar` (dashboard titles, links, range labels, etc.).

## Related

- [CONNECTIONS.md](CONNECTIONS.md) – Calendar section (Google Calendar, Microsoft Outlook) and OAuth setup.
- [USER_ISOLATION.md](USER_ISOLATION.md) – User-scoped credentials and data; calendar uses the same model as email.
- [WEB_UI.md](WEB_UI.md) – Settings → Connections; Calendar category and Calendar Dashboard.
