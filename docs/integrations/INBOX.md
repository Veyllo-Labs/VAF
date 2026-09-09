# The inbox: one list of conversations across every channel

The inbox answers one question for the person and for the agent: who wrote, when, and
does somebody wait for me. It lists every conversation of a user across WhatsApp, Telegram
and Discord (groups included), the mail threads of the mail store, and the A2A rooms, newest
first, with the same four states everywhere. The rows are built once, in
`vaf/core/inbox.py`, and every surface reads them: the agent's tool, the command line, the
routes behind the Posteingang window, and the per-channel windows' own lists. The rules for
"unread", "waits for you" and "done" live here, not in a browser: a window only clears the
pill and the chip of the row it just opened until the next fetch confirms it.

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
| `waits`, `waits_reason` | `unanswered` (the last word is the other side's, nobody answered, the text asks for an answer, and the person has not opened the conversation since; mail: and the sender is somebody who reads one), `owner_asked` (the agent asked the person about this chat and the person has not opened it since), `invitation` (a room waits for the person's answer). Reading takes a conversation off "waits": the person read it and decides for themselves whether to answer |
| `answered_by_agent` | the newest message is the agent's own send (mail: the newest message carries the answered mark; an older reply in the thread says nothing about the mail that arrived after it) |
| `done` | marked done and nothing newer arrived, or the newest message is the person's own reply (mail: the newest message sits in the Sent folder). A newer message reopens |
| `is_group` | WhatsApp `@g.us`, a negative Telegram id, every room |
| `mode` | which lane answers: `owner`, `contact` (Front Office), `conversation` (WhatsApp reply window open), `readonly`, `needs_assign` (an unresolved WhatsApp `@lid`), `admin` (Discord), `relay` (Telegram), `mail`, `room` |
| `reply_window_until` | the WhatsApp reply window, computed from the store with the bridge's rule (a test pins that the two agree) |
| `bulk` | mail only: the thread is bulk mail (`is_bulk_mail`, see the rules), listed only when the bulk toggle asks |
| `can_compose` | WhatsApp only: the person may write themselves where the agent does not answer, a read-only chat or every chat once the channel switch (`inbound_to_agent`) is off (the WhatsApp window's rule) |
| `session_id`, `jump` | what the agent session and the channel window need to land on this conversation |

## The rules

The rules are pure functions in `vaf/core/inbox.py`, each pinned by a test: `chat_state`
(messenger), `mail_thread_state`, `room_state`, `chat_mode`, `is_group`,
`reply_window_until`, `reply_expectation`. The agent's reply lifts "waits" but does not
close a row: the person may still want to see what was said in their name. The agent's
question to the person (`owner_asked`) is answered by the person, or by the agent writing
to the contact again, or lifted by the person opening the chat.

**Does the last message ask for an answer?** A "danke", a "bis später" or a thumbs-up
waits for nobody, and no model is asked to tell. `reply_expectation(text)` scores the
newest inbound message from its text alone, 0 to 1. Links and quoted speech are stripped
first (a link's own "?" is not a question, a question quoted from somebody else asks
nothing of the reader), and an automatic reply, an order confirmation, a verification code
or a list footer scores 0 outright. Then a question mark in any script, the two emoji
marks included (+0.4), a clause that opens like a question or a request after a greeting
or a filler ("wann", "kannst du", "soll ich", "und du", "could you"), a request anywhere
("bitte", "let me know", "melde dich", "schick mir") or an objection ("aber", "but") raise
it (+0.2); a message that is, or begins with, a thank-you, goodbye, acknowledgement or
deferral lowers it (-0.5, only -0.2 when a request follows it, "danke, schick mir bitte die
Adresse" is a request with a polite opening); a closer inside a message of up to twelve
words lowers it a little (-0.3); an explicit "nothing to answer" marker ("FYI", "nur zur
Info", "no action needed") lowers it by 0.5; emoji and digits are not words, and an
emoji-only message counts as none (-0.4); a longer message nudges up (+0.1 above four
words, +0.15 above twelve). A question mark outweighs a closer ("ok?" asks); "wie
besprochen"-type idioms are not question words; a greeting ("Guten Abend, ...") is skipped
so that what follows it decides, and an addressee after it ("Hallo Max, danke dir") is told
by its capital letter; words like "klar", "genau" or "ja" are closers only as the whole
message or its beginning, a short confirmation ("10 Uhr passt", "Freitag geht bei mir") and
a goodbye with a day in it at the very end ("bis Freitag" elsewhere is a deadline) count as
a closer inside, a deferral ("ich sag dir morgen Bescheid", "kann ich dir morgen sagen")
counts as a closer (-0.5, -0.2 when a request follows it), a chat filler ("ja", "ok",
"danke") in front of a question or a request is no closer at all ("ja und du" asks), a
clause that opens like a question ("kommst du", "schaffst du das", "und dir", "ja oder
nein"), an objection after something ("danke, aber wo genau"; "aber gerne" answers) or a
problem report ("der Link funktioniert nicht") counts as a request, a salutation to a
class of people at the very start ("Dear DeepSeek API user,", "Liebe Kundin, lieber Kunde,")
marks a mass mail, and a template phrase (an order confirmation, "your verification
code", a list footer) marks an automatic text unless a question mark says a person is
asking about it. The
lists are German and English with the thanks and goodbyes a German chat borrows (French,
Italian, Turkish, Spanish, Japanese, Chinese), each pinned by a test table. A plain greeting or
statement lands at 0.6 and waits; the configured `inbox_waits_threshold` (default 0.6, not
in the UI, see [CONFIG_SCHEMA.md](../setup/CONFIG_SCHEMA.md)) decides: lower it and more
chats wait, raise it and fewer do. Mail threads run the newest message's snippet through
the same rule, and a mail from something that reads no answer never waits at all
(`is_automated_sender`: a no-reply, do-not-reply or notification address, a newsletter, a
mailer daemon, a status page, by the address's local part or its display name, and any
message the sync filed under a non-primary Gmail category such as promotions or updates);
rooms wait on unread frames and invitations only.

**Bulk mail is not inbox material.** The mail lane lists primary mail only unless asked:
a thread in the Junk folder (the provider's or the person's own placement, which outranks
any tab stamp), a thread whose newest message carries a bulk category (the provider's
tab, promotions or social or updates or forums, the person's own label from the mail
client's relabel picker, or a sender rule learned from one), and, when no category was set
at all, a thread from a sender who reads no answer (`is_automated_sender`) stay out of the
list and its counts (`is_bulk_mail`; `stored_per_channel` still counts them and
`bulk_hidden` says how many the listing dropped, which the tool and the command line
repeat). A thread filed under primary, or under a label of the person's own, is never
bulk, whatever its sender: the person's or the provider's word wins over the heuristic
(a sender rule's answer is stored even when it says primary, so the rule reaches every
later mail of that sender on any provider). The
toggle "Show bulk mail" in the window, `include_bulk` on
`list_conversations` and on the agent's tool, `--bulk` on the command line and `bulk` on
the routes show them, and "mark all as read" follows the same toggle.

## The marks

The person's own state per messenger chat lives next to the messages, in
`channel_message_store.chat_marks` (`seen_ts`, `done_ts`, `owner_asked_ts`, keyed on
username, channel and chat id). On the day the marks table arrives every stored chat counts
as read (its seen mark is seeded to its newest message), so it waits only from its next
message on. Mail keeps IMAP's Seen flag as its read marker and takes the
done mark from the same table (`channel='mail'`, the thread id); a room keeps its cursor as
the read marker and takes the done mark the same way. `mark_conversation` writes them:
`seen` on a mail thread marks every unseen message of the thread read (local first, the
mail window's own rule moved server-side); `seen` on a room moves the person's cursor to
the newest frame (`Room.mark_read`, as the room view does when it is shown, and announces
`rooms_changed` when it moved; an invitation moves nothing, it is read by answering it; a
room that is not the person's answers as unknown, for seen and done alike). The owner-asked mark is written by the agent itself: in Front Office
mode a send tool that names no foreign recipient reached the owner, and `Agent._chat_post_dispatch`
records it on the chat the runner stamped for the turn (`agent._front_office_chat`).
The inbox lists a mapped WhatsApp `@lid` as its own row, while the WhatsApp window folds it
into its number and marks both store keys when that merged row is opened; a row opened in
the inbox marks its own key alone. "Mark all as read" is `mark_all_seen(username, scope,
channels=, include_groups=)`: the messenger chats of each lane through the store's
`mark_channel_seen` (one transaction over the whole channel, a seed of the marks rows and
then one channel-wide update, no id list and so no cap, one announce, a marker never
moves backwards; only the chats the listing shows as unread or waiting on the agent's
unanswered question are touched, so the count is exact),
the unread threads of the mail lane through the same per-thread seen as a single row (the
newest 200 threads, the lane's reach, one mail service for the call, one flags op per
message for the writeback as the mail window's own read marking does), and the person's
cursor of every unread room moved to its newest frame through `Room.mark_read`, the one
primitive the room view uses when it is shown (an invitation waits for a decision, not
for reading, and stays). Group chats and rooms follow the group toggle. It returns how
many conversations were read per channel (a messenger or mail lane without a store, and
Discord for anybody but the local admin, are absent; rooms are present whenever wanted);
the done and owner-asked marks are left alone.

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
`include_done`, `include_bulk` (promotions, social, newsletters, notifications and junk
mail are hidden unless asked), and for the mail lane `account_id` and `folder` (they narrow
the lane at the source, before the counts and the cut to `max_chats`). The output leads with the
next-step hint (read one conversation with the per-channel read tools or `read_mail`,
search mail with `find_mail`, never call `inbox` again for the same request), then the
counts and one line per conversation, then the "IDs by index" block `read_mail` needs; mail
rows pass the same phishing filter the mail tools apply. The Telegram and Discord indexes
are re-projected from the session files before listing, as their tools did; nothing waits on
a bridge. The tool is not in the Front Office allow-list.

## Command line

`vaf inbox list` prints the same rows as a table (When, Channel, Name, Unread, Waits, Mode,
Preview) with the counts above it; `--channel`, `--view`, `--limit`, `--no-groups`, `--done`,
`--bulk`, `--query` narrow it and `--json` prints one object per line. It runs as the machine owner
(no `--scope`: the CLI has no authentication) behind the same terminal door as `vaf session`,
because it prints chats. Read-only by design: the terminal prints, it does not read for the
person. The seen mark is written where the person reads (opening a conversation in a window)
or where they say they have read everything (the window's "mark all as read",
`mark_all_seen`), and the done mark has no button. A `vaf inbox read` would be one call to
that same primitive, so the two surfaces could not disagree; it is left out until a headless
install asks for it, which is the measurement that earns the command.

## Routes

`vaf/api/inbox_routes.py` serves the window and the footer badge as the caller
`contact_routes.get_current_vaf_user` resolves it (the request's user, or the local admin
outside network mode); every store read runs off the event loop, and no GET waits on a bridge.

| Route | Answer |
|---|---|
| `GET /api/inbox?channel&view&groups&done&bulk&q&limit` | `rows`, `counts` and `channels` as `list_conversations` returns them (`channel` is one name, a comma list or `all`; `bulk` shows the mail lane's bulk mail), plus `status` per channel: WhatsApp `linked` and `running`, Telegram and Discord `configured` and `running` (Discord for the local admin only), mail `accounts` and `last_sync_at`. What the process knows about itself, never a round trip |
| `GET /api/inbox/summary?groups&done&bulk` | the whole inbox's `counts` (`all`, `waits`, `unread`, `agent`, `per_channel`, `waits_per_channel`, `unread_per_channel`, `invitations`, `stored_per_channel`) under the group, done and bulk toggles, never narrowed by a channel, a view or a query: the footer badge reads `waits` and `unread`, the inbox window's rail reads every number |
| `GET /api/inbox/history?channel&id&limit` | one conversation in the pane shape (`role`, `content`, `timestamp`, `content_type`, `sender`) |
| `POST /api/inbox/marks` with `{channel, id, seen?, done?}` | `mark_conversation`; `done` is the primitive without a button; a room's `seen` moves the person's cursor (an invitation's moves nothing); a body that marks nothing, a room that is not the person's and a Discord mark from anybody but the local admin answer 400 |
| `POST /api/inbox/marks/all` with `{channels?, groups?, bulk?}` | `mark_all_seen`: every conversation of the named channels (a list, a comma list or `all`, the default, and an empty string means the default; an empty list and an unknown name are a 400), group chats and rooms included unless `groups` is false, bulk mail only when `bulk` is true (the strings false, 0, no and off read as false), counts as read; answers `moved` per channel (a messenger or mail lane without a store, and Discord for anybody but the local admin, are absent, no 400); the messenger stores announce `inbox_changed` themselves, the room lane `rooms_changed` when a cursor moved, and the route `inbox_changed` once more for the mail lane |

There is no send route and no compose box: writing happens in the channel window, which
the draft jump opens.

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
mark per store key behind the row, so an `@lid` merged into its number is read too), and
the pill and the "waits for you" chip go out at once: reading takes a chat off "waits",
because the person has read it and decides for themselves whether to answer (the
predicates above say the same server-side, so the count in the footer and the "N waiting
for you" button drop with it). The conversation header keeps its amber sentence until the
window next fetches (the channel windows fetch on open, on refresh and after an action,
not on the signal), so the reader still sees why it was flagged; the inbox window keeps it
while the row is open. The mail window shows the same
chip on its thread rows and the same header button, and opening a thread clears both. The shell
exports the pieces the inbox window reads as well (the bubbles, the history hook, the
chips) and the compose box the WhatsApp window uses, so nothing is copied a fourth time.

## The window

The inbox window (`web/components/inbox/InboxWindow.tsx`) is the fourth row of the sidebar
footer, between the calendar and the logs, with a badge: the amber count of conversations
that wait for the person, or a red dot when something is unread and nobody waits. Its
three panes are the rail (the four views with their counts, the five channels with their
counts and an amber number where somebody waits, the group, done and bulk toggles, the channel
status lines; the rail's numbers describe the whole inbox from the summary route, whatever
one channel the list is narrowed to and whatever the search box says, because a rail that
follows the search cannot show where else somebody waits; they follow the signal, so they
drop a moment after the chips do), the list (a search over every channel, one row per conversation with the
channel square on the avatar, the kind tag for groups and rooms, the preview with who said
it, and the chip line: unread, waits, agent answered, done, and the lane that answers), and
the preview (the conversation in the shell's bubbles, the amber note when the agent asked
the person or a room waits for an invitation answer, and the actions). The reason is said
in one sentence, from `waits_reason` and the name: "The last message came from Alice, still
unanswered", "The agent asked you a question about this chat", "This room waits for your
answer to the invitation"; the channel windows say the same in their conversation header.
"Open in the channel window" closes the inbox and opens Settings on Connections with a jump into the WhatsApp,
Telegram or Discord window or the mail client (a repeat jump to the same chat fires again,
because the page hands the jump in once and resets it when Settings consumed it); a room
opens in the sidebar. There is no "done" button and no per-row "read" button: opening a row
reads it, and a read row no longer waits, so in the "waits for you" view it leaves the list while its
conversation stays open in the preview (the window keeps the opened row, and the amber
sentence that said why it waited, until another row is chosen). The done mark stays a
primitive of the marks route and `mark_conversation` without a button; the "show done"
toggle shows the conversations the person answered last. "Mark all as read" in the window's
header reads every conversation of the selected channel (or of all of them) at once, group
chats and rooms as the group toggle says, through `POST /api/inbox/marks/all`; the view and
the search do not narrow it. It is disabled while the selection holds nothing a read can
clear (an invitation waits for a decision, so it does not count; the summary's
`unread_per_channel`, `waits_per_channel` and `invitations` tell). The channel windows carry
the same button for their own channel ("All read"). "Write a draft" jumps with the
draft flag and the Composer starts writing: the WhatsApp window selects the chat and runs
the Composer's draft (the person watches the draft land in the compose box and sends or
rewrites it there), the mail client opens the thread's reply composer and runs the Mail
Composer's draft. The inbox itself has no input field: it is the place to read and to
decide, writing happens in the channel window with the Composer beside it (one compose
box per channel, not a second one in the inbox). Opening a row posts its seen mark like the channel windows do, a room row's too (its cursor moves as the room view moves it; an invitation row posts nothing and keeps its chip, it is read by answering it). The window refetches
on `inbox_changed` and `rooms_changed` (debounced 400 ms), never on a timer; the footer badge
reads `GET /api/inbox/summary` on the same signal. Escape closes a running search first (66),
steps back from the preview to the list on a phone (67), and closes the window last (65).
On a phone the rail becomes a chip strip and the list and the preview stack, one at a time,
with a back button; the desktop markup is unchanged.

## API (module)

- `list_conversations(username, user_scope_id, *, channels=None, view="all", include_groups=True, include_done=False, query="", limit=200, now=None, mail_account_id=None, mail_folder=None, include_bulk=False)` returns `{rows, counts, channels}`; `view` is one of `all`, `waits`, `unread`, `agent`; the group, done and bulk toggles apply before the counts, the view after them; `query` keeps rows whose name or preview contain it or whose stored messages match (`search_hits`); `mail_account_id` and `mail_folder` narrow the mail lane at the source; the mail lane always reads its newest 200 threads, whatever `limit` says, so the counts cover them. `counts` carries `all`, `waits`, `unread`, `agent`, `per_channel`, `waits_per_channel`, `unread_per_channel`, `invitations` and `bulk_hidden` (after the toggles, before the view) and `stored_per_channel` (what each lane holds before any toggle, filter or cut; the tool says "nothing stored" from that number alone).
- `mark_conversation(username, user_scope_id, channel, id, *, seen=False, done=None)`.
- `mark_all_seen(username, user_scope_id, *, channels=None, include_groups=True, include_bulk=False, now=None)` returns `{channel: read}` for the lanes it touched (a messenger lane without a store, and Discord for anybody but the local admin, are absent).
- `conversation_history(username, user_scope_id, channel, id, limit=200)` in the channel windows' pane shape (`role`, `content`, `timestamp`, `content_type`, `sender`).
- `search_hits(username, user_scope_id, query, channels)`.

The facade exports nothing of this on purpose: every consumer is first-party, and the first
embedder who asks for a cross-channel inbox is the measurement that earns an export.
