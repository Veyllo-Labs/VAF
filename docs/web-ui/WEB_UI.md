# Web UI Documentation

## Overview

The VAF Web UI provides a browser-based interface for interacting with the Veyllo Agentic Framework. It offers real-time communication, session management, and visual feedback for agent operations.

### 3. System Tray (New)
The Web UI can be launched directly from the **VAF System Tray App** (Menu -> Open WebUI). The server runs in the background.

## Architecture

### Backend (Python)

**Location**: `vaf/core/web_server.py`, `vaf/core/web_interface.py`

The backend consists of two main components:

#### 1. FastAPI Server (`web_server.py`)

- **Framework**: FastAPI with Uvicorn
- **Port**: 8001 (default)
- **Protocol**: WebSocket + REST
- **CORS**: Enabled for local development

**Endpoints**:
- `GET /`: Health check endpoint (includes the running `version`)
- `GET /api/version`: The running VAF version (source of truth: `vaf/version.py`)
- `WebSocket /ws`: Real-time bidirectional communication

#### 2. Interface Manager (`web_interface.py`)

Singleton pattern manager that:
- Maintains WebSocket connections
- Manages message queues between CLI and Web UI
- Broadcasts updates to all connected clients
- Handles thread-safe communication between main thread (CLI) and server thread

**Key Methods**:
- `connect(websocket)`: Accept new WebSocket connection
- `disconnect(websocket)`: Remove connection
- `broadcast(message)`: Send message to all clients
- `broadcast_to_session(session_id, message)`: Send only to connections subscribed to that session
- `broadcast_to_user(user_id, message)`: Send only to connections for that user (e.g. session list refresh)
- `push_update(data)`: Thread-safe update from CLI to Web UI
- `register_agent(agent)`: Link agent instance for control. Exactly ONE agent is linked (the headless runner's worker 1), so this is a control channel, not a roster: anything that has to reach every agent in the process - re-applying a changed provider or API key, for one - must use `reload_all_api_backends()` from `vaf/core/agent.py` instead. Using this pointer for that is what left four of five chat workers on a replaced key until a restart.

### Frontend (Next.js)

**Location**: `web/`

**Technology Stack**:
- Next.js 16 (React 18)
- TypeScript
- Tailwind CSS
- Lucide React (icons)
- WebSocket API (native browser WebSocket)
- next-intl for UI localization (see [I18N.md](../platform/I18N.md))

**Main Component**: `app/page.tsx`

## Features

### 1. Real-Time Chat Interface

- **Message Display**: User and assistant messages with distinct styling. Messages are shown in conversation order (oldest first). On session reload (`history_update`), messages are sorted by server order (`_order` index), **not** by timestamp - this prevents ordering issues for network clients where client-side and server-side timestamps differ. A final turn-based role sort ensures correct ordering within each turn: system → tool → assistant.
- **Streaming Responses**: Live updates as agent generates responses. When the agent uses a tool, the text *after* the tool is shown in a **separate** assistant bubble (so you see: first answer → tool card → follow-up answer), instead of one bubble that keeps updating.
- **Thinking Process**: Collapsible accordion showing agent's reasoning (`<think>` blocks)
- **System Steps**: Timeline visualization of agent workflow steps

### 2. Session Management

- **Create Sessions**: New chat sessions via sidebar
- **Load Sessions**: Switch between existing conversations
- **Delete Sessions**: Remove unwanted sessions
- **Auto-Save**: Sessions persist automatically
- **Session List**: Displays recent sessions for the current user only (filtered by `user_scope_id`). Every session command (load, chat, delete, rename, hide, artifact edit) verifies ownership before acting; other users' sessions are not accessible. Legacy sessions with no recorded scope are admin-only when acting on them.
- **Thinking mode:** When the agent runs in the background (idle thinking), its output is appended to your main chat session (user-scoped default, e.g. `web-default-<scope>`) so you see it in the same conversation. Legacy thinking-only sessions are hidden from the sidebar. The message input stays available so you can reply. See [Thinking-Mode.md](../agents/Thinking-Mode.md).

### 2b. Agent Rooms in the Sidebar

An agent room is a conversation with several agents in it, some of which may not be
VAF and may not be on this machine. Rooms stand **above** the conversations in the
sidebar, marked with a group icon, showing how many agents are in the room and how
many frames have not been read.

- **The order is not decided here.** `SessionManager.list_ui` puts rooms first, and
  the terminal app renders the same list in the same order. A surface that sorted for
  itself would be a second rule, and the two would drift.
- **A room row is not a session row.** It cannot be renamed or deleted from the
  sidebar, and clicking it sends `open_room`, never `load_session`. Its id is prefixed
  (`room:<id>`) precisely so that a loader cannot accept it by accident. Every place
  the frontend picks a session on its own - the auto-load on connect and the fallback
  after a delete - filters the rooms out first, because the first entry in the list is
  a room for anybody who is in one.
- **The room is read INSIDE the ordinary chat area**, read-only. The chat's frame is
  untouched - sidebar, header, composer, scroll area and column width are the chat's
  and stay exactly where they are - and ONLY the placing of the content changes. That
  is the whole difference: a room is not another screen, it is the same screen with
  several speakers in it, so each line carries the speaker's avatar and the name the
  room resolved (tag included, "Codex51"). The user's own agent is drawn apart from
  the rest, because an agent that is not ours is a full agent of its own and never a
  second voice of ours. Membership frames (join, leave, role) render as a quiet
  centred line rather than as a message: a join has no words in it, and drawing one
  with a name above a sentence would put a line in an agent's mouth.
  Picking a conversation puts the room away, since both occupy the same place - and
  while a room is open it is the row marked active, not the conversation underneath.
  The strip above the message box follows the same rule: the workspace folder and the
  retrieval sources describe a CONVERSATION, so a room shows its own chip there - name
  and member count - instead of the hidden chat's chips. Clicking that chip opens the
  ROOM's shared folder in the same workspace window a chat's folder opens in; the
  folder resolves through the same server lane (the room id rides in the sessionId
  slot), so browsing, uploading and deleting need no second code path. The token
  gauge is the deliberate exception and stays visible with a room open: the agent
  answering in the room is the same main agent with the same context window, and
  hiding its gauge hid the one number that explains a slow or clipped reply.
  Typing `@` in the composer completes ROOM MEMBERS (never workflows) from a popup
  anchored directly above the input, where the mention is being typed.
  Days and times read exactly like the chat's: the same DaySeparator between
  calendar days and the same clock per message (today shows the time, older
  messages name the day), off the frame's own timestamp.
  The sidebar badge counts what the PERSON has not seen - from their own reading
  position (the cli lane the browser shares with the terminal), never from the
  agent's backlog, whose cursor only moves when its turn runs. Looking at the
  room IS reading it: the transcript builder advances that position after the
  transcript went out (never before - a send that failed was not read), so the
  badge goes out the moment the room has been seen.
  Below the messages, a member that is composing gets the same bouncing-dots bubble
  the chat shows, behind its own avatar and name - in a group chat "somebody is
  typing" without a name is a question, not an answer. Composing means exactly two
  things: our own agent's room-turn marker is live (it is really writing an answer),
  or a human is pressing keys in their input box (the browser reports a throttled
  `room_typing` signal into an in-memory map the projection expires after seconds).
  Merely READING paints nobody as typing - that used to be a third, derived signal
  with a two-minute window, and it made an agent that only monitors its room look
  permanently busy. Reading shows as a READ RECEIPT instead: every other member's
  read position travels in the payload (`readPositions`), and the view stacks a
  small initials circle under the last message each reader's cursor covers -
  overlapping, capped at twenty with the remainder as a `+N` chip, moving down as
  they read on. Remote wire peers keep their reading position in their own seat
  file, so they never produce a receipt (the protocol's documented boundary); they
  appear only through what they write. The 3-second room poll refreshes all of it;
  nothing is ever sent on the room's wire for presence.
- **What the room is FOR stands in the header**, under its kind, one line, with the
  full text on hover. The mission is handed to every agent in every turn it takes;
  before this it was in the payload and on no surface, which made the person who set
  it the only member of the room who could not read it back.
- **Open votes are DOCKED above the message box**, not in the transcript. Twice
  measured, twice wrong: at the top of the conversation a vote scrolled off in a view
  that opens at the newest message, and inside the header it looked right in the source
  while actually sitting after the header's closing tag, which scrolls the same way.
  Docked to the composer it travels with the one part of the screen that never moves,
  and the conversation slides up to make room for it rather than being covered - a
  transition on the column's bottom padding, back down when the panel goes.
  Several open questions are TABS, not a stack: a stack pushes the conversation off
  screen to show questions nobody asked to see all at once.
  Each card carries the question, who asked, how many have answered, who is still
  missing by name, the options as buttons (the viewer's own choice outlined), the
  public ballots, and a COUNTDOWN - black in the light theme, amber in the dark one,
  ticking in the browser off the deadline the server sent. It reads "alle haben
  gewählt" instead of a time once nobody is missing, because a vote ends the moment
  every member has answered and waits for no clock.
  The panel arrives and LEAVES as a movement, which is not decoration here: a vote
  ends by itself, so the card disappearing is the normal case, and the last set is held
  for the length of the exit animation after the poll stops carrying it.
  When the room closes a vote it posts the result as a message - centred and framed
  rather than in a member's bubble, because the ROOM said it: the host's lane carries
  it because somebody has to write it, and crediting a member with a count they did not
  make would be putting words in their mouth. The card and that line change places.
- **What is RUNNING is docked above the message box too**, next to the votes: at most
  three tasks, freshest first, each with its progress count and current step, and a
  "+N weitere" when there are more. The full board stays in the transcript, because
  finished work that disappears reads as work that never happened - this strip answers
  the other question, "what is anybody doing", without scrolling for it. It exists
  because the board had the votes' defect: a member reported `6/7` correctly and the
  person who had asked to see it never did, the card being a hundred messages up.
  Each row names WHO is on it before what it is - in a room with several agents the
  name is the half that cannot be inferred from the other one, and the first version
  showed only the work.
  How much room the conversation makes for both docks is MEASURED (a `ResizeObserver`
  on the dock, feeding the message column's bottom padding), not a constant: two panels
  of their own height plus a composer that grows with what is typed cannot be answered
  by one number, and the first version's number was already wrong for two cards.
  The whole strip is a button, and it opens the room's panel on a second tab, **Wer
  macht was**: the same board grouped by the member doing it, members with something
  running first, finished work dimmed but kept. Two questions, two tabs - answered in
  one list, "who is on what" is buried under everyone who has nothing running, and
  answered in a window of its own, a group chat grows a third surface for something
  the room panel is already the place for. The tab shape is the sub-agent window's,
  not a new one.
  The second tab is the TASK LIST: every task the room has ever had. Opening it widens
  the panel and splits it - on the left the chain NEWEST FIRST, grouped by day, one
  LINE per task (time, who, a coloured dot for how it went, what it was); on the right
  a search over the whole record and the entry picked, with who did it, who asked, when
  the last report came, how many reports it took, and the outcome in the words of
  whoever reported last. A task nobody reported an outcome for says so rather than
  showing an empty box.
  Newest first, and three things have to agree on it or the same complaint comes back:
  the sort, the scroll position (top, not bottom) and which row is preselected (the
  first, so the detail pane is about something on screen). A record is opened to see
  what just happened, not to read the year from the beginning.
  Rows and not cards, deliberately: every tool that shows this - Asana's action log,
  Jira's activity view, Linear's issue list - puts one piece of work on one line, and
  the card version turned twenty entries into a page and a half of padding that reads
  as empty and crowded at once. The two-column shape is the memory surface's, for the
  same reason: a list answers "which one" and only a detail answers "what came of it".
  Fetched when the tab is opened, never carried in the payload the browser polls every
  three seconds; how many tasks a room has stands with its other figures (kind, your
  role, members, opened) as a fact rather than a button, because the tab is the way in. The live board keeps thirty minutes
  of finished work and nothing older, which is the ordinary pattern (Google Tasks
  folds, Linear and Gmail archive, monday auto-archives) with one simplification VAF
  can afford: the archive already exists and is authoritative, since the transcript is
  write-once and `vaf a2a tasks` prints every task there has ever been.
- **The row carries a pencil and a bin**, in the place a conversation carries them,
  doing the room's version of each. The pencil edits the name IN THE ROW, exactly as a
  conversation does - the same gesture one row apart must not be a different interaction
  - and the new name goes to the manifest (host only) rather than becoming a message
  nobody wrote. The bin DELETES the room, which is what a bin means everywhere else here: it is removed from this machine and everybody in it loses access. It closes the room first, so a peer reading it over a wire is told why rather than finding it gone. Keeping a copy is `vaf a2a export`, and the confirmation says so. Ending a conversation WITHOUT removing it is a separate act, `vaf a2a close`.
- **The header carries an information button, not a close button.** A click on any
  conversation already leaves the room, so a close there was a third exit crowding out
  the header's one job in a group chat: answering who is in it. It opens a full panel,
  sized like Settings because it holds the same sort of content: what kind of room this
  is, your role in it, how many agents, when it was opened, and then every member with
  their role, what that role MAY SEND (read off the table that enforces it, so it cannot
  promise something the room refuses), their self-description, and when they were last
  heard from. Liveness is shown as a TIME rather than a verdict: nobody keeps a socket on
  a file-only room, so a bare "not responding" would read as a fault where none exists.
  Each member carries a small badge saying WHAT it is and whose: person or agent, and
  the name of the other half of its household. It is the one line in that panel a
  member cannot write about itself - the room recomputes the handle from an account it
  admits instead of reading a claim - and a guest that named no account carries no
  badge rather than a guessed one.
  Each member can be removed from there. No remove button is drawn for the room's own
  host handles: an action offered and then refused reads as a fault, and this is a rule
  with a different answer.
  The panel also shows and sets YOUR AGENT'S MODE for this room (observe / assist /
  autonomous, `set_room_agent_mode`): the mode is the user's standing decision, held
  in the agent's member file and read live by the next room wake - autonomous is
  "work while I sleep", granted per room and revocable in the same place. The
  command derives the agent's handle from the caller's own scope, so another
  account's agent is unreachable by construction. Whether the viewer may remove anybody is answered for the lane
  the BROWSER acts on, not whichever lane the sidebar row resolved first. Speakers are shown with the name the room resolved for them, tag
  included ("Codex51"), which is also the name a mention has to be typed against.
  The user's own agent is drawn differently from the strangers: a foreign agent is a
  full agent of its own and is never shown as a second voice of ours.
- **The person writes from the composer.** Sending in an open room goes out as
  `room_say` under the browser's own CLI-lane membership - the same participant a
  terminal join lands on, so one person is one member no matter where they type.
  Agents write through their room tools, foreign ones through `vaf a2a say`.

### 3. Status Indicators

- **Connection Status**: Visual indicator (green/red) in header
- **Local Model Idle**: Shows `Idle` when the local model is unloaded and waiting for a prompt
- **Loading States**: Animated dots during agent processing
- **Workflow Steps**: Real-time display of Router, Workflow, System, and Info events. The **Router** step shows which tools were selected for the turn (e.g. `Router: LLM-based: list_calendar_events` or `Router: Script-based: web_search`), so you can see when and which tools the agent is using.
- **Inline Tool Status**: Visual cards for running/completed tools directly in the chat stream. Tool events (`tool_update`) are always emitted regardless of background thinking mode - they are no longer gated by `_emit_to_web_ui()` to avoid race conditions with the `VAF_THINKING_MODE` environment variable. After page reload, tool cards show the correct status (`completed`/`error`) from the `toolStatus` field in `history_update` messages. A tool that returns **without running** - e.g. a state-changing tool gated by the plan requirement (`[PLAN REQUIRED] …`) - is shown as a **non-success** (red), not a green check, so a gated call is never mistaken for a completed one (the agent did **not**, for example, actually save a memory).

### 4. Sub-Agent Panel & Tool Cards

- **Docked Panel**: Sub-agent output renders in a right-side panel that slides in/out.
- **Auto-Open**: The panel opens when a sub-agent starts (via tool events/logs).
- **Tool Card Toggle**: Clicking a sub-agent tool card expands details and opens the panel; collapsing the card closes the panel.
- **Auto-Close Guard**: The panel does not auto-close while any sub-agent step is still running.

### 5. Message Features

**Thinking Details**:
- Extracted from `<think>...</think>` tags
- Collapsible accordion UI; a subtle shimmer + animated dots while the model is still thinking, collapsing to a compact header with a measured **duration pill** (e.g. `Thinking Process · 2.4s`) once done. The duration is measured live and cached per message (keyed by timestamp) so it survives the inline→timeline remount.
- Monospace font for technical content

**Actions Timeline** (`web/components/TurnActionsTimeline.tsx`):
- A turn's thinking blocks, tool calls, and any conversational lines the model emits **between** tool calls (a `'say'` step - common with reasoning models like DeepSeek, e.g. "let me look closer") are grouped into **one** collapsible timeline anchored on the turn's first assistant message (stable while streaming, so cards never remount). The final answer renders below it.
- A left **rail** with one dot per action - solid black = thinking, **hollow black ring = an intermediate spoken line (`'say'`)**, hollow gray ring = a running/failed tool, solid gray = a completed tool - grows down as steps arrive. The living **white-dot avatar** walks down to the active (running) step and returns to the top when the group collapses; a `'say'` step is always "done" so it never steals the avatar.
- While the turn runs the timeline stays **expanded** until generation **ends** (so an intermediate line mid-turn no longer folds the rail while the agent is still working), then it **collapses** to a borderless circle-row ("N actions") that re-expands on click. Past turns (and reloads) render collapsed by default.
- Grouping is additive with safe fallbacks: only turns with ≥1 tool group. Intermediate answer text used to abandon grouping (per-row fallback); it is now rendered as a `'say'` rail step instead, so the grouping holds. Tool rows persist across reload via the session cache (the server stores only a per-turn tool summary, not the individual cards).

**Long reply collapse**:
- A bot answer longer than ~800 chars collapses to a ~300-char preview with a "Show full response" toggle, but **only once a newer user message exists** (i.e. it is a *past* answer). The current/streaming answer and short replies are never collapsed.
- Collapse is computed at **render time** from the message's position + length; only the user's manual *expand* choices are stored, keyed by the stable message **timestamp**. It is deliberately **not** a set of array indices: those shifted whenever a message was removed (`clear_last_assistant`, dedup) and collapsed the wrong bubble (tiny replies collapsed, long ones stayed open, the streaming reply collapsed mid-stream).

**System Steps**:
- Timeline-style visualization
- Icons for different step types (Router, Workflow, Safety). Router steps show the selected tool name(s) (LLM-based or script-based selection; see [TOOL_ROUTER_ARCHITECTURE.md](../agents/TOOL_ROUTER_ARCHITECTURE.md)).
- Automatic filtering of redundant messages

**Wake / system-activity messages (`kind`)** - *extension point*:
A proactive backend message can carry a `kind` tag: `emit_agent_message_append(content, session_id, role, kind="…")` (`web_interface.py`). The Web UI then renders that message as its own **agent-style row** (avatar + speech bubble) with a kind-specific look, instead of a plain user/assistant bubble. This is how a fired **timer** appears, and it is the hook for other proactive/background activity.

- **Frontend:** `Message.kind` carries the tag; the `agent_message_append` handler stores it; the message render loop has an `_isWake` branch (matched by `msg.kind`, or by the `⏰ Timer fired` content prefix when reloaded from history) that draws the wake row **before** the normal role branches. The trigger is sent as `role="user"` so it still creates a bubble boundary (the agent's reply lands in its own bubble), but `kind` overrides how it is drawn. The row mirrors the agent layout (`justify-center` → `max-w-[85%]` avatar + bubble) so the avatar aligns with the agent's, shows only the user's note (the internal "Act on it…" framing is stripped), and carries the same timestamp as the agent messages. See `web/app/page.tsx` (`_isWake`).
- **Timer - two states (`_wakeDone`):** while the agent is still handling the fired timer it shows an **active** look (the real agent avatar + an amber clock **badge** in the corner + an amber bubble - "look here"); once the agent has replied (a completed assistant message follows and generation has stopped, or a newer user turn exists) it **settles** to a quiet look (neutral dim avatar, no badge, neutral bubble, amber only in the small "TIMER" label). On reload a past timer is already in the settled state.
- **Extending it:** to add a new activity (e.g. `kind="thinking"` or `kind="background"`), emit it from the backend with that `kind` and add a branch in the `_isWake` render. For kind-specific avatars, `AgentAvatar` takes an optional `tint={{ body, dot }}` (added for this) - e.g. the intended **purple agent-avatar** for `thinking`. See [AgentAvatar.md](AgentAvatar.md).

### 6. Settings

**Admin-only tabs:** The following Settings tabs are visible only to admin users: **General**, **AI & Model**, **Advanced**, and **Local Network**. Non-admin users are automatically redirected to the first allowed tab if they land on an admin-only tab. Both the sidebar filter (`adminOnly` flag in the CATEGORIES array) and content rendering guards (`currentUser?.role === 'admin'`) enforce this. The admin role is determined from the stored JWT role on the WebSocket connection.

Under **Settings → Interface** you can set:

- **Language** - UI language (e.g. German, English). Stored in the browser only (`localStorage`). See [I18N.md](../platform/I18N.md) for how translations and new languages are managed.
- **Appearance → Dark mode** - a neutral `#181818` dark theme (default off/light). Stored in the browser only (`localStorage.vaf_theme`). For the exact colors of every surface, control and the agent avatar in each theme, see [LIGHTMODE.md](LIGHTMODE.md) and [DARKMODE.md](DARKMODE.md) (design tokens in [DESIGN.md](DESIGN.md)).
- **Custom cursor** - VAF's custom dot cursor vs. the system pointer.
- **Date & Time** - Timezone, date format, and time format (24h/12h). Stored in your user identity and used in the system prompt and when the agent shows dates and times.

**Settings → AI & Model:** the main provider and model, **Context effort**, the optional Vision model, and - grouped with them as model/provider settings - **Sub-Agents** (run in separate terminals, provider, the **Tool / Workflow model**, timeout) and **Thinker (background)**. For both you can pick a separate provider; the model is a dropdown of that provider's models that defaults to "same as main chat" (for the Thinker the model picker shows only when a non-inherit provider is selected). See [Thinking-Mode.md](../agents/Thinking-Mode.md).

**Context effort** is the stepped slider on that tab, and it is the one setting that decides what a reply costs: an API is sent the whole conversation again on every round-trip and bills every token, so the value is the price of ONE reply, not a capacity. The rungs come from the backend (`GET /api/config/context-effort`, computed by `resolve_context_effort` in `vaf/core/context.py`) and are never rebuilt in the browser: they run from 8,000 tokens up to the configured model's real context window, so a 128k model shows seven rungs and a 32 768-token local model four. The endpoint takes the provider and model as query parameters, so the ladder is already right for a provider the user has picked but not yet saved. For a local model the slider is disabled with a note, because local tokens are free and the agent ignores the budget there. It writes `context_compress_tokens`, which is admin-only - the AI & Model tab is admin-only anyway. Lowering it does not delete anything: older turns are summarized and the full history stays restorable with `/restore`. See [CONTEXT_MANAGEMENT.md](../memory/CONTEXT_MANAGEMENT.md#real-time-api-token-tracking-self-calibration).

**The archive.** The delete dialog offers to keep a copy instead of losing the conversation outright, ticked by default: the common regret is a chat deleted for tidiness that the agent later needed, not an archive that grew. Archiving is a **move of the session file**, not a second export format (`SessionManager.archive`): the file already IS the whole conversation, already encrypted at rest, already carrying its owner - so every existing reader keeps working on it, which is what makes the memory lane able to retrieve from it later, and there is no second format to teach every reader. It lands in `<sessions>/archive/<scope>/`, owner-only. **User isolation does not rest on the directory:** `list_archived` re-reads the `user_scope_id` recorded inside each file and skips anything that does not match, so a stray copy in the wrong folder is ignored rather than listed (a test moves one there to prove it). The web layer checks ownership before either path, archives first and deletes only if the move failed - a chat the user asked to be gone must never quietly stay. Archived chats are shown as **archive boxes** in a narrow-card grid rather than as list rows, each box above its own centred caption: the panel is a place things were put away, and a row reads like a menu entry. Hovering lifts a box and takes its lid straight up, level, the way one comes off a document box - the affordance says there is something inside without a caption saying it. In the dark theme the hovered box takes a yellow rim, which is the one place that colour is used there and so reads as "this is the one" rather than as decoration. Opening one shows the conversation with a pinned header - the way back and the delete action stay in place while the chat scrolls, since a long conversation is exactly where they are wanted and exactly where they used to scroll away - a readable way back (the link used to be grey on near-black, a control you had to hunt for) and a **Delete from archive** action beside it. That deletion is the end of the line and its dialog says so: this is the last copy, and because the memory lane reads the archive, the agent stops being able to recall the conversation. It uses the same arming delay as the other destructive dialogs - a padlock and a count, opening when it is safe to press. **Escape steps back one level**, never straight to the bottom: a dialog closes to the chat it is about, that chat closes to the box grid, the grid closes to Settings, and Settings closes to the conversation. It joins the modal's existing stacked-escape handler in z-index order rather than adding a second listener. An **Archive** button sits in **Settings → Persona & Memory** beside *User identity* and opens a window with the memory graph's footprint (`max-w-[95vw] h-[90vh]`), split the way the memory tab is: the search and its hits on the LEFT, the archived chats on the RIGHT. Clicking a hit opens that chat scrolled to the message the hit was found in - a search that only names the chat leaves the reader to find the sentence again by hand. Both endpoints (`GET /api/archive/chats`, `GET /api/archive/chats/{id}`) take the scope from the AUTHENTICATED caller and never from a parameter, and reading re-checks that the id is in that caller's own listing before touching a file, answering 404 either way so a guessed id cannot tell absent from somebody-else's. The search runs on the server across every archived chat (`SessionManager.search_archived`) and uses the SAME matcher as Cross Chat Hints - `query_terms` / `_match_text` / `_excerpt` from `vaf/core/cross_chat.py` - so it folds umlauts and reaches into compounds from both sides exactly as the agent does. Two matchers would have meant a phrase the agent can find and the user cannot, in the same archive. What it deliberately does NOT inherit is that lane's SELECTION rules (`cross_chat_hint_min_terms`, `min_score`, the corpus filler filter): those decide which chats are worth prompt space, while a search box has to show what it found, including a single common word. Every hit carries the words it actually matched, AS THEY APPEAR in the text - not the folded query terms - and both the hit list and the opened message highlight those: a reader who typed `Pruefung` matched `Prüfung`, and marking what they typed would have marked nothing, leaving them to guess what the search found. Every occurrence is marked, not only the one the hit pointed at. Opening a hit also outlines the **passage** the agent would be handed for it - the same 300-character window `cross_chat._excerpt` quotes, computed a second time against the RAW message because that excerpt's offsets address a collapsed, folded copy and would land elsewhere. So the block a reader sees marked in the chat is what the model sees, with the matching words picked out inside it. The mark is black on white text in the light theme and yellow with black text in the dark one, so it reads as a mark in both rather than inheriting a body colour.

**Deleting a chat asks first, unless there is nothing to lose.** An EMPTY chat - no messages and no attachments known to the client - is deleted straight away: asking about it would be friction with no decision behind it, and the freshly-created-and-never-used chat is the one people delete most. Attachments count as content even with no messages, because a document can be added and never sent, and losing it silently is exactly what the dialog exists to prevent. For anything else the trash icon in the sidebar opens a dialog rather than deleting on the spot: the removal cannot be undone and it takes the chat's attached documents with it. The confirm button is disarmed for three seconds and counts down, because a destructive button under the cursor gets pressed before the sentence above it is read - the same delay now guards the room dialog, which had no arming period. The dialog names the documents it will delete when they are known, and its main sentence warns about attachments unconditionally: only chats opened in the current browser session have their document list on the client (`sidebar_documents_restored` arrives on load), so an empty list means "not known", never "none", and the warning must not read as a promise that there are none.

**Settings → Usage:** what this instance has actually consumed - tokens in total, number of requests, an estimated cost, and a table of accounts sorted heaviest first, so "who used the most" is the first row. The numbers are **tokenizer-independent by construction**: every one of them was reported by the provider for a call it billed (`last_request_usage`), never counted by a tokenizer of VAF's. **When a provider reports nothing**, the call is not dropped. We always know WHO called and WHERE it went - the lane, provider and model are decided before the request leaves - and only the provider's own count is missing, which is what an aborted or failed stream withholds. The call is counted, sized by a deliberately crude fallback (words plus punctuation, no tokenizer: it exists to stop a hole, not to be right), and marked as an estimate in three places - `usage=estimated` in the per-call log, a `no_usage_calls` count, and an `estimated_tokens` figure so a reader can subtract it and get back to what the providers actually reported. Measured against a provider's dashboard the residue was 0.6%; this is what it is made of. They are also **complete by construction**: recording happens inside `APIBackendManager.chat_completion`, the one method every lane in the product reaches a model through, so the coder, sub-agents, vision, voice, memory compaction, the mail composer and the browser agent are counted alongside the chat. That replaced a per-turn hook in the agent which could only ever see the chat lane - everything else spent invisibly. A turn now only labels itself (`Agent._set_usage_context`: whose scope, which lane) and the backend books the call. **The ledger is the record; the log is a copy:** the per-day, per-user, per-lane totals in `<data dir>/spend/*.json` are what the view, the export and the budget cap read, while `usage_YYYY-MM-DD.log` is a per-call trace for a human (lane, provider, model, tokens, cost, session). Coverage is now every lane without exception: tools reach a model through `BaseTool.query_llm` -> `complete()`, which labels the call with the `caller` it already receives (`tool:web_search`, `tool:librarian`, ...), so a search inside a chat turn no longer bills as the chat. The **local** path in `complete()` posts straight at the llama server and never touches the backend manager; it records too, because free is not the same as invisible and a view that shows only the paid calls cannot answer what the machine did. The **coder** runs in a subprocess over its own HTTP client - the last lane that could spend without appearing anywhere, and usually the largest - and books both of its paths, the streaming one after asking for `stream_options.include_usage`. Beyond those, each lane names itself with the `@usage_lane(...)` decorator from `vaf/core/cost.py` - `memory`, `vision`, `voice`, `librarian`, `mail`, `browser`, and - derived by `Agent._set_usage_context` from markers the agent already carries - `subagent`, `automation`, `thinking`, `room`, `background`, with `main` for an ordinary chat turn. The unattended lanes were always counted (they reach the model through the same backend); naming them is what lets an operator see that a 2am automation, not a person, is the reason for a day's bill. Order is deliberate: a sub-agent runs its own agent whose run kind is an ordinary chat, so the process marker wins - so the log answers which part of the product spent the tokens, not only that they were spent. The decorator exists instead of a `with` block at each site because these are whole functions whose bodies would have to be reindented, and it handles generators explicitly: a streaming lane does its work while the caller consumes it, so a label that ended when the generator was built would cover none of the tokens. A **Refresh** button sits beside the tab's description, because the ledger keeps being written while the dialog is open. Nothing is built on that log, because logs rotate and get deleted; it is the only log writer that ignores the debug-logging switch, since a spend record a settings toggle can silence is not a record. That is why two providers who disagree about what a token is still add up to the invoice. **The unit travels with the amount.** Veyllo publishes list prices in EUR and every other provider in USD, so each recorded call stores the currency it was priced in (`CostEstimate.currency`), the ledger keeps a per-currency map beside the legacy `usd` total, and the page prints from that map - a period that used two providers shows both amounts rather than a sum of euros and dollars, which would mean nothing. The legacy field stays for ledgers written before this and for the daily cap that reads it, **Converting between EUR and USD** is a two-state toggle at the top right of the first panel, and the choice is remembered across visits - a reader who works in one currency should not have to say so again. Selecting one converts EVERY figure on the tab, the price comparison included: without that, the cheapest model per provider would be picked by comparing euros against dollars. The toggle appears only when a rate is available. The rate is the European Central Bank's daily reference rate, fetched server-side (`GET /api/usage/fx`) because the ECB's own endpoint sends no CORS headers, and cached for a day - it is published once per business day, so asking per page view would be pointless traffic on a free service. A single footer at the foot of the tab names the ECB as the rate's source (their terms ask for that attribution) and states once what every figure above already is: an estimate from published list prices, without liability, on prices a provider can change at any time and which take a while to be updated here. Said once at the end rather than beside each number, where it would be noise. Conversion is a VIEW: the ledger keeps what each provider billed in its own currency, it is off by default, and amounts with no recorded currency are never converted - there is nothing to convert from. When no rate can be had the toggle does not appear at all rather than converting at a guessed number. A cost is shown as ONE number or not at all: where a period mixes currencies, or holds amounts recorded before the unit was stored, the figure is a dash and the reason is written beneath it - two amounts joined by a plus is not something a reader can act on, and adding euros to dollars would be a number that means nothing. The same figures and the same maintenance action exist on the command line (`vaf usage show`, `vaf usage set-currency EUR`), because a headless install has no tab and a ledger rewrite must not require reaching into Python. Amounts recorded before the unit was stored can be attributed ONCE, by the operator rather than by the software: `stamp_legacy_currency("EUR")` in `vaf/core/cost.py` fills in only what is missing - a day that straddles the change keeps the amounts it already recorded - writes a `.bak` beside each ledger first, and is a no-op on a second run. It exists because the operator knows which provider they were running and the ledger cannot: the field was called `usd` while a Veyllo call inside it was euros. An amount that cannot be attributed to a currency - a record from before the unit was stored, or the part of a straddling day that predates it - is carried under `?` and printed without a symbol. Carried rather than guessed, because the field was called `usd` while a Veyllo call inside it was euros; and carried rather than dropped, because dropping it is the defect this replaced: a period holding both kinds of record showed only the newer amounts, hiding almost the entire total behind a few cents. `_merge_currencies` is the one place that does this, so the totals, the day, the lane, the provider row and the export cannot disagree about it. The money beside it is the one estimate on the page - it comes from a price table that ages, and calls to a model missing from that table are priced at the expensive end and flagged as an upper bound. **Money is admin-only, and it is stripped on the server.** A non-admin's response carries their own tokens and call count and nothing else: no cost, no percentage share, no other account. The share is withheld with the cost on purpose - a percentage of a total is a statement about everyone else - and the comparison and export panels are hidden with it. Doing this in the UI alone would have left the numbers readable in a network tab. A `costs_visible` flag tells the page which of the two it received. The tab is visible to everyone, but the two views are different endpoints, not a client-side filter: an admin gets every account (`GET /api/usage`), anyone else only their own line (`GET /api/usage/me`), because one tenant must never see another's consumption. Local models are free and contribute tokens but no cost. The chat's own **Context Window** header carries a **Usage** button straight to this tab: that header says what the conversation is carrying, and what it has cost is the next question a reader has. See [CONTEXT_MANAGEMENT.md](../memory/CONTEXT_MANAGEMENT.md) for the setting that decides how many tokens a single reply spends.

Below the table the tab continues with two more panels. **Last 7 days** is a bar chart (inline SVG, no charting dependency) with a dashed line across the busiest day, so a bar is read against something rather than against nothing; days with no traffic are drawn as gaps rather than skipped, because a chart that drops quiet days makes a burst look like steady traffic. **A bar is clickable**: it opens that day's breakdown by lane - chat, thinking, automation, sub-agent, group chat, memory, vision, voice, librarian, mail, browser - each with its tokens, requests and (for an admin) cost, sorted heaviest first. A day recorded before lanes were tracked says so instead of showing an empty list. The whole series is instance-wide, so a non-admin receives it EMPTY rather than omitted: the page reads its length, and it would disclose when other accounts were busy, which is the same thing the percentage shares are withheld for. Under it each account gets a share bar with its percentage, request count and token count - that is the "who used the most" answer as a number. **By provider and model** sits inside the first panel, between the headline numbers and the per-account table, and answers where the work actually ran. VAF can point chat, vision, sub-agents, the tool model and the thinker at different providers - or at a local model - and the price between them differs by an order of magnitude, so a total without that split is unreadable. Every recorded call already carried its provider and model; the ledger now keys a breakdown on `provider/model`, aggregated on the day and over the period, and the same rows appear when a bar in the chart is opened. Local rows show tokens with no cost, which is what a local model is.

**What this would cost elsewhere** prices the very same tokens - the same 30-day window as the table above, named in the panel so the figures are never read as a monthly rate or a lifetime total - with each provider's public list price, quoting each one at **its cheapest model for this usage** - which model that is depends on the sent/received ratio, so it is decided per provider from the real token counts rather than fixed to a "representative" model that could flatter one side. Veyllo leads the list by construction (it is this product's own API); the rest follow. Tapping a provider opens a dialog with its whole model list: the per-million rates for sent and received tokens, what each model would have cost for these exact tokens, the cheapest one marked, and the token counts the arithmetic used - so the row's headline figure can be checked rather than trusted. The last row is a custom one where the reader names their own price and enters the two rates (the inputs appear only after it is opened, so an empty form does not sit beside the real providers looking mandatory). Prices come from `GET /api/usage/prices`, served from `PROVIDER_PRICING` in `vaf/core/cost.py` - the same source the flat `PRICES` index and therefore the ledger's own estimate is derived from, so the page cannot quote one number and bill by another. They are standard list prices only: no cache, batch or off-peak discount and no long-context surcharge, because a comparison assembled from each provider's best case would flatter whoever runs the most discount programmes (DeepSeek is quoted at peak, OpenAI at short context). **Currency is reported, never converted:** Veyllo publishes in EUR and the others in USD, and the module carries no exchange rate it would have to keep current, so the unit travels with the price and the footnote says so. Every price also carries **the date it was last checked** (`PRICES_AS_OF`, served as `as_of` and rendered beside the footnote and in the dialog): providers reprice, and a list price shown without a date is a claim about today that was verified on some other day. Move that stamp in the same change as any price. **Export** (admin only) downloads `GET /api/usage/export`: the period as XML, carrying everything the tab shows and in the same shape - totals, per user, per provider/model, and one `<day>` per date with its own lane and provider rows, so a reader can follow a figure from the total down to the day it happened on. The document names its period on the root element (`from`/`to`) and every amount is written as `<estimated-amount currency=... value=.../>`, one per currency, never a sum across them. A `<note>` is the FIRST element, deliberately: a reader opens this in a spreadsheet and sees numbers before prose, so the caveat cannot sit at the bottom. It is written as a statement of method rather than a disclaimer - what the token counts are, that the amounts are estimates from list prices without discounts, that an unpriced model makes the figure an upper bound, that currencies are not converted, and that local models carry tokens and no amount. The `<method>` block underneath keeps the same facts in field-by-field form.

**Settings → Advanced:** **Attachments** (hierarchical document indexing), system options - including the tool-step budget (`max_tool_turns_per_step`, with a no-limit switch `tool_loop_unlimited`, both admin-only) and the admin's own hands-off switch (skips tool confirmations for the admin's account, announced per use as a `gate_bypassed` event) - and management panels (each a row that opens a sub-modal): **Tools**, **MCP**, and **Workflows**. The **MCP** row ("N connected / M configured") opens a panel to add, edit, or remove MCP servers (admin only) - see [MCP_INTEGRATION.md](../agents/MCP_INTEGRATION.md). The last row, **Update and Repair**, opens the maintenance dialog described below.

**Settings → Advanced → Update and Repair** (`web/components/settings/UpdateRepairModal.tsx`, admin only). A wide dialog with two halves.

*Right half - the services.* The Docker containers are drawn as a node graph: a VAF hub on the left, one node per container on the right, one edge each. The colour is not "is it running" but "does it answer": green when the container runs and its probe succeeds, red when a required container is down or runs without answering, amber when it publishes a different port than the configuration expects or its own health check is unhappy (and for an optional container that runs without answering), grey for an optional container that is absent or stopped. Data comes from `GET /api/system/services` (`vaf/core/service_health.py`), polled every 10 seconds while the dialog is open and paused while a repair or update runs. Everything that is not green is listed underneath, and **Repair** starts `POST /api/system/services/repair`. While a container is inside its own start window the button waits instead: it counts down and says the containers are starting, because right after a VAF start the services are legitimately on their way up and repairing them would only restart what is already booting. The countdown is each container's own `start_period` (30 seconds for the database, 120 for the speech services), so it is right per service rather than one number for all, and the dialog polls every 3 seconds while that lasts instead of every 10. That repair is a JOB, not a request: it can take minutes (starting a container engine), so the POST returns `202` and the dialog follows `GET /api/system/services/repair` every 1.5 seconds, rendering each step as it finishes. The same run is `vaf repair` in a terminal - see [DOCKER_SERVICES.md](../setup/DOCKER_SERVICES.md) for what repair does and what it deliberately never does.

*Left half - the version.* The installed version and the timestamp of the last update check come from `GET /api/system/update`, which reads `~/.vaf/update_cache.json` and never touches the network. **Check for updates** (`POST /api/system/update/check`) is the only thing here that asks GitHub. When an update exists, the new version, its release notes and a restart warning appear; **Update now** asks once more and then calls `POST /api/system/update/apply`.

*What happens during an update.* The endpoint spawns a DETACHED `vaf update` process and answers immediately, because the update stops the very server that answered. The dialog then polls `GET /api/version` every 2.5 seconds with a 2 second timeout, treating every failure as "still restarting", and shows the elapsed time; a version higher than the one it started from means success, and the page reloads itself three seconds later. After ten minutes it stops guessing and points at the tray and `~/.vaf/logs`. Two `sessionStorage` keys carry this across the reload: `vaf_update_pending` (written BEFORE the POST, so closing and reopening the dialog resumes the waiting screen) and `vaf_update_done` (the one-shot "update finished" line). The dialog does nothing to the WebSocket; `page.tsx` reconnects on its own and the final reload re-initialises everything. Six situations refuse with a `409` and a sentence instead of a broken restart: a package (pip) install; a source tree that is not a git checkout (a downloaded archive - `vaf update` can adopt it, but only after a terminal prompt that explains what `git reset --hard` does to the folder, and an unattended run would answer that prompt for the user); server mode without `systemd-run`; an earlier update that never finished; a repair still running; and a desktop install started without `vaf start` (no pidfile, so the updater's stop step would do nothing and its start step would add a second server). A second click while an update is already spawned is refused too, because both processes would fetch and reset the same checkout against each other.

**Settings → AI - Download model from Hugging Face:** This option is only available when the **local** model provider is selected. You can paste a Hugging Face repo id that **contains GGUF files** (e.g. `seanbailey518/Nanbeige4.1-3B-GGUF` or `Edge-Quant/Nanbeige4.1-3B-Q8_0-GGUF`). Base-model repos (safetensors only) do not offer GGUF; if you see "No GGUF files found", search on Hugging Face for "<model name> GGUF" and use that repo. Click Download to open the confirmation dialog. A confirmation dialog opens with the model card (README) and a list of GGUF files to choose from; after you confirm, the download starts. Progress (percentage, bytes, speed) and a Cancel button are shown in Settings. If you close Settings while a download is in progress, the download continues in the background; a compact progress indicator appears on the main chat page so you can see status and cancel from there. When the download finishes, a toast shows success or error and the model list refreshes. The same progress banner also appears when VAF **auto-downloads** a model on first use (for example an empty `models/` directory, where the default model is fetched on the first prompt) - not only for WebUI-initiated downloads - and clears the same way when it completes.

**Settings → Connections:** Manage external integrations (Email, Calendar, Cloud, Discord, Telegram, WhatsApp, GitHub, etc.). A search field at the top filters the list by name or category (e.g. type “GitHub” or “Kalender” to jump to that connection). The **GitHub** category opens a dashboard with a rights-overview strip (toggle read/write per account), connected accounts, an event timeline (agent actions, newest first), and a repositories panel (repos for the selected account with links to GitHub). The **Calendar** category shows Google Calendar and Microsoft Outlook; they use the same OAuth connection as Email (connect Gmail or Outlook under Email first). The **Email** tile opens the mail client window (three-pane; its gear opens the in-client account panel). Suspicious emails are marked there with a warning badge and reason; they remain visible to the user, but are hidden from agent mail tools by default for safety. For safety, IMAP/SMTP hosts that resolve to loopback or a private/link-local address are refused (SSRF guard); operators running a legitimate LAN or self-hosted mail server can opt in by setting `email_allow_private_hosts: true` in `config.json` (default `false`). When a calendar is connected, the settings (gear) icon opens the **Calendar Dashboard**: left sidebar lists connected accounts with links to open Google Calendar or Outlook in the browser; main area shows upcoming events from the API with selectable range and refresh. See [CONNECTIONS.md](../integrations/CONNECTIONS.md) and [CALENDAR_INTEGRATION.md](../integrations/CALENDAR_INTEGRATION.md).

**Settings → Automations:** View scheduled automations (user-scoped when multi-user is used; root/global automations such as "Daily calendar check" are also shown so the list matches the agent's `list_automations` tool). To create one manually: click **Create New** (or use the **Automation** entry in the sidebar footer) to open the calendar; choose month, then day, then an hour slot. The sidebar footer also includes **Notifications** (opens the Notifications popup) and **Settings**. Opening the Automation popup (footer) also triggers the calendar ensure-daily-check API when a calendar is connected, so the Daily calendar check appears in the list without opening Settings first. A popup lets you set repeat (once, daily, weekly, monthly, hourly), time, a detailed prompt, and an optional name. Creation is sent via WebSocket (`create_automation`); the list refreshes on success. The agent can also create automations via the `create_automation` tool in chat.

The same automation calendar includes a **per-user planner**:

- **To-do list** (left column): User and agent can add items via an "Add to-do" popup (text and optional due date). Each item has a done checkbox (updates via WebSocket) and a delete button. Data is stored per user and loaded when the calendar opens.
- **Notes** (bottom section, fixed height): User and agent can add notes via an "Add note" popup (optional title, content). Each note shows created-at and can be deleted. The list scrolls inside a fixed-height area so the layout does not grow.

Planner data is loaded with `get_automation_notes` and `get_automation_todos` when the calendar is opened (from the footer or from Settings). Create/update/delete use WebSocket messages; the UI updates optimistically where applicable. The agent can manage the same data via tools: `add_automation_note`, `add_automation_todo`, `list_automation_notes`, `list_automation_todos`, `delete_automation_note`, `delete_automation_todo`.

**Logs window (admin only):** Clicking **Logs** in the sidebar opens a split-pane log viewer (same window size as the Automation window). It is only visible to users with the `admin` role.

The left sidebar has five sections:

- **Overview** (top) - the protection dashboard (see below). The default view when opening the window.
- **Timeline** - the agent tool-use timeline as a horizontal scrubber (see below).
- **Tool Use** - the same timeline events as a vertical, expandable tool-call list ("Tool Calls", newest first; sub-agent, thinking-run, and training entries get their own icons).
- **Activity** - the notification feed: thinking-mode results, automation run results (success/error + summary), handoff decisions, and channel replies. Items expand to show the full summary. Loaded from `GET /api/notifications`; new items pushed via WebSocket.
- **Log Files** (collapsible) - lists every `.log` file in the VAF log directory (`~/.vaf/logs/`), grouped by domain (rag, memory, backend, prompt, headless, attach, tools, tool_use, security, …) with a colour dot per domain. Collapsed by default. Most domains only fill while debug logging is enabled (on by default; no UI switch - opt out via `debug_logs_enabled` in `~/.vaf/config.json`); the **security** domain (blocked access attempts, failed logins, rejected senders) is always written, independent of that setting. There is deliberately no switch in Settings: the setting is a config-only opt-out, and the Logs page's empty states name the config key directly, so a config with a legacy `false` is not a dead end (a live incident on a macOS install once forced the switch into the UI because the empty states pointed at a switch that did not exist).

**Whose activity is shown:** beside the date picker (Timeline and Tool Use only) sits a user picker, which appears once the machine has more than one account. Picking a name reloads both views for that person alone. The client sends a USERNAME, never a scope id - the users endpoint deliberately does not hand scope ids to a browser, so `GET /api/logs/timeline/events?user=<name>` resolves the name against the auth DB itself, and a name that resolves to nobody yields an empty list rather than everything. Two things stay deliberately unfiltered: `chain_ok` and `total_raw` keep describing the whole day's file, because the audit badge must not claim a verification it only ran over a selection, and the response carries `unattributed`, the number of the day's entries that carry no user at all. Tool Use prints that number under a filter, because entries written before identities were stamped, and background work that belongs to no user, are invisible under a filter and would otherwise read as "this person did nothing". The other views are unaffected: log FILES are one process-global directory (there is no per-user log file to show), and the Activity feed is the caller's own.

**Security notification badges:** for admins the frontend polls `GET /api/security/alert-count` once a minute; a red dot appears on the sidebar **Logs** button while the newest security event is newer than the last-seen marker (`localStorage` key `vaf_logs_seen_ts`; the same dot also lights when the hash chain reports tampering). Inside the window, the **Log Files** section header shows a pulsing red count under the same unread gate, and expanding it while unread jumps straight to the security log. Opening the security log marks everything up to the newest event as seen and clears both badges - the marker is unread-based, not a permanent counter.

#### Overview view

The Overview is an antivirus-style protection dashboard summarising VAF's security posture for admins:

- **Hero panel** - a large shield showing the overall status as a worst-of roll-up over all modules: `critical` (red; tampered hash chain, a quarantined or high-risk skill) > `attention` (amber; e.g. Docker down so execution is blocked, memory DB down, exposed container ports, permissive channels, a medium-risk skill, a security refusal overridden today, a skill re-scan alert today) > `ok` (green), with a grey no-data floor so absent data never reads as safe. The headline names the actual reasons, not just the colour. Below it, **today's blocked count is a clickable badge** opening the unfiltered event list for the day; it is amber when an override or a re-scan alert is among them and otherwise carries the hero's own colour. The badge exists because the roll-up reads module *states*: until it was added the shield could print "no anomalies" while a stopped HIGH skill install and an admin override sat as two numbers in the Skills panel. A block stays a plain count (the guard working, like the firewall row that stays green while reporting deflections); an override and an alert are what raise the shield.
- **Module status list** - one row per protection module (audit chain, sandbox, firewall/LAN incl. Docker isolation, user isolation, channels, phishing shield, guardrails) with a traffic-light dot and short status. Clicking a row opens a detail popup; the firewall and channels popups lazily fetch the recent blocked/rejected attempts from `GET /api/security/events`.
- **Audit chain panel** - live view of the selected day's hash chain (verified/tampered, event count, last event, tail hashes) with a date selector; while today is selected the chain refreshes every 5 s. The selection follows the calendar: after midnight it advances to the new day as soon as it has events, unless the admin explicitly pinned an older day (picking the current day again re-enables following). All date math uses LOCAL time - the backend names timeline files by the server's local day, and the earlier UTC comparison kept showing yesterday as "Today" until 02:00 CEST.
- **Skills panel** - a donut of installed skills by scan level (clean/low/medium/high) plus today's blocked installs, admin overrides, and re-scan alerts. These three counters stay here rather than moving to the hero badge because they are skill-specific by construction - all six emit sites are skill operations - while the badge counts every kind of blocked attempt. Clicking a skill opens its scan detail (`GET /api/security/skills/{id}/scan`) with resolution actions: **delete** and **isolate** are plain admin actions, while **acknowledge** (medium finding) and **restore** (quarantined skill) additionally require the admin's TOTP code, so a stolen admin session alone cannot silence a warning.
- **Background agent** - per-user thinking-mode status (admin view across scopes) from `GET /api/thinking/status`.
- **Recent supervised activity** - the newest supervised actions of the selected day, derived frontend-side from the already-loaded audit-chain events (no extra request) and attributed per user via the isolation scope map.
- **Supervised units** - currently running sub-agent units from `GET /api/supervisor/status`, with per-user attribution and a stale marker.

Module data comes from the admin-only `GET /api/security/overview` aggregator plus the reused `GET /api/thinking/status`, `GET /api/supervisor/status`, `GET /api/memory/health`, and `GET /api/mail/messages` endpoints, refreshed every 30 s while the Overview is open. Fetch failures (non-admin 403, backend down) leave the affected rows grey (not measured), never green. The backend design (module collectors, event-log contract, skill lifecycle) is documented in [Security Dashboard](../security/SECURITY_DASHBOARD.md).

#### Timeline view

The Timeline is a **horizontal scrubber** modelled after video-editing software. It is split into two vertical sections:

```
┌────────────────────────────────────────────────────────────────────────┐
│  [2/5  Activity panel]  │  [3/5  ReactFlow canvas]                    │
├─────────────────────────┴────────────────────────────────────────────── │
│  [Lane labels]  ████████  ███  ██████████  ████   (timeline bars)      │
│                 ←── older                      newer ──→               │
└────────────────────────────────────────────────────────────────────────┘
```

**Bottom row - timeline bars** (left-to-right = time, lanes = tool category):
- Each bar represents one completed tool call; width = duration.
- Colour = category: blue (web/search), green (files), purple (memory), orange (code/bash), pink (messaging), indigo (sub-agents), teal (tool learning - Whare Wananga training runs).
- **Ruler** at the top of the bar area shows time ticks; a **red "now" line** marks the live position (today only).
- **Mouse wheel** scrolls horizontally. **Ctrl + scroll** zooms in/out. `+`/`−` buttons in the top-right corner also zoom.
- Timeline is anchored to the bottom; lanes grow upward as more categories appear.

**Cursor (playhead):**
- Click anywhere on the bar area to place a **thick black cursor line** with a time badge in the ruler. This marks the inspection point.
- The dashed line follows the mouse as a hover indicator. The red line = live "now".
- Clicking the same position again removes the cursor.
- **`▶ live`** button (appears when scrolled away from the right edge) jumps the cursor to the current time and re-enables auto-scroll.

**Top-left - Activity panel (2/5 width):**
- Empty when no cursor is set ("Click on the timeline to inspect that moment").
- When a cursor is placed, shows all events whose bars are **touched by the cursor line** (start ≤ cursor ≤ start+duration). Point events use ±15 s tolerance.
- Each row: coloured left stripe, tool name, duration, status, args preview (`→`), result preview (`←`).
- **Live mode** (Live toggle active): panel refreshes every 3 s to show what the agent is doing right now.

**Top-right - ReactFlow canvas (3/5 width):**
- Populated when cursor is placed; empty otherwise.
- Shows the same events as the Activity panel as **ProcessNodes**: animated cards with tool name, duration bar, status icon, args snippet. Running nodes pulse.
- Layout: events sorted chronologically left→right, one row per lane, minimum 16 px gap - no overlaps.
- Click a node to **select** it (coloured glow ring); click empty canvas space to deselect.
- **Node detail window** (window-in-window in the Activity panel): clicking a node opens a floating panel over the Activity panel showing full event details and **real log lines** fetched from the server (see Log Context API below). Close with the red ✕ button.

A **date selector** in the header lets you switch between days. A **hash-chain integrity badge** has three states: red shield = tampered/deleted event, green shield = chain verified **and at least one event exists**, neutral gray shield ("No data yet") = no events for the day. The gray state exists because the API reports `chain_ok: true` for a missing or empty timeline file (an empty chain is vacuously intact), so a green "intact" claim with zero events would be misleading - the same honesty floor the Overview hero applies with its no-data state.

**Live toggle** auto-refreshes the timeline events every 5 s and enables the live cursor mode. The manual Refresh button also applies.

#### Hash-chain tamper detection

Every timeline event is written to `timeline_YYYY-MM-DD.jsonl` by `log_timeline_event()` in `vaf/core/log_helper.py`. Each event object includes:

```
{
  "ts":        "2026-05-28T14:23:01.123",
  "type":      "tool_start" | "tool_end" | "subagent_start",
  "tool":      "<name>",
  "call_id":   "<uuid>",
  "args":      "<preview>",          // tool_start only
  "status":    "ok" | "error",       // tool_end only
  "duration_s": 1.23,                // tool_end only
  "result":    "<preview>",          // tool_end only
  "prev_hash": "<sha256 of previous event>",
  "hash":      "<sha256 of this event>"
}
```

The hash is SHA-256 of the canonical JSON of the event (all fields except `hash` itself, keys sorted). `prev_hash` for the first event of the day is the fixed chain seed (`TIMELINE_CHAIN_SEED` in `vaf/core/log_helper.py`; files written by builds before the versioned seed start with bare `"GENESIS"` and still verify). This forms a forward-linked chain: deleting or modifying any event breaks all subsequent hashes, which the API detects and surfaces as `chain_ok: false`.

#### API endpoints

All timeline, log, and security endpoints require the `admin` role:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/logs` | List all `.log` files with metadata |
| `GET` | `/api/logs/{filename}?tail=500` | Last N lines of a log file |
| `GET` | `/api/logs/timeline/dates` | Dates for which JSONL files exist |
| `GET` | `/api/logs/timeline/events?date=YYYY-MM-DD&merge=true` | Merged timeline events + `chain_ok` |
| `GET` | `/api/logs/timeline/context` | Event-specific log lines (see below) |
| `GET` | `/api/security/overview` | Aggregated protection-module status (sandbox, firewall, isolation, channels, guardrails, skills) |
| `GET` | `/api/security/alert-count` | Today's security-event count + newest timestamp (sidebar dot poll) |
| `GET` | `/api/security/events?date=YYYY-MM-DD&limit=100` | Structured blocked/rejected access attempts for a day |
| `GET` | `/api/security/skills/{id}/scan` | Live re-scan detail for one skill |
| `POST` | `/api/security/skills/{id}/delete`, `.../isolate` | Skill resolution (plain admin action) |
| `POST` | `/api/security/skills/{id}/acknowledge`, `.../restore` | Skill resolution (admin plus TOTP `code` in the body) |

The security lane is gated via `require_admin` (`vaf/api/security_routes.py`). The Overview additionally reuses the existing `/api/thinking/status`, `/api/supervisor/status`, `/api/memory/health`, and `/api/mail/messages` endpoints rather than duplicating their logic.

The `merge=true` parameter (default) pairs `tool_start` + `tool_end` events by `call_id` into a single merged record. Still-running tools (no matching `tool_end`) appear with `status: "running"`.

**Log Context API** (`GET /api/logs/timeline/context`) returns log entries specific to the clicked event rather than all lines in a time window:

| Query param | Description |
|-------------|-------------|
| `ts` | ISO timestamp of the event (required) |
| `type` | Event type: `thinking_run`, `tool_end`, `tool_start`, … |
| `run_id` | For `thinking_run`: extracts the full multi-line block from `vaf_think_*.log` |
| `call_id` | For tool events: matched against the JSONL timeline entry |
| `tool` | Tool name: filters `tool_use_*.log` lines to this tool. `multi_tool_use.parallel` matches nothing - the wrapper returns before the funnel, so only its inner tool calls are logged |
| `session` | Session ID for additional filtering |
| `window_s` | Time window in seconds (default 30, max 120) |

Response always includes the matching JSONL entry (full call_id/session/scope/duration/result context). For `thinking_run` + `run_id`, the full conversation block (user, assistant, tool turns) is returned. Files larger than 2 MB are skipped.

Path-traversal is prevented on all file endpoints: the resolved path is checked to be a direct child of the log directory (no symlink escape).

#### Log gating

All debug log functions in `vaf/core/log_helper.py` (`append_domain_log`, `append_domain_log_always`, `log_attachment`, `log_thinking_run`, `log_telegram_reply`, `log_discord_reply`, `log_whatsapp_*`, `log_timeline_event`) respect the `debug_logs_enabled` setting - no files are written when it is off. The security event log (`vaf/core/security_events.py`, the `security` domain in the file rail) is deliberately exempt: rejected access attempts are audit signal, not debug noise, and are written regardless of that setting.

#### Terminal log viewer

Selecting a domain under **Log Files** opens a **terminal-style viewer** (dark `#0d1117` background, monospace font, blue timestamps, line numbers). Controls:
- **Live toggle** - auto-refreshes every 5 s.
- **Refresh button** - manual reload.
- **Filter input** - client-side line filter with match highlighting.
- **Auto-scroll checkbox** - keeps the view pinned to the bottom.

The viewer shows the last 500 lines of the selected file. The header shows the filename and, when truncated, how many lines are shown vs. total.

### 7. Code Viewer

The Code Viewer is a Monaco-based (VS Code engine) code editor in the right panel. It opens automatically when the agent creates a code file (`.py`, `.js`, `.ts`, `.html`, `.css`, etc.) or when you drag a code file into the chat.

**Features:** Syntax highlighting for 40+ languages, live refresh every 2 s while the agent is generating (shown by a pulsing **LIVE** badge), in-browser editing, and save back to disk via `POST /api/file/save` (Ctrl+S supported). The header shows the detected language, filename, save state, and last-updated time. The footer shows the full file path and unsaved-changes indicator.

**Agent context:** While the Code Viewer is open, the full file content (up to 30 000 chars) is sent with every chat message via `codeViewerFile`. The backend stores it in `session.runtime_state["code_viewer_file"]` and the headless runner injects it into `effective_input` as a numbered-line block (`--- CURRENTLY OPEN IN CODE VIEWER: <name> ---`) before calling the agent. This means the file content is never stored in the message history (avoiding raw-text bleed into the chat UI on reload). Content comes from the already-loaded viewer state, so it works for both server-path files and browser-dragged files. A small chip (filename + line count) appears on the sent user message to indicate which file was attached.

**File routing:** `.html`/`.htm` files created by the agent open in the **HTML Viewer** (see §7a below). Other code files open in the Code Viewer. Documents open in the Document Editor. **Image files** (`png/jpg/jpeg/gif/webp/svg/bmp/ico`) open in the dedicated **Image Viewer** (`web/components/ImageViewer.tsx`, green chip) - a single-image panel with the same docked-window geometry as the Document Viewer. Images are **not** synced as sidebar documents and are skipped by the attachment RAG indexer (`vaf/memory/attachment_rag.py`), so opening an image never tries to text-index it as a document.

**Image Viewer - vision description & agent context:** when an image is opened, it is described once by the vision model via `POST /api/image/describe` (cached per session in `runtime_state["image_descriptions"]`; reuses a chat-uploaded image's existing `base_description` when present). The chat-upload base-description path and this endpoint share a process-wide, image-keyed memo (`vaf/core/vision_infer.py` → `describe_image_cached`, per-key locked), so the same image is never described - or billed - twice, even when the viewer is opened mid-turn. The describe endpoint enforces the same per-user session-ownership check as the other session endpoints (fail-closed). The description is shown in the viewer as selectable/searchable text, and - **while the viewer stays open** - the frontend sends it as `imageViewerContext` with every chat message; the backend stores it in `runtime_state["image_viewer_context"]` and `headless_runner.py` injects a `--- CURRENTLY OPEN IN IMAGE VIEWER: <name> (vision description) ---` block into that turn's input, then clears it (same per-turn lifecycle as the Code Viewer's `codeViewerFile`). So the agent reasons over the image the user is looking at, and can still call `analyze_image` for finer detail. The image bytes themselves never enter the main model's context.

**Image Viewer - mark a region & ask:** a highlighter toggle in the viewer lets the user drag a **yellow rectangle** over part of the image. On release the frontend burns the rectangle into a full-res copy and also produces a zoomed crop of the region (both via `<canvas>`, same-origin so untainted). While a marking is set, an "Markierung aktiv" chip appears above the input and the next chat message carries `markedRegion: {name, annotated, crop}`. The backend (`web_server.py` chat handler) runs vision **once** on the annotated image + crop with the user's question (offloaded via `asyncio.to_thread`), stores the focused answer in `runtime_state["marked_region_answer"]`, and `headless_runner.py` injects a `--- MARKED REGION … ---` block into that turn, then clears it. The marking is **one-shot** (auto-cleared after the question) so an idle marking never re-bills a vision call - re-draw to ask again. Coordinates are never sent; the yellow box is burned into the image because vision models read "what's in the yellow box" far more reliably than pixel coordinates.

**Learning a document from the viewer.** Every document row carries a learn
button (graduation-cap icon, next to the delete button, in the dock and the
overlay list). It is disabled until the attachment indexing reports ready
(`attachment_indexed`); the click is the confirmation and sends
`learn_document_start {sessionId, name}`. The server resolves the PERSISTED
file itself (a client-supplied path is never trusted: the document must carry a
real path inside the session's attachments directory), gates on session
ownership, refuses a second concurrent learn per session, and spawns the same
batched `learn_agent` job the `learn_document` tool uses. Progress renders as
the learning banner ("batch N of M" with a determinate bar and a Cancel that
sends `learn_document_cancel {taskId}`); the button shows a spinner while
learning and a checkmark when done, and `GET /api/memory/learn-status/{doc_tag}`
serves the coverage (learned/partial, pages) across reloads. The completion
message with the full numbers arrives as a chat message.

**Maintenance banner.** Machine-level maintenance jobs (the memory re-embed
migration after an embedding-model change; generic by `kind` for later jobs)
render in the same banner family above the composer: a title, a `done/total`
count and a determinate bar, driven by `maintenance_progress` frames (see
[WEBUI_WEBSOCKET_FLOW.md](WEBUI_WEBSOCKET_FLOW.md)). There is no Cancel: the
job is resumable and finishing it is the safe state. The banner is shown to
every connected client - the job is machine-wide and memory search quality is
affected for everyone until it completes. A client connecting mid-job requests
the current snapshot with `get_maintenance_status` on socket open. On
completion the banner shows a success state and clears itself; on failure it
stays visible longer and points at the log.

### 7a. HTML Viewer

A dedicated viewer for HTML files (reports, generated web pages). Opens automatically when the agent creates a `.html` or `.htm` file - shown as an **orange chip** in the chat (distinct from violet code chips and blue download chips).

**Which message a chip belongs to.** Every file chip is bound by the `turnId` its
`file_created` event carries (see [WEBUI_WEBSOCKET_FLOW.md](WEBUI_WEBSOCKET_FLOW.md)),
never by arrival order. A file is announced from inside a tool call, so the answer it
belongs to usually does not exist yet; the chip waits in `createdFiles` until the
assistant bubble with that id appears, and is placed at the latest when the turn
completes. A grouped agentic turn renders one bubble but owns several assistant
messages, so the chips of the WHOLE turn are collected for it - a chip bound to the
turn's final answer would otherwise never be drawn. Chips are frontend state only:
they are not part of the stored history and disappear on reload or session switch.

**Features:**
- **Preview mode** (default): native iframe render with `allow-scripts allow-forms` - JavaScript-heavy reports (Chart.js, D3, etc.) work correctly.
- **Source mode**: Monaco editor (read-only, HTML syntax highlighting) - toggle with the `Preview / Source` buttons in the header.
- **Download button**: saves the file locally as `text/html`.
- Loads file content via `/api/file?path=…` if only a path is given (no pre-loaded content).

### 8. Document Editor

The Document Editor is a rich-text editor in the right panel (dock or overlay). It supports opening files (HTML, DOCX, etc.), editing, and exporting.

**Editor split:** The Web UI now has two editor paths:

- **Native DOCX editor** for `.docx` files. This path is model-driven and uses a native `DOCX -> NativeDocxDocument -> DOCX` flow instead of the old HTML roundtrip.
- **Legacy HTML editor** for HTML and other non-DOCX editor flows. This path still uses the iframe/contentEditable editor.

See also: [DOCUMENT_EDITOR_NATIVE_DOCX.md](../documents/DOCUMENT_EDITOR_NATIVE_DOCX.md)

**Layout and behaviour:** The editor keeps an A4 page layout in the right panel (210 x 297 mm sheets, 25 mm padding, automatic block pagination). Per-session state stores the open file plus unsaved editor state. For DOCX files this includes the native document model; for legacy flows it includes HTML/text content. Printing maps one sheet to exactly one page (`@page size: A4; margin: 0` plus a page break per sheet).

**Markdown files:** `.md`/`.mdx`/`.markdown` files (e.g. research and document-agent reports) are rendered to HTML with `marked` (GFM) when loaded, so the editor shows a formatted document instead of raw Markdown source. On save, `/api/file/save` converts the edited HTML back to Markdown (html2text) - a `.md` file on disk never ends up containing HTML.

**Agent context:** When the editor is open, its plain-text content is sent with each chat message. The backend prepends it as `--- CURRENT DOCUMENT (Editor): <title> ---` so the agent sees the current document. For native DOCX sessions this plain text is derived from the native document model, not from browser HTML. You can select text in the editor (e.g. placeholders); the selection is added as a chip and sent with the message. The agent can replace that range via the `replace_editor_selection` tool when a marked region exists. Without a manual marking, the agent can still rewrite a specific sentence or paragraph from the open editor document via `replace_editor_text`, which targets an exact snippet from the current editor content.  
For Document Viewer attachments (paperclip), the backend uses a **session-scoped attachment retrieval lane** (scoped by `session_id` + `user_scope_id`, TTL-based) and injects a "document context active" block plus **top-k relevant snippets** into each turn. This keeps context stable for large documents and avoids prepending full attachment text every message.  
If you want durable long-term memory from current attachments, use `learn_attached_knowledge` (requires explicit confirmation).

**Indexing-status indicator:** while an attachment is being indexed into the retrieval lane, the Document Viewer header status reflects it - an amber pulsing dot with "Indexiere…" during indexing, green "Bereit" when ready, red "Fehler" on error (driven by `attachment_indexing` / `attachment_indexed` / `attachment_index_error` WebSocket events). When `learn_attached_knowledge` learns a document, the viewer slowly walks through all pages (~2s per page) until learning finishes, so the long-running operation is visibly in progress.

**Workflow behaviour:** If the message contains the editor document block (`CURRENT DOCUMENT (Editor)`), workflow matching is skipped so the agent uses tools (e.g. `replace_editor_selection`) instead of starting a workflow.

**DOCX behaviour:** The native DOCX editor loads `.docx` through dedicated backend endpoints and saves back to `.docx` from the same native model. This avoids the old lossy `DOCX -> HTML -> DOCX` save path for DOCX editing.

**Preview and PDF:** Gotenberg/LibreOffice remains the high-fidelity Office-to-PDF solution for the Document Viewer and future preview workflows, but it is not the mutable editing engine for the native DOCX editor. The editor's immediate PDF export is generated from the current editor preview state.

**UI:** Closing the editor (X) shows a browser confirm dialog.

**Drafts from agent:** When you ask in the Web UI for the agent to write or compose text (e.g. *"Schreib mir einen Text …"*, *"Verfasse …"*, *"Write me a text …"*), the agent’s reply is also opened in the **Document Editor** as a draft. The draft is saved under the session’s data folder (`data_dir/drafts/<session_id>/entwurf.md`). You can edit the text there, improve it, and use **Save** to overwrite the draft or **Download HTML** to export it. This only applies to Web UI prompts (not e.g. Telegram), and only when the reply is substantial (after stripping `<think>` blocks).

## Local Model Idle Behavior

When the provider is `local`, the tray process only loads the model on real activity (prompt/CLI heartbeat). If there are no active WebUI WebSocket connections for 15 seconds, the model is unloaded from VRAM unless persistence is enabled.

## Switching between the local model and an API

When you switch the provider (Local ↔ API) in Settings, a centered **“Changing model”** overlay appears for about 5 seconds, after which the page reloads. At the same time, switching from Local to API makes the tray unload the model from VRAM (the `llama-server` is stopped), while switching from API to Local loads the model into VRAM. Details: [MODEL_AND_PROVIDER_SWITCHING.md](../llm/MODEL_AND_PROVIDER_SWITCHING.md).

## Local HTTP Backend Reuse

The local LLM runs as a single HTTP backend on `127.0.0.1:8080`. When a prompt arrives, VAF first checks `/health` and reuses the existing backend if it is already running (or still loading). This prevents duplicate `llama-server` processes and keeps WebUI and CLI on the same server instance.

## WebSocket Protocol

### Client → Server Messages

```json
{
  "type": "chat",
  "content": "User message text",
  "sessionId": "uuid",
  "sidebarDocuments": [],
  "editorDocument": { "name": "Document title", "content": "Plain text of editor body" },
  "editorSelections": [{ "start": 0, "end": 10, "text": "selected text" }]
}
```

- `sessionId` is required. Optional: `sidebarDocuments` (Document Viewer attachments), `editorDocument` (when Document Editor is open; plain text only, derived from the current editor state; for native DOCX sessions this is flattened from the native model), `editorSelections` (marked ranges in the editor for `replace_editor_selection`), `codeViewerFile` (when Code Viewer is open; `{ name, path, content }` of the currently displayed file - sent automatically on every message so the agent can answer line-specific questions).

```json
{
  "type": "new_session"
}
```

```json
{
  "type": "load_session",
  "id": "session-uuid"
}
```

```json
{
  "type": "delete_session",
  "id": "session-uuid"
}
```

```json
{
  "type": "get_sessions"
}
```

### Server → Client Messages

```json
{
  "type": "session_list",
  "sessions": [
    {"id": "uuid", "title": "Session Name", "date": "ISO timestamp"}
  ]
}
```

```json
{
  "type": "history_update",
  "messages": [
    {"role": "user|assistant|system", "content": "text", "timestamp": 1234567890}
  ],
  "sessionId": "uuid"
}
```

```json
{
  "type": "agent_message_update",
  "role": "assistant",
  "content": "Partial or complete response",
  "sessionId": "uuid"
}
```

```json
{
  "type": "new_log",
  "entry": {
    "timestamp": "ISO timestamp",
    "message": "Log message",
    "level": "info|warning|error",
    "source": "System|Agent|Router|Step X/Y|Info"
  }
}
```

```json
{
  "type": "tool_update",
  "subType": "start|end|error",
  "toolId": "unique-id",
  "name": "tool_name",
  "data": "arguments (start) or result (end)",
  "timestamp": "ISO timestamp",
  "sessionId": "uuid"
}
```

```json
{
  "type": "editor_apply_edit",
  "sessionId": "uuid",
  "selectionIndex": 0,
  "newText": "replacement text",
  "start": 0,
  "end": 10
}
```
Sent when the agent calls `replace_editor_selection` or when a text-targeted editor edit resolves to a concrete character range. The frontend replaces the character range `[start, end]` in the Document Editor with `newText` and removes the matching selection chip if one existed. For native DOCX sessions the edit is applied to the native document model; for legacy sessions it is still applied to HTML/text content.

```json
{
  "type": "rag_results",
  "query": "The search query used",
  "sources": [
    {
      "text": "Snippet text...",
      "full_text": "Full text...",
      "score": 0.85,
      "metadata": {"source": "file.txt", "title": "My Note", "tags": ["work", "important"]}
    }
  ]
}
```

```json
{
  "type": "cross_chat_hints",
  "hints": [
    {
      "session_id": "green123456",
      "session_name": "Invoices",
      "age": "2 days ago",
      "score": 0.82,
      "text": "The travel expense report is in that PDF"
    }
  ]
}
```
Cross Chat Hint: pointers into the user's **other** chats that were added to this turn's prompt below the retrieved memories. Rendered as its own section in the RAG snippets panel rather than inside `sources`, because that list is sorted by score and sliced at ten, so hints mixed into it would sit in the prompt and be invisible here. Sent on every turn the lane runs, with an empty `hints` list when nothing matched, so the panel stops showing the previous turn's hints. Routed with `push_update_to_user` and dropped when the scope is unknown - it carries text out of another conversation.

## Configuration

### Enabling/Disabling Web UI

**Via CLI Flag**:
```bash
vaf run --no-web  # Disable Web UI
vaf run --web     # Enable Web UI (default)
```

**Via Config File** (`config.json` in the VAF app directory):
```json
{
  "web_ui_enabled": true
}
```

### Tray Autostart

Use `tray_autostart` to control whether the tray app starts when the OS logs in:

```json
{
  "tray_autostart": false
}
```

### Sub-Agent Terminals (Global Setting)

`sub_agents_in_separate_terminals` applies to CLI and workflow execution. In the WebUI,
sub-agents still run headless and stream output to the docked panel even when this
setting is enabled.

### Web Search API Keys

Under Settings → General, the section **Web Search (API)** lets you set optional keys for web search:

- **Brave Search API Key** – Used first when set (from [Brave API dashboard](https://api-dashboard.search.brave.com/app/keys)).
- **Google Search API Key** and **Google Search Engine ID (cx)** – Used if both are set (Custom Search API and a Programmable Search Engine that searches the entire web).

If none are set, the tool uses the default path (scrape Google, then DuckDuckGo). Stored in `config.json` as `api_key_brave_search`, `api_key_google_search`, and `google_search_engine_id`.

### Port Configuration

**Backend Port**: Hardcoded to 8001 in `web_server.py`
**Frontend Port**: Auto-detected (starts at 3000, increments if occupied)

**API routing**: Next.js proxies `/api/*` to the backend via the catch-all API route (`app/api/[...path]/route.ts`), which forwards to `http://127.0.0.1:8005` (internal HTTP channel). Next.js also rewrites `/sounds/*` to the backend for notification sound files. The mail client and other Web UI features use the same proxy path (`/api/...`) so frontend calls stay same-origin while backend transport stays internal.

**Local network (other devices):** Enable Local Network in Settings → Local Network (or run `vaf server on`). Network mode is TLS-only and always uses the integrated HTTPS proxy. Access is via `https://127.0.0.1:8443` (or `:443`), and from other devices via `https://<LAN-IP>:8443`. Use `vaf server status` to see active LAN URLs. The tray restarts services automatically when network settings change.

**Entry-point behavior (`3000` vs `8443`)**:
- `:3000` is the frontend runtime/dev entry point.
- `:8443` is the HTTPS proxy entry point (available when Local Network + TLS is enabled).
- Optional strict mode: set `VAF_ENFORCE_8443_ONLY=1` to redirect requests from `:3000` to `https://<host>:8443` via `web/proxy.ts`. Keep this disabled unless `:8443` is guaranteed to be active; otherwise users may see `ERR_CONNECTION_REFUSED`.

## Development

### Running Frontend Locally

```bash
cd web
npm install
npm run dev
```

### Building for Production

```bash
cd web
npm run build
npm start
```

### Frontend Dependencies

See `web/package.json`:
- `next`: 16.1.6
- `react`: ^18
- `lucide-react`: ^0.300.0 (icons)
- `tailwind-merge`: ^2.2.0 (utility merging)
- `clsx`: ^2.1.0 (conditional classes)

## Integration with CLI

The Web UI runs alongside the CLI interface:

1. **Startup**: When `vaf run` executes, it starts:
   - FastAPI backend on port 8001
   - Next.js frontend (auto-detected port)
   - Opens browser automatically

2. **Message Flow**:
   - User types in Web UI → WebSocket → `input_queue` → CLI processes
   - CLI generates response → `push_update()` → WebSocket → Web UI displays

3. **Session Sync**:
   - Web UI session changes → Commands to CLI → Agent reloads history
   - CLI saves messages → Broadcast to Web UI → UI updates

## UI Components

### Message Bubble

- **User**: Right-aligned, indigo background, rounded corners
- **Assistant**: Left-aligned, white background with border, includes bot icon. When the agent uses tools mid-turn, the reply is split: the part before the tool stays in one bubble, the part after the tool appears in a new bubble so tool usage and the follow-up answer are visible separately.
- **System**: Timeline-style with icons, minimal styling
- **Tool**: Card-style component showing tool name, arguments, status (running/completed), and result

### Tool Message

- **Compact header (single row)**: status dot (black pulsing dot while running → green check on success → red alert on error), tool name, the main argument, and a right-aligned **result counter** (`läuft…` while running; line/size count or sub-agent runtime once done).
- **Status**: Dynamic border color (Blue=Running, Green=Success, Red=Error), an indeterminate progress bar while running, and a brief success flash on completion.
- **Collapsible**: Details (args/result/output) are collapsible to save space; the open/close logic is unchanged (sub-agent tool cards still open the docked panel).
- **Live Updates**: Updates in real-time as tool execution progresses

### Sidebar

- **Collapsed**: 64px width (icon only)
- **Expanded**: 288px width (on hover)
- **Smooth Transition**: 300ms duration

### Input Box

- **Features**: Attachment button, text input, voice input, send button; file chips and token stats above the form when relevant. When the document panel is open with attachments, **quote chips** appear above the input: any text selected in the panel is automatically added as a quoted snippet (colored by order: dark, orange, pink, blue, green). Chips show a red hover state; clicking a chip removes that quote only. Sent messages combine the typed input and all quote snippets (joined by blank lines).
- **Layout**: On a **new chat** (no messages), the input bar is shown **centered** in the viewport with a short welcome line (“How can I help you?”). After the first message is sent, the bar **animates** (≈500 ms) to its **fixed position at the bottom** and stays there for the rest of the conversation.
- **States**: Disabled during loading, focus ring on interaction.
- **Submit**: Enter key or click send button.

## Best Practices

### Performance

- **Bounded UI buffers**: UI keeps recent entries bounded for smooth rendering (for example, the sub-agent console panel keeps the latest 500 lines).
- **Session list paging**: Backend session list limit is configured server-side (currently 500 in `web_server.py`).
- **Auto-Scroll**: Smooth scroll to latest message
- **Debouncing**: WebSocket messages processed immediately (no artificial delay)

### Error Handling

- **Connection Loss**: Status indicator shows "disconnected"
- **Reconnection**: Manual page refresh required
- **Invalid Messages**: Silently caught and logged to console

### Security

- **CORS**: Restricted by middleware (localhost and private-LAN origin patterns), not unrestricted `*`.
- **Authentication**: The dashboard treats `GET /api/auth/me` as the source of truth for a valid session (JWT in `Authorization: Bearer` and/or `vaf_token` cookie, validated server-side) and redirects to `/login` when that call is not OK.
- **Next.js edge proxy (`web/proxy.ts`)**: Next.js 16 uses this file as the **Proxy** middleware (replacing the older `middleware.ts` name). It guards routes by **validating the `vaf_token` JWT's `exp` claim** (an edge-safe payload decode), **not by mere cookie presence** - an expired, malformed, or missing token counts as unauthenticated. So `/` without a valid token → `/login`, and `/login` with a valid (unexpired) token → `/`. A present-but-expired cookie is **actively cleared** by the guard (the browser cannot delete an httpOnly cookie itself); without this, an expired-but-present cookie used to fight the client (401 on `/` but a presence-only 307 from `/login`) and cause an infinite `/login ↔ /` redirect loop. The backend (`/api/auth/me`, `/ws`) stays the real authority - the guard only has to **agree with it on expiry**, so never regress this to a presence-only check. After a successful `/me` on the login page, the app uses a **full navigation** to `/` so session and assets align with the HTTPS entry point.
- **Login-page bounce (cookie-only, deliberate)**: the login page's already-authenticated check calls `/api/auth/me` with the **cookie only** (no `Authorization`/localStorage fallback), so the client and the server-side gate in `web/proxy.ts` route on a **single authority**. Never re-add a Bearer/localStorage fallback to this check: a still-valid localStorage token next to an expired cookie makes `/` and `/login` disagree forever and reintroduces the `/login ↔ /` navigation loop (live incident 2026-07-22). As a backstop, a `sessionStorage` circuit breaker (`vaf_login_bounce`) keeps the user on the login form after two bounces within 15 seconds instead of looping, and a failed cookie check also drops any stale localStorage token.
- **Cookie lifetime = token lifetime**: the backend derives the `vaf_token` cookie's `max_age` from the JWT's own `exp` claim at the single place that sets the cookie (`_set_auth_cookie`/`_cookie_max_age_for` in `vaf/api/auth_routes.py`), so the cookie can never outlive the token it carries; the old hardcoded 30-day cookies are gone and `remember_me` no longer extends the session. This removes the cookie/token desync class behind the redirect loop structurally.
- **`GET /api/auth/me` (backend)**: When both `Authorization: Bearer` and `vaf_token` are present, the server tries the **Bearer token first**, then the cookie, so a stale cookie cannot shadow a valid in-memory token. This applies to the dashboard's session check; the login-page bounce deliberately sends no Bearer header (see above).
- **Origin scope note**: Auth state can differ between `http://localhost:3000` and `https://localhost:8443` because cookies/storage are origin-scoped.

## Troubleshooting

### Web UI Not Starting

**Check**:
1. npm installed: `npm --version`
2. Backend/proxy status: `vaf server status`
3. Frontend process started and reachable (`http://localhost:3000` in local mode or `https://localhost:8443` in TLS/network mode)

**Logs**: `logs/web_debug.log`

### Server not reachable (full-screen message)

When the backend is down or unreachable, the Web UI shows a full-screen message: *"Server not reachable. Make sure VAF is running (e.g. \"vaf run\")."* with a **Try again** button. This appears when the initial auth/health check fails (for example VAF not started, proxy/backend not reachable, or TLS endpoint unavailable).

**What to do**: Start VAF (`vaf run` or open Web UI from the system tray). Ensure backend/proxy ports are free, then click **Try again** or refresh the page.

### `Bad Gateway` on `https://…:8443` (including `/login`)

The integrated HTTPS proxy forwards page requests to the Next.js process on `http://127.0.0.1:3000`. If the frontend is still starting or was restarted (tray log: stopping/starting frontend), the proxy returns **502** with body `Bad Gateway`. Wait until the tray reports the frontend ready on port 3000, then reload.

### WebSocket Connection Failed

**Causes**:
- Backend not running
- Port 8001 blocked by firewall
- Browser security restrictions

**Solution**: Check browser console for errors, verify backend is running

### Messages Not Appearing

**Causes**:
- Session mismatch (switched sessions during response)
- WebSocket disconnected
- Message filtering (e.g., "Agent Thinking..." is intentionally hidden)

**Solution**: Refresh page, check connection status

## Session Workspace Window (file browser)

Every open chat shows a slim workspace chip above the input field - leftmost element of the indicator bar (RAG/token displays stay on the right; the bar mirrors the input row geometry so nothing sticks out beyond the input field). It is a standing "this chat has its own workspace" affordance, not a "you already saved something" indicator: opening a chat creates its (empty) workspace folder right away, so the chip appears immediately, before any file, upload, or agent output ever lands in it. Clicking it opens a centered window in Context-Window size with an explorer-style file browser: a Back button with navigation history, a clickable address bar (which covers "up one level", so there is no separate Up button), and an icon grid (folder and file tiles) - empty at first, same as any other folder.

Files and folders can be deleted from the tile hover actions; a confirmation dialog inside the window warns before anything is removed (folders delete recursively with their item count shown). `POST /api/session/workspace/delete` enforces the same ownership and boundary rules as browsing: targets must stay inside the workspace root, the root itself cannot be deleted, traversal names are rejected.

- **Root = the chat's own folder** (`VAF_Projects/<uid[:8]>/<session_id>/`), which can contain several project folders; legacy sessions fall back to their single project directory. Resolution lives in `_resolve_session_workspace` (`vaf/core/web_server.py`), which takes a `create` flag: `GET /api/session/workspace` and the upload endpoint pass `create=True` (opening a chat, or saving into it, always has somewhere to write); browsing or deleting a specific entry inside an already-known workspace stays read-only (`create=False`, the default) and never conjures a folder into existence just to fail a lookup inside it.
- **Deleting a chat cleans up an unused workspace.** `SessionManager.delete()` (`vaf/core/session.py`) removes the chat's workspace folder too, but ONLY when it is still empty at that point (no visible files or folders, ignoring dotfiles like the channel label below) - a workspace that was actually used for real output is never touched by deleting the chat; only the session record goes away and the files stay on disk. When the folder DOES survive, the chat's title is written into the workspace label first (`_preserve_workspace_title`, unless the user already set one via Rename - an explicit rename always wins), so the orphaned folder keeps a human name in the Data Explorer instead of showing the raw session-id folder name (the title lives in the session record, which is about to be deleted). This is what keeps eager creation from littering `VAF_Projects` with abandoned empty directories. Two safety rails on that emptiness decision: while a sub-agent or workflow is still running (or pending) for the session, workspace removal is skipped entirely - "empty right now" says nothing when a live run may drop its first output file between the check and the rmtree; and the recursive emptiness walk passes `onerror=raise`, because `os.walk`'s default silently skips unreadable subdirectories (a permission-denied subtree full of files would otherwise read as "empty" and be deleted - anything that cannot be fully inspected is kept).
- **In-chat image uploads** are stored here too, under `attachments/`, and emit a `file_created` event.
- **Navigation:** folders open on click, a breadcrumb and a `..` row navigate back. Browsing is strictly confined to the chat folder - `subpath` values are normalized server-side and escapes are rejected with 400 (`_resolve_workspace_subdir`).
- **Download** per file via the existing `GET /api/file?path=...` endpoint; file rows are draggable out of the browser (Chromium `DownloadURL`).
- **Upload** via the footer button (multi-select) or by dropping files anywhere into the list - they land in the currently open folder. Sent as base64 JSON to `POST /api/session/workspace/upload` (25 MB cap, filename sanitized).
- **Data source:** `GET /api/session/workspace?sessionId=...&subpath=...` lists non-hidden folders (with item counts) and files (size, modified).
- **User isolation:** both endpoints verify session ownership (`metadata.user_scope_id` vs. the requesting user; local admin exempt; a session with NO recorded scope is admin-only, same policy as the WebSocket gate) and `GET /api/file` refuses downloads from another user's `VAF_Projects/<uid[:8]>/` subtree - see `docs/security/USER_ISOLATION.md`.
- **Live refresh:** chip and window refresh on session switch, on every `file_created` event and after uploads.

This matters most when VAF runs as a server: the browser is then the only way to get files in and out of the workspace.

### Central Data Explorer (all workspaces)

The workspace window's index view lists ALL of the requesting user's workspaces (`GET /api/workspaces`, security model in `docs/security/USER_ISOLATION.md`). Its visual language, implemented in the index grid in `web/app/page.tsx`:

- **Amber folder icon** = workspace of a live chat. **Gray folder icon + "chat deleted" badge** = an orphaned workspace: the chat was deleted, its files were kept (deleting a chat never deletes files). A **green dot** marks the currently open chat's workspace.
- **Display name precedence** (`resolve_workspace_display_name`): user-set label (Rename) > live chat title > raw folder name (== session id). Because delete-time title preservation (above) writes the title into the label, orphans normally keep their chat's name; a raw session-id name only appears for orphans created before that behavior existed.
- **Sort order**: one flat list, live chats first, orphans at the end (each group alphabetical by display name). Deliberately NOT two sections - server-side sort in `list_my_workspaces` (`vaf/core/web_server.py`).
- **Info button** in the header toggles a legend panel explaining exactly these signals (an explicit panel, not a hover tooltip, so it works on touch). Rename changes only the display label; Delete removes the folder and its files permanently (confirmation dialog).
- **Search** (input in the window header, index view only): workspace names filter instantly client-side; from 2 characters the same query also runs a debounced server search (`GET /api/workspaces/search`, `_search_one_workspace` in `vaf/core/web_server.py`) over file/folder NAMES and text-file CONTENTS inside the caller's own workspaces. A tile stays visible when its name matches OR the server found matching files; matched paths render under the tile (snippet in the tooltip). The query is the only client input, never a path, so the per-user root scoping is the boundary; on top of that:
  - **Symlink containment (user-isolation critical):** file CONTENTS are read only when the file's real path stays inside the workspace root. `os.walk` runs with `followlinks=False` (symlinked directories are never descended), and a symlinked FILE that escapes the workspace (an agent/coder can create files here, so a user could plant one pointing at another user's `VAF_Projects` file or any host file) is never opened for content - it can still match by NAME, which is a no-leak. The top-level walk also skips any workspace entry that is a symlink escaping the user's root.
  - **Bounded on every axis:** per workspace 5 hits, 400 content files, an entry (dir+file) breadth cap, depth 6, 1 MB per file; binary files (NUL in the first 8 KB) match by name only; dotfiles skipped. On top of the per-workspace caps a whole-request budget (wall-clock deadline plus total files-read and entries-visited across ALL of the user's workspaces) stops the walk on the shared thread pool, so a crafted wide/deep tree cannot monopolize it (each debounced keystroke fires a fresh search).
  - Stale responses are dropped via a sequence counter that is bumped on every change including clearing the box, so an in-flight response can never paint match chips under an emptied search field.

## Future Enhancements

Potential improvements:
- File upload support (Paperclip button currently placeholder)
- Multi-user support with authentication
- Persistent WebSocket reconnection
- Message search and filtering
- Export conversation history
- Mobile-responsive design improvements
