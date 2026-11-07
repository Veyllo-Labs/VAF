# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The confirmation card: what a request of the Veyllo App's agent parked, confirmed by the
user's tap on the phone that asked (protocol section 12).

A request (``agent_ask``) runs no tool that starts work or changes how VAF works from now on
(vaf/core/agent_origin.py), and sends nothing to somebody else (vaf/core/outbound_hold.py).
For the tools that declare a card kind (``BaseTool.agent_request_confirm_kind``) and for those
sends there is a third answer: the call is PARKED, its request stays open, and the phone that
asked gets ``{"t": "confirm", "id", "ask", "kind", "what", "exp"}``. ``what`` is VAF's own
plain text of exactly the call (every argument, never cut, at most ``WHAT_MAX`` code points;
never the requesting agent's words), ``exp`` ten minutes on. The app answers
``{"t": "confirm_answer", "id", "ok"}``, only from the user's tap:

- Yes: exactly the parked call runs once, in a wake turn of kind ``confirm`` of the app chat
  (`run_confirmed`, then the turn answers the request with its outcome). An action runs
  through ``Agent.execute_tool`` inside ``agent_origin.confirmed_call_scope``, with the
  request's limits standing (``asked_by_agent``) except gate (e) for that one call; a send
  leaves through ``outbound_hold.send_draft``, whose own claim makes a click in the web UI at
  the same moment harmless, and only while the draft is still what the card showed
  (``outbound_hold.draft_digest``).
- No: the request is answered that it did not run, and a draft is discarded.
- No answer in time: the request is answered that it did not run; a draft still waits in the
  web UI.
- A Send, Discard or edit of the draft in the web UI settles the card (`settle`), and the
  request is answered with the draft's state.
- ``cancel`` with the request's id from the asking device drops the card (and the draft).
- An app that answers ``unknown_type`` for ``confirm`` cannot show cards: every card sent to
  that connection is settled, and its next parked item is answered at once without one.

One card waits per request at a time. A round that parked more than one item (an action and a
message, or two messages) cards the first, and the rest follow one by one (``followups``): each
next card opens once the one before was answered Yes and its turn has answered (`open_followup`),
and the answer that turn wrote travels with the request (``bridge.Reply.carried``) into its final
``done``. Any other end of a card (No, no answer in time, a decision in the web UI, an app without
cards) sends no further card: the messages still waiting stay drafts in the web UI, and the
request's answer says so.

The answer counts only from the device the card went to and the account of its row: another
device, another account or an unknown id all get ``confirm_invalid`` (one code, no oracle),
and the card stays open for the right one. A card answered or settled answers ``used``, one
past ``exp`` ``expired``. Every change of state happens once, under the bridge's lock, from
an expected state.

The card is not in the device's outbox: a device that is away gets the relay's notice while
a card waits (vaf/core/app_link/relay.py), and the card again after its next ``welcome``, with
the same id and ``exp``.

NAMED BOUNDARY: memory only, like the request it answers (the bridge's open requests and the
task queue live in this process). A restart of VAF drops every card; nothing runs after it,
a tap on such a card gets ``confirm_invalid``, and a parked draft still waits in the web UI
(its own store keeps it). The CLI cannot list or answer cards: an answer counts only from the
tap on the phone.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from vaf.core.agent_origin import CONFIRM_SECONDS, WHAT_MAX, ParkedCall
from vaf.core.app_link import CHANNEL, devices
from vaf.core.tool_contract import AGENT_REQUEST_CONFIRM_KINDS

logger = logging.getLogger(__name__)

#: A card that was decided or ran out stays known this long after its ``exp``, so a late tap
#: still gets an exact ``used`` or ``expired``.
TOMBSTONE_SECONDS = 600
#: Cards one request may open in sequence (a confirmed turn may park again).
CONFIRMS_PER_ASK_MAX = 3
#: An approved card whose turn the queue no longer holds after this long (the web UI's stop
#: dropped it) answers its request instead of waiting for nothing.
START_GRACE_SECONDS = 30
#: The card kinds the protocol knows.
KINDS = AGENT_REQUEST_CONFIRM_KINDS + ("send",)
#: The first line of a confirm wake turn (the web chat shows the rest of it on the wake row).
CONFIRM_WAKE_PREFIX = "✔ Confirmed in the app:"

_SENDABLE = ("held", "failed")
_OPEN = ("pending", "approved", "running")

# What the requesting agent reads when the card ends without a run. Its own language: it is
# that agent's answer, which it passes on to the user.
DECLINED = "Your user declined this in the Veyllo App: '{tool}' did NOT run."
DECLINED_SEND = ("The message was NOT sent: your user declined it in the Veyllo App, and the "
                 "draft is discarded.")
EXPIRED = ("Your user did not confirm this in the Veyllo App within {minutes} minutes: '{tool}' "
           "did NOT run.")
EXPIRED_SEND = ("Your user did not confirm the message in the Veyllo App within {minutes} "
                "minutes: it was NOT sent, and it still waits as a draft in VAF's web UI.")
REFUSED_BY_APP = ("The Veyllo App on this phone cannot show confirmations yet (an app update "
                  "brings them): '{tool}' did NOT run.")
REFUSED_BY_APP_SEND = ("The Veyllo App on this phone cannot show confirmations yet (an app "
                       "update brings them): the message was NOT sent, and it waits as a draft "
                       "in VAF's web UI.")
FOLLOWUPS_WAIT = ("The other message this request prepared was NOT sent either: it waits as a "
                  "draft in VAF's web UI.")
CANNOT_SHOW = ("[APP AGENT] '{tool}' could not be shown to your user for confirmation, so it did "
               "NOT run. Tell your user it was not done.")
DECIDED_ELSEWHERE = "Your user decided in VAF's web UI: {status}"
NOT_STARTED = "'{tool}' did NOT run: it was stopped before it started."
_INSTRUCTION = ("Answer the app's agent with the outcome in a few sentences. Do not run it "
                "again.")


@dataclass
class Pending:
    """One card. ``tool``, ``args_json`` and ``digest`` for an action; ``draft_ref`` and
    ``draft_digest`` for a send."""
    id: str
    ask_id: str
    device_id: str
    scope: str
    username: str
    chat_sid: str
    kind: str
    what: str
    headline: str
    exp: float
    created: float
    tool: str = ""
    args_json: str = ""
    digest: str = ""
    draft_ref: str = ""
    draft_digest: str = ""
    state: str = "pending"
    decided: float = 0.0
    # The live session the card last went to (`refused_by_app` settles what went to it).
    sent_to: int = 0
    # The drafts of the same round still to be carded, in order, once this card's Yes has run.
    followups: List[str] = field(default_factory=list)

    @property
    def is_send(self) -> bool:
        return self.kind == "send"

    def frame(self) -> Dict[str, Any]:
        return {"t": "confirm", "id": self.id, "ask": self.ask_id, "kind": self.kind,
                "what": self.what, "exp": int(self.exp)}

    def meta(self) -> Dict[str, Any]:
        """The request's reply as the bridge finds it (a request answers under its own id)."""
        return {"user_scope_id": self.scope, "username": self.username, "app_agent_ask": True,
                "app_ask_id": self.ask_id, "app_device_id": self.device_id,
                "app_reply_id": self.ask_id}


_cards: Dict[str, Pending] = {}


def _lock():
    from vaf.core.app_link import bridge
    return bridge._lock


def _minutes() -> int:
    return CONFIRM_SECONDS // 60


def reset() -> None:
    """Forget every card (tests, and what a restart of VAF does)."""
    with _lock():
        _cards.clear()


def has_pending(device_id: str) -> bool:
    """Whether a card waits for this device's answer (the relay keeps its notice for it)."""
    now = time.time()
    with _lock():
        return any(c.device_id == str(device_id) and c.state == "pending" and now <= c.exp
                   for c in _cards.values())


def card(card_id: str) -> Optional[Pending]:
    with _lock():
        return _cards.get(str(card_id))


# ── the card's text for a send ───────────────────────────────────────────────────────────

def _draft(ref: str, username: str, scope: str) -> Optional[Dict[str, Any]]:
    from vaf.core import outbound_hold
    rows = outbound_hold.draft_rows(ref, username=username, user_scope_id=scope)
    return rows[0] if rows else None


def _call_args(row: Dict[str, Any], username: str, scope: str) -> Dict[str, Any]:
    from vaf.core import channel_message_store as store
    from vaf.core.config import resolve_caller_username
    try:
        who = resolve_caller_username(username, scope, allow_lookup=True)
        found = store.held_send(int(row["id"]), who, scope) or {}
        args = json.loads(str(found.get("args") or "{}"))
        return args if isinstance(args, dict) else {}
    except Exception:
        return {}


def send_what(row: Dict[str, Any], args: Dict[str, Any], lang: Optional[str]) -> str:
    """The card's text for a draft: the headline, then whom it goes to (the name when known
    and the address), every Cc and Bcc, the subject and the attachment names of a mail, or
    every further argument of a WhatsApp call (a voice message's language, the file), and the
    whole text last. Shown as the action cards show their arguments
    (`agent_origin.card_lines`: invisible characters made visible, continuation lines
    indented, the short ones first), so no subject or text can write a line that reads as a
    recipient. Nothing is cut."""
    from vaf.core import outbound_hold
    from vaf.core.agent_origin import card_lines, confirm_headline
    mail = str(row.get("channel") or "") == "mail"
    head = confirm_headline("send_mail" if mail else "send_whatsapp", lang) or (
        "Send a mail" if mail else "Send a WhatsApp message")
    if mail:
        middle = [(key, row.get(key)) for key in ("cc", "bcc", "subject")
                  if str(row.get(key) or "").strip()]
        if row.get("attachments"):
            middle.append(("attachments", ", ".join(str(a) for a in row["attachments"])))
        text = ("body", row.get("preview") or "")
    else:
        skip = {"to_phone", "phone_number", "message"}
        middle = [(key, args[key]) for key in sorted(args) if key not in skip]
        text = ("message", row.get("preview") or "")
    lines = (card_lines([("to", outbound_hold._addressee(row))]) + card_lines(middle)
             + card_lines([text]))
    return "\n".join([head, ""] + lines)


# ── opening a card (the end of the request's turn) ───────────────────────────────────────

def _fallback(action: Optional[ParkedCall]) -> str:
    if action is not None:
        return CANNOT_SHOW.format(tool=action.tool)
    from vaf.core.agent_origin import APP_AGENT_DRAFT_TEXT
    return APP_AGENT_DRAFT_TEXT


def open_card(meta: Dict[str, Any], *, action: Optional[ParkedCall] = None,
              draft_ref: str = "", followups: Any = (), carried: str = "") -> Optional[str]:
    """The request's turn ended at a parked call (``action``) or a held draft (``draft_ref``):
    keep the request open and send its card to the device that asked. None when the card is
    open (the card's outcome answers the request), else the text to answer the request with
    now: a draft waits in the web UI only (``APP_AGENT_DRAFT_TEXT``), an action did not run
    (``CANNOT_SHOW``). No card for a device that no longer stands or is not the account's, for
    an app that cannot show cards, for a request at ``CONFIRMS_PER_ASK_MAX`` cards, for a
    draft that is not waiting or whose text would be over ``WHAT_MAX``. ``followups`` are the
    round's other drafts, carded after this card's Yes (`open_followup`); without a card they
    wait in the web UI, and the text says so. ``carried`` is an answer the request already has
    (a confirmed turn's), kept for its final ``done``. Never raises."""
    rest = [str(r) for r in (followups or ()) if str(r) and str(r) != str(draft_ref)]
    try:
        out = _open_card(meta, action=action, draft_ref=draft_ref, followups=rest,
                         carried=carried)
    except Exception:
        logger.debug("app link: opening a confirmation failed", exc_info=True)
        out = _fallback(action)
    if out is None:
        return None
    return "\n\n".join(t for t in (str(carried or "").strip(), out,
                                    FOLLOWUPS_WAIT if rest else "") if t)


def _open_card(meta: Dict[str, Any], *, action: Optional[ParkedCall],
               draft_ref: str, followups: List[str] = (), carried: str = "") -> Optional[str]:
    from vaf.core.app_link import bridge
    scope = str(meta.get("user_scope_id") or "").strip()
    did = str(meta.get("app_device_id") or "")
    aid = str(meta.get("app_ask_id") or "")
    fallback = _fallback(action)
    if not (meta.get("app_agent_ask") and scope and did and aid) or (action is None and not draft_ref):
        return fallback
    row = devices.get(did)
    if not row or str(row.get("user_scope_id") or "") != scope or not devices.row_stands(row):
        return fallback
    session = bridge.session_for(did)
    if session is not None and getattr(session, "confirm_unsupported", False):
        return fallback
    username = str(meta.get("username") or row.get("username") or "")
    from vaf.core import vocab
    lang = vocab.resolve_user_language(scope, username)
    now = time.time()
    entry = Pending(id=str(uuid.uuid4()), ask_id=aid, device_id=did, scope=scope,
                    username=username, chat_sid=devices.chat_session_id(scope), kind="",
                    what="", headline="", exp=now + CONFIRM_SECONDS, created=now)
    if action is not None:
        if action.kind not in KINDS or action.kind == "send":
            return fallback
        entry.kind, entry.what, entry.tool = action.kind, action.what, action.tool
        entry.args_json, entry.digest = action.args_json, action.digest
    else:
        from vaf.core import outbound_hold
        draft = _draft(draft_ref, username, scope)
        if draft is None or draft.get("state") not in _SENDABLE:
            return fallback
        args = _call_args(draft, username, scope) if draft.get("kind") == "call" else {}
        entry.kind, entry.what = "send", send_what(draft, args, lang)
        entry.tool = str(draft.get("tool") or "")
        entry.draft_ref, entry.draft_digest = str(draft["ref"]), outbound_hold.draft_digest(draft)
    if len(entry.what) > WHAT_MAX:
        return fallback
    entry.headline = entry.what.split("\n", 1)[0]
    with _lock():
        opened = sum(1 for c in _cards.values()
                     if c.scope == scope and c.device_id == did and c.ask_id == aid)
        reply = bridge._replies.get(bridge._rkey(scope, meta.get("app_reply_id")))
        if opened >= CONFIRMS_PER_ASK_MAX or reply is None or not reply.ask \
                or reply.device_id != did:
            return fallback
        reply.awaiting = entry.id
        if str(carried or "").strip():
            reply.carried = "\n\n".join(t for t in (reply.carried, str(carried).strip()) if t)
        entry.followups = list(followups or ())
        _cards[entry.id] = entry
    # The request stays open: the runner's last net must not close its reply, and a later
    # text of this turn goes nowhere (`bridge.send_text`).
    meta.pop("app_reply_id", None)
    meta["app_ask_closed"] = True
    _send_card(entry, row)
    return None


def _send_card(entry: Pending, row: Optional[Dict[str, Any]] = None) -> None:
    from vaf.core.app_link import bridge
    session = bridge.session_for(entry.device_id)
    if session is not None and bridge._send(entry.device_id, entry.frame()):
        entry.sent_to = id(session)
        return
    row = row or devices.get(entry.device_id)
    if row:
        bridge._went_offline(row)


def resend(session) -> int:
    """After ``welcome``: every card of this device that still waits, again, with its id and
    ``exp`` unchanged. How many."""
    did = session.device["device_id"]
    now = time.time()
    with _lock():
        due = sorted((c for c in _cards.values()
                      if c.device_id == did and c.state == "pending" and now <= c.exp),
                     key=lambda c: c.created)
    sent = 0
    for entry in due:
        if session.send_json(entry.frame()):
            entry.sent_to = id(session)
            sent += 1
    return sent


# ── the answer ───────────────────────────────────────────────────────────────────────────

def on_answer(session, row: Dict[str, Any], message: Dict[str, Any]) -> None:
    """``confirm_answer`` from the app (module docstring for the order of the checks)."""
    cid, ok = str(message["id"]), bool(message["ok"])
    did, scope = row["device_id"], str(row.get("user_scope_id") or "")
    now = time.time()
    error, settle_as, approved = "", "", None
    with _lock():
        entry = _cards.get(cid)
        if entry is None or entry.device_id != did or entry.scope != scope:
            error = "confirm_invalid"
        elif now > entry.exp:
            error = "expired"
            if entry.state == "pending":
                entry.state, entry.decided, settle_as = "expired", now, "expired"
        elif entry.state != "pending":
            error = "used"
        elif not ok:
            entry.state, entry.decided, settle_as = "declined", now, "declined"
        else:
            entry.state, entry.decided, approved = "approved", now, entry
    if error:
        session.send_json({"t": "error", "code": error, "ref": cid})
    if settle_as:
        _settled(entry, settle_as)
    if approved is not None:
        _queue_confirmed(approved)


def _queue_confirmed(entry: Pending) -> None:
    """The user's Yes: the wake turn that runs the call once and answers the request."""
    from vaf.core.task_queue import enqueue_wake_turn
    try:
        enqueue_wake_turn(
            kind="confirm", session_id=entry.chat_sid,
            text=f"{CONFIRM_WAKE_PREFIX} {entry.headline}", source=CHANNEL,
            user_scope_id=entry.scope, username=entry.username,
            extra={"origin_channel": CHANNEL, "task_class": "interactive",
                   "app_device_id": entry.device_id, "app_agent_ask": True,
                   "app_ask_id": entry.ask_id, "app_confirm_id": entry.id})
    except Exception:
        logger.debug("app link: queueing a confirmed call failed", exc_info=True)
        with _lock():
            if entry.state != "approved":
                return
            entry.state = "cancelled"
        _finish(entry, NOT_STARTED.format(tool=_name(entry)))


def _name(entry: Pending) -> str:
    return entry.tool or "the message"


def _finish(entry: Pending, text: str) -> None:
    from vaf.core.app_link import bridge
    try:
        bridge.finish_reply(entry.meta(), text)
    except Exception:
        logger.debug("app link: answering a request after its card failed", exc_info=True)


def _draft_parts(entry: Pending):
    from vaf.core import outbound_hold
    parsed = outbound_hold.parse_ref(entry.draft_ref)
    return parsed if parsed else (None, None)


def _settled(entry: Pending, how: str) -> None:
    """The card ended without a run (its state is set already): its draft as the outcome
    says, and the request answered once."""
    from vaf.core import outbound_hold
    tool = _name(entry)
    minutes = _minutes()
    if how == "declined":
        text = DECLINED.format(tool=tool)
        if entry.is_send:
            kind, num = _draft_parts(entry)
            dropped = False
            try:
                dropped = bool(kind and outbound_hold.discard_draft(
                    kind, num, username=entry.username, user_scope_id=entry.scope))
            except Exception:
                dropped = False
            if dropped:
                text = DECLINED_SEND
            else:
                draft = _draft(entry.draft_ref, entry.username, entry.scope)
                text = DECIDED_ELSEWHERE.format(
                    status=outbound_hold.status_line(draft) if draft else "the draft is gone.")
    elif how == "expired":
        text = (EXPIRED_SEND if entry.is_send else EXPIRED).format(tool=tool, minutes=minutes)
    elif how == "refused_by_app":
        text = REFUSED_BY_APP_SEND if entry.is_send else REFUSED_BY_APP.format(tool=tool)
    elif how == "decided_elsewhere":
        draft = _draft(entry.draft_ref, entry.username, entry.scope)
        text = DECIDED_ELSEWHERE.format(
            status=outbound_hold.status_line(draft) if draft else "the draft is gone.")
    elif how == "not_started":
        text = NOT_STARTED.format(tool=tool)
    else:
        # cancelled: the request was withdrawn, or its device no longer stands.
        text = ""
    if text and entry.followups:
        text = f"{text}\n\n{FOLLOWUPS_WAIT}"
    _finish(entry, text)


def followups_of(meta: Dict[str, Any]) -> List[str]:
    """The drafts still to be carded after the card a confirmed turn ran (none for any other
    turn)."""
    entry = card(str(meta.get("app_confirm_id") or "")) if meta.get("app_confirm_id") else None
    return list(entry.followups) if entry is not None else []


def open_followup(meta: Dict[str, Any], answer: str) -> Optional[str]:
    """The turn a Yes woke has answered: the next draft of the same round gets its card, with
    ``answer`` kept for the request's final ``done``. None when that card is open, else the
    text the request is answered with now (``answer``, and that the other messages wait in the
    web UI when their card could not be shown)."""
    rest = followups_of(meta)
    if not rest or not meta.get("app_agent_ask"):
        return answer
    return open_card(meta, draft_ref=rest[0], followups=rest[1:], carried=answer)


def refused_by_app(session) -> int:
    """The app answered ``unknown_type`` for ``confirm``: it cannot show cards. Every card sent
    to that connection is settled, and the session takes no card any more. How many."""
    session.confirm_unsupported = True
    did = (session.device or {}).get("device_id", "")
    with _lock():
        hit = [c for c in _cards.values()
               if c.device_id == did and c.state == "pending" and c.sent_to == id(session)]
        for entry in hit:
            entry.state, entry.decided = "refused_by_app", time.time()
    for entry in hit:
        _settled(entry, "refused_by_app")
    return len(hit)


# ── cancel and the sweep ─────────────────────────────────────────────────────────────────

def cancel(row: Dict[str, Any], ask_id: str) -> bool:
    """``cancel`` of a request whose card waits, from the device that asked: the card is
    settled (its draft discarded), or the confirmed turn still in the queue dropped, and the
    request answered with ``done`` and no text. False when the request has no such card (it
    runs or never parked: the bridge's own cancel applies)."""
    from vaf.core.app_link import bridge
    did, scope = row["device_id"], str(row.get("user_scope_id") or "")
    with _lock():
        mine = [c for c in _cards.values() if c.device_id == did and c.scope == scope
                and c.ask_id == str(ask_id) and c.state in ("pending", "approved")]
        if not mine:
            return False
        entry = mine[-1]
        if entry.state == "approved":
            reply = bridge._replies.get(bridge._rkey(scope, entry.ask_id))
            if reply is None or reply.awaiting != entry.id:
                return False          # its turn started: the running reply's cancel stops it
        was = entry.state
        entry.state, entry.decided = "cancelled", time.time()
    if was == "approved":
        from vaf.core.task_queue import TaskQueue
        TaskQueue().drop_queued_tasks_for_session(
            entry.chat_sid, where=lambda t: (t.metadata or {}).get("app_confirm_id") == entry.id)
    elif entry.is_send:
        from vaf.core import outbound_hold
        kind, num = _draft_parts(entry)
        try:
            if kind:
                outbound_hold.discard_draft(kind, num, username=entry.username,
                                            user_scope_id=entry.scope)
        except Exception:
            pass
    _finish(entry, "")
    return True


def _draft_moved(entry: Pending) -> bool:
    """Whether the draft of a send card was decided or changed elsewhere."""
    from vaf.core import outbound_hold
    draft = _draft(entry.draft_ref, entry.username, entry.scope)
    if draft is None or draft.get("state") not in _SENDABLE:
        return True
    return outbound_hold.draft_digest(draft) != entry.draft_digest


def settle(now: Optional[float] = None) -> int:
    """The sweep: a card past ``exp`` expires, a send card whose draft was sent, discarded or
    edited in the web UI is decided elsewhere, a card of a device that no longer stands is
    cancelled, an approved card whose turn the queue dropped answers its request, and cards
    long over are forgotten. How many were settled."""
    wall = time.time() if now is None else now
    with _lock():
        items = list(_cards.values())
    out: List[tuple] = []
    for entry in items:
        how = ""
        try:
            if entry.state == "pending":
                if wall > entry.exp:
                    how = "expired"
                elif not devices.stands(entry.device_id):
                    how = "cancelled"
                elif entry.is_send and _draft_moved(entry):
                    how = "decided_elsewhere"
            elif entry.state == "approved" and wall - entry.decided > START_GRACE_SECONDS:
                from vaf.core.task_queue import TaskQueue
                if not TaskQueue().has_queued(entry.chat_sid, in_flight=True, where=lambda t, e=entry: (
                        (t.metadata or {}).get("app_confirm_id") == e.id)):
                    how = "not_started"
        except Exception:
            logger.debug("app link: checking a confirmation failed", exc_info=True)
            continue
        if not how:
            continue
        with _lock():
            expected = "approved" if how == "not_started" else "pending"
            if entry.state != expected:
                continue
            entry.state = "cancelled" if how == "not_started" else how
            entry.decided = wall
        out.append((entry, how))
    for entry, how in out:
        _settled(entry, how)
    with _lock():
        for key in [k for k, c in _cards.items() if c.state not in _OPEN
                    and wall > max(c.exp, c.decided) + TOMBSTONE_SECONDS]:
            del _cards[key]
    return len(out)


# ── the confirmed run (the wake turn's first step, in the runner) ─────────────────────────

def run_confirmed(agent: Any, task: Any) -> Optional[str]:
    """Run the call the user confirmed, exactly once, and return the report the wake turn
    reads: line one ``CONFIRM_WAKE_PREFIX`` and the headline, then the outcome. None when the
    task names no card that is approved for it (claimed already, cancelled, or forgotten by a
    restart): then no turn runs, and nothing either.

    The card is claimed first (``approved`` -> ``running``), and only while the device that
    tapped Yes still stands (else it is settled as cancelled). An action runs through
    ``agent.execute_tool`` with the stored arguments, on the agent the runner bound to this
    turn (the device row's identity, ``asked_by_agent`` standing), inside
    ``agent_origin.confirmed_call_scope``. A send is read again: sent only while it waits and
    is what the card showed, through ``outbound_hold.send_draft`` with the bound identity and
    no wake of its own; the report carries the draft's decision line, so the next turn's
    decision note does not tell it twice."""
    meta = task.metadata if isinstance(getattr(task, "metadata", None), dict) else {}
    cid = str(meta.get("app_confirm_id") or "")
    # The device that tapped Yes must still stand when the call runs: one removed while its
    # confirmed turn waited in the queue (a lost phone) runs nothing.
    stands = devices.stands(str(meta.get("app_device_id") or ""))
    with _lock():
        entry = _cards.get(cid)
        if entry is None or entry.state != "approved" \
                or entry.device_id != str(meta.get("app_device_id") or "") \
                or entry.scope != str(meta.get("user_scope_id") or "") \
                or entry.ask_id != str(meta.get("app_ask_id") or ""):
            return None
        if not stands:
            entry.state, entry.decided = "cancelled", time.time()
        else:
            entry.state = "running"
    if not stands:
        _settled(entry, "cancelled")
        return None
    try:
        body = _run_send(agent, entry) if entry.is_send else _run_action(agent, entry)
    finally:
        with _lock():
            entry.state, entry.decided = "done", time.time()
    return "\n".join([f"{CONFIRM_WAKE_PREFIX} {entry.headline}", body, "", _INSTRUCTION])


def _run_action(agent: Any, entry: Pending) -> str:
    from vaf.core.agent_origin import call_digest, confirmed_call_scope, confirmed_result_text
    try:
        args = json.loads(entry.args_json or "{}")
    except Exception:
        args = None
    if not isinstance(args, dict) or call_digest(entry.tool, args) != entry.digest:
        return f"'{entry.tool}' did NOT run: its stored call could not be read back."
    with confirmed_call_scope(entry.tool, entry.digest):
        result = agent.execute_tool(entry.tool, dict(args))
    return (f"Your user CONFIRMED '{entry.tool}' in the Veyllo App, and it ran once just now. "
            f"Its result:\n{confirmed_result_text(entry.tool, result)}")


def _run_send(agent: Any, entry: Pending) -> str:
    from vaf.core import outbound_hold
    kind, num = _draft_parts(entry)
    draft = _draft(entry.draft_ref, entry.username, entry.scope)
    if kind is None or draft is None or draft.get("state") not in _SENDABLE \
            or outbound_hold.draft_digest(draft) != entry.draft_digest:
        status = outbound_hold.status_line(draft) if draft else "the draft is gone."
        return ("Your user confirmed the message in the Veyllo App, but it had been decided in "
                f"VAF's web UI meanwhile, and the phone sent nothing: {status}")
    role = str(getattr(agent, "_current_user_role", None) or "user")
    res = outbound_hold.send_draft(kind, num, username=entry.username,
                                   user_scope_id=entry.scope, user_role=role, wake=False,
                                   tools=getattr(agent, "tools", None))
    after = _draft(entry.draft_ref, entry.username, entry.scope) or draft
    line = outbound_hold.status_line(after)
    if res.get("ok"):
        return f"Your user CONFIRMED the message in the Veyllo App. {line}"
    return (f"Your user CONFIRMED the message in the Veyllo App, but sending it failed "
            f"({res.get('error') or 'no reason given'}). {line}")
