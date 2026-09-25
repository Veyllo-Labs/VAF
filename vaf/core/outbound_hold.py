# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A send the agent prepares on the person's own chat turn waits for that person.

Live incident: the person asked for a mail to be written in the web chat, the agent called
`send_mail`, and the mail left to a real external address in the same breath. Nothing
stopped it, by design of the pieces involved: `send_mail` declares `permission_level="write"`,
so the confirmation gate (which fires on "dangerous") never sees it, it is not in
`trust.RISKY_TOOLS` either, and the tool queues with `undo_seconds=0`, so there was not even
an undo window. The tool's own `confirm_high_risk` parameter is not a second opinion at all:
its refusal text tells the MODEL to call again with the flag set, so the model clears its own
gate.

WHAT THIS MODULE DECIDES, and nothing else: whether a given outward call is parked for the
person instead of dispatched. The parking itself, the approval and the discard live where the
send lane already lives, because the two lanes are genuinely different artifacts:

- MAIL freezes the message. `MailService.queue_send(hold=True)` builds the complete RFC822
  bytes, mints the Message-ID, writes the ledger row and parks the op as `held`. What the
  person approves is byte-for-byte what leaves, and `approve_draft` only flips the state. So
  the mail half is one argument, not a new store.
- A MESSENGER call cannot be frozen that way: there is no artifact until the bridge sends, a
  voice message is synthesized into a temporary file that is unlinked after the send, and the
  recipient is resolved at send time. So the CALL is parked (tool plus arguments) and
  re-dispatched through the same tool on approval, which keeps one send path per channel and
  keeps the tool's own file jail and identity handling.

WHO IS HELD, measured rather than assumed. Only a call that can reach somebody other than the
person themselves is worth a click:

- `send_mail`, `reply_mail`, `forward_mail`: always. Every one of them takes a free recipient.
- `send_whatsapp` WITH `to_phone`: it is the only messenger tool with a free recipient, and
  with that argument it also bypasses the reply-window check, so it can reach any number.
- `send_whatsapp` without `to_phone`, `send_telegram`, `send_discord`: never. Their parameter
  schemas carry no recipient at all; the target is resolved inside the tool as the account
  owner's own endpoint. Parking one would hold a message addressed to the very person who
  would have to click Approve.
- `send_slack`: never, it has no send path (its `run` answers that Slack sending is not
  supported yet).
- `send_to_user` and the automation delivery lane: never. They do not go through these tools
  at all; `send_to_main_messenger` dispatches to the low-level senders.

WHEN, and this is a POSITIVE test on purpose. The hold applies when the turn's chat source is
the web UI, which is the one surface that has a card to decide on. A negative test ("hold
unless this looks like a background run") would park what the workflow engine sends, because
the engine dispatches through the same tool funnel with no chat source and nobody watching -
its send steps would wait for a click that never comes. Voice calls are a named boundary for
the same reason: `voice_call` is a person, but not a person looking at the card.

Front Office answers need no exclusion here: a Front Office turn is restricted to its own
allow-list, its mail answer goes through `queue_send` in the runner rather than a tool, and
`send_whatsapp` refuses any recipient but the owner in that mode.

THE TURN ENDS AT THE DRAFT. Once a round of tool calls parked a draft, the chat turn stops
right there (`Agent.chat_step`, after every result of that round is in the history): no
further model call and no "your draft is ready" sentence, because the card IS the answer. What
happens next is the person's word, and each word is ONE function here, so the card's route and
the terminal cannot disagree about what a click does:

- SEND (`send_draft`) delivers through the lane's own path and, once no draft of that chat is
  still waiting, queues a wake turn for the chat (`task_queue.enqueue_wake_turn`, kind
  `draft`), so the agent carries on where it stopped. The turn is deliberately NOT held open
  for the click: one chat worker serves every chat by default (`parallel_main_workers=1`), so
  a turn waiting on a person would hold up every other chat, every channel message and every
  automation for as long as the person reads - the confirmation gate does exactly that and
  needs a five-minute timeout for it. Ending the turn and waking the chat blocks nothing and
  needs no timeout.
- DISCARD (`discard_draft`) ends it. Nothing is queued.
- EDIT (`revise_draft`) changes the words first, in the stored draft itself: a mail is edited
  inside its frozen bytes (`MailService.revise_draft`), a parked call gets new arguments.

A newer draft from the same chat to the same person on the same channel REPLACES one that is
still waiting (`replace_older_drafts`): asked to change a draft in words, the agent writes a
new one, and two cards for one message is one card too many.

The agent learns what became of a draft it wrote from its own history. Every draft its history
created and no later message reports is looked up at the start of the chat's next turn and
written in as one `[Context:` note (`decision_notes`), so a discard, a replacement and a send
made from the terminal (whose process has no queue to wake anything in) all reach it once. It
can also ASK: the `list_drafts` tool reads this ledger (`chat_drafts`, `draft_rows`,
`status_line`) and answers in the words `decision_notes` recognises.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

# The states a draft can be SENT from: it is waiting, or its last attempt answered and the
# message did not leave. `sent` and `discarded` have left the person's hands.
_SENDABLE: Tuple[str, ...] = ("held", "failed")
# What the person still owns, which is one state more: a send interrupted mid-flight may or
# may not have arrived, so it is theirs to look at and drop, but never to repeat with one
# click. A messenger send carries no idempotency key, so nobody can make that call for them.
_ACTIONABLE: Tuple[str, ...] = _SENDABLE + ("ambiguous",)
# What a chat still has open: the person's to decide, or on its way right now (`sending`: a
# parked call being re-dispatched, a released mail the outbox has not delivered yet).
_OPEN: Tuple[str, ...] = _ACTIONABLE + ("sending",)
# How long a draft on its way counts as "a send in flight" for the wake: the lease after which
# a parked call stuck in `sending` becomes `ambiguous` (`reclaim_stranded_held_sends`). A mail
# the outbox has not managed to deliver for longer than that must not hold back every later
# wake of its chat.
_IN_FLIGHT_SECONDS = 300.0

# The one chat source with a place to decide. See the module docstring for why this is a
# positive test and not an exclusion list.
HOLD_SOURCES = ("web",)

# The mail tools, all of which funnel into MailService.queue_send.
MAIL_HOLD_TOOLS = ("send_mail", "reply_mail", "forward_mail")

CONFIG_KEY = "outward_send_hold"

#: Every parked call's result starts with this. It is the contract between the tools and the
#: chat lane: a result that begins with it means a draft is waiting, which is what tells the
#: browser to show the card. A marker beats parsing an id back out of a sentence, and it works
#: for both lanes, whose ids live in different stores.
HELD_PREFIX = "NOT SENT YET."

#: The two lanes a draft lives in, and so the first half of its ref: `mail:12` is op 12 of the
#: mail outbox, `call:7` row 7 of the parked calls. The two number independently, which is why
#: a bare id is never enough.
DRAFT_KINDS = ("mail", "call")

#: The assistant message a turn ends with when it stopped at a draft. It is what the history
#: and the session file hold for that turn, and the browser shows nothing for it: the card is
#: the answer. A fixed sentence on purpose - the browser hides exactly this text, never a
#: prefix, so a real answer can never be mistaken for it - and plain words, because a stored
#: assistant message loses anything in square brackets on reload (`_clean_history_text`).
TURN_ENDS_AT_DRAFT = "Draft waiting for the user's decision; the turn ended here."

#: The first line of the wake turn a sent draft queues. The browser draws that turn as a wake
#: row by this prefix after a reload, when the kind is gone (as for the timer and process
#: lanes).
DRAFT_WAKE_PREFIX = "✉ Draft sent:"

# Where the history says a draft was created, and where it says what became of it. The second
# shape is written by this module only (`decision_notes`, the wake text), so a draft is
# reported to the agent once. NAMED BOUNDARY: a draft parked before results named their ref
# ("... waiting as a draft ... (draft 348)") is not matched, so no note reports it. Measured
# when the lookup was added: 2 such drafts, both in 1 of 31 stored chats. The agent reads
# their fate with `list_drafts` (a bare number looks in both lanes) instead of a second
# parser for a format nothing writes any more.
_CREATED_RE = re.compile(r"NOT SENT YET\. Draft (mail|call):(\d+)")
_DECIDED_RE = re.compile(r"Draft (mail|call):(\d+) was (?:SENT|DISCARDED|REPLACED)")


def draft_ref(kind: str, entry_id: Any) -> str:
    """`mail:12` / `call:7`: one draft, whichever lane it lives in."""
    return f"{kind}:{int(entry_id)}"


def parse_ref(ref: str) -> Optional[Tuple[str, int]]:
    """(kind, id) of a ref, or None for anything that is not one."""
    kind, _, num = str(ref or "").partition(":")
    if kind not in DRAFT_KINDS or not num.isdigit():
        return None
    return kind, int(num)


def lane_of(tool_name: str) -> str:
    """Which lane a holdable tool parks in: the mail tools build a draft in the mail outbox,
    everything else is a parked call."""
    return "mail" if (tool_name or "").strip() in MAIL_HOLD_TOOLS else "call"


def created_refs(text: str) -> List[str]:
    """The drafts a tool result says it parked, in order."""
    return [f"{k}:{i}" for k, i in _CREATED_RE.findall(str(text or ""))]


def _config_get(config_get: Optional[Callable[[str, Any], Any]], key: str, default: Any) -> Any:
    if callable(config_get):
        try:
            return config_get(key, default)
        except Exception:
            return default
    try:
        from vaf.core.config import Config
        return Config.get(key, default)
    except Exception:
        return default


def reaches_a_third_party(tool_name: str, args: Optional[Dict[str, Any]] = None) -> bool:
    """True when this call can put a message in front of somebody other than the person.

    The measurement behind the messenger half: `send_telegram` and `send_discord` have no
    recipient parameter, so they can only reach the account owner's own endpoint, and holding
    one would park a message addressed to the person clicking Approve.
    """
    name = (tool_name or "").strip()
    if name in MAIL_HOLD_TOOLS:
        return True
    if name == "send_whatsapp":
        return bool(str((args or {}).get("to_phone") or "").strip())
    return False


def holds_outward_send(tool_name: str, args: Optional[Dict[str, Any]] = None, *,
                       source: Optional[str] = None, unattended: bool = False,
                       config_get: Optional[Callable[[str, Any], Any]] = None) -> bool:
    """Should this call be parked for the person instead of sent?

    Four conditions, all of them necessary: the turn belongs to a surface that can show the
    decision, somebody is actually there for it, the call can reach a third party, and the
    person has not switched the hold off.

    `unattended` is the second one, and it exists because the web source alone is not enough:
    a timer the person set in the web UI fires later, on a clock, with that same source and
    nobody at the screen. The person asked for that message to go out at that time, so it goes
    out - the hold is for what the agent prepares while they are sitting there, not for the
    work they scheduled. The runner marks such a turn; automations and workflows never reach
    this function at all, because they carry no chat source.
    """
    if unattended:
        return False
    if str(source or "").strip().lower() not in HOLD_SOURCES:
        return False
    if not reaches_a_third_party(tool_name, args):
        return False
    return bool(_config_get(config_get, CONFIG_KEY, True))


def preview_of(tool_name: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """What the person reads before deciding: recipient, subject and body, per tool."""
    a = args or {}
    name = (tool_name or "").strip()
    if name == "send_whatsapp":
        extra = []
        if str(a.get("voice_lang") or "").strip():
            extra.append("voice message")
        if str(a.get("file_path") or "").strip():
            extra.append("attachment")
        return {
            "recipient": str(a.get("to_phone") or "").strip(),
            "subject": ", ".join(extra),
            "body": str(a.get("message") or ""),
        }
    return {
        "recipient": str(a.get("to") or a.get("to_phone") or ""),
        "subject": str(a.get("subject") or ""),
        "body": str(a.get("body") or a.get("message") or ""),
    }


def held_result(tool_name: str, args: Optional[Dict[str, Any]] = None, *,
                entry_id: Optional[int] = None) -> str:
    """The tool result a parked call returns to the model.

    Deliberately says NOTHING that reads as a delivery. The automation lane counts a delivery
    by looking for the literal phrase "sent to the user via" in a send tool's result, and the
    Front Office lane records "the agent asked the owner" for any send result that is not an
    error, so a parked call must not be phrased like a send. It is also not an error: the
    model did its work, the person simply decides.

    It opens with the draft's ref (`Draft mail:12`), which is how the chat finds the card's
    place under the turn that wrote it and how `decision_notes` finds the draft again.
    """
    p = preview_of(tool_name, args)
    who = p["recipient"] or "the recipient"
    head = (f"{HELD_PREFIX} Draft {draft_ref(lane_of(tool_name), entry_id)} holds the message "
            f"to {who}." if entry_id is not None
            else f"{HELD_PREFIX} The message to {who} is a draft.")
    return (
        f"{head} It waits for the user to send, edit or discard it. Your turn ends here and "
        "you will be told what they decided; do NOT claim it was sent, and do not call the "
        "tool again for the same message."
    )


def storable_args(args: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The part of a call that can be written down and replayed.

    The dispatcher injects live objects into a chat call: `_agent` rides along on every send
    tool so `send_whatsapp` can see whether the turn is a Front Office one. Storing that is
    impossible (it is not JSON) and wrong (the approval is the person's own act, not a Front
    Office turn), so underscore keys and anything unserializable are dropped here rather than
    at the caller. Measured: without this, parking raised, the guard failed open, and the
    message went out - a hold that silently does not hold.
    """
    out: Dict[str, Any] = {}
    for key, value in (args or {}).items():
        if str(key).startswith("_"):
            continue
        try:
            json.dumps(value)
        except Exception:
            continue
        out[str(key)] = value
    return out


def park_messenger_call(tool_name: str, args: Dict[str, Any], *,
                        username: str, user_scope_id: Optional[str],
                        session_id: str = "") -> int:
    """Park a messenger call and return its id. Identity is the caller's own.

    The NAME is resolved here, once, the way the dispatcher resolves it for every tool call
    (`resolve_caller_username`), because the row is found again by it: the store keys a parked
    call on the name, and the surfaces that list it ask under the name the person is signed in
    as. A turn's `_current_username` is None on the normal path for a session that stores no
    username, and parking such a row under "" would put it where nothing looks for it - not
    sent, not listed, not discardable, which is the one outcome this module exists to prevent.
    `allow_lookup` is on because a park happens once per held message, not once per dispatch,
    so the round trip that resolves a tenant's real account name is affordable exactly here."""
    from vaf.core import channel_message_store as store
    from vaf.core.config import resolve_caller_username

    username = resolve_caller_username(username, user_scope_id, allow_lookup=True)
    p = preview_of(tool_name, args)
    channel = "whatsapp" if (tool_name or "").strip() == "send_whatsapp" else (tool_name or "")
    return store.park_held_send(
        username, channel, tool_name,
        json.dumps(storable_args(args), ensure_ascii=False),
        user_scope_id=user_scope_id, chat_id="", recipient=p["recipient"],
        preview=p["body"], session_id=session_id or "",
    )


def _epoch(value: Any) -> float:
    """Seconds since the epoch from either clock in play. The parked calls stamp a float; the
    mail outbox stamps an ISO string (`vaf/mail/store._now`). One listing sorts both, so the
    conversion happens here rather than in a caller that would get it right only for its own
    half."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        try:
            return float(text)
        except Exception:
            return 0.0


def _call_attachments(args_json: Any) -> List[str]:
    """The file a parked messenger call would send, by name, for the row. `send_whatsapp`
    carries it as `file_path`; the row shows the name and never the path, which is the
    caller's own directory and not the reader's business."""
    try:
        args = json.loads(str(args_json or "{}"))
    except Exception:
        return []
    path = str((args or {}).get("file_path") or "").strip() if isinstance(args, dict) else ""
    if not path:
        return []
    import os
    return [os.path.basename(path.rstrip("/\\")) or path]


def _call_row(r: Dict[str, Any]) -> Dict[str, Any]:
    """One parked call in the listing's row shape."""
    kind, eid = "call", int(r["id"])
    return {
        "kind": kind, "id": eid, "ref": draft_ref(kind, eid), "channel": r.get("channel") or "",
        "tool": r.get("tool") or "", "recipient": r.get("recipient") or "",
        "cc": "", "bcc": "", "attachments": _call_attachments(r.get("args")),
        "subject": "", "preview": r.get("preview") or "",
        "created_ts": float(r.get("created_ts") or 0.0),
        "decided_ts": float(r.get("decided_ts") or 0.0),
        "session_id": r.get("session_id") or "",
        "state": str(r.get("state") or "held"), "error": str(r.get("error") or ""),
        "edited": bool(r.get("edited")), "replaced_by": str(r.get("replaced_by") or ""),
    }


def _mail_row(d: Dict[str, Any]) -> Dict[str, Any]:
    """One mail draft in the listing's row shape.

    Every address and every file, because what the person approves is what leaves: a card
    that showed the To line alone let a Bcc or a document go out unseen. The state is the
    draft's own (`MailService.draft_state`): a send that did not leave comes back to the card
    with its reason, one that may have left comes back without a Send button."""
    kind, eid = "mail", int(d.get("op_id") or 0)
    return {
        "kind": kind, "id": eid, "ref": draft_ref(kind, eid), "channel": "mail",
        "tool": "send_mail", "recipient": str(d.get("to") or ""),
        "cc": str(d.get("cc") or ""), "bcc": str(d.get("bcc") or ""),
        "attachments": [str(a) for a in (d.get("attachments") or []) if str(a)],
        "subject": str(d.get("subject") or ""), "preview": str(d.get("body") or ""),
        "created_ts": _epoch(d.get("created_at")),
        "decided_ts": _epoch(d.get("decided_at")),
        "session_id": str(d.get("chat_session_id") or ""),
        "state": str(d.get("state") or "held"), "error": str(d.get("error") or ""),
        "edited": bool(d.get("edited")), "replaced_by": str(d.get("replaced_by") or ""),
    }


def _mail_service(user_scope_id: Optional[str]):
    """The caller's own mail service, or None when this identity has no mail lane at all.

    None rather than an exception for the two cases that MEAN "no mail here": a scope the
    fail-closed constructor refuses, and an install whose mail module is not importable. Not
    for anything else. `MailStore` creates its file on construction, so a missing store is not
    an error at all - it answers with an empty outbox - and a broad `except` could only ever
    turn a real failure (a permission error, a corrupt database) into "no mail account", which
    is the one answer that sends the person looking in the wrong place. Those propagate.
    """
    scope = str(user_scope_id or "").strip()
    if not scope:
        return None
    try:
        from vaf.mail.service import MailService
    except ImportError:
        return None
    try:
        return MailService(scope)
    except ValueError:
        return None


def _close_quietly(svc: Any) -> None:
    """Close the mail store's thread-local connection. The verbs run on whatever thread the
    route or the CLI hands them, and that thread must not keep a handle to somebody's mail.db.
    Housekeeping, so it never replaces the verb's answer."""
    try:
        svc.store.close()
    except Exception:
        pass


def recipient_name(channel: str, recipient: str, username: str,
                   user_scope_id: Optional[str] = None) -> str:
    """The name the person knows the recipient by, or "" when nobody is on record.

    The contact book first (`contacts_store`, the person's own book), then the display name a
    mail address carries ("Anna Berg <anna@example.com>"). Only the FIRST address of a mail is
    named: the card says who it goes to, and the full list is right under the name. Fail-open
    to "": a name is a convenience, the address is always shown."""
    raw = str(recipient or "").strip()
    if not raw:
        return ""
    try:
        from vaf.core import contacts_store
        if channel == "whatsapp":
            return (contacts_store.get_contact_name_by_phone(raw, username, user_scope_id) or "").strip()
        if channel == "mail":
            from email.utils import getaddresses
            pairs = [(n, a) for n, a in getaddresses([raw]) if a]
            if not pairs:
                return ""
            display, addr = pairs[0]
            hit = contacts_store.find_contact_by_channel("email", addr, username, user_scope_id)
            name = str((hit or {}).get("name") or "").strip()
            return name or str(display or "").strip()
    except Exception:
        return ""
    return ""


def _with_names(rows: List[Dict[str, Any]], username: str,
                user_scope_id: Optional[str]) -> List[Dict[str, Any]]:
    seen: Dict[Tuple[str, str], str] = {}
    for r in rows:
        key = (str(r.get("channel") or ""), str(r.get("recipient") or ""))
        if key not in seen:
            seen[key] = recipient_name(key[0], key[1], username, user_scope_id)
        r["recipient_name"] = seen[key]
    return rows


def pending(username: str, user_scope_id: Optional[str] = None, *,
            limit: int = 50, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Everything waiting for this person's word, newest first, in one row shape.

    Two sources, one shape, the way `vaf.core.inbox` unifies its five: the mail outbox's held
    drafts and the parked messenger calls. `kind` says which lane an id belongs to, because
    approving is a different act in each: a mail draft is already an artifact and only has to
    be released, a parked call has to be dispatched. `ref` is the two together.

    `session_id` narrows the list to ONE conversation: a message being written in one chat
    must never appear in another, and the person switching chats mid-draft is the ordinary
    case, not the exception. A draft that belongs to no chat (a Front Office answer, a draft
    from before this existed) is then left out on purpose: its home is the inbox and the mail
    window, not somebody's conversation.

    A call whose last attempt FAILED is listed too, with the reason on it: it is still the
    person's to send or drop, and a draft that is only reachable while everything works is not
    a guard. So is one left AMBIGUOUS by a worker that died mid-send, which can only be
    dropped. Reading the list is also where such a row is parked
    (`reclaim_stranded_held_sends`), because this is the one call every surface makes first.
    """
    want = str(session_id or "").strip()
    rows: List[Dict[str, Any]] = []
    try:
        from vaf.core import channel_message_store as store
        store.reclaim_stranded_held_sends(username, user_scope_id)
        for state in _ACTIONABLE:
            for r in store.held_sends(username, user_scope_id, state=state, limit=limit,
                                      session_id=want or None):
                rows.append(_call_row(r))
    except Exception:
        pass
    try:
        # NAMED BOUNDARY: no scope, no mail half, and no fallback to the admin scope here. The
        # mail lane is fail-closed by contract (`MailService` refuses an empty scope, EMAIL_CLIENT
        # "Scoping rule"), so a held mail exists only under a real scope, and every caller of
        # this listing carries one: the route binds the signed-in user's scope or the local
        # admin's (`get_current_vaf_user`), the CLI binds the configured admin scope
        # (`resolve_owner_identity`) and is scopeless only when the config names none, in which
        # case no MailService could have parked a draft for anybody. The parked-call half
        # answers a None scope with the admin's store because that store keys on it that way.
        if user_scope_id:
            from vaf.mail.service import MailService
            svc = MailService(user_scope_id)
            try:
                drafts = svc.list_drafts()
            finally:
                # Guarded on its own: this whole half is fail-open, so a close that raised
                # would take the drafts down with it, which is the silence the listing exists
                # to remove.
                _close_quietly(svc)
            for d in drafts:
                if want and str(d.get("chat_session_id") or "") != want:
                    continue
                rows.append(_mail_row(d))
    except Exception:
        pass
    rows.sort(key=lambda r: r.get("created_ts") or 0.0, reverse=True)
    return _with_names(rows[: max(1, int(limit))], username, user_scope_id)


def chat_drafts(username: str, user_scope_id: Optional[str], session_id: str, *,
                limit: int = 50) -> List[Dict[str, Any]]:
    """The drafts ONE chat produced, newest first, in `pending`'s row shape: EVERY one that
    still waits or is on its way, plus the newest `limit` decided ones.

    What the card in the conversation reads. A draft stays on screen after the decision, as
    the record of what became of it (sent, discarded, replaced), in the turn that wrote it,
    and that record is bounded; what still waits is not, or a long chat's oldest open draft
    would wait where nothing lists it (and `wake_after_send` would not see it waiting).
    `pending` answers only what still waits, which is what the terminal and the inbox want.
    A chat with no id has no drafts."""
    sid = str(session_id or "").strip()
    if not sid:
        return []
    rows: List[Dict[str, Any]] = []
    try:
        from vaf.core import channel_message_store as store
        store.reclaim_stranded_held_sends(username, user_scope_id)
        rows.extend(_call_row(r) for r in store.held_sends(
            username, user_scope_id, state="", limit=limit, session_id=sid))
        for state in _OPEN:
            rows.extend(_call_row(r) for r in store.held_sends(
                username, user_scope_id, state=state, limit=None, session_id=sid))
    except Exception:
        pass
    try:
        if user_scope_id:
            from vaf.mail.service import MailService
            svc = MailService(user_scope_id)
            try:
                drafts = svc.list_chat_drafts(sid, limit=limit)
            finally:
                _close_quietly(svc)
            rows.extend(_mail_row(d) for d in drafts)
    except Exception:
        pass
    unique: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        unique.setdefault(str(r.get("ref") or ""), r)
    ordered = sorted(unique.values(), key=lambda r: r.get("created_ts") or 0.0, reverse=True)
    open_rows = [r for r in ordered if r.get("state") in _OPEN]
    decided = [r for r in ordered if r.get("state") not in _OPEN][: max(1, int(limit))]
    kept = {id(r) for r in open_rows + decided}
    return _with_names([r for r in ordered if id(r) in kept], username, user_scope_id)


def resolve_tool(tool_name: str) -> Optional[Any]:
    """One instance of a parkable send tool, for a caller that has no Agent.

    The approval surfaces (an HTTP route, the CLI) must not build an Agent to send one
    message: that loads every tool, the MCP servers and the prompt manager. Only the tools
    this module can park are resolvable, so the lookup cannot become a general back door into
    tool dispatch from a route.
    """
    name = (tool_name or "").strip()
    if name != "send_whatsapp":
        return None
    try:
        import importlib
        import inspect

        from vaf.tools.base import BaseTool
        mod = importlib.import_module(f"vaf.tools.{name}")
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if issubclass(obj, BaseTool) and obj is not BaseTool and getattr(obj, "name", "") == name:
                return obj()
    except Exception:
        return None
    return None


# What a delivered message looks like, per tool. The classification runs the SAFE way round:
# a draft is consumed only on a result this code RECOGNISES as a delivery, and anything else
# keeps it waiting with the text on it. The two mistakes are not equal - a draft kept costs
# one more click, a draft consumed loses the message - and the first shape of this test made
# the expensive one: it looked for failure prose ("failed", "error", ...) and `send_whatsapp`
# has six returns that match none of it ("WhatsApp bridge is not running.", "WhatsApp could
# not deliver the message: ...", "No delivery confirmation ...", "Message was blocked ...",
# "Access denied: outside your own data", "[TOOL BLOCKED] ..."), so a bridge that was down
# reported success and ate the draft.
# NAMED BOUNDARY: one tool can be parked as a call today (`send_whatsapp` with `to_phone`), so
# one list is the whole rule. A second holdable messenger tool is the moment this belongs on
# the tool itself (a declared success marker or a structured result), not in a second entry
# here; the mail lane already has that in its op state and does not pass through this.
_DELIVERED: Dict[str, Tuple[str, ...]] = {
    "send_whatsapp": ("message sent via whatsapp", "voice message sent via whatsapp",
                      "document sent via whatsapp"),
}


def delivery_succeeded(tool_name: str, result: str) -> bool:
    """Did this result say the message left? Unknown tool, unknown wording: no."""
    markers = _DELIVERED.get(str(tool_name or "").strip())
    if not markers:
        return False
    head = str(result or "").strip().lower()
    return head.startswith(markers)


def approve_call(entry_id: int, *, username: str, user_scope_id: Optional[str],
                 user_role: str, tools: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Send a parked call now: {"ok": bool, "result": str}.

    The call is re-dispatched through its own tool, with the identity of whoever approves
    resolved here and never read back from the parked row. Two reasons, both security: a tool
    declaring `file_access` installs its jail from `user_scope_id` and `user_role`, so a stale
    pair would either open the wrong jail or none at all, and an approval must not carry a
    privilege the approving session does not have.

    The row is CLAIMED (held -> sending) before the call runs, the way the mail outbox claims
    an op, so two clicks cannot send the same draft twice. A send that fails puts the draft
    back with the error on it: a bridge that is down must not consume the draft. A draft whose
    last attempt FAILED is claimable again, because the tool answered and the message did not
    leave. One left AMBIGUOUS is not: the worker died between the bridge and the bookkeeping,
    so a second attempt could be a second delivery, and nobody may make that choice on the
    person's behalf. They see it with the reason and drop it, or ask the agent again.
    """
    from vaf.core import channel_message_store as store

    row = store.held_send(entry_id, username, user_scope_id)
    was = str((row or {}).get("state") or "")
    if was == "ambiguous":
        return {"ok": False, "result": (
            "The last attempt was interrupted and this message may already have been sent. "
            "Check the conversation: drop the draft if it arrived, and ask for it again if it "
            "did not.")}
    if not row or was not in _SENDABLE:
        return {"ok": False, "result": "This draft is not waiting any more."}
    tool_name = str(row.get("tool") or "")
    tool = (tools or {}).get(tool_name) or resolve_tool(tool_name)
    if tool is None:
        return {"ok": False, "result": f"Tool '{tool_name}' is not available."}
    try:
        args = json.loads(str(row.get("args") or "{}"))
    except Exception:
        args = {}
    if not isinstance(args, dict):
        args = {}
    args.update({"user_scope_id": user_scope_id, "username": username, "user_role": user_role})
    if not store.settle_held_send(entry_id, username, "sending", user_scope_id, expect=was):
        return {"ok": False, "result": "This draft is not waiting any more."}
    try:
        result = str(tool.run(**args))
    except Exception as exc:                                   # noqa: BLE001
        result = f"Failed to send: {exc}"
    ok = delivery_succeeded(tool_name, result)
    store.settle_held_send(entry_id, username, "sent" if ok else "failed", user_scope_id,
                           error="" if ok else result[:300], expect="sending")
    return {"ok": ok, "result": result}


def discard_call(entry_id: int, *, username: str, user_scope_id: Optional[str]) -> bool:
    """Drop a parked call, waiting or failed. False when it was not the person's any more."""
    from vaf.core import channel_message_store as store
    return store.settle_held_send(entry_id, username, "discarded", user_scope_id,
                                  expect=_ACTIONABLE)


# ── the person's word: one function per verb, for every surface ─────────────────

def _chat_row(kind: str, entry_id: int, username: str,
              user_scope_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """One draft in the listing's row shape, whatever its state, or None."""
    try:
        if kind == "call":
            from vaf.core import channel_message_store as store
            row = store.held_send(int(entry_id), username, user_scope_id)
            return _call_row(row) if row else None
        svc = _mail_service(user_scope_id)
        if svc is None:
            return None
        try:
            d = svc.get_chat_draft(int(entry_id))
        finally:
            _close_quietly(svc)
        return _mail_row(d) if d else None
    except Exception:
        return None


def send_draft(kind: str, entry_id: int, *, username: str, user_scope_id: Optional[str],
               user_role: str = "user", wake: bool = True,
               tools: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Send one waiting draft now: {"ok", "state", "error"}.

    The lane's own path does the sending - `release_held_draft` for a mail (release AND drain,
    read back by state), `approve_call` for a parked call (claimed, re-dispatched through its
    own tool with the approving person's identity). A mail answers `state` "" with error
    "not waiting" for a draft that is not there to send, which every surface turns into "no
    such draft"; `no mail account` when this identity has no mail lane at all.

    `wake` queues the chat's wake turn once the send left and nothing else in that chat still
    waits (`wake_after_send`). The terminal passes False: its process holds no queue that
    anybody drains, and the next turn's decision note reports the send instead.
    """
    if kind == "call":
        from vaf.core import channel_message_store as store
        before = str((store.held_send(int(entry_id), username, user_scope_id) or {}).get("state") or "")
        if before not in _ACTIONABLE:
            # The mail lane's word for it, so every surface reads one answer for "no such
            # draft" (another identity's id included: the lookup is keyed on the name).
            return {"ok": False, "state": "", "error": "not waiting"}
        res = approve_call(int(entry_id), username=username, user_scope_id=user_scope_id,
                           user_role=user_role, tools=tools)
        ok = bool(res.get("ok"))
        state = "sent" if ok else ("ambiguous" if before == "ambiguous" else "failed")
        out = {"ok": ok, "state": state, "error": "" if ok else str(res.get("result") or "")}
    elif kind == "mail":
        svc = _mail_service(user_scope_id)
        if svc is None:
            return {"ok": False, "state": "", "error": "no mail account"}
        try:
            from vaf.mail.service import release_held_draft
            # A released mail the outbox run could not deliver is parked `failed`, and the card
            # shows it with its reason and a Send button. That button is the person asking
            # again, so the op goes back to `held` first - only for a confirmed failure
            # (`chat_draft` says `failed`); one that may have arrived stays refused.
            op = svc.store.get_op(int(entry_id))
            if op and op.get("state") == "failed" and svc.chat_draft(op).get("state") == "failed":
                svc.store.mark_op(int(entry_id), "held", expect_state="failed")
            out = dict(release_held_draft(str(user_scope_id or ""), str(username or ""),
                                          int(entry_id), service=svc))
        finally:
            _close_quietly(svc)
    else:
        raise ValueError(f"unknown draft kind {kind!r}")
    if out.get("ok") and wake:
        try:
            wake_after_send(kind, int(entry_id), username=username,
                            user_scope_id=user_scope_id, user_role=user_role)
        except Exception:
            # The message left; a chat that is not woken learns it at its next turn
            # (`decision_notes`). A failed wake must not turn a delivery into an error.
            pass
    return out


def discard_draft(kind: str, entry_id: int, *, username: str,
                  user_scope_id: Optional[str]) -> Optional[bool]:
    """Drop one waiting draft. True when dropped, False when it was not waiting, None when this
    identity has no mail lane at all (a mail id then names nothing). Nothing was on the wire,
    so nothing is recalled, and nothing is woken: a discard is the end of that turn."""
    if kind == "call":
        return discard_call(int(entry_id), username=username, user_scope_id=user_scope_id)
    if kind == "mail":
        svc = _mail_service(user_scope_id)
        if svc is None:
            return None
        try:
            return bool(svc.discard_draft(int(entry_id)))
        finally:
            _close_quietly(svc)
    raise ValueError(f"unknown draft kind {kind!r}")


def revise_draft(kind: str, entry_id: int, *, username: str, user_scope_id: Optional[str],
                 body: Optional[str] = None, subject: Optional[str] = None) -> Dict[str, Any]:
    """New words for a waiting draft: {"ok", "error"}.

    `body` is the text (the mail body, the WhatsApp message), `subject` a mail's subject line
    and ignored for a call. A text that is only whitespace is refused ("empty"): a card cannot
    send nothing, and a mail with an empty body is a slip, not a decision. A draft that is not
    waiting, or whose last attempt may already have gone out, keeps its words ("not waiting").
    """
    if body is not None and not str(body).strip():
        return {"ok": False, "error": "empty"}
    if kind == "call":
        from vaf.core import channel_message_store as store
        row = store.held_send(int(entry_id), username, user_scope_id)
        if not row or str(row.get("state") or "") not in _SENDABLE:
            return {"ok": False, "error": "not waiting"}
        if body is None:
            return {"ok": True, "error": ""}
        try:
            args = json.loads(str(row.get("args") or "{}"))
        except Exception:
            args = {}
        if not isinstance(args, dict):
            args = {}
        args["message"] = str(body)
        preview = preview_of(str(row.get("tool") or ""), args)["body"]
        ok = store.revise_held_send(int(entry_id), username, json.dumps(args, ensure_ascii=False),
                                    preview, user_scope_id, expect=_SENDABLE)
        return {"ok": ok, "error": "" if ok else "not waiting"}
    if kind == "mail":
        svc = _mail_service(user_scope_id)
        if svc is None:
            return {"ok": False, "error": "no mail account"}
        try:
            ok = svc.revise_draft(int(entry_id), subject=subject, body=body)
        finally:
            _close_quietly(svc)
        return {"ok": bool(ok), "error": "" if ok else "not waiting"}
    raise ValueError(f"unknown draft kind {kind!r}")


def _recipient_key(channel: str, recipient: str) -> Tuple[str, ...]:
    """Who a draft goes to, spelled so two drafts to the same person compare equal: the
    digits of a number, the lowercased addresses of a mail (display names left out)."""
    raw = str(recipient or "")
    if channel == "mail":
        from email.utils import getaddresses
        return tuple(sorted({a.strip().lower() for _, a in getaddresses([raw]) if a.strip()}))
    return (re.sub(r"\D", "", raw),)


def replace_older_drafts(new_ref: str, *, session_id: str, username: Optional[str],
                         user_scope_id: Optional[str], keep: Iterable[str] = ()) -> List[str]:
    """Retire the drafts a new one replaces, and return their refs.

    A draft still waiting in the SAME chat, on the SAME channel, to the SAME recipient is
    replaced by the newer one: asked in words to change a draft, the agent writes it again,
    and the person would otherwise face two cards for one message and could send both.
    `keep` is the round that just parked `new_ref` - two drafts parked in ONE round are two
    messages the model meant, not a revision. A draft whose last attempt may already have gone
    out (`ambiguous`) is never retired here: it is a fact the person has to look at.
    """
    parsed = parse_ref(new_ref)
    sid = str(session_id or "").strip()
    if parsed is None or not sid:
        return []
    from vaf.core.config import resolve_caller_username
    who = resolve_caller_username(username, user_scope_id, allow_lookup=True)
    rows = chat_drafts(who, user_scope_id, sid)
    new = next((r for r in rows if r.get("ref") == new_ref), None)
    if new is None:
        return []
    target = (new["channel"], _recipient_key(new["channel"], new["recipient"]))
    skip = set(keep or ()) | {new_ref}
    replaced: List[str] = []
    for r in rows:
        if r.get("ref") in skip or r.get("state") not in _SENDABLE:
            continue
        if (r["channel"], _recipient_key(r["channel"], r["recipient"])) != target:
            continue
        if r["kind"] == "call":
            from vaf.core import channel_message_store as store
            ok = store.settle_held_send(int(r["id"]), who, "replaced", user_scope_id,
                                        expect=_SENDABLE, replaced_by=new_ref)
        else:
            svc = _mail_service(user_scope_id)
            if svc is None:
                continue
            try:
                ok = bool(svc.discard_draft(int(r["id"]), replaced_by=new_ref))
            finally:
                _close_quietly(svc)
        if ok:
            replaced.append(str(r["ref"]))
    return replaced


def _channel_word(channel: str) -> str:
    """The channel as the agent reads it: the registry's label, "mail" for the mail lane
    (which is no bridge channel)."""
    if channel == "mail":
        return "mail"
    from vaf.core.channels import channel_label
    return channel_label(channel, default=channel or "message")


def _addressee(row: Dict[str, Any]) -> str:
    name = str(row.get("recipient_name") or "").strip()
    addr = str(row.get("recipient") or "").strip()
    if name and addr and name != addr:
        return f"{name} ({addr})"
    return name or addr or "the recipient"


def decision_line(row: Dict[str, Any]) -> str:
    """What became of one draft, in the words the agent reads and `_DECIDED_RE` recognises;
    "" while it still waits."""
    ref, state = str(row.get("ref") or ""), str(row.get("state") or "")
    what = f"{_channel_word(str(row.get('channel') or ''))} to {_addressee(row)}"
    if state in ("sent", "sending"):
        edited = " after changing its text" if row.get("edited") else ""
        underway = " It is on its way: the outbox delivers it shortly." if state == "sending" else ""
        return f"Draft {ref} was SENT by the user{edited} ({what}).{underway}"
    if state == "discarded":
        return f"Draft {ref} was DISCARDED by the user ({what}). Nothing was sent."
    if state == "replaced":
        return (f"Draft {ref} was REPLACED by your newer draft {row.get('replaced_by') or ''} "
                f"before anybody sent it ({what}).").replace("  ", " ")
    return ""


def status_line(row: Dict[str, Any]) -> str:
    """Where one draft stands, whatever its state: what became of it (`decision_line`, so a
    lookup reports a decision in the words `decision_notes` recognises and the next turn does
    not hear it twice), or why it is still the person's to decide."""
    line = decision_line(row)
    if line:
        return line
    ref, state = str(row.get("ref") or ""), str(row.get("state") or "")
    what = f"{_channel_word(str(row.get('channel') or ''))} to {_addressee(row)}"
    if state == "failed":
        reason = str(row.get("error") or "").strip() or "no reason given"
        return (f"Draft {ref} was NOT sent: its last attempt failed ({reason}). It waits for the "
                f"user to send it again, edit or discard it ({what}).")
    if state == "ambiguous":
        return (f"Draft {ref} MAY already have been sent: its send was interrupted, and only the "
                f"user can tell whether it arrived ({what}).")
    return f"Draft {ref} WAITS for the user to send, edit or discard it ({what})."


def draft_rows(ref: str, *, username: Optional[str],
               user_scope_id: Optional[str]) -> List[Dict[str, Any]]:
    """The draft `ref` names, whatever became of it: `mail:12` or `call:7`, or a bare number,
    which both lanes number independently and so may name one draft in each. Only the caller's
    own drafts are found, because each lane is read with the caller's identity."""
    text = str(ref or "").strip().lower().replace(" ", "")
    parsed = parse_ref(text)
    wanted = [parsed] if parsed else (
        [(kind, int(text)) for kind in DRAFT_KINDS] if text.isdigit() else [])
    if not wanted:
        return []
    from vaf.core.config import resolve_caller_username
    who = resolve_caller_username(username, user_scope_id, allow_lookup=True)
    rows = [r for r in (_chat_row(kind, num, who, user_scope_id) for kind, num in wanted) if r]
    return _with_names(rows, who, user_scope_id)


def decision_notes(texts: Iterable[str], *, username: Optional[str],
                   user_scope_id: Optional[str]) -> str:
    """The `[Context:` note that tells the agent what became of its drafts, or "".

    `texts` is what the agent has read so far in this chat (its history, plus the turn's own
    input): a draft it created there (`_CREATED_RE`) that no later text reports
    (`_DECIDED_RE`) and that has been decided since is reported, once, because the note itself
    carries the reporting shape. A draft still waiting is not news. The history is the ledger,
    so nothing extra is stored: a discard, a replacement and a send from the terminal all
    arrive the same way, and a chat whose history no longer holds the draft asks about nothing.
    """
    created: List[str] = []
    reported = set()
    for text in texts or ():
        t = str(text or "")
        if "Draft " not in t:
            continue
        for ref in created_refs(t):
            if ref not in created:
                created.append(ref)
        reported.update(f"{k}:{i}" for k, i in _DECIDED_RE.findall(t))
    open_refs = [r for r in created if r not in reported]
    if not open_refs:
        return ""
    from vaf.core.config import resolve_caller_username
    who = resolve_caller_username(username, user_scope_id, allow_lookup=True)
    lines = []
    for ref in open_refs:
        parsed = parse_ref(ref)
        row = _chat_row(parsed[0], parsed[1], who, user_scope_id) if parsed else None
        if row is None:
            continue
        _with_names([row], who, user_scope_id)
        line = decision_line(row)
        if line:
            lines.append(f"- {line}")
    if not lines:
        return ""
    return "[Context: what became of your drafts since you wrote them]\n" + "\n".join(lines)


def wake_text(row: Dict[str, Any]) -> str:
    """What the chat's wake turn says after a draft was sent. Line one is what the browser
    shows on the wake row (the addressee); the rest is for the agent."""
    label = str(row.get("recipient_name") or "").strip() or str(row.get("recipient") or "").strip()
    lines = [f"{DRAFT_WAKE_PREFIX} {label}", decision_line(row)]
    if row.get("edited"):
        text = str(row.get("preview") or "")
        if len(text) > 1500:
            text = text[:1500] + " [...]"
        lines += ["The user changed the text before sending. This is what left:", text]
    lines += ["", "Continue with what the message was for. If nothing is left to do, say in one "
                  "short sentence that it went out."]
    return "\n".join(lines)


def wake_after_send(kind: str, entry_id: int, *, username: str,
                    user_scope_id: Optional[str], user_role: str = "user") -> bool:
    """Queue the wake turn for the chat a sent draft came from, and say whether it did.

    Only once NOTHING in that chat still waits to be sent: two drafts from one turn wake the
    chat once, after the second decision, and whatever the person decided about the first is
    reported by the decision note of that same turn. Another draft of the chat that is being
    sent at this very moment counts as waiting too, so two Send clicks racing each other wake
    the chat once, from whichever answers last; "this very moment" is `_IN_FLIGHT_SECONDS`,
    so a mail the outbox cannot deliver does not hold back the chat for good. A draft that
    belongs to no chat wakes nothing. The draft itself counts as sent once it left the
    person's hands (`sending` included: a released mail the outbox delivers shortly). The turn
    is a person's (they just clicked), so it is attended: a message the agent sends in it is
    held again."""
    import time as _time
    row = _chat_row(kind, int(entry_id), username, user_scope_id)
    sid = str((row or {}).get("session_id") or "").strip()
    if not row or not sid or row.get("state") not in ("sent", "sending"):
        return False
    now = _time.time()

    def _in_flight(r: Dict[str, Any]) -> bool:
        return r.get("state") == "sending" and now - float(r.get("decided_ts") or 0.0) < _IN_FLIGHT_SECONDS

    others = [r for r in chat_drafts(username, user_scope_id, sid) if r.get("ref") != row.get("ref")]
    if any(r.get("state") in _SENDABLE or _in_flight(r) for r in others):
        return False
    _with_names([row], username, user_scope_id)
    from vaf.core.task_queue import enqueue_wake_turn
    enqueue_wake_turn(kind="draft", session_id=sid, text=wake_text(row), source="web",
                      user_scope_id=user_scope_id, username=username, role=user_role,
                      extra={"draft": row["ref"]})
    return True
