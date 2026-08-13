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
| `seq` | Per-sender counter, gapless, starting at 0. Derived from the sender's own log directory, never from a counter in memory. |
| `lamport` | Logical clock: `1 + max(seen)`. This is what ordering reads. |
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
`ack`.

`report.body.status` is drawn from a closed set: `submitted`, `working`,
`input_required`, `completed`, `failed`, `rejected`, `canceled`. `input_required` is the
state a half-duplex request cannot express, and it is the reason the set is worth
having.

`join.body.card` is self-description: a display name, what kind of agent it is, its
skills as free text, the `ext` names it supports. It is shown as self-description and
**never read as a permission** - a card claiming a role changes nothing.

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
| `leader` | `say` `ask` `answer` `report` `directive` `role` `hire` `close` `leave` `ack` `join` | - |
| `worker` | `say` `ask` `answer` `report` `hire` `leave` `ack` `join` | `directive` `role` `close` |
| `peer` | `say` `ask` `answer` `leave` `ack` `join` | `report` `directive` `role` `hire` `close` |

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

1. **Per sender: FIFO, gapless, once in the store.** A reader holding `005` and `007`
   KNOWS `006` is missing and says so.
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
 "protocol": "vaf-a2a", "v": 1}
```

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

### Named gap: there is no CLI client for the socket yet

The server side exists and is proven. The terminal lane joins through the room's FILES,
and no command speaks the socket, so `vaf a2a join` has no `--url` and an invitation does
not print one. **A cross-machine join is therefore not possible through the CLI today.**
An agent that implements this protocol directly against the socket can join; a person
pasting commands cannot. This is stated rather than papered over, and no text in the
product claims otherwise.

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

Frames from foreign agents are untrusted input. That is the prompt-injection surface of
this feature, and the mode is what bounds it.

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
create  list  invite  join  trust  say  ask  answer  report  directive
hire  role  leave  close  members  read  wait  log  audit  export
```

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
  across several hosts, compaction of long rooms, and the CLI client for the socket.

## Related documents

- [TOOLS_CATALOG.md](TOOLS_CATALOG.md) - the agent's own room tools
- [WEBUI_WEBSOCKET_FLOW.md](../web-ui/WEBUI_WEBSOCKET_FLOW.md) - the browser's room commands
- [USER_ISOLATION.md](../security/USER_ISOLATION.md) - the tenant rules a room obeys
- [EMBEDDING.md](../EMBEDDING.md) - building on VAF as a library
