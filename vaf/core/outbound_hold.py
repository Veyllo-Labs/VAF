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
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Tuple

# The states a draft can be sent or dropped from: it is waiting, or its last attempt failed
# and it is still the person's. `sent` and `discarded` have left their hands.
_ACTIONABLE: Tuple[str, ...] = ("held", "failed")

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
    """
    p = preview_of(tool_name, args)
    who = p["recipient"] or "the recipient"
    tail = f" (draft {entry_id})" if entry_id is not None else ""
    return (
        f"{HELD_PREFIX} The message to {who} is waiting as a draft for the user to send or "
        f"discard{tail}. Tell the user it is ready for their word and do NOT claim it was "
        "sent; do not call the tool again for the same message."
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


def pending(username: str, user_scope_id: Optional[str] = None, *,
            limit: int = 50, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Everything waiting for this person's word, newest first, in one row shape.

    Two sources, one shape, the way `vaf.core.inbox` unifies its five: the mail outbox's held
    drafts and the parked messenger calls. `kind` says which lane an id belongs to, because
    approving is a different act in each: a mail draft is already an artifact and only has to
    be released, a parked call has to be dispatched.

    `session_id` narrows the list to ONE conversation, which is what the card in a chat asks
    for: a message being written in one chat must never appear in another, and the person
    switching chats mid-draft is the ordinary case, not the exception. A draft that belongs to
    no chat (a Front Office answer, a draft from before this existed) is then left out on
    purpose: its home is the inbox and the mail window, not somebody's conversation.

    A call whose last attempt FAILED is listed too, with the reason on it: it is still the
    person's to send or drop, and a draft that is only reachable while everything works is not
    a guard. Reading the list is also where a row a crashed worker left mid-send is parked
    (`reclaim_stranded_held_sends`), because this is the one call every surface makes first.
    """
    want = str(session_id or "").strip()
    rows: List[Dict[str, Any]] = []
    try:
        from vaf.core import channel_message_store as store
        store.reclaim_stranded_held_sends(username, user_scope_id)
        for state in _ACTIONABLE:
            for r in store.held_sends(username, user_scope_id, state=state, limit=limit):
                if want and str(r.get("session_id") or "") != want:
                    continue
                rows.append({
                    "kind": "call", "id": int(r["id"]), "channel": r.get("channel") or "",
                    "tool": r.get("tool") or "", "recipient": r.get("recipient") or "",
                    "subject": "", "preview": r.get("preview") or "",
                    "created_ts": float(r.get("created_ts") or 0.0),
                    "session_id": r.get("session_id") or "",
                    "state": str(r.get("state") or "held"), "error": str(r.get("error") or ""),
                })
    except Exception:
        pass
    try:
        from vaf.mail.service import MailService
        if user_scope_id:
            svc = MailService(user_scope_id)
            for d in svc.list_drafts():
                if want and str(d.get("chat_session_id") or "") != want:
                    continue
                rows.append({
                    "kind": "mail", "id": int(d.get("op_id") or 0), "channel": "mail",
                    "tool": "send_mail", "recipient": str(d.get("to") or ""),
                    "subject": str(d.get("subject") or ""), "preview": str(d.get("body") or ""),
                    "created_ts": _epoch(d.get("created_at")),
                    "session_id": str(d.get("chat_session_id") or ""),
                    "state": "held", "error": "",
                })
    except Exception:
        pass
    rows.sort(key=lambda r: r.get("created_ts") or 0.0, reverse=True)
    return rows[: max(1, int(limit))]


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
    back to held with the error on it: a bridge that is down must not consume the draft. A
    draft whose last attempt FAILED is claimable again, because it is still the person's to
    send; only the states that have left the person's hands (sent, discarded) are not.
    """
    from vaf.core import channel_message_store as store

    row = store.held_send(entry_id, username, user_scope_id)
    was = str((row or {}).get("state") or "")
    if not row or was not in _ACTIONABLE:
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
