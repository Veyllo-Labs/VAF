# The inbox: one list of conversations across every channel

The inbox answers one question for the person and for the agent: who wrote, when, and
does somebody wait for me. It lists every conversation of a user across WhatsApp, Telegram
and Discord (groups included), the mail threads of the mail store, and the A2A rooms, newest
first, with the same four states everywhere. The rows are built once, in
`vaf/core/inbox.py`, and every surface reads them: the agent's tool, the command line, the
routes behind the Posteingang window, and the per-channel windows' own lists. Nothing about
"unread", "waits for you" or "done" is computed in a browser.

## The row

One shape for five sources:

| Field | Meaning |
|---|---|
| `key` | `<channel>:<id>` |
| `channel` | `whatsapp`, `telegram`, `discord`, `mail`, `room` |
| `id` | the store's chat id, the mail thread id, or the room id |
| `name`, `preview`, `preview_from` | who and what was said last (`them`, `agent`, `you`, or a room member's label) |
| `last_ts`, `message_count` | the newest message and the store's own count (tombstones excluded), one meaning on every surface |
| `unread` | messenger: inbound messages after the person last opened the chat; mail: IMAP's unseen count; room: the person's own reading position |
| `waits`, `waits_reason` | `unanswered` (the last word is the other side's and nobody answered), `owner_asked` (the agent asked the person about this chat), `invitation` (a room waits for the person's answer) |
| `answered_by_agent` | the newest message is the agent's own send (mail: the newest message carries the answered mark; an older reply in the thread says nothing about the mail that arrived after it) |
| `done` | marked done and nothing newer arrived, or the newest message is the person's own reply (mail: the newest message sits in the Sent folder). A newer message reopens |
| `is_group` | WhatsApp `@g.us`, a negative Telegram id, every room |
| `mode` | which lane answers: `owner`, `contact` (Front Office), `conversation` (WhatsApp reply window open), `readonly`, `needs_assign` (an unresolved WhatsApp `@lid`), `admin` (Discord), `relay` (Telegram), `mail`, `room` |
| `reply_window_until` | the WhatsApp reply window, computed from the store with the bridge's rule (a test pins that the two agree) |
| `can_compose` | WhatsApp only: the person may write themselves where the agent does not answer (the WhatsApp window's rule) |
| `session_id`, `jump` | what the agent session and the channel window need to land on this conversation |

## The rules

The rules are pure functions in `vaf/core/inbox.py`, each pinned by a test: `chat_state`
(messenger), `mail_thread_state`, `room_state`, `chat_mode`, `is_group`,
`reply_window_until`. The agent's reply lifts "waits" but does not close a row: the person
may still want to see what was said in their name. The agent's question to the person
(`owner_asked`) is answered by the person, or by the agent writing to the contact again.

## The marks

The person's own state per messenger chat lives next to the messages, in
`channel_message_store.chat_marks` (`seen_ts`, `done_ts`, `owner_asked_ts`, keyed on
username, channel and chat id). Mail keeps IMAP's Seen flag as its read marker and takes the
done mark from the same table (`channel='mail'`, the thread id); a room keeps its cursor as
the read marker and takes the done mark the same way. `mark_conversation` writes them:
`seen` on a mail thread marks every unseen message of the thread read (local first, the
mail window's own rule moved server-side); `seen` on a room is refused, because opening the
room moves the cursor. The owner-asked mark is written by the agent itself: in Front Office
mode a send tool that names no foreign recipient reached the owner, and `Agent._chat_post_dispatch`
records it on the chat the runner stamped for the turn (`agent._front_office_chat`).

## Identity

Messenger rows are read as `contacts_store.message_channel_username` spells the store's
username; the Discord lane exists only for the local admin (the bridge writes every row under
`admin` with no scope); the mail lane only when `mail_utils.mail_v2_active` and the store
exists; rooms through `session._room_rows`. Listing is store-only and never waits on a bridge:
a chat the store never saw has nothing to say about unread or waiting.

## Live refresh

Every writer of the message store, and every mark, announces `inbox_changed` to the person's
browsers (`web_interface.notify_inbox_changed`, the calendar signal's twin), throttled per
scope with a trailing edge so a history sync tells them once and once more at the end. The
mail sync supervisor announces it after a sync that saw new mail, flag updates or vanished
messages (its `on_change` observer, registered by the web server), the mail window's
read-flag change announces it, and a mail thread's `seen` through the marks route announces
it itself, because IMAP's flag is no store writer. The `rooms_changed` signal refreshes the
inbox as well.

## The agent's tool

`inbox` is the agent's view of the same rows and replaced the four per-channel listings
(`whatsapp_inbox`, `telegram_inbox`, `discord_inbox`, `mail_inbox`). Parameters: `channel`
(one of the five, or `all`), `view` (`all`, `waits`, `unread`, `agent`), `max_chats`
(1-200, default 30; the user's number is passed as is), `query`, `include_groups`,
`include_done`, and for the mail lane `account_id` and `folder` (they narrow the lane at the
source, before the counts and the cut to `max_chats`). The output leads with the
next-step hint (read one conversation with the per-channel read tools or `read_mail`,
search mail with `find_mail`, never call `inbox` again for the same request), then the
counts and one line per conversation, then the "IDs by index" block `read_mail` needs; mail
rows pass the same phishing filter the mail tools apply. The Telegram and Discord indexes
are re-projected from the session files before listing, as their tools did; nothing waits on
a bridge. The tool is not in the Front Office allow-list.

## Command line

`vaf inbox list` prints the same rows as a table (When, Channel, Name, Unread, Waits, Mode,
Preview) with the counts above it; `--channel`, `--view`, `--limit`, `--no-groups`, `--done`,
`--query` narrow it and `--json` prints one object per line. It runs as the machine owner
(no `--scope`: the CLI has no authentication) behind the same terminal door as `vaf session`,
because it prints chats. Read-only by design: marks are set in the inbox window, where the
person sees what they are closing.

## Routes

`vaf/api/inbox_routes.py` serves the window and the footer badge as the caller
`contact_routes.get_current_vaf_user` resolves it (the request's user, or the local admin
outside network mode); every store read runs off the event loop, and no GET waits on a bridge.

| Route | Answer |
|---|---|
| `GET /api/inbox?channel&view&groups&done&q&limit` | `rows`, `counts` and `channels` as `list_conversations` returns them (`channel` is one name, a comma list or `all`), plus `status` per channel: WhatsApp `linked` and `running`, Telegram and Discord `configured` and `running` (Discord for the local admin only), mail `accounts` and `last_sync_at`. What the process knows about itself, never a round trip |
| `GET /api/inbox/summary` | `waits`, `unread`, `all` and `waits_per_channel` for the footer badge and the channel windows' own buttons |
| `GET /api/inbox/history?channel&id&limit` | one conversation in the pane shape (`role`, `content`, `timestamp`, `content_type`, `sender`) |
| `POST /api/inbox/marks` with `{channel, id, seen?, done?}` | `mark_conversation`; a room's `seen` answers 400, and so does a body that marks nothing |

There is no send route: a WhatsApp row posts to `POST /api/whatsapp/send`, as the WhatsApp
window does.

## The channel windows

The WhatsApp, Telegram and Discord dashboards (`GET /api/<channel>/dashboard`) read the same
overview: every chat the message store holds is a session, its `message_count` is the store's
count, and each session carries `last_preview`, `preview_from`, `unread`, `waits`,
`waits_reason`, `answered_by_agent` and `done` from `chat_state`. The WhatsApp window's
Conversation badge and `reply_window_until` come from the same rows through
`reply_window_until`, so the dashboard asks the bridge for the chat list and the names only.
The Telegram activity log only seeds a chat the store never saw and feeds the chart; the
Discord payload carries `sessions` (the admin's store rows). `GET /api/mail/threads` rows carry
`waits`, `waits_reason`, `done` and `answered_by_agent` from `mail_thread_state` and the done
marks, so the mail window and the Posteingang never disagree about who waits.

In the windows themselves (`web/components/connections/ChannelDashboardShell.tsx`, the
shell WhatsApp, Telegram and Discord are built on), every row carries a chip line under the
preview: the unread pill (the mail window's red pill, the one unread token on every
surface), then "Waits for you" (amber; its tooltip says when the agent asked the person),
"Agent answered" (green) or "Done" (quiet). The list header's right side turns into an
"N waiting for you" button that selects the next waiting chat, round and round; the
conversation header repeats the chip and says in one amber line when the agent asked the
person about this chat. Opening a chat posts its seen mark (`POST /api/inbox/marks`, one
mark per store key behind the row, so an `@lid` merged into its number is read too), the
pill goes out at once, and "waits" stays, because reading is not answering. The mail
window shows the same chip on its thread rows and the same header button. The shell
exports the pieces the inbox window reads as well (the bubbles, the history hook, the
compose box, the chips), so nothing is copied a fourth time.

## The window

The inbox window (`web/components/inbox/InboxWindow.tsx`) is the fourth row of the sidebar
footer, between the calendar and the logs, with a badge: the amber count of conversations
that wait for the person, or a red dot when something is unread and nobody waits. Its
three panes are the rail (the four views with their counts, the five channels with their
counts and an amber number where somebody waits, the group and done toggles, the channel
status lines), the list (a search over every channel, one row per conversation with the
channel square on the avatar, the kind tag for groups and rooms, the preview with who said
it, and the chip line: unread, waits, agent answered, done, and the lane that answers), and
the preview (the conversation in the shell's bubbles, the amber note when the agent asked
the person or a room waits for an invitation answer, and the actions). "Open in the channel
window" closes the inbox and opens Settings on Connections with a jump into the WhatsApp,
Telegram or Discord window or the mail client (a repeat jump to the same chat fires again,
because the page hands the jump in once and resets it when Settings consumed it); a room
opens in the sidebar. "Done" and "Reopen" write the done mark. "Write a draft" jumps with the
draft flag: the WhatsApp window puts the cursor into the Composer's instruction field, the
mail client opens the thread and its reply composer. The compose box is offered for WhatsApp rows the person writes
in themselves (`can_compose`, the WhatsApp window's rule) and posts to the WhatsApp send
route; the other channels have no owner send route yet, which is a named boundary, not an
omission. Opening a row posts its seen mark like the channel windows do. The window refetches
on `inbox_changed` and `rooms_changed` (debounced 400 ms), never on a timer; the footer badge
reads `GET /api/inbox/summary` on the same signal. Escape closes a running search first (66),
steps back from the preview to the list on a phone (67), and closes the window last (65).
On a phone the rail becomes a chip strip and the list and the preview stack, one at a time,
with a back button; the desktop markup is unchanged.

## API (module)

- `list_conversations(username, user_scope_id, *, channels=None, view="all", include_groups=True, include_done=False, query="", limit=200, now=None, mail_account_id=None, mail_folder=None)` returns `{rows, counts, channels}`; `view` is one of `all`, `waits`, `unread`, `agent`; the group and done toggles apply before the counts, the view after them; `query` keeps rows whose name or preview contain it or whose stored messages match (`search_hits`); `mail_account_id` and `mail_folder` narrow the mail lane at the source.
- `mark_conversation(username, user_scope_id, channel, id, *, seen=False, done=None)`.
- `conversation_history(username, user_scope_id, channel, id, limit=200)` in the channel windows' pane shape (`role`, `content`, `timestamp`, `content_type`, `sender`).
- `search_hits(username, user_scope_id, query, channels)`.

The facade exports nothing of this on purpose: every consumer is first-party, and the first
embedder who asks for a cross-channel inbox is the measurement that earns an export.
