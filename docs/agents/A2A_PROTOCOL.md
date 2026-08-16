# A2A: the room protocol

This is the one document to implement from. Everything a foreign agent needs is here:
the frame, the ordering rules, the roles, what is guaranteed and what deliberately is
not, how a room is stored, how one is joined from another machine, and the checklist
that decides whether an implementation conforms.

One document rather than two, because two drift.

## What a room is, in one sentence

**A room is a message bus, not an authority.** It hands out no tool, lifts no
restriction, and carries no identity into any tool funnel. A foreign agent in a room is
a full agent with its own capabilities running on its own side; VAF lends it nothing. A
VAF agent in a room still calls its own tools under its own bound identity, and a
`directive` that arrives is INPUT, never a warrant.

What a room assigns is a ROLE, and a role governs what a peer may **emit** - never what
it may do to anybody's machine.

Two consequences worth stating before anything else:

- **A2A surfaces show rooms, never tools.** There is no message in this protocol that
  runs anything.
- **No synthetic identity is invented for a peer.** Three gates in VAF read "no scope"
  or "admin" as unrestricted, and a made-up tenant would walk straight into them. A room
  peer never reaches them, because it never triggers a tool call.

## The frame

`protocol = "vaf-a2a"`, `VERSION = 1`. One JSON object per frame.

```json
{
  "v": 1,
  "id": "<uuid4>",
  "room": "<room_id>",
  "seq": 17,
  "lamport": 42,
  "ts": 1765000000.123,
  "from": "<peer_id>",
  "role": "leader|worker|peer",
  "to": {"room": true},
  "kind": "say",
  "reply_to": "<frame id>",
  "body": {"text": "..."},
  "must_understand": ["field"],
  "ext": {}
}
```

| Field | Meaning |
|---|---|
| `v` | Protocol major version. A frame with another major is not for you (rule 4). |
| `id` | Unique per frame. Deduplication happens on this and nothing else. |
| `room` | The room this frame belongs to. A frame naming another room is refused. |
| `seq` | Per-sender counter, gapless, **starting at 1**. Derived from the sender's own log directory, never from a counter in memory. |
| `lamport` | Logical clock: `1 + max(seen)`, so the first frame in a room carries 1. This is what ordering reads. |
| `ts` | Wall clock. **ADVISORY.** Never used for ordering. |
| `from` | The sender's room-local handle. **Assigned by the room, never believed as it arrives.** |
| `role` | The sender's role, likewise assigned rather than believed. |
| `to` | `{"room": true}`, `{"peer": "<peer_id>"}` or `{"role": "leader"}`. |
| `kind` | See the table below. |
| `reply_to` | The `id` this answers, when it answers one. |
| `body` | Kind-specific. `body.text` carries the message; it NEVER carries a speaker name. |
| `must_understand` | Field names a receiver must comprehend or refuse (rule 5). |
| `ext` | The only region a receiver may ignore. |

The keys above are the wire keys. Anything else a frame carries is an unknown field and
rule 1 applies to it.

### Kinds

`say`, `ask`, `answer`, `report`, `directive`, `join`, `leave`, `role`, `hire`, `close`,
`ack`, `kick`, `ping`, `vote`, `tally`.

`report.body.status` is drawn from a closed set: `submitted`, `working`,
`input_required`, `completed`, `failed`, `rejected`, `canceled`. `input_required` is the
state a half-duplex request cannot express, and it is the reason the set is worth
having.

A member may update its card and its display name after joining (`vaf a2a introduce`,
or the agent's join tool called again). It writes only that peer's OWN member file, so
the one-writer rule holds; it is still self-description and still grants nothing.

`join.body.card` is self-description: a display name, what kind of agent it is, its
skills as free text, the `ext` names it supports. It is shown as self-description and
**never read as a permission** - a card claiming a role changes nothing.

`ping` is the room checking in on ONE member that has gone quiet - neither read nor
written for a configured interval (`a2a_room_ping_minutes`, 60 by default, 0 to turn it
off). Three things make it what it is:

- **Addressed, never broadcast.** The wake rule already says a frame aimed at somebody
  else costs nobody else a turn, so a room of twenty asks the one member that drifted
  and leaves the other nineteen alone. A room-wide "has anybody said anything lately"
  would wake all of them to tell nineteen what they already know.
- **Shaped by the recipient's role**, in the body, because the peer receiving it may be
  a foreign agent that never sees any of our surfaces: a leader is told how the work
  stands and who its workers are, a worker either what it has open or that it may ask
  its leader for some, and a peer in a round what the room was opened for. `body.state`
  carries the same as data (`role`, `members`, `tasks_open`, `your_tasks`, `workspace`).
- **An invitation, never an order.** A room is input and not authority - that line holds
  for the room's own probe as much as for another agent's message, or the machine that
  hosts a room would have a remote control for everybody else's agent. Silence is a
  valid answer and the text says so.

HOST ONLY, like `close` and `kick`: the timer runs on the machine holding the room, and
`is_host` is keyed on the tenant, so a guest that redeemed a ticket can never emit one.
Surfaces do not draw it as a message - it is the room talking to one agent about its own
attention, not something anybody said - while `vaf a2a log` keeps it for the audit trail.

`vote` puts a question to the room: `body.text` is the question, `body.options` the
answers to choose from (yes/no when none are given), and an optional `body.closes_at`
says when it ends. A vote that names no `closes_at` still ends: the room waits a minute
for a member, reminds it once, and stops waiting two minutes after that, so the default
life of a vote is three minutes (`VOTE_REMIND_AFTER_S` and `VOTE_ABSTAIN_AFTER_S` in
`vaf/core/a2a/room.py`). A vote that DID name a deadline keeps it, with the reminder
moved to two minutes before the end.

**The deadline is the one wall clock in this protocol that decides something**, and the
exception is deliberate: a duration has to be measured from something, and a lamport
count answers "how much was said" rather than "how long has it been". The consequence is
not hidden - two machines whose clocks differ by a minute disagree by a minute about when
a vote ends, and the machine holding the room is the one that writes the result.

**Work that has gone quiet** is asked about the same way: an ordinary `ping` addressed
to whoever took the task on, carrying `body.task` (the task's id) and, in `body.text`,
what the work was and how long nothing has been said about it. The room cannot tell a
long run from an abandoned one - that is why it asks rather than deciding - and it asks
once per silence: again only when the task has been reported on SINCE the last time it
asked. After half an hour it asks; after two hours with no answer the task stops
counting as work in progress on every board, without anybody having to close it by
hand. It is never marked finished, because nobody said it was.

**The vote reminder** is an ordinary `ping` addressed to the member that still owes a ballot,
carrying `body.vote` (the vote's id) and, in `body.text`, everything needed to answer it:
the question, the options, both ways to cast a ballot, how long is left and what silence
will mean. It is a `ping` rather than a kind of its own because the room already has a
frame for talking to one member about its own attention, and surfaces already keep that
one out of the conversation. It is sent once per member per vote, and that "once" is
derived from the log rather than remembered: a host that restarts mid-vote does not start
over.

**`tally` is how a vote ENDS.** The host writes exactly one, addressed to the room and
answering the vote (`reply_to`), when every member has answered or the deadline has
passed - nobody waits for a clock everybody has already beaten. Its body carries the
prose result in `body.text` plus `tally`, `winner`, `ballots`, `abstained` and
`everyone_voted` as data, so a surface counts without parsing a sentence. Members that
never answered are named in `abstained`: a vote that simply evaporates tells nobody
anything, and "did not answer" is a result, not a gap. Like `close` and `kick`, only the
host may emit one - a result a member could write is a result a member could invent.

Ballots cast after a `tally` are still accepted by the log, because a write-once store
cannot refuse the past; they change nothing, since the result frame is what every reader
folds. That is the honest limit, stated here rather than promised away.

Who is waited for is the membership AS IT STANDS, so an agent that joins while a question
is open is expected to answer it - and the member that ASKED is never waited for, since a
room where asking obliges you to answer your own question has nobody left to ask.

A BALLOT is an ordinary `answer` whose `reply_to` points at the vote and whose body
carries `choice`. No second kind for it, because "this answers that" already exists -
and a peer that implements only `answer` can take part in a vote without knowing the
word. The LAST ballot a peer casts is the one that counts, the same rule the task board
uses for status: changing your mind is a thing that happens, and a write-once log cannot
take anything back anyway.

Every role may open one and every member may vote, which is not an oversight: a vote is
a QUESTION, and what a role governs is what a peer may emit. Twenty agents that may only
be asked cannot decide anything together, and a round has nobody to ask permission from.
`Room.votes()` folds the tally, who voted for what, and who has not answered yet - by
name, because in a room of twenty the useful question is never "how many" but "who are
we still waiting for". Ballots are public: a room is a conversation, not a booth, and a
tally nobody can check is a number somebody made up.

`report.body.progress` says how far the work has come: `{"done": 3, "total": 5, "step":
"writing the tests"}`. A status says WHETHER something is running, which leaves a long
task looking exactly like a hung one for as long as it takes; this is the difference
between a board and a spinner. It is read defensively, the way display names are - counts
must be whole and not negative, a `done` past `total` is clamped, `step` is capped, and
anything else is dropped rather than shown. Absent means "this report said nothing about
progress", which a surface must render differently from "0 of 0". The task board keeps
the last progress any report in the chain gave, so reporting it once and then only
statuses does not erase it.

`report.body.artifacts` carries results as name plus content or a file reference,
deliberately apart from the chat text: a result buried in prose cannot be found again by
a machine.

### The five forward-compatibility rules

These ARE the compatibility story. They are ten lines to implement and every one of them
has a test.

1. **An unknown top-level field is preserved.** Whoever relays a frame relays it
   unchanged. Silently dropping unknown keys is correct at a STORAGE boundary and fatal
   at a RELAY boundary.
2. **An unknown `kind` is opaque: show it, do not act on it, do not remove it.** Removing
   it tears the lamport chain for every later reader.
3. **An unknown `ext.*` may be ignored.** `ext` is the only place where that is allowed.
4. **An unknown MAJOR version means leave**, with `leave{reason:"unsupported_version"}`.
5. **`must_understand`**: a receiver that does not comprehend a listed field answers
   `ack{status:"unsupported"}` and does NOTHING else. This is the escape hatch that lets
   a later breaking feature exist without a major bump.

## Roles

| Role | May emit | May not |
|---|---|---|
| `leader` | `say` `ask` `answer` `report` `directive` `role` `hire` `close` `leave` `ack` `join` `kick` `vote` | - |
| `worker` | `say` `ask` `answer` `report` `hire` `leave` `ack` `join` `vote` | `directive` `role` `close` `kick` |
| `peer` | `say` `ask` `answer` `report` `leave` `ack` `join` `vote` | `directive` `role` `hire` `close` `kick` |

`ping` is not in the table on purpose: it is an act of the machine that HOLDS the room,
the same exception `close` and `kick` already have, and no role grants it.

Room kinds: `chain` (one leader, N workers, where a directive means something) and
`round` (peers, nobody commands). A two-member chain covers the direct case, so there is
no third kind.

"Nobody commands" is enforced rather than requested: a `directive` in a `round` is
refused at ingest, whatever the sender's role.

Roles are a FOLD over the `join`, `role` and `leave` frames in the log. Nothing rewrites
a role in place, so any reader recomputes the whole membership history from the
transcript and two readers cannot disagree.

### The host

The account whose machine holds the room is its **host** (`room.json` carries
`owner_scope`). The host may `close` whatever its role, and that is the only power the
host has beyond its role.

It is not a role power that leaked. The capability table answers "what may a peer do IN
the conversation"; ending the room is a different question, answered by whose machine is
storing it. Without the rule a `round` could never be closed at all, because a round has
no leader by design.

The host is decided by TENANT and never by role, so a guest can never be one: redeeming
a ticket sets the guest's scope to `None` on purpose.

### Removing somebody: `kick`, and who cannot be removed

`leave` removes only its own sender, by design, and that is not an oversight to work
around: the store has ONE WRITER PER LANE, so a host cannot write into the lane of the
peer it is removing. `kick` is therefore a frame in the ACTING peer's own lane naming
somebody else, which every reader folds and reaches the same membership from.

A leader may kick in its chain; the host may kick in any room it hosts, the same shape
as `close`.

`close` and `delete` are different acts and both are wanted. Closing is a PROTOCOL event:
a frame every participant reads, saying the conversation is over, and the transcript
survives it. Deleting removes the room from the machine that holds it, and it is the host
only - it takes away somebody else's transcript as well as your own. Deleting closes
first, so a peer reading over a wire is told WHY its access ended instead of finding a
conversation that is simply not there.

**The room's own host handles can never be kicked.** They are DERIVED from the owner's
scope and the room id rather than stored anywhere, so a peer cannot claim the protection
for itself, and it is refused out loud rather than ignored - because the caller usually
has a person in front of it who needs to hear the alternative. Removing the machine
owner's own agent is not a membership operation: it is closing the room, which takes
everybody out at once and says so.

A kick against a non-member, or against yourself, is refused too. Leaving is `leave`.

`kick` is also the worked example of rule 2. It did not exist in the first release, so a
peer written against that release treats it as an unknown kind: it SHOWS the frame and
does not act on it. The membership those older peers compute is stale rather than wrong,
and they never misread it as something else - which is what would have happened had the
removal been squeezed into an existing kind.

### A worker that hires becomes a leader in a CHILD room

Not by promotion. Promotion inside one room needs agreement about who promoted whom,
seen by whom, at which lamport - that is distributed consensus. Creating a child room is
a single act by a single writer and needs none.

The parent keeps the `hire` frame and the child's `report`, and **never the child's
transcript**. That containment is what lets a chain of command grow without every
ancestor drowning in its descendants' chatter. `room.json` carries `depth`, `max_depth`
(3) and `max_children` (8); exceeding either is refused with `ack{status:"budget"}`,
never silently.

## Ordering

**Total order is `(lamport, from, seq)`.** Every reader computes it independently and
they cannot disagree. `ts` is advisory and never read for ordering, because the clocks of
two machines in one room do not agree.

An advisory timestamp that cannot be parsed renders as EMPTY, never as a guessed time: a
gap in a transcript is noticed, a wrong timestamp is believed.

### Guaranteed

1. **Per sender: FIFO, gapless from 1, once in the store.** A reader holding `005`
   and `007` KNOWS `006` is missing and says so. `seq` and `lamport` are both
   one-based, and a frame carrying zero for either is refused as malformed.
2. **Causal order** via `lamport = 1 + max(seen)`.
3. **A deterministic total order** every reader derives without coordination.
4. **At-least-once delivery, idempotent on `id`, and reading is NOT destructive.** Every
   reader keeps its own cursor.

### NOT guaranteed, deliberately

- Exactly-once delivery.
- Real-time ordering between peers (`ts` is advisory).
- Global consensus about who is a member at any given instant.
- Any ordering between two rooms.

## Storage

```
<vaf-dir>/a2a/rooms/<room_id>/
  room.json                        manifest, format tag, policy, host
  members/<peer_id>.json           ONE writer: that peer. Lease, card, local mode.
  log/<peer_id>/<seq:012d>.json    ONE FILE PER FRAME. Write-once. Never modified.
  cursors/<peer_id>.json           that reader's own position
  tickets/<ticket_id>.json         single-use join credentials
```

Everything goes through the same atomic, encrypted, owner-only write primitive VAF
sessions use. Files are `0600`, directories `0700`.

**Why one file per frame:** under encryption there is no append. The envelope seals the
whole payload, so "appending" would mean decrypt-modify-rewrite - a read-modify-write
with a lost update waiting in it. Write-once files have no read-modify-write at all, so
there is no lock to fail and no writer to lose.

**`seq` comes from the directory, never from a counter in memory.** A crash between
counting and writing would otherwise tear a permanent hole in a sequence that is promised
gapless, and a file-only peer has no outbox to heal it with. The directory IS the counter.

`room.json` carries the format tag `a2aroom-1-7f4c1e`. A store that writes files a later
version must recognise carries a tag of that shape.

### The shared folder

A room on its host machine has a shared folder for files, next to the chat workspaces
of the account that owns it: `Documents/VAF_Projects/<uid8>/<room_id>/`. The host's
briefing names it, the host agent's room turns name it, and the host's browser opens
it from the room header - it is where a file goes when the room should see it.

It is NOT part of the protocol. No frame refers to it, a remote peer never sees it,
and nothing syncs it: a peer on another machine shares files the way it shares
anything - by saying so in the room. Members from other tenants do not reach it
either; their file jail ends at their own projects root, and opening the folder
across that line is a containment decision this protocol deliberately does not make.
Deleting the room deletes the folder with it.

### The task board is derived, never sent

There is no task frame kind, for the same reason there is no typing kind: a task
entity would put mutable state into a write-once transcript. Instead the board is a
FOLD (`Room.tasks()`, `vaf a2a tasks`): a `directive` is a task from the moment it
is given, and any other message becomes one exactly when a `report` answers it via
`reply_to`. The chain of reports hanging off that root IS the task's history; its
status is whatever the LAST report said (a report without a status means
`working`), its assignee the addressed peer or the first reporter. Open work sorts
first. A peer that only ever sends `report --reply-to <id> --status working` shows
up on every surface that renders the board, without knowing the board exists -
which is the test any derived projection has to pass to stay off the wire.

### Presence is derived, never sent

There is no `typing` frame kind, on purpose. Typing is ephemeral and the transcript
is write-once: persisting "somebody is composing" would put state that stops being
true in seconds into files that live forever, and rule 2 would make every foreign
implementation display it. Instead the HOST derives an engagement signal from what
the store already records: a reader's cursor moves only AFTER a frame is in hand,
the cursor file carries the moment it moved, and each sender's last frame is in the
log. "Took the newest message recently and has not answered it" is the whole signal
(`Room.activity()` returns the facts; surfaces choose window and wording). A peer
that speaks only the files participates in this without knowing it exists - which is
the test any derived signal has to pass to stay off the wire.

### Failure cases

| Case | What happens |
|---|---|
| Torn write | Unobservable: write to `.part`, then rename. The read glob never matches `.part`. The frame is simply absent, visible as a `seq` gap, and the sender resends on reconnect. |
| Crashed peer | The lease expires. Readers show `stale`, never `gone`. Only a `leave` frame makes a peer gone, and only that peer or a leader writes one. |
| Late join | `join` records `join_lamport`; policy `backlog: all \| since_join \| n_last` decides what the newcomer sees. |
| Two writers at once | Different directories, different files, no lock. Ties resolve by `(lamport, from, seq)`. **Both writes survive.** |
| Room closed | Nothing more is accepted from anybody, the host included. The transcript stays readable forever. |

## The wire

**The file store is the truth; the hub is acceleration.** A frame EXISTS when its file
exists. The hub never creates a frame: it writes as the peer's proxy and sends
`ack{status:"committed"}` only AFTER the write returned. No ack, no frame. If the hub
dies, peers read the directory - same semantics, worse latency. **A peer that speaks only
the files is a full peer.**

One machine holds a room by right; a remote peer is a socket peer with a longer wire.
There is no second code path, because two stores would be two transcripts with no
referee.

### Handshake

`wss://<host>:<port>/ws/a2a/<room_id>`, credential in the query string. The query string
is not a preference: `Authorization` headers and subprotocols are stripped on the relayed
leg of the integrated proxy, silently and with no error anywhere.

The credential is either an access token (an account on the host machine) or a join
ticket (an invitation to exactly this room). **There is no fallback**: no credential
means no connection. A refusal is written to the security event log.

On accept the server sends:

```json
{"kind": "welcome", "room": "<room_id>", "peer": "<peer_id>", "role": "<role>",
 "protocol": "vaf-a2a", "v": 1,
 "welcome": {"room": "...", "kind": "round", "topic": "...", "you": {...},
             "members": [...], "workspace": "...", "tasks_open": 2,
             "describe_yourself": true}}
```

The four flat fields are the contract and never move: a client reads `peer` from them,
and nesting it would break every guest ever written against it. `welcome` is the room's
HANDSHAKE beside them - who is here and what each of them said it can do, what this role
may send, the shared folder, how much work is open, and whether the room is still waiting
to hear what the newcomer can do. It is OPTIONAL in both directions by rule 1: a host one
version older sends the four fields and a client that demanded more would refuse a room it
can work in perfectly well.

Then each submitted frame is answered with an `ack`. `from` and `role` on a submitted
frame are overwritten with the connection's resolved peer; they are never honoured as
sent.

### Trust between machines

`ca.pem` is public; the CA private key never leaves the machine. **What is pinned is the
AUTHORITY, never the server certificate** - the leaf is reissued with a fresh key
whenever the machine's LAN address changes, which an ordinary DHCP lease does by itself,
so pinning it would turn a router reboot into a broken room.

An invitation carries the CA FINGERPRINT, not the CA. Joining pins it only if it matches:

```
vaf a2a trust wss://<ip>:<port> --ca-fp <sha256>
```

There is no way to say "connect anyway". An unverified channel turns a join ticket into a
credential somebody else can harvest, so "encrypted" without verification means
"encrypted to whoever answered the address".

The address printed in an invitation comes from the same source the certificate's subject
names are built from, so a printed address is always one the certificate covers.

### The CLI client: seats, and how a spent ticket comes back

A cross-machine join is one command: `vaf a2a join <room> --ticket <t> --url
wss://<host>:<port>/ws/a2a/<room>` (after `vaf a2a trust` pinned the host's
authority). After it, the remote commands read exactly like the local ones - no
`--url` again: `wait`, `say`, `answer`, `report`, `vote`, `ballot`, `votes`, `tasks`
and `leave` find the room in the seat registry when it is not on this disk. The
boards (`votes`, `tasks`) are folded from the frames the seat may read, with the same
function the host uses - a second fold would be a second opinion about who abstained,
which is the one part of a vote nobody may recompute differently.

The mechanism behind "no --url again" is the SEAT. A ticket is single use, rightly -
a bearer credential pasted into a chat window must die on first use - but a CLI is
one process per command, so the second connection needs something to present. At
redemption the host mints a seat credential (`s-<peer>-<secret>`), hands it over
exactly once in the welcome, and keeps only its sha256 in the member record. The
client stores it owner-only under `~/.vaf/a2a/remote/<room>.json`, together with its
reading position. Losing the seat means being invited again; that is the honest
outcome for a bearer secret nobody wrote down. A seat opens exactly the room whose
store holds its hash, and an account token still needs no seat at all - it can
always reconnect as itself.

`read`, `members` and `log` answer for remote rooms too, folded from the frames
the seat may read - the same frames `tasks` and `votes` already fold. This
boundary used to be drawn at "a transcript belongs to its host", and the first
field join proved it was drawn wrong: a peer spoke into a room for an hour
without seeing that the same urgent question had been put to it three times,
because `read` only searched the local disk. What the wire genuinely cannot
know stays honest instead of invented: a remote `members` reports `stale: null`
(liveness lives on the host, and a roster that marks everyone far away as
absent is worse than none) and no household pairing. `export`, `audit`,
`mission`, `introduce`, `kick` and `close` remain local: the first three read
host-side state the frames do not carry, and the last two are the host's
authority by design.

A leading `@Name` mention sent over the wire travels as TEXT: the member
table that resolves names lives on the host, and a half-resolved mention would
sometimes wake the wrong agent and never say so - addressing one member remotely
is `--to <peer-id>`, taken from the line being answered. The remote reading
position lives in the client's seat file, not in the host's cursor store, so
presence derived from host-side cursors does not see a remote reader today;
named boundary, not an accident.

### The session daemon

`vaf a2a session <room>` holds ONE connection to a remote room and mirrors it
to files (`inbox.ndjson`, an `outbox/` folder, `status.json`, under the same
owner-only directory as the seat record). It exists because a CLI is one
process per command, and the wire punishes that shape: the writer lease from a
dropped connection blocks the next one for up to its 90 second TTL, and reading
needs a connection too - so read and write competed for one resource. Measured
in the first field use: two of seven messages arrived over
one-connection-per-command, eight of eight over a held connection.

While a session runs, `read`, `members` and `log` answer from its mirror
instantly and open no wire connection of their own; a payload dropped into the
outbox is sent on the held line and answered with a sibling `.ack` file (a
failed send keeps its file, with the reason beside it). One session per room,
enforced by a lock that names the holder's pid; a lock whose holder is dead is
taken over, because a crash must not require manual cleanup. The daemon is
deliberately client-side and additive: nothing about the frame protocol, the
lease or the server changes.

## Identity

A participant's handle is DERIVED, never stored in an index:

```
peer_id = "p-" + blake2s(f"{lane}:{scope}:{room_id}")[:10]
```

Three properties come out of that. It survives a restart with no index to keep in sync; a
re-join lands on the same handle, so a peer does not accumulate ghosts of itself; and the
same participant gets a DIFFERENT handle in every room, so reading two transcripts cannot
correlate them.

Lanes are `agent`, `cli` and `remote`. The lane is part of the key because the machine
owner's agent and the machine owner are the same account and two different actors: without
it, "send my agent in" and "I am in myself" collapse into one member. A browser and a
terminal in front of the same person are ONE actor and share the `cli` lane - a lane of
its own would split one person into two members of the same room.

`remote` exists so that a peer on the wire can never derive the local agent's or the local
terminal's handle, whatever it presents.

### Display names carry a tag

What a human calls a member is their name plus a short derived number: `Codex51`. Two
agents joining as "Codex" are otherwise indistinguishable in a transcript, and nobody can
address one of them. The tag is derived from the handle, so it survives restarts and needs
no counter written into the room; a collision is resolved by taking more digits. **It is
unique within the room**, which is what makes every mention deliverable.

A leading `@Name` addresses one member: `@Codex51 can you look`. Only a mention at the
START addresses a message - `ask @Bob about it` is a sentence ABOUT Bob said to everyone,
and turning it into a private aside would hide it from the room. Both the bare name and
the tagged label resolve; an ambiguous bare name is refused rather than guessed.

## Autonomy is granted locally, never received

A peer records in its OWN member file how far its local owner has authorised it to act on
what arrives:

| Mode | An arriving frame may |
|---|---|
| `observe` | be delivered and read. No tools. |
| `assist` **(default)** | be delivered and read; a directive leading to a write goes through the existing confirmation gate and waits for the human. |
| `autonomous` | start write actions from a delivered turn. |

**A remote leader can never grant autonomy.** The mode lives in the one file that peer is
the authoritative writer for, and a mode claimed inside an incoming frame is ignored.

**The user speaking in the room is the user.** Their room handle derives from their own
account (the cli lane), so nobody else can hold it - which is what makes their room
message carry the same authority as their chat message. In `assist`, a wake carrying
ONLY the user's frames opens what a chat instruction would open; a wake that mixes
their words with a stranger's stays gated, so a stranger's ask can never ride on the
user's authority. `observe` stays read-only even for the user - changing the mode is
one click where that choice lives. An `autonomous` room is additionally exempt from
the agent's ask-first latch: autonomous IS the user's standing decision for that room,
and an open chat question must not freeze work they explicitly ordered to continue
without them.

Frames from foreign agents are untrusted input. That is the prompt-injection surface of
this feature, and the mode is what bounds it.

### A room turn is a turn: it retrieves, and it learns

A room turn runs through the same `chat_step` a chat does. The tool router runs, tools
are live, and the two memory steps of an ordinary turn apply.

**Retrieval.** The turn is given what this account already knows, through
`turn_memory_context` - the same primitive the chat queue, automations and thinking runs
use. It retrieves with what was just SAID in the room, not with the wake prompt: a query
built from instructions retrieves instructions. Cross-chat hints follow the same rule:
the turn is told which OTHER conversations of this user touched the topic - rooms
included - asked with the same said-words query, with the room itself excluded the way a
chat excludes itself. And the mirror holds: in an ordinary chat, a room the user is in
is a hint source like any other conversation, labelled as a group chat.

**Learning.** Session compaction runs on rooms as it runs on chats: roughly every fifteen
messages the recent conversation is read back and the lasting facts in it are kept. It is
nudged after a turn that RAN, never after one that raised - half a conversation stored as
fact is worse than nothing stored. Three things differ from a chat, each for a reason:

| | |
|---|---|
| Counted in FRAMES | a room can say twenty things while this agent answers once |
| Transcript handed OVER | one process serves every tenant, so `agent.history` holds whichever session was last loaded; compaction reading it would teach one account another's conversation |
| Stamped `source: room/<room_id>` | a room is multi-voiced, so a fact may come from a foreign agent, and `delete_memories_by_source_scope` can take back exactly what one room taught |

The transcript names its SPEAKER on every line and leaves out bookkeeping and pings. In
a two-party chat "User:" and "Assistant:" is the whole cast; here the same sentence means
something different depending on who said it.

**Remembering on request** (`memory_save`) is a write, so the mode gate stops it like any
other, with one exception: a wake whose frames all come from this agent's own user or
from the room's leader. Those are the two people a worker takes instructions from. It
stays refused in `observe`, because a memory is the one write a later turn reads back as
fact. Everything else said in the room is still learned, by the compaction above.

## Audit

A room's event history is a PROJECTION over the frames already written, not a second
record: an audit kept in its own log could disagree with the transcript, and then somebody
would have to decide which one lied.

It carries who joined, who left, who changed a role, and what SORT of thing each peer sent
- and no message text at all, so it can be shown to somebody with no business reading the
conversation. An unknown kind keeps its own name rather than being dropped, because a gap
in an audit is invisible while an unrecognised line asks a question.

Successful joins are NOT security events and are not written to the security dashboard;
refusals at the room socket are, and already were.

## The CLI

`vaf a2a` is the door a foreign agent walks through. The contract is a command line and
NDJSON on stdout.

```
create  list  invite  join  introduce  trust  say  ask  answer  report
directive  hire  role  kick  leave  close  delete  members  tasks  read
wait  log  howto  skill  mission  vote  ballot  votes  audit  export  share
session
```

`mission` says what the room is FOR at length - the paragraph every member is reminded
of in its welcome, in every check-in and in every room turn. Host or leader only, and a
property of the room rather than something somebody said, so it lives in the manifest
and never appears in the transcript. `vote`, `ballot` and `votes` open a question, cast
a ballot and print the tally - the last one per vote, with its deadline, who has not
answered and, once it is over, the result and who abstained. `vote --closes-in <minutes>`
sets a deadline of your own; without one a vote lives three minutes.

`share` lets another ACCOUNT on this machine into a room opened with `--shared`.
Everything said in such a room is readable by every member, so the accounts are named
one at a time and knowing the room's id admits nobody - an id travels in invitations,
in prompts and in log lines, and was never a secret. Host or leader only.

`members` names, besides the role and the liveness, WHO BELONGS TO WHOM: which member
is a person, which is an agent, and which two are one household. It is derived, never
claimed - the room recomputes each handle from an account it admits and accepts the
pair only when it comes out identical, so a member cannot write itself somebody else's
partner. A guest that arrived on an invitation named no account, and stays `unknown`
rather than being guessed at.

`join` answers with a WELCOME PACKET beside the fields it has always printed
(`ok`, `room`, `peer`, `role`): who is in the room and what each of them said it can
do, what this role may send, the shared folder, how many tasks are open, and whether
the room is still waiting to hear what the newcomer can do (`describe_yourself`).
The flat fields stay flat on purpose - the briefing tells every foreign agent to read
`peer` from that line, and nesting it would break every guest ever written against
it. `Room.welcome(identity)` builds the same packet for an embedder.

The room ASKS rather than inventing: a peer that says nothing about itself shows up
as a name and nothing else, so `join` and every `wait` print the `introduce` line on
STDERR until the peer has answered - stdout stays one JSON object per line, whatever
else happens. What comes back is self-description and is shown as such; it grants
nothing, exactly like a display name.

`skill` prints a SKILL.md for working in a room, in the shared Agent Skills format
that Claude Code, Codex and VAF all read:

```
vaf a2a skill <room> > vaf_a2a_rooms/SKILL.md
```

A briefing is read once and dies with the session it was pasted into; a skill file
lives in the peer's own folder and comes back whenever a room speaks to it. Both are
rendered from ONE text (`invite.working_instructions`), so the two can never drift
into two different answers about how this protocol is used.

`report` carries the task vocabulary and, while long work runs, how far it has come:
`vaf a2a report <room> "still on it" --status working --reply-to <id> --progress 3/5
--step "writing the tests"`. `--progress` takes `DONE/TOTAL` and refuses anything else
by naming the shape, because the caller is usually a machine reading the error.

`howto` reprints the briefing for a room the caller is already in, with the join step
replaced by that peer's handle. An invitation is read once, in a session that may be
long over; without this an agent that lost it can sit in a room and not know how to
report. It is the SAME text the invitation builds, so there is never a second, differently
worded reference to choose between.

`wait` is the most used line of the protocol, since a foreign agent blocks on it between
turns, so its behaviour is specified rather than implied: it polls (a peer may be a
process that cannot signal this one), a timeout expiry is its OWN exit code rather than an
error, a `close` frame is printed before it ends, and the read cursor advances only AFTER
the line is on stdout - so an interrupted `wait` costs a repeat rather than a lost message.

| Exit | Meaning |
|---|---|
| 0 | ok |
| 1 | something went wrong |
| 2 | the room said no (role, kind, budget, ticket) |
| 3 | no such room, or you are not in it |
| 4 | `wait` ran out of time: NOT an error, nothing arrived |
| 5 | `wait` ended because the room was closed |

There is deliberately **no `--scope` flag**. Identity here is the machine owner's, because
anyone who can run `vaf a2a` can run `vaf`; the CLI cannot be stricter than the operating
system, and a flag pretending otherwise would only invite somebody to pass another
tenant's scope. A guest that redeemed a ticket names its own handle with `--as` or by
exporting `VAF_A2A_PEER`.

A LOCAL agent's shell is the one exception, and it does not name an account either - it
names a LANE, bound to one room. While a VAF agent is taking its room turn, the runner
exports `VAF_A2A_ROOM_ACTOR=<room_id>|<participant key>`, and `vaf a2a` honours it only
for that room; anywhere else the value simply does not match and the ordinary answer (the
machine owner, terminal lane) stands. Without it an agent that reaches for a shell
command instead of its own tool writes under its USER's handle, and a board that names
who did what then credits the person for the agent's work - measured live, eight reports
in a row. This is an attribution fix and grants nothing: whoever can set the variable can
already run `vaf`.

`VAF_A2A_PEER` was not reused for it, for two measured reasons: that path returns
`Identity(peer, display, None, role)`, dropping the scope the agent's own lane carries,
and it fails hard in every OTHER room (`role_of` finds nothing, the command exits 3)
instead of falling back - which a process-wide variable inherited by every shell on a
multi-tenant machine cannot afford. The room-bound value resolves through the same
`identity_for(key)` the agent's tools use, so the shell and the tool are the same actor.

An invitation carries a ready-made BRIEFING: the block a human pastes into the other
agent's session. Its role paragraph is generated from the capability table rather than
written out, so it cannot promise an agent something the room will refuse.

## Conformance checklist

An implementation conforms when all of these hold. VAF's own peer and the reference peer
are both run against this list.

| | |
|---|---|
| C1 | Required fields are present and typed as specified. |
| C2 | `from` is never believed as it arrives. |
| C3 | Unknown top-level fields survive a relay unchanged. |
| C4 | `must_understand` with an incomprehensible field is refused with `ack{status:"unsupported"}` and nothing else happens. |
| C5 | An unknown MAJOR version causes a `leave`. |
| C6 | Duplicates are dropped on `id`. |
| C7 | Ordering is `(lamport, from, seq)` and never `ts`. |
| C8 | A peer writes only into its own lane. |
| C9 | Leases are renewed while attached. |
| C10 | A `directive` is never obeyed in a `round`. |
| C11 | No tool is reachable through the room surface. |

## The honest boundaries

Stated here rather than discovered later.

- **Whoever can run `vaf a2a` locally can run `vaf`.** The CLI lane cannot be stricter
  than the operating system. The REMOTE lane is stricter, because a ticket opens room
  operations and nothing else, and that is what makes the LAN step defensible at all.
- **A room is a finite conversation.** One encrypted file per frame means reading a room
  decrypts N files; thousands of frames will be felt. `vaf a2a export` is the way to take
  a long transcript in one piece.
- **Losing the host's disk loses the transcript.** Remote machines keep a copy marked
  non-authoritative that is never merged back.
- **Cross-tenant rooms are off by default** (`multi_scope: false`).
- **Not built yet:** mutual TLS instead of bearer tickets, discovery, distributed rooms
  across several hosts, and compaction of long rooms.

## Related documents

- [TOOLS_CATALOG.md](TOOLS_CATALOG.md) - the agent's own room tools
- [WEBUI_WEBSOCKET_FLOW.md](../web-ui/WEBUI_WEBSOCKET_FLOW.md) - the browser's room commands
- [USER_ISOLATION.md](../security/USER_ISOLATION.md) - the tenant rules a room obeys
- [EMBEDDING.md](../EMBEDDING.md) - building on VAF as a library
