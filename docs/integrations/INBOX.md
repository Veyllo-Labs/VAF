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
| `answered_by_agent` | the newest message is the agent's own send (mail: the newest message carries the answered mark) |
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
`rooms_changed` signal refreshes the inbox as well.

## API (module)

- `list_conversations(username, user_scope_id, *, channels=None, view="all", include_groups=True, include_done=False, query="", limit=200, now=None)` returns `{rows, counts, channels}`; `view` is one of `all`, `waits`, `unread`, `agent`; the group and done toggles apply before the counts, the view after them; `query` keeps rows whose name or preview contain it or whose stored messages match (`search_hits`).
- `mark_conversation(username, user_scope_id, channel, id, *, seen=False, done=None)`.
- `conversation_history(username, user_scope_id, channel, id, limit=200)` in the channel windows' pane shape (`role`, `content`, `timestamp`, `content_type`, `sender`).
- `search_hits(username, user_scope_id, query, channels)`.

The facade exports nothing of this on purpose: every consumer is first-party, and the first
embedder who asks for a cross-channel inbox is the measurement that earns an export.
