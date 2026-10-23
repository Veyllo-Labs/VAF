# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import time
import threading
import traceback
import os
import sys
import gc
import re
import logging
import uuid
from typing import NamedTuple

# CRITICAL: Disable CUDA for PyTorch BEFORE any torch import to prevent memory explosion
# PyTorch pre-allocates GPU memory even when using CPU-only models!
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")  # Hide GPU from PyTorch
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "max_split_size_mb:32")

import requests
from vaf.core.agent import Agent
from vaf.core.channels import CHAT_CHANNELS, CHAT_SESSION_PREFIXES
from vaf.core.identity_binding import (Identity, identity_from_metadata,
                                       resolve_scope_identity)
from vaf.core.task_queue import TaskQueue
from vaf.core.turn_binding import TurnBinding, TurnRefused, bind_turn, end_turn
from vaf.core.subagent_ipc import take_result_notification
from vaf.core.session import (SessionManager, Session, TurnStatus, failed_turn_meta,
                              stored_turn_copy, turn_context_messages_since_last_user)
from vaf.core.tray_context import TrayContext
from vaf.core.web_interface import get_web_interface
from vaf.core.platform import Platform
from vaf.core.log_helper import append_domain_log, append_domain_log_always, get_app_log_dir, get_dated_log_path, is_debug_logging_enabled
from vaf.core.config import Config, subagent_provider_override
from pathlib import Path

# Memory management constants - AGGRESSIVE to prevent 25GB situations.
# The warning threshold must sit ABOVE the process baseline: with the embedding
# model deliberately kept loaded the runner idles around 2.4GB, and the standard
# cleanup (cache clear + gc.collect) cannot hand freed heap pages back to the OS,
# so a threshold below baseline warns every 30s forever without ever clearing.
MEMORY_CHECK_INTERVAL = 30  # Check memory every 30 seconds
MEMORY_THRESHOLD_MB = 4096  # Trigger standard cleanup above 4GB
MEMORY_CRITICAL_MB = 6144  # Force aggressive cleanup (unload models) above 6GB

# Throttle stream updates to WebUI so the UI does not lag behind (max ~12 updates/sec)
STREAM_EMIT_THROTTLE_SEC = 0.08
CHANNEL_HISTORY_WINDOW_MESSAGES = 15


class _StopGenerationRequested(Exception):
    """Internal control-flow exception for cooperative user stop."""
    pass


def _room_ping_sweep() -> None:
    """The clock the rooms THIS machine holds run on: check-ins and vote deadlines.

    Why it lives in the runner: the room store has no daemon of its own, this loop
    already runs for every tenant, and both acts are HOST acts - the machine
    holding the room is the only one that can see every member's cursor, and the
    only one allowed to say how a vote ended. A guest cannot emit either (`is_host`
    is keyed on the tenant, and a redeemed ticket has none), so there is nothing to
    coordinate between machines.

    Two clocks, deliberately not one setting: the idle check-in is an hourly
    courtesy that a person may turn off (`a2a_room_ping_minutes`, 0 = never), while
    a vote's deadline is part of the protocol - turning check-ins off must not
    leave every vote open forever, waiting for a result nobody will write.

    Cheap by construction: it answers from data the store already keeps, and every
    frame it emits is addressed to ONE peer, so nineteen other members of a
    twenty-member room are not woken to hear that somebody else was quiet.

    Never raises into the loop: a damaged room must not stop the drain that every
    other room depends on.
    """
    from vaf.core.a2a.room import Room, list_rooms, owner_tenant, participant_key
    from vaf.core.config import Config

    try:
        minutes = float(Config.get("a2a_room_ping_minutes", 60) or 0)
    except Exception:
        minutes = 0.0
    quiet_for = minutes * 60.0

    now = time.time()
    for room_id in list_rooms():
        try:
            room = Room.open(room_id)
            if room.closed:
                continue
            owner = owner_tenant(room.manifest.get("owner_scope"))
            if not owner:
                continue
            # The host acts as its own AGENT lane here rather than the terminal's:
            # the check-in is the room's machine speaking, and the agent lane is the
            # one that is always present while VAF runs.
            host = room.identity_for(participant_key("agent", owner))
            if host is None or not room.is_host(host):
                continue
            # A PERSON's lane is not an agent to wake: a check-in there reaches a
            # human who is simply not at the keyboard, once an hour, forever.
            # Measured in the first live sweep, which asked the owner's terminal
            # handle whether it was still with the room.
            #
            # EVERY account represented here, not just the one that owns the room:
            # in a room across tenant lines the other four people are ordinary idle
            # peers to this loop, and it would ping each of them hourly into a lane
            # where nothing wakes and nothing answers - the same regression, one
            # tenant over. Derived exactly rather than guessed, the same key the
            # browser and the CLI both act under.
            human_peers = set()
            for _tenant in room.tenants():
                _handle = room.identity_for(participant_key("cli", _tenant))
                if _handle is not None:
                    human_peers.add(_handle.peer_id)
            _sweep_votes(room, host, human_peers, now)
            _sweep_quiet_work(room, host, human_peers, now)
            if quiet_for <= 0:
                continue
            # Once per interval, DERIVED from the store the way the vote and task
            # once-rules already are - the check-in is itself a frame. This lived
            # in a process-local dict first, and the docstring called a restart
            # re-ask "the harmless direction"; a day of live restarts measured it
            # otherwise: every restart re-pinged every idle member within seconds,
            # until a quarter of a busy room's frames were check-ins - each one
            # lighting the person's unread badge for a frame no view shows.
            checked = room.check_ins()
            for peer in room.idle_peers(quiet_for_s=quiet_for, now=now):
                if peer == host.peer_id or peer in human_peers:
                    continue          # the host is asking; a person is not an agent
                if (now - checked.get(peer, 0.0)) < quiet_for:
                    continue          # asked inside this interval, maybe by an earlier process
                try:
                    room.ping(host, peer)
                except Exception:
                    continue
        except Exception:
            continue


def _sweep_quiet_work(room, host, human_peers, now: float) -> None:
    """Ask about open work nothing has been said about for half an hour.

    The room cannot tell a long run from an abandoned one - that is the whole reason
    it asks rather than deciding. A report either way costs one line and is what keeps
    the board from filling with work nobody is doing; measured on the first long-lived
    room, ten entries counted as running and eight had last been reported on more than
    a day earlier.

    Skips the same two members every check-in skips, for the same two reasons: a person
    is not an agent to wake, and a frame the host addressed to itself cannot wake it
    because a peer's unread set never contains its own writes.
    """
    try:
        due = room.task_nudges(now=now)
    except Exception:
        return
    for peer, task in due:
        if peer == host.peer_id or peer in human_peers:
            continue
        try:
            room.nudge_task(host, peer, task)
        except Exception:
            continue


def _sweep_votes(room, host, human_peers, now: float) -> None:
    """End the votes that are over, and nudge the members still holding one up.

    In that order, and the order matters: a vote whose deadline has passed is
    concluded rather than reminded about, or the last thing an agent would hear
    about a question is a reminder for one that had already ended.

    Two members get no reminder, for two different reasons, and neither of them is
    exempt from the deadline - a rule that only applied to some members would be a
    different rule for whoever is best placed to ignore it.

    The person's own lane, because a nudge there reaches a human who is looking at
    the vote card in their browser, with its countdown, right now.

    The host's own agent, because a frame it wrote cannot wake it: a peer's unread
    set never contains its own writes, so a self-addressed reminder would be a
    frame with no reader. It learns about the vote when the vote itself arrives -
    a vote is addressed to the room, so it wakes every member including this one -
    and again in any room turn while the question stays open.
    """
    try:
        room.conclude_votes(host, now=now)
    except Exception:
        pass
    try:
        due = room.vote_reminders(now=now)
    except Exception:
        return
    for peer, entry in due:
        if peer == host.peer_id or peer in human_peers:
            continue
        try:
            room.remind_vote(host, peer, entry)
        except Exception:
            continue


def _front_office_chat_ref(metadata, username):
    """Which conversation a Front Office turn belongs to, in the message store's terms, so the
    agent's question to the person can be recorded on that chat (the inbox's "waits for you,
    the agent asked"). Read off the task metadata the bridges already carry; an unresolved
    WhatsApp @lid has no store key and gives None."""
    meta = metadata or {}
    try:
        jid = str(meta.get("whatsapp_chat_jid") or "").strip()
        if jid:
            from vaf.api.whatsapp_bridge import _jid_to_chat_id
            key = _jid_to_chat_id((username or "").strip() or "admin", jid)
            return {"channel": "whatsapp", "chat_id": key} if key else None
        tg = str(meta.get("telegram_chat_id") or "").strip()
        if tg:
            return {"channel": "telegram", "chat_id": tg}
        dc = str(meta.get("discord_author_id") or "").strip()
        if dc:
            return {"channel": "discord", "chat_id": dc}
        tid = meta.get("email_thread_id")
        if tid is not None and str(tid).strip():
            # The inbox keys a mail conversation on its thread id (chat_marks channel "mail").
            return {"channel": "mail", "chat_id": str(tid).strip()}
    except Exception:
        return None
    return None


def _deliver_email_reply(task, meta: dict, final_text: str) -> None:
    """A Front Office answer on mail leaves through the outbox (MailService.queue_send):
    the case anchor as Message-ID, the RFC 3834 loop guard, the thread's References, the
    quoted original under the answer; held for the owner's approval in draft mode,
    delivered at once in send mode. Never a send tool, never a direct SMTP call, so every
    answer has its Sent copy, its ledger row and its delivery stamp (EMAIL_CLIENT.md,
    "Native send"). Never raises: a failure is logged and the mail stays waiting."""
    from vaf.core.email_accounts import get_account
    from vaf.core.log_helper import append_lane_log
    from vaf.mail.service import MailService, deliver_queued_sends
    scope = str(meta.get("user_scope_id") or "").strip()
    account_id = str(meta.get("email_account_id") or "").strip()
    pk = meta.get("email_message_pk")
    if not scope or not account_id or pk is None:
        return
    out = _prepare_channel_outbound(final_text)
    if not out:
        append_lane_log("email_inbound", f"REPLY EMPTY after sanitize pk={pk} session={task.session_id!r}", always=True)
        return
    try:
        svc = MailService(scope)
        pre = svc.reply_prefill(int(pk))
        if not pre or not pre.get("to"):
            append_lane_log("email_inbound", f"REPLY SKIP no recipients pk={pk}", always=True)
            return
        apk = svc.store.account_pk(account_id)
        case_id = str(meta.get("email_case_id") or "")
        root_anchor = ""
        if apk is not None and case_id:
            earlier = svc.store.sent_ids_for_case(apk, case_id)
            root_anchor = str(earlier[0].get("message_id") or "") if earlier else ""
        hold = str(meta.get("email_reply_mode") or "draft") != "send"
        queued = svc.queue_send(
            account_id, pre["to"], pre["subject"], f"{out}{pre.get('body') or ''}",
            cc=pre.get("cc") or "", in_reply_to=pre.get("in_reply_to") or "",
            references=pre.get("references") or "", undo_seconds=0,
            case_id=case_id, sent_by="front_office", hold=hold, agent_written=True,
            root_anchor=root_anchor, reply_to_pk=int(pk), thread_id=meta.get("email_thread_id"))
        if apk is not None and case_id:
            svc.store.touch_case(apk, case_id, outbound=True)
            if hold:
                svc.store.set_case_status(apk, case_id, "held")
        delivered = False
        if not hold:
            acc = get_account(account_id, meta.get("username"), user_scope_id=scope)
            if acc:
                deliver_queued_sends(scope, acc, meta.get("username"), account_id, service=svc)
                delivered = svc.send_outcome(int(queued.get("op_id") or 0)).get("state") == "done"
            # Answered only once the mail really left: a queued or failed send leaves the
            # case as it was, and the sweep that delivers it later marks the message.
            if delivered and apk is not None and case_id:
                svc.store.set_case_status(apk, case_id, "answered")
        append_lane_log("email_inbound",
                        f"REPLY {'HELD' if hold else ('SENT' if delivered else 'QUEUED')} pk={pk} op={queued.get('op_id')} case={case_id or '-'} to={str(pre['to'])[:3]}***",
                        always=True)
    except Exception as e:
        append_lane_log("email_inbound", f"REPLY FAILED pk={pk}: {e}", always=True)
        return
    try:
        from vaf.core.web_interface import notify_inbox_changed
        notify_inbox_changed(scope)
    except Exception:
        pass


_CONTACT_TURN_FAILED_EN = "Sorry, I can't reply right now. Please try again a little later."


def _turn_error_text(meta: dict, error, contact=None) -> str:
    """What a messenger receives when a chat turn failed: it raised (`error` is the exception)
    or it handed a backend failure back as its text (`error` is that text, see
    `completion.is_error_reply`).

    The owner's own chat gets the error itself: it is theirs to read and act on. A contact's
    (Front Office) turn gets a fixed sentence and never the exception text, which is internal
    (file paths, store and session ids, a provider's raw answer) while the reader is a third
    party. The sentence follows the language on the contact's record, else English: the
    owner's language says nothing about the person writing in, the same rule the Front Office
    prompt gives the model."""
    if (meta or {}).get("from_contact"):
        lang = contact.get("preferred_language") if isinstance(contact, dict) else None
        try:
            from vaf.core import vocab
            text = vocab.pick("contact_turn_failed", lang)
        except Exception:
            text = ""
        return text or _CONTACT_TURN_FAILED_EN
    err_msg = str(error).replace("\n", " ")[:400]
    return f"Sorry, something went wrong: {err_msg}"


def _drop_pending_voice_reply(source, meta: dict) -> None:
    """Make the next reply to this chat a text: the Telegram and the WhatsApp bridge read a
    reply aloud while a voice note from that chat is pending (Telegram keys it by chat id,
    WhatsApp by user and JID; Discord has no voice reply). A failed turn's sentence goes out
    as text, also after a voice note. Never raises."""
    try:
        if source == "telegram" and meta.get("telegram_chat_id"):
            from vaf.api.telegram_bridge import _voice_reply_lock, _voice_reply_pending
            key = str(meta["telegram_chat_id"])
        elif source == "whatsapp" and meta.get("whatsapp_chat_jid"):
            from vaf.api.whatsapp_bridge import _voice_reply_lock, _voice_reply_pending
            key = f"{meta.get('username') or 'admin'}|{meta['whatsapp_chat_jid']}"
        else:
            return
        with _voice_reply_lock:
            _voice_reply_pending.pop(key, None)
    except Exception:
        pass


def _send_turn_error_reply(task, error, contact=None) -> None:
    """Answer a failed turn on the messenger it came from (Telegram, Discord, WhatsApp), so
    nobody waits for a reply that will not come; the text is `_turn_error_text`'s. `contact`
    is the record the runner pinned for THIS turn (`agent._front_office_contact`). Mail
    answers nothing here: a Front Office mail that failed stays waiting for the owner, and
    the mail lane's log says so. Never raises."""
    try:
        source = getattr(task, "source", None)
        meta = (task.metadata or {}) if getattr(task, "metadata", None) else {}
        text = _turn_error_text(meta, error, contact)
        _drop_pending_voice_reply(source, meta)
        if source == "telegram" and meta.get("telegram_chat_id"):
            from vaf.core.telegram_reply import send_telegram_reply
            send_telegram_reply(str(meta["telegram_chat_id"]), text)
        elif source == "discord" and meta.get("discord_channel_id"):
            from vaf.core.discord_reply import send_discord_reply
            send_discord_reply(str(meta["discord_channel_id"]), text)
        elif source == "whatsapp" and meta.get("whatsapp_chat_jid"):
            from vaf.core.whatsapp_reply import send_whatsapp_reply
            username = meta.get("username") or "admin"
            jid = str(meta["whatsapp_chat_jid"])
            send_whatsapp_reply(
                username, jid, text,
                user_scope_id=meta.get("user_scope_id") and str(meta["user_scope_id"]) or None,
            )
        elif source == "email":
            from vaf.core.log_helper import append_lane_log
            append_lane_log("email_inbound",
                            f"REPLY SKIP the turn failed pk={meta.get('email_message_pk')} "
                            f"session={getattr(task, 'session_id', None)!r}", always=True)
    except Exception:
        pass


def answer_failed_load(task, error, contact=None) -> None:
    """Close a chat turn whose chat could not be loaded, the way a turn that raised is closed.

    The turn does not run. The agent holds no chat after a failed load
    (`Agent.load_session_context` fails closed): no history to answer from, and no chat
    to store the exchange in. Storing it anyway could replace a chat file that did not
    load because it could not be read, which is still recoverable. The web UI gets the
    error in the chat's timeline and its turn closed; a messenger gets the usual failure
    reply (`_send_turn_error_reply`, the fixed sentence for a contact, in the language on
    `contact`, the record the turn was bound with, which is resolved before the load).
    Never raises."""
    try:
        get_web_interface().log(
            f"The chat could not be loaded for session {task.session_id}: {error}",
            level="error", source="System", session_id=task.session_id)
    except Exception:
        pass
    try:
        get_web_interface().emit_message_complete(content="", session_id=task.session_id)
    except Exception:
        pass
    _send_turn_error_reply(task, error, contact=contact)


def drain_failure_notice(session_id, error) -> None:
    """Tell the chat whose sub-agent result could not be summarized, so its browser is not
    left waiting. Only that chat: an emit without a session goes to every connection
    (`web_interface._push_session_update`), and after a load that failed closed the agent
    names no current session any more. No chat known, nobody is told. Never raises."""
    if not session_id:
        return
    try:
        msg = f"Sub-Agent completed - summary could not be generated ({type(error).__name__})."
        get_web_interface().emit_agent_message("assistant", msg, session_id=session_id)
        get_web_interface().emit_message_complete(content=msg, session_id=session_id)
    except Exception:
        pass


def _deliver_channel_reply(task, final_text: str, *, session_mgr=None, contact=None,
                           turn: TurnStatus = TurnStatus()) -> None:
    """Send a finished turn's answer back where the task came from: Telegram, Discord,
    WhatsApp, or the mail outbox (`_deliver_email_reply`); a web turn sends nothing here.

    A contact's turn that produced no answer is not sent: the contact gets the fixed
    sentence `_send_turn_error_reply` sends for a turn that raised, and a mail nothing.
    That is a turn the engine marked (`turn.failed`, the agent's `last_turn`: the backend
    failed or loop protection stopped it), or one whose outgoing text is a failure
    (`completion.is_error_reply`, which also covers the runner's own "[Error] No response
    ...") or a stop note (`completion.is_stop_note`). A turn that had answered before such a
    failure ended it (`turn.failure`) is sent its answer, without that text. The text is
    judged as the contact would receive it, after
    `_prepare_channel_outbound`: the model's reasoning never leaves, so reasoning that quotes
    an "[Error] ..." tool result does not decide the turn. The owner's own chats get the text
    as it is, and so does the web UI, which the runner fed before this. `contact` is the
    record pinned for THIS turn. `session_mgr` serves the WhatsApp fallback that reads the
    chat's JID off the session. Never raises."""
    try:
        task_source = getattr(task, "source", None)
        meta = (task.metadata or {}) if getattr(task, "metadata", None) else {}
        chat_id = meta.get("telegram_chat_id")
        try:
            from vaf.core.log_helper import log_telegram_reply
            log_telegram_reply(f"HEADLESS task_source={task_source!r} chat_id={chat_id!r} final_len={len(str(final_text))}")
        except Exception:
            pass
        if meta.get("from_contact"):
            from vaf.core.completion import is_error_reply, is_stop_note
            if turn.failure and not turn.failed:
                # Answered, then a later round failed: the answer goes out, the failure not,
                # taken off as the text's tail by the same rule the stored copy follows. A
                # failure the outgoing text does not end with (the tool-call JSON strip cut
                # into it), or holds once more, cannot be taken off safely, so nothing of the
                # turn goes out: the marker check below would catch a provider failure anyway,
                # a failure text without a marker it would not.
                from vaf.core.session import take_off_turn_failure
                answer = take_off_turn_failure(final_text, turn.failure)
                if answer is None:
                    _send_turn_error_reply(task, str(final_text or ""), contact=contact)
                    return
                final_text = answer
            visible = _prepare_channel_outbound(final_text)
            if turn.failed or is_error_reply(visible) or is_stop_note(visible):
                _send_turn_error_reply(task, final_text, contact=contact)
                return
        if task_source == "telegram" and chat_id:
            from vaf.core.telegram_reply import send_telegram_reply
            out = _prepare_channel_outbound(final_text)
            if not out:
                try:
                    log_telegram_reply(
                        f"HEADLESS reply empty/blocked after sanitize "
                        f"raw_len={len(str(final_text))} chat_id={chat_id}"
                    )
                except Exception:
                    pass
            else:
                send_telegram_reply(str(chat_id), out)
        elif task_source == "discord":
            discord_channel_id = meta.get("discord_channel_id")
            if discord_channel_id:
                from vaf.core.discord_reply import send_discord_reply
                try:
                    from vaf.core.log_helper import log_discord_reply
                    log_discord_reply(
                        f"HEADLESS task_source=discord channel_id={discord_channel_id!r} final_len={len(str(final_text))}"
                    )
                except Exception:
                    pass
                out = _prepare_channel_outbound(final_text)
                if out:
                    send_discord_reply(str(discord_channel_id), out)
        elif task_source == "email":
            # A Front Office answer on mail: through the outbox, held or sent
            # by the channel's reply mode (see _deliver_email_reply).
            _deliver_email_reply(task, meta, final_text)
        elif task_source == "whatsapp":
            chat_jid = meta.get("whatsapp_chat_jid")
            username = meta.get("username") or "admin"
            # Fallback: session may have whatsapp_chat_jid if task metadata was not persisted (e.g. self-chat @lid)
            if not chat_jid and session_mgr is not None:
                try:
                    _s = session_mgr.load(task.session_id)
                    chat_jid = (_s.metadata or {}).get("whatsapp_chat_jid")
                    if not username or username == "admin":
                        username = (_s.metadata or {}).get("username") or "admin"
                except Exception:
                    pass
            if chat_jid:
                from vaf.core.whatsapp_reply import send_whatsapp_reply
                try:
                    from vaf.core.log_helper import log_whatsapp_reply
                    log_whatsapp_reply(
                        f"HEADLESS task_source=whatsapp jid={chat_jid!r} final_len={len(str(final_text))}"
                    )
                except Exception:
                    pass
                out = _prepare_channel_outbound(final_text)
                if out:
                    send_whatsapp_reply(username, str(chat_jid), out, user_scope_id=meta.get("user_scope_id") and str(meta["user_scope_id"]) or None)
                else:
                    try:
                        from vaf.core.log_helper import log_whatsapp_reply
                        log_whatsapp_reply(f"HEADLESS whatsapp reply BLOCKED by sanitize jid={chat_jid!r}")
                    except Exception:
                        pass
            else:
                try:
                    from vaf.core.log_helper import log_whatsapp_reply
                    log_whatsapp_reply(
                        f"HEADLESS whatsapp reply SKIP no chat_jid session={task.session_id!r}"
                    )
                except Exception:
                    pass
    except Exception:
        pass


def _clean_for_session(text: str) -> str:
    """The chat turn's stored copy: full content for display; only redacted blocks removed.
    <think> blocks stay so the UI can show thinking after reload (the replay in agent.py
    strips them from the model's context)."""
    if not text:
        return ""
    cleaned = re.sub(r'<redacted_reasoning>.*?</redacted_reasoning>', '', text, flags=re.DOTALL)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()


def _chat_turn_copy(response_parts, response_text, *, ends_at_draft, turn):
    """The chat lane's stored copy of a turn and the metadata to store it with; `turn` is the
    agent's `last_turn`, read right after the turn.

    The copy is the stream, cleaned (`_clean_for_session`); a failure that followed an answer
    is its tail, cleaned apart and recorded the way the copy holds it (`stored_turn_copy`).
    When nothing streamed, or the turn ended at a draft (what streamed before the draft is in
    the tool-call message, and the turn's own answer is the fixed sentence the browser
    hides), the copy is the turn's return value, which never holds such a failure: nothing
    is recorded, only the failed-turn mark can apply."""
    if response_parts and not ends_at_draft:
        return stored_turn_copy("".join(response_parts), turn, _clean_for_session)
    return _clean_for_session(str(response_text or "")), failed_turn_meta(turn.failed)


def _drain_turn_copy(response_parts, *, turn):
    """The sub-agent drain's stored copy of its summary turn and the metadata to store it
    with: the stream through `_clean_drain_text`, a failure that followed an answer cleaned
    apart as its tail (`stored_turn_copy`). The summary turn is a turn like any other, so a
    failed one is marked and an answered-then-failed one records its failure. `turn` is the
    agent's `last_turn`, read right after the summary turn."""
    return stored_turn_copy("".join(response_parts or []), turn, _clean_drain_text)


def _clean_drain_text(text: str) -> str:
    """The sub-agent drain's stored copy of its summary turn: reasoning removed, leaked
    tool-call JSON stripped, blank-line runs collapsed."""
    text = re.sub(r"<think>.*?</think>", "", str(text or ""), flags=re.DOTALL)
    text = _strip_tool_calls_json(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _apply_channel_history_window(agent, source: str) -> None:
    """
    Keep only a small recent window for channel sessions (Telegram/WhatsApp/Discord)
    so stale long-tail chat history does not dominate tool decisions.
    """
    if source not in CHAT_CHANNELS + ("email",):
        return
    try:
        raw_limit = Config.get("channel_history_window_messages", CHANNEL_HISTORY_WINDOW_MESSAGES)
        limit = int(raw_limit)
    except Exception:
        limit = CHANNEL_HISTORY_WINDOW_MESSAGES
    limit = max(6, min(limit, 80))

    history = getattr(agent, "history", None)
    if not isinstance(history, list):
        return
    if len(history) <= limit + 1:
        return

    # Preserve system prompt if present, then keep only latest conversation entries.
    first = history[0] if history else None
    tail = history[-limit:]
    if isinstance(first, dict) and first.get("role") == "system":
        agent.history = [first] + tail
    else:
        agent.history = tail

def _strip_tool_calls_json(text: str) -> str:
    """Remove raw tool_calls JSON blobs and fragments from reply text."""
    if not text:
        return text
        
    # Check for common tool call JSON patterns
    if "tool_calls" not in text and '"name":' not in text and '"function":' not in text:
        return text
        
    # Pattern-based removal for leaked JSON fragments (incl. tail like ", "name": "update_working_memory"}, "type": "function", "index": 0}]}")
    import re
    patterns = [
        r'\[?\s*\{\s*"tool_calls":.*$',
        r'\{\s*"name":\s*"[^"]*",\s*"arguments":.*$',
        r'\{\s*"index":\s*\d+,\s*"id":.*$',
        r'",\s*"name":\s*"[^"]*",\s*"type":\s*"function".*$',
        r'"\}\s*,\s*"type":\s*"function".*$',
        r'",\s*"name":\s*"[^"]*"\}\s*,\s*"type":\s*"function".*$',
        r',\s*"name":\s*"[^"]*"?\s*$',
        r'\}\s*,\s*\{\s*"index":\s*\d+.*$',
        r'\}\s*,\s*\{\s*"name":\s*"[^"]*".*$',
        r'\]\s*\}\s*$',
        r'\}\s*\]\s*\}\s*$',
    ]
    
    out = text
    for p in patterns:
        out = re.sub(p, '', out, flags=re.DOTALL | re.MULTILINE)
        
    # Legacy brace counting for full blocks (fallback)
    if '{"tool_calls"' in out or "{'tool_calls'" in out:
        temp_out = []
        i = 0
        while i < len(out):
            start = out.find('{"tool_calls"', i)
            if start == -1:
                start = out.find("{'tool_calls'", i)
            if start == -1:
                temp_out.append(out[i:])
                break
            temp_out.append(out[i:start])
            # Match closing brace
            depth = 0
            j = start
            in_dq = False
            escape = False
            while j < len(out):
                c = out[j]
                if escape:
                    escape = False
                    j += 1
                    continue
                if c == "\\" and in_dq:
                    escape = True
                    j += 1
                    continue
                if c == '"' and not in_dq:
                    in_dq = True
                    j += 1
                    continue
                if c == '"' and in_dq:
                    in_dq = False
                    j += 1
                    continue
                if in_dq:
                    j += 1
                    continue
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                j += 1
            i = j
        out = "".join(temp_out)
        
    return out.strip()


# The internal-phrase net lives in vaf/core/outbound_sanitizer.py now: the four send
# TOOLS consume it too, and the extension layer must not import a private symbol of
# this worker loop. The runner's own chain below keeps calling the same net.
from vaf.core.outbound_sanitizer import sanitize_outgoing_message

# Regex to strip [WORKFLOW_ASYNC:...] lines so the rest of the message can be sent if only that line is internal.
_WORKFLOW_ASYNC_LINE = re.compile(r"^\[WORKFLOW_ASYNC:[^\]]+\][^\n]*\n?", re.MULTILINE)


def _strip_workflow_async_from_message(text: str) -> str:
    """Remove [WORKFLOW_ASYNC:...] status lines from text so they are never sent to contacts."""
    if not text or not text.strip():
        return text
    cleaned = _WORKFLOW_ASYNC_LINE.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


# Paragraph openers that mark model deliberation (chain-of-thought emitted as plain
# content, without <think> tags). Language check would be wrong (English-speaking
# users get legit English replies), so the guard is STRUCTURAL - see
# _strip_untagged_cot_prefix.
_COT_OPENERS = (
    "let me ", "let's ", "actually,", "actually ", "wait,", "wait.", "hmm",
    "okay,", "okay so", "alright,", "alright so",
)
_COT_RESULT_SAYS = re.compile(r"^the .{0,40}\bresult says\b", re.IGNORECASE)


def _is_deliberative_paragraph(p: str) -> bool:
    s = p.strip().lower()
    return s.startswith(_COT_OPENERS) or bool(_COT_RESULT_SAYS.match(s))


def _strip_untagged_cot_prefix(text: str) -> str:
    """Conservative structural guard against untagged chain-of-thought reaching a
    messenger (live 2026-07-13: 1034 chars of plain English deliberation went to
    Telegram). Strips the LEADING deliberation block only when ALL of these hold:
    the FIRST paragraph is deliberative, at least TWO paragraphs in the lead block
    are deliberative, and at least one paragraph survives after it (the actual
    user-facing tail). Anything less stays untouched - a legit reply that merely
    contains a rhetorical 'Actually,' mid-text is never trimmed."""
    if not text or not text.strip():
        return text
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paras) < 3 or not _is_deliberative_paragraph(paras[0]):
        return text
    last_delib = max((i for i, p in enumerate(paras) if _is_deliberative_paragraph(p)), default=-1)
    if last_delib < 0 or last_delib + 1 >= len(paras):
        return text
    lead = paras[: last_delib + 1]
    if sum(1 for p in lead if _is_deliberative_paragraph(p)) < 2:
        return text
    logging.getLogger(__name__).warning(
        "SANITIZE: stripped %d leading deliberation paragraph(s) from outgoing message", len(lead)
    )
    return "\n\n".join(paras[last_delib + 1:]).strip()


def _prepare_channel_outbound(text: str) -> str:
    """THE outbound sanitizer chain for messenger sends (Telegram/WhatsApp/Discord),
    shared by the normal headless reply path AND the sub-agent result drain. The
    drain previously hand-copied a shorter chain and drifted (skipped the
    workflow-async strip, the internal-phrase net and any CoT guard) - one shared
    helper makes a fifth divergent copy structurally impossible. Pure text->text:
    it never decides WHETHER a message is sent, only what the text looks like.
    Never used for the WebUI/session lane (the browser already streamed the
    original text - invariant 4.2)."""
    out = str(text or "")
    out = re.sub(r"<think>.*?</think>", "", out, flags=re.DOTALL)
    out = _strip_tool_calls_json(out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    out = _strip_workflow_async_from_message(out)
    out = sanitize_outgoing_message(out)
    out = _strip_untagged_cot_prefix(out)
    return (out or "").strip()


def _user_asked_for_text(user_input: str) -> bool:
    """True if the user prompt looks like a request to write/compose text (for opening in Document Editor)."""
    if not (user_input and user_input.strip()):
        return False
    lower = user_input.strip().lower()
    triggers = (
        "schreib mir", "verfasse", "schreibe einen text", "verfasse einen text",
        "text schreiben", "schreib einen text", "schreibe mir", "write me a text",
        "write me ", "draft", "entwurf", "formuliere", "formulier mir",
        "schreib einen", "schreibe einen", "text über", "text zu ",
    )
    return any(t in lower for t in triggers)


def _format_sidebar_doc(d: dict) -> str:
    """Format one sidebar document for the agent: --- FILE: name (Speicherort: path) ---\\ncontent\\n----------------"""
    name = d.get("name", "")
    content = d.get("content", "")
    path = d.get("path")
    header = f"--- FILE: {name}"
    if path:
        header += f" (Speicherort: {path})"
    header += " ---"
    return f"{header}\n{content}\n----------------"


def _maybe_open_draft_in_editor(
    session_id: str,
    user_input: str,
    response_text: str,
    source: str,
    *,
    editor_has_content: bool = False,
    user_scope_id: str | None = None,
) -> None:
    """
    If the user asked for a text (e.g. "Schreib mir einen Text") and the response is substantial,
    save it to a draft file and open the Document Editor so the user can edit or save.
    Only runs for Web UI (source == 'web').

    The draft goes into the chat's own workspace (``drafts/entwurf.md``), because the editor
    reads and saves it through the file routes, and those answer an account without admin
    rights only inside its own tree. It lived in the data directory, which worked only while
    the routes served that directory to every account. It is an ordinary folder there
    (`session.DRAFTS_DIR`): the workspace window lists it, and deleting the chat keeps it
    like any other file.
    ``user_scope_id`` is the turn's own, so the workspace is found without reading the session
    back off disk.
    """
    if not session_id or (str(source or "").strip().lower() != "web"):
        return
    if editor_has_content:
        return
    if not _user_asked_for_text(user_input or ""):
        return
    # Strip <think>...</think> for draft content
    clean = re.sub(r"<think>.*?</think>", "", response_text, flags=re.DOTALL)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    if len(clean) < 200:
        return
    try:
        from vaf.core.session import DRAFTS_DIR, get_session_workspace_dir
        workspace = get_session_workspace_dir(session_id, create=True, user_scope_id=user_scope_id)
        if workspace is None:
            return
        draft_dir = workspace / DRAFTS_DIR
        draft_dir.mkdir(parents=True, exist_ok=True)
        draft_path = draft_dir / "entwurf.md"
        draft_path.write_text(clean, encoding="utf-8")
        from vaf.core.web_interface import notify_document_created
        notify_document_created(session_id, str(draft_path.resolve()), title="Entwurf")
    except Exception:
        pass


# One stall report per stuck task, remembered here so a three-hour hang writes
# one stack dump rather than one every metrics tick.
_REPORTED_STALLS: set = set()
STALL_THRESHOLD_S = 300.0


def report_stalls(tq, *, threshold_s: float = STALL_THRESHOLD_S,
                  reported: set = _REPORTED_STALLS) -> int:
    """Say WHERE a stuck worker is stuck, while it still can be said.

    The live incident this exists for: a worker took a LOAD_SESSION command and
    never finished it. For three hours the metrics printed inflight_total=1,
    every queue count read zero, and when the process was finally stopped the
    stack of the stuck thread died with it - the root cause is unknowable to
    this day. A task in flight past the threshold now writes one loud log line
    and dumps EVERY thread's Python stack to stall_<date>.log, so the next such
    hang carries the line of code it is sleeping on.

    Returns how many NEW stalls were reported (for tests; the caller ignores it).
    """
    try:
        stalls = tq.stalled_tasks(threshold_s)
    except Exception:
        return 0
    new = 0
    for stall in stalls:
        key = (stall.get("worker"), stall.get("session_id"), stall.get("preview"))
        if key in reported:
            continue
        reported.add(key)
        new += 1
        append_domain_log_always(
            "headless",
            f"[STALL] worker={stall.get('worker')} has held a task for "
            f"{stall.get('age_s')}s (session={stall.get('session_id') or '-'}, "
            f"preview={stall.get('preview')!r}); dumping all thread stacks")
        try:
            import faulthandler
            with open(get_dated_log_path("stall", "log"), "a", encoding="utf-8") as fh:
                fh.write(f"\n=== stall report: {stall} ===\n")
                faulthandler.dump_traceback(file=fh, all_threads=True)
        except Exception:
            pass
    return new


def deliver_stray_room_answer(agent, room_wake: dict, answer, hist_before: int) -> bool:
    """Post a room turn's plain-text answer into its room when room_send never ran.

    Detection is by the turn's own history: a room_send that ran left a tool
    entry behind. Placeholder returns ("...", empty) are not answers and are
    not delivered, and neither is the text of a turn that failed or was stopped
    (the agent's `last_turn.failed`, a loop-protection note): it is the engine's, a
    provider's raw answer or the owner's spend, and the room's other members
    read what is posted. Returns True when a fallback delivery happened.
    """
    try:
        text = str(answer or "").strip()
        if not text or text == "...":
            return False
        from vaf.core.completion import is_stop_note
        if agent.last_turn.failed or is_stop_note(text):
            append_domain_log_always(
                "headless", f"[ROOM] failed turn not posted room={room_wake.get('room_id')}")
            return False
        history = getattr(agent, "history", []) or []
        for entry in history[hist_before:]:
            if (entry.get("role") == "tool"
                    and str(entry.get("name") or "") == "room_send"):
                return False
        from vaf.core.a2a.room import Room, participant_key
        room = Room.open(str(room_wake.get("room_id") or ""))
        identity = room.identity_for(
            participant_key("agent", room_wake.get("scope")))
        if identity is None:
            return False
        room.say(identity, text[:4000])
        append_domain_log_always(
            "headless",
            f"[ROOM] answer delivered by fallback (model wrote plain text, "
            f"{len(text)} chars) room={room_wake.get('room_id')}")
        return True
    except Exception as e:
        append_domain_log_always("headless", f"[ROOM] stray-answer fallback failed: {e}")
        return False


def new_session_for_task(task) -> Session:
    """The session for a task whose chat has no file yet (a first message from Telegram,
    Discord or WhatsApp, a refused web turn), so its first exchange is stored. It carries
    the caller's username and scope like every saved chat: a session without them belongs
    to nobody, and the person who wrote could not open it."""
    sid = task.session_id
    meta = (task.metadata or {}) if getattr(task, "metadata", None) else {}
    if sid.startswith("telegram_"):
        label = f"Telegram {sid.replace('telegram_', '', 1)}"
    elif sid.startswith("discord_"):
        label = f"Discord {sid.replace('discord_', '', 1)}"
    elif sid.startswith("whatsapp_"):
        label = f"WhatsApp {sid.replace('whatsapp_', '', 1)}"
    else:
        label = sid
    session = Session(id=sid, name=label)
    for key in ("whatsapp_chat_jid", "username", "user_scope_id"):
        if meta.get(key) is not None:
            session.metadata[key] = meta[key]
    return session


def turn_refusal_reason(agent, task) -> str:
    """Why this turn cannot run at all, or "" - decided before chat_step, which would only
    log the reason and leave the chat with "[Error] No response was produced". Two cases:
    an installation without local models whose chat provider is still the local one, and
    an API provider whose backend could not be built (no key, a failed setup); the wording
    is the agent's own (``Agent._no_backend_reason``). A local provider on an installation
    with local models is no case: its model is loaded below, on first use. A messenger turn
    is a case too; ``refuse_turn_without_backend`` decides that it gets no reply."""
    from vaf.core import inference_profile
    if agent.api_backend:
        return ""
    if agent.provider == "local" and inference_profile.local_inference_allowed():
        return ""
    return agent._no_backend_reason()


def refuse_turn_without_backend(task, why: str, session_mgr) -> None:
    """Close a turn that has no backend on this installation (inference_profile = api and
    no provider yet): the reason goes to the chat and the turn ends. The exchange is
    stored like any turn's, so a reload shows the message and why it got no answer
    instead of losing both. Never raises.

    A messenger turn (WhatsApp, Telegram, Discord, Slack) ends without a reply and is only
    logged: the reason is the admin's setup, not something to tell whoever wrote in, and
    running the turn anyway sent them "[Error] No response was produced ... try closing the
    Document Editor" (measured through _prepare_channel_outbound, which lets it pass)."""
    from vaf.core.channels import CHANNEL_LABELS
    if getattr(task, "source", None) in CHANNEL_LABELS:
        append_domain_log_always("headless", f"{task.source} turn closed without a reply, no "
                                             f"backend: session={task.session_id} ({why[:160]})")
        return
    try:
        get_web_interface().emit_agent_message("assistant", why, session_id=task.session_id)
        get_web_interface().emit_message_complete(content=why, session_id=task.session_id)
    except Exception:
        pass
    try:
        try:
            session = session_mgr.load(task.session_id)
        except FileNotFoundError:
            session = new_session_for_task(task)
        text = str(getattr(task, "input_text", "") or "").strip()
        if text:
            session.add_message(role="user", content=text)
        session.add_message(role="assistant", content=why)
        session_mgr.save(session, sync_state=False)
    except Exception as e:
        append_domain_log_always("headless", f"refused turn not stored for {task.session_id}: "
                                             f"{type(e).__name__}: {e}")


# ── The turn binding: what each lane's turn is bound to (vaf/core/turn_binding.py) ─────────
# One producer per lane. The lane hands its result to bind_turn before the turn and calls
# end_turn after it; no lane writes a binding attribute on the agent itself.

_EDITOR_WRITE_TOOLS = ("replace_editor_selection", "replace_editor_text")


class _ProjectPaths(NamedTuple):
    """A chat's project folders: its workspace and the folder its edits go to. The second is
    also where its VAF.md is looked for (`Agent._project_context_block`); empty is none."""
    workspace: str
    edit_path: str


def _chat_project_paths(session_mgr, session_id) -> _ProjectPaths:
    """This chat's workspace and active project, as the turn's note and its VAF.md use them.

    [SESSION WORKSPACE] is the stable workspace root set on first file creation
    (session.project_path); [ACTIVE PROJECT] is the most recently created or edited project
    (runtime_state["last_project_path"]). A chat whose recorded folder is unsafe (a home
    directory, recorded before the project-dir guard existed) gets none of it, or the agent
    keeps sending the coder back there. A chat whose files were written in-process never got
    project_path anchored (the setter lived only in the subprocess HTTP path), so the
    workspace is derived from THIS chat's id when its folder exists, never from the
    process-global pointer. Sessions from before session.project_path keep their last
    project as the edit folder with no workspace.

    A read, nothing more: it restores no state into the agent and repoints nothing, because
    it runs before the turn is bound. Never raises."""
    try:
        session = session_mgr.load(session_id, restore_state=False, repoint=False)
        workspace = getattr(session, "project_path", "") or ""
        last_proj = (getattr(session, "runtime_state", None) or {}).get("last_project_path") or ""
        from vaf.core.workspace_guard import is_unsafe_project_dir
        if last_proj and is_unsafe_project_dir(last_proj):
            last_proj = ""
        if workspace and is_unsafe_project_dir(workspace):
            workspace = ""
        if not workspace:
            try:
                from vaf.core.session import get_session_workspace_dir
                # The chat's own scope, from the file just read: the helper would read the
                # file again through the module-global manager and repoint it.
                owner_scope = (getattr(session, "metadata", None) or {}).get("user_scope_id")
                derived = get_session_workspace_dir(session_id, create=False,
                                                    user_scope_id=owner_scope)
                if derived and os.path.isdir(str(derived)):
                    workspace = str(derived)
            except Exception:
                pass
        if workspace and os.path.isdir(workspace):
            edit_path = (last_proj
                         if (last_proj and last_proj != workspace and os.path.isdir(last_proj))
                         else workspace)
            return _ProjectPaths(workspace, edit_path)
        if last_proj and os.path.isdir(last_proj):
            return _ProjectPaths("", last_proj)
    except Exception:
        pass
    return _ProjectPaths("", "")


def _editor_tool_exclusions(session_mgr, session_id) -> frozenset:
    """The editor write tools this turn may not see: both while no editor document is open,
    the selection tool while nothing is selected. A turn with no chat (an agent room's) has
    no editor document. A read like `_chat_project_paths`; an unreadable chat hides both.
    Never raises."""
    if session_mgr is None or not session_id:
        return frozenset(_EDITOR_WRITE_TOOLS)
    try:
        session = session_mgr.load(session_id, restore_state=False, repoint=False)
        runtime_state = getattr(session, "runtime_state", None) or {}
        editor_document = runtime_state.get("editor_document") or {}
        has_content = bool((editor_document.get("content") or "").strip())
        selections = runtime_state.get("editor_selections") or []
    except Exception:
        return frozenset(_EDITOR_WRITE_TOOLS)
    if not has_content:
        return frozenset(_EDITOR_WRITE_TOOLS)
    if not selections:
        return frozenset({"replace_editor_selection"})
    return frozenset()


def _front_office_book_owner(task, meta):
    """Whose address book and calendar a contact's turn reads: the task's account; on Discord
    the local admin's real one, because the Discord lane is the local admin's alone and its
    tasks carry the literal "admin" identity. Raises what `local_admin_identity` raises."""
    username = meta.get("username") or "admin"
    scope = meta.get("user_scope_id")
    if getattr(task, "source", None) == "discord":
        from vaf.core.contacts_store import local_admin_identity
        username, scope = local_admin_identity()
    return username, scope


def _front_office_contact_record(task, meta):
    """The contact record of a contact's turn, or None.

    Whoever was ADMITTED, whatever the book says about them: this turn exists because the
    bridge let the message through, so the question here is who is speaking, not whether
    they may. Filtering by the permission left an open channel's sender without their own
    record, and the model was told "no contact record" about a person who is in the book.
    Pinned on the agent for the turn: contact_history reads that person's correspondence
    across every channel and nothing else, which is why it needs no identity argument
    (FRONT_OFFICE.md). Never raises."""
    contact = None
    try:
        from vaf.core.contacts_store import find_contact_by_channel
        task_source = getattr(task, "source", None)
        if task_source == "telegram":
            tid = meta.get("telegram_user_id")
            if tid:
                username, scope = _front_office_book_owner(task, meta)
                contact = find_contact_by_channel("telegram", str(tid), username, user_scope_id=scope)
        elif task_source == "whatsapp":
            jid = meta.get("whatsapp_chat_jid")
            if jid:
                username, scope = _front_office_book_owner(task, meta)
                contact = find_contact_by_channel("whatsapp", str(jid), username, user_scope_id=scope)
        elif task_source == "email":
            addr = str(meta.get("email_from") or "").strip().lower()
            if addr:
                username, scope = _front_office_book_owner(task, meta)
                contact = find_contact_by_channel("email", addr, username, user_scope_id=scope)
        elif task_source == "discord":
            aid = str(meta.get("discord_author_id") or "").strip()
            if aid:
                username, scope = _front_office_book_owner(task, meta)
                contact = find_contact_by_channel("discord", aid, username, user_scope_id=scope)
    except Exception:
        contact = None
    return dict(contact) if isinstance(contact, dict) else None


def _task_turn_binding(task, session_mgr) -> TurnBinding:
    """What a queued task's turn is bound to, decided from the task and its chat.

    The identity is the task's, taken raw (`identity_from_metadata`), and the chat is the
    task's own, reloaded from the store when its transcript changed under a live agent (a
    rewind: the in-memory history is otherwise authoritative, so without it the worker
    answers from the very reply the store no longer has). A contact's turn (Front Office)
    gets the Front Office tools and nothing else, offered and run: the engine's allowed set
    holds against the router, the riders, the owner's recent tools and any name the model
    calls, and a set that cannot be applied fails the task rather than opening every tool to
    a stranger. Raises nothing of its own beyond what building the binding raises."""
    from vaf.core.front_office_tools import FRONT_OFFICE_ALLOWED_TOOLS
    from vaf.core.task_queue import wake_kind as _wake_kind
    task_meta_for_env = (task.metadata or {}) if getattr(task, "metadata", None) else {}
    # Did a PERSON send this turn? Only the queue boundary can tell: a timer and an
    # automation arrive with the user's own text and no marker. The agent uses it for the
    # ask-first latch and for the room streak, and the lane reads this one decision.
    _wake = _wake_kind(task_meta_for_env)
    _turn_is_human = bool(
        getattr(task, "task_class", "") == "interactive"
        and not _wake
        and str(task.input_text or "").strip()
    )
    from_contact = bool(task_meta_for_env.get("from_contact"))
    return TurnBinding(
        identity=identity_from_metadata(task_meta_for_env),
        source=str(getattr(task, "source", None) or "web"),
        session_id=task.session_id,
        # The account the web enqueued the turn for, as the queue's fairness reads it.
        account=(task_meta_for_env.get("enqueue_user_scope_id")
                 or task_meta_for_env.get("user_scope_id")),
        force_reload=bool(task_meta_for_env.get("force_reload")),
        turn_id=getattr(task, "turn_id", None),
        allowed_tools=FRONT_OFFICE_ALLOWED_TOOLS if from_contact else None,
        front_office=from_contact,
        front_office_chat=(_front_office_chat_ref(task_meta_for_env, task_meta_for_env.get("username"))
                           if from_contact else None),
        front_office_contact=(_front_office_contact_record(task, task_meta_for_env)
                              if from_contact else None),
        # Nobody is at the screen for this one. A timer fires on a clock and a compaction is
        # housekeeping, and both arrive with the source of the chat they belong to - "web"
        # for a timer the person set in the browser. The outward hold reads this: a message
        # the person SCHEDULED must leave at the scheduled time, not wait for a click they
        # are not there to give (vaf/core/outbound_hold.py). Only a timer: a finished
        # background command orders no send.
        unattended=bool(_wake == "timer" or task_meta_for_env.get("compaction")),
        is_human=_turn_is_human,
        # Which wake this turn is, from the queue and never from the text, with the text as
        # VAF wrote it: the input the agent reads carries this turn's workspace note in
        # front of it. The result-grounding judge counts a report kind's text as evidence
        # (Agent._turn_reports).
        wake=(_wake, str(task.input_text or "")) if _wake else None,
        excluded_tools=_editor_tool_exclusions(session_mgr, task.session_id),
        project_dir=_chat_project_paths(session_mgr, task.session_id).edit_path,
    )


def _room_turn_binding(room_wake: dict, identity: Identity) -> TurnBinding:
    """What an agent room's turn is bound to: the room's account and a conversation of its own.

    The conversation is keyed per room AND per account (``<room_id>.<peer_id>``, the agent's
    own handle in that room), so its working memory is the room's and this account's: a key
    without the account would share one working memory between two accounts' agents in a
    shared room, which is why an empty handle raises. Its store lives apart from the chats'
    (`MainPersistenceManager(conversation=)`): the key is visible to the room's members and a
    web chat names its own id, so a store under the chats' folder could be opened as a chat.
    The turn has no chat: none of the last chat's messages, summary, working memory or
    project file, and its tools see no chat. Source "web": the runner's baseline, not a
    channel, so outward sends are held as on the web.

    EVERY key a downstream reader consumes must be copied into ``room_turn``: the gate and
    the hint lane read ``agent._room_turn``, not the wake, and a key computed in the wake but
    not copied fails CLOSED - measured with from_authority, which the gate read for a day
    while no runner ever handed it over."""
    room_id = str((room_wake or {}).get("room_id") or "").strip()
    peer_id = str((room_wake or {}).get("peer_id") or "").strip()
    if not room_id or not peer_id:
        raise ValueError("a room turn needs its room and the agent's own handle in it")
    return TurnBinding(
        identity=identity,
        source="web",
        conversation=f"{room_id}.{peer_id}",
        room_id=room_id,
        room_turn={"room_id": room_wake["room_id"],
                   "mode": room_wake["mode"],
                   "query": room_wake.get("query", ""),
                   "from_user": room_wake.get("from_user", False),
                   "from_user_only": room_wake.get("from_user_only", False),
                   "from_authority": room_wake.get("from_authority", False)},
        synthetic=True,
        excluded_tools=_editor_tool_exclusions(None, None),
    )


def _session_owner_identity(meta) -> Identity:
    """Who owns a stored chat, for a lane that has only the chat: its scope and name from the
    session file, and admin rights from the ACCOUNT (`config.is_admin_account`), because
    session files carry no role. A chat with no scope (a legacy chat, a Discord chat) gets
    no role, which is what its own queued turns get."""
    from vaf.core.config import is_admin_account
    meta = meta or {}
    scope = meta.get("user_scope_id")
    return Identity(scope=scope, username=meta.get("username"),
                    role="admin" if is_admin_account(scope) else None)


def _drain_turn_binding(session_mgr, session_id) -> TurnBinding:
    """What the turn that summarizes a chat's finished sub-agent work is bound to: that
    chat's owner, its source and its project folder, marked synthetic (VAF wrote the prompt,
    not a person).

    A chat whose file is gone (deleted while its sub-agent ran) or cannot be read has no
    owner to run as, and raises `TurnRefused`: bound as nobody, the turn would run with no
    scope, which is the machine owner's freedoms (an open file jail, no account allowlist),
    over a result the chat's account produced, and its reply would recreate the chat with
    no owner. A readable chat that names no scope (a legacy chat) is bound as it is, as its
    own queued turns are. An empty id raises ValueError."""
    try:
        session = session_mgr.load(session_id, restore_state=False, repoint=False)
        meta = dict(getattr(session, "metadata", None) or {})
    except Exception as e:
        raise TurnRefused(f"the chat of this result is gone or cannot be read "
                          f"({type(e).__name__})") from e
    return TurnBinding(
        identity=_session_owner_identity(meta),
        source=str(meta.get("source") or "web"),
        session_id=session_id,
        synthetic=True,
        excluded_tools=_editor_tool_exclusions(session_mgr, session_id),
        project_dir=_chat_project_paths(session_mgr, session_id).edit_path,
    )



def _result_chat(record):
    """The chat a sub-agent record belongs to: the session it names, else None. Never the
    chat the worker happens to hold: one agent serves every chat and every account, so "the
    current chat" is whoever wrote last. A record names no session when the lane that
    started the work had none: an agent room's turn (the record carries the room instead),
    a scheduled automation. Measured with that fallback: a room's coder result went into
    another account's chat, and with a contact's turn last, to the contact."""
    sid = str(getattr(record, "session_id", "") or "").strip()
    return sid or None


def _split_results_by_chat(results):
    """(results with a chat, results without one). The drain delivers the first kind into
    their own chats and leaves the second pending: no lane may read them yet, and consuming
    them would drop them. The queue is cleared at the next start."""
    with_chat, without_chat = [], []
    for record in results or []:
        (with_chat if _result_chat(record) else without_chat).append(record)
    return with_chat, without_chat

# The threads this process runs the agent loop in, by worker id: the health check
# (vaf/core/health.py) asks whether they still live. A loop that died answers no chat
# again, while the web server around it keeps answering every request but the chat.
_WORKER_THREADS: dict = {}
_WORKER_THREADS_LOCK = threading.Lock()


def worker_status():
    """{"started": n, "alive": m} over the agent loops this process started, or None when it
    started none (a terminal, a test): then nobody here is waiting on one."""
    with _WORKER_THREADS_LOCK:
        threads = list(_WORKER_THREADS.values())
    if not threads:
        return None
    return {"started": len(threads), "alive": sum(1 for t in threads if t.is_alive())}


def run_headless_agent(worker_id: int = 1, total_workers: int = 1):
    """
    Run a headless agent loop that processes tasks from the TaskQueue.
    This is designed to run in a background thread within the Tray App.
    """
    with _WORKER_THREADS_LOCK:
        # A loop restarted under the same id (web_server._ensure_headless_agent_runner)
        # replaces the dead one.
        _WORKER_THREADS[worker_id] = threading.current_thread()
    def _lifecycle(msg: str) -> None:
        """Log lifecycle events unconditionally (ignores debug_logs_enabled toggle)."""
        try:
            append_domain_log_always("headless", f"[LIFECYCLE] {msg}")
        except Exception:
            pass

    # IMMEDIATE: Create log dir and write startup marker (consolidated in headless_YYYY-MM-DD.log)
    # Uses _always variant so these appear even when debug_logs_enabled=False.
    log_dir = get_app_log_dir()
    try:
        append_domain_log_always("headless", f"[STARTUP] Headless Runner STARTING (worker={worker_id}/{total_workers})")
        append_domain_log_always("headless", f"[STARTUP] PID: {os.getpid()} worker={worker_id}")
        append_domain_log_always("headless", f"[STARTUP] Log dir: {log_dir}")
    except Exception as e:
        print(f"[Headless] Failed to write startup log: {e}")

    # Clean up ALL IPC queues from previous runs.
    # Active/pending tasks from a dead process can never complete,
    # and completed results from old sessions confuse the new agent.
    _lifecycle("IPC cleanup starting")
    if worker_id == 1:
        try:
            from vaf.core.subagent_ipc import get_ipc
            ipc = get_ipc()
            stale_active = ipc._read_json(ipc.active_file)
            stale_pending = ipc._read_json(ipc.pending_file)
            stale_results = ipc._read_json(ipc.results_file)
            total = len(stale_active) + len(stale_pending) + len(stale_results)
            if total:
                ipc.clear_all()
                append_domain_log_always("headless", f"[STARTUP] Cleared {total} stale IPC entries "
                                  f"(active={len(stale_active)}, pending={len(stale_pending)}, "
                                  f"results={len(stale_results)})")
                print(f"[Headless] Cleared {total} stale IPC entries from previous run")
        except Exception as e:
            _lifecycle(f"IPC cleanup failed (non-critical): {e}")
            print(f"[Headless] IPC cleanup failed (non-critical): {e}")

    # Kill leftover sub-agent processes from previous runs.
    _lifecycle("Stale process cleanup starting")
    if worker_id == 1:
        try:
            from vaf.core.platform import Platform
            killed = Platform.stop_webui_subagent_processes(session_id=None)
            if killed:
                append_domain_log_always("headless", f"[STARTUP] Killed {killed} stale sub-agent processes")
                print(f"[Headless] Killed {killed} stale sub-agent processes")
        except Exception as e:
            _lifecycle(f"Stale process cleanup failed (non-critical): {e}")
            print(f"[Headless] Stale process cleanup failed (non-critical): {e}")

    # Start Memory Profiler IMMEDIATELY
    if worker_id == 1:
        try:
            from vaf.core.memory_profiler import start_profiler
            start_profiler()
            print(f"[Headless] Memory Profiler started - logging to {get_dated_log_path('memory', 'log')}")
        except Exception as e:
            print(f"[Headless] Memory Profiler failed to start: {e}")
            append_domain_log_always("headless", f"[STARTUP] Memory Profiler FAILED: {e}")

    # Ensure UTF-8 output to avoid Windows charmap crashes
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("[Headless] Starting Agent Loop...")
    _lifecycle("Pre-Agent-init: starting Agent() constructor")
    
    # Initialize Agent (retry on failure)
    # We use verbose=False to keep logs clean
    agent = None
    _agent_attempt = 0
    while agent is None:
        _agent_attempt += 1
        _lifecycle(f"Agent init attempt {_agent_attempt}")
        try:
            agent = Agent(verbose=False, register_signals=False, run_kind="chat")
            # A runner lane from its first prompt on: this process serves every chat, so its
            # working directory is nobody's project and its VAF.md nobody's to read
            # (Agent._project_context_block). Every task sets its own source below.
            agent._current_chat_source = "web"
            _lifecycle("Agent() constructor OK, calling init_chat()")
            agent.init_chat()
            _lifecycle("init_chat() OK")
            # Result-ownership: this runner's idle drain delivers sub-agent results with
            # full side effects (SubAgentWindow, subagent_output, messenger summaries).
            # Marking the agent makes USER chat turns leave pending results to the drain
            # instead of consuming them mid-reply (chat-while-subagent-runs).
            agent._runner_owns_subagent_delivery = True

            # Register with Web Interface (single owner to avoid pointer races)
            if worker_id == 1:
                get_web_interface().register_agent(agent)

            print(f"[Headless] Agent initialized and ready (worker={worker_id}).")
            _lifecycle(f"Agent initialized and ready (worker={worker_id})")
            try:
                if worker_id == 1:
                    get_web_interface().log("Headless agent initialized and ready.", level="info", source="System")
                # Send initial token stats so WebUI shows context usage immediately
                used, total = agent.get_token_usage()
                api_backend = getattr(agent, 'api_backend', None)
                input_tokens = 0
                output_tokens = 0
                if api_backend:
                    usage = api_backend.last_request_usage
                    input_tokens = int(usage.get("input_tokens", 0))
                    output_tokens = int(usage.get("output_tokens", 0))
                    # NOTE: Do NOT overwrite `used` with session_usage here.
                    # input/output_tokens come from last_request_usage (THIS turn only) -> no cross-user mixing.
                    # get_token_usage() already returns the correct context snapshot.
                stats = {
                    "used": used,
                    "total": total,
                    "percent": round((used / total) * 100, 1) if total else 0.0,
                    "api": bool(api_backend),
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens
                }
                if worker_id == 1:
                    get_web_interface().emit_stats(stats)
            except Exception:
                pass
        except Exception as e:
            _lifecycle(f"Agent init FAILED attempt {_agent_attempt}: {e}")
            print(f"[Headless] Agent initialization failed: {e}")
            traceback.print_exc()
            try:
                get_web_interface().log(f"Headless agent init failed: {e}", level="error", source="System")
            except Exception:
                pass
            time.sleep(2)

    # Task Queue — wrapped in try/except so daemon thread crashes are captured
    try:
        _lifecycle("Creating TaskQueue singleton")
        tq = TaskQueue()
        _lifecycle("TaskQueue OK, resetting runtime state")
        # Self-heal: after worker restarts, clear stale in-flight runtime locks so
        # queued tasks cannot remain blocked behind orphaned session locks.
        tq.reset_runtime_state(include_queued=False)
        _lifecycle("TaskQueue runtime state reset OK")
        # CRITICAL: Connect SessionManager to agent's registry for full state persistence
        _lifecycle("Creating SessionManager")
        session_mgr = SessionManager(state_registry=agent.state_registry)
        _lifecycle("SessionManager OK")
    except Exception as _setup_err:
        _lifecycle(f"FATAL setup crash after Agent init: {_setup_err}")
        append_domain_log_always("headless", f"[FATAL] {traceback.format_exc()}")
        raise

    # No task before the one-time repair of the chats is over. It rewrites chat files, and a
    # turn saved between its read and its write would be lost; this thread starts beside the
    # web server, whose startup runs the same pass, so whichever comes first does it and the
    # other waits (vaf/core/at_rest_migration.py, repair_context_copies_once).
    try:
        from vaf.core.at_rest_migration import repair_context_copies_once
        repair_context_copies_once()
    except Exception as _repair_err:
        _lifecycle(f"Context state repair skipped: {_repair_err}")

    # Main Loop
    last_subagent_check = 0.0
    # Rooms are polled rather than pushed: a peer may be a foreign process that has
    # no way to signal this runner. The interval is its own so a busy sub-agent lane
    # cannot starve a conversation.
    last_room_check = 0.0
    ROOM_POLL_SECONDS = 2.0
    # WHICH accounts to poll for. One process serves every tenant on this machine, and
    # the agent object's bound identity is whatever the last queued turn left behind -
    # so polling "the current scope" means every other tenant's agent is never woken,
    # and which one wins is decided by whoever happened to chat last.
    #
    # Refreshed slowly on purpose: the list comes from the room manifests on disk, and
    # a tenant that gains its first room is not urgent to the SECOND. The identities
    # are cached beside it because resolving one is an uncached database round trip -
    # its own docstring says one per RUN is affordable, one per call is not.
    room_scopes: list = []
    last_room_scopes = 0.0
    ROOM_SCOPES_TTL = 60.0
    room_identities: dict = {}
    # The room sweep is keyed on the ROOM, not on this loop: a member is asked at
    # most once per check-in interval, whatever else happens here. The TICK is
    # faster than that interval because the sweep also runs vote deadlines, and a
    # reminder due one minute after a question was put is a minute late if it is
    # only looked for every sixty seconds. Fifteen seconds is the resolution of
    # every countdown a surface shows, and it costs one fold per held room.
    last_ping_sweep = 0.0
    PING_SWEEP_SECONDS = 15.0
    last_subagent_ui_update = 0.0
    last_memory_check = 0.0
    last_queue_metrics = 0.0
    waited_for_server_ready = False  # Wait for 8080 to return 200 before first chat (avoids 503)
    open_subagent_sessions = set()
    _unrouted_results = set()   # results that name no chat, logged once each
    subagent_last_activity = {}
    subagent_last_steps = {}
    from vaf.core.config import Config
    _lifecycle("Config imported, ready for main loop")

    # Optional parallel worker pool (safe rollout via config). The effective worker count is the admin's
    # requested parallel_main_workers, CLAMPED per provider: API providers up to max_parallel_api_workers,
    # the single shared local llama-server to max_parallel_local_workers (and its --parallel slots) to avoid
    # VRAM OOM. Default parallel_main_workers=1 -> effective=1 -> no spawn -> identical to legacy behavior.
    if worker_id == 1 and total_workers == 1:
        try:
            cfg = Config.load()
            policy = str(cfg.get("queue_policy", "legacy") or "legacy").strip().lower()
            requested = int(cfg.get("parallel_main_workers", 1) or 1)
            provider = str(cfg.get("provider", "local") or "local").strip().lower()
            if provider == "local":
                slots = int(cfg.get("n_parallel", 0) or 0)  # 0 = auto-detect
                local_cap = int(cfg.get("max_parallel_local_workers", 2) or 2)
                cap = min(local_cap, slots) if slots > 0 else local_cap
            else:
                cap = int(cfg.get("max_parallel_api_workers", 5) or 5)
            effective = max(1, min(requested, cap))
            # Per-session serialization (task_queue) holds in BOTH policies, so >1 worker is safe under
            # 'legacy' too; weighted_fair only adds lane fairness. Imply the parallel spawn when effective>1.
            if effective > 1:
                if policy != "weighted_fair":
                    append_domain_log_always("headless", "[STARTUP] parallel workers on with queue_policy='legacy' (FIFO/priority); set 'weighted_fair' for lane fairness across interactive/automation/background")
                for wid in range(2, effective + 1):
                    threading.Thread(
                        target=run_headless_agent,
                        kwargs={"worker_id": wid, "total_workers": effective},
                        daemon=True,
                    ).start()
                append_domain_log_always("headless", f"[STARTUP] Spawned {effective - 1} additional worker(s) (provider={provider}, requested={requested}, cap={cap})")
        except Exception as e:
            append_domain_log_always("headless", f"[STARTUP] Worker spawn skipped: {e}")

    def _check_and_cleanup_memory():
        """Check memory usage and cleanup if needed."""
        nonlocal last_memory_check
        now = time.time()
        if now - last_memory_check < MEMORY_CHECK_INTERVAL:
            return
        last_memory_check = now

        try:
            import psutil
            process = psutil.Process()
            memory_mb = process.memory_info().rss / (1024 * 1024)

            # Log memory usage to file for debugging (consolidated in memory.log)
            append_domain_log("memory", f"[USAGE] Memory: {memory_mb:.0f}MB")

            if memory_mb > MEMORY_CRITICAL_MB:
                print(f"[Headless] CRITICAL: Memory usage {memory_mb:.0f}MB > {MEMORY_CRITICAL_MB}MB - aggressive cleanup!")
                _aggressive_memory_cleanup()
            elif memory_mb > MEMORY_THRESHOLD_MB:
                print(f"[Headless] WARNING: Memory usage {memory_mb:.0f}MB > {MEMORY_THRESHOLD_MB}MB - running cleanup...")
                _standard_memory_cleanup()
            else:
                # Still run gc periodically
                gc.collect()
        except ImportError:
            # psutil not available, just run gc
            gc.collect()
        except Exception as e:
            print(f"[Headless] Memory check error: {e}")

    def _standard_memory_cleanup():
        """Standard memory cleanup - clear caches, run gc (but KEEP models loaded)."""
        try:
            # Clear embedding cache only (NOT the model - reloading is slow and fragments memory)
            from vaf.memory.embeddings import get_embedding_service
            svc = get_embedding_service()
            if svc:
                svc.clear_cache()
        except Exception as e:
            print(f"[Headless] Embedding cleanup error: {e}")

        # Force garbage collection
        gc.collect()

        try:
            import psutil
            memory_mb = psutil.Process().memory_info().rss / (1024 * 1024)
            print(f"[Headless] After cleanup: {memory_mb:.0f}MB")
        except:
            pass

    def _aggressive_memory_cleanup():
        """Aggressive memory cleanup - unload models if needed."""
        _standard_memory_cleanup()

        try:
            # Unload embedding model completely
            from vaf.memory.embeddings import reset_embedding_service
            reset_embedding_service()
            print("[Headless] Unloaded embedding model")
        except Exception as e:
            print(f"[Headless] Embedding reset error: {e}")

        # Multiple gc passes for better cleanup
        for _ in range(3):
            gc.collect()

        # REMOVED: torch import causes 1GB+ RAM explosion!
        # CUDA cache clearing is not needed when CUDA_VISIBLE_DEVICES="" is set

        try:
            import psutil
            memory_mb = psutil.Process().memory_info().rss / (1024 * 1024)
            print(f"[Headless] After aggressive cleanup: {memory_mb:.0f}MB")
        except:
            pass

    def _get_subagent_model_info():
        cfg = Config.load()
        effective_provider = subagent_provider_override() or cfg.get("provider", "local")
        if effective_provider != "local":
            model = cfg.get(f"api_model_{effective_provider}", "") or cfg.get("model", "")
        else:
            model = cfg.get("model", "")
        return effective_provider, model

    _lifecycle(">>> ENTERING MAIN LOOP <<<")
    _loop_iteration_count = 0
    while True:
        try:
            _loop_iteration_count += 1
            if _loop_iteration_count <= 3:
                _lifecycle(f"Main loop iteration {_loop_iteration_count}")
            if worker_id == 1:
                _now = time.time()
                if _now - last_queue_metrics >= 2.0:
                    last_queue_metrics = _now
                    try:
                        qstats = tq.get_queue_stats()
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                                f.write(
                                    f"{_dt.now().isoformat()} [METRICS] "
                                    f"interactive={qstats.get('interactive', 0)} "
                                    f"automation={qstats.get('automation', 0)} "
                                    f"background={qstats.get('background', 0)} "
                                    f"inflight_total={qstats.get('inflight_total', 0)} "
                                    f"inflight_sessions={qstats.get('inflight_sessions', 0)}\n"
                                )
                        get_web_interface().push_update(
                            {
                                "type": "queue_stats",
                                "stats": qstats,
                            }
                        )
                        report_stalls(tq)
                    except Exception:
                        pass
            # check for tasks
            task = tq.get()
            if task:
                try:
                    if is_debug_logging_enabled():
                        from datetime import datetime as _dt
                        with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                            prev = (task.input_text or "")[:60]
                            f.write(f"{_dt.now().isoformat()} QUEUE_GET session_id={task.session_id} preview={repr(prev)} queue_size_after={tq.get_queue_size()}\n")
                except Exception:
                    pass
                print(f"[Headless] Processing task for session {task.session_id}...")
                # Do not log "Processing task..." to Web UI – redundant after every user message
                if getattr(task, "source", None) == "whatsapp":
                    try:
                        from vaf.core.log_helper import log_whatsapp_inbound
                        log_whatsapp_inbound(f"HEADLESS processing session={task.session_id}")
                    except Exception:
                        pass

                # Set current user from task metadata before load_session_context so init_chat/build_prompt get User identity
                meta = (task.metadata or {}) if getattr(task, "metadata", None) else {}
                # Defensive session/source integrity checks.
                try:
                    expected_sid = str(meta.get("enqueue_session_id") or "").strip()
                    if expected_sid and str(task.session_id) != expected_sid:
                        append_domain_log(
                            "headless",
                            f"[ROUTING_WARN] session mismatch: task.session_id={task.session_id} "
                            f"metadata.enqueue_session_id={expected_sid} source={getattr(task, 'source', '')}",
                        )
                    source = str(getattr(task, "source", "") or "").strip().lower()
                    # A compaction is enqueued by the runner itself (source "web") for
                    # whichever session just ran, messenger sessions included: it is not a
                    # cross-channel message and must reach the compaction branch, or a chat
                    # namespace is never written.
                    if (source == "web" and meta.get("compaction") is not True
                            and str(task.session_id).startswith(CHAT_SESSION_PREFIXES)):
                        append_domain_log(
                            "headless",
                            f"[ROUTING_BLOCK] Dropping cross-channel task from web to {task.session_id}",
                        )
                        try:
                            tq.task_done(task=task)
                        except Exception:
                            tq.task_done()
                        continue
                except Exception:
                    pass
                # Bind the turn before anything is built for it: account, identity, source,
                # the Front Office set and the other markers, the chat and its project folder
                # (vaf/core/turn_binding.py). A turn of an account without access does not
                # start: one queued before the access was taken away would run with the role
                # it was queued with, and a deactivated account's paired messenger still
                # queues turns (vaf.core.revocation.account_stands).
                _binding = None
                _bound = False
                try:
                    _binding = _task_turn_binding(task, session_mgr)
                    bind_turn(agent, _binding)
                    _bound = True
                except TurnRefused:
                    append_domain_log("headless", f"[REVOKED] Dropping a queued task of an account "
                                                  f"without access for {task.session_id}")
                    try:
                        tq.task_done(task=task)
                    except Exception:
                        tq.task_done()
                    continue
                except Exception as e:
                    print(f"[Headless] Failed to load session context: {e}")
                    if not str(task.input_text).startswith("__CMD__"):
                        # A chat turn on a chat that did not load is answered as failed and
                        # does not run (answer_failed_load). A command still runs: it is no
                        # turn on that chat, and LOAD_SESSION loads the chat it names itself.
                        answer_failed_load(task, e, contact=(
                            _binding.front_office_contact if _binding is not None else None))
                        # A stop requested for this turn ends with it. task_done keeps stop
                        # requests, and only the turn's own check further down consumes one;
                        # left standing, it would stop the session's next turn instead.
                        tq.clear_stop(task.session_id)
                        try:
                            tq.task_done(task=task)
                        except Exception:
                            tq.task_done()
                        continue
                # Debug: Log user scope for RAG troubleshooting (consolidated in rag.log)
                append_domain_log("rag", f"[Headless] Task user_scope_id={meta.get('user_scope_id')}, username={meta.get('username')}")

                if _bound:
                    # Persist user to session metadata so load_session_context gets it on next load
                    # Also persist telegram_chat_id for sessions from Telegram (needed when subagent completes later)
                    if (
                        meta.get("user_scope_id") is not None
                        or meta.get("username") is not None
                        or meta.get("telegram_chat_id") is not None
                        or meta.get("discord_channel_id") is not None
                        or meta.get("whatsapp_chat_jid") is not None
                    ):
                        try:
                            session = session_mgr.load(task.session_id)
                            # Conditional: only stamp the owner scope when the session has NONE yet (a new
                            # session). Never relabel an already-owned session to the task's scope — that
                            # would let a queued chat take over another user's session. Defense-in-depth
                            # behind the WS ownership gate (_ws_session_owner_ok).
                            if meta.get("user_scope_id") is not None and not (session.metadata or {}).get("user_scope_id"):
                                session.metadata["user_scope_id"] = meta.get("user_scope_id")
                            if meta.get("username") is not None:
                                session.metadata["username"] = meta.get("username")
                            if meta.get("telegram_chat_id") is not None:
                                session.metadata["telegram_chat_id"] = meta["telegram_chat_id"]
                            if meta.get("discord_channel_id") is not None:
                                session.metadata["discord_channel_id"] = meta["discord_channel_id"]
                            if meta.get("whatsapp_chat_jid") is not None:
                                session.metadata["whatsapp_chat_jid"] = meta["whatsapp_chat_jid"]
                            if meta.get("voice_lang"):
                                session.metadata["voice_lang"] = meta["voice_lang"]
                            if getattr(task, "source", None) == "telegram":
                                session.metadata["source"] = "telegram"
                            if getattr(task, "source", None) == "discord":
                                session.metadata["source"] = "discord"
                            if getattr(task, "source", None) == "whatsapp":
                                session.metadata["source"] = "whatsapp"
                            session_mgr.save(session)
                        except Exception:
                            pass
                    # Channel sessions are intentionally bounded to a short recent history
                    # window to avoid stale chat outputs being reused instead of fresh tool calls.
                    _apply_channel_history_window(agent, getattr(task, "source", None))

                # Process Input
                input_text = task.input_text
                is_cmd = str(input_text).startswith("__CMD__")

                # Check for System Commands (similar to run.py) - do NOT load model for commands
                if is_cmd:
                    _handle_command(input_text, agent, session_mgr)
                    try:
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                                f.write(f"{_dt.now().isoformat()} QUEUE_DONE session_id={task.session_id} (cmd)\n")
                    except Exception:
                        pass
                    tq.task_done()
                    continue

                # Speaker-confirmation reply (messenger lane): if a voice
                # confirmation is pending for THIS OWNER's scope and the
                # message parses as an answer (yes / no / "no, that's Peter"),
                # consume it deterministically - no LLM turn, ack sent back on
                # the same channel. Owner-only: never for contact-relay
                # messages, and only for the scope the bridge authenticated.
                if (
                    getattr(task, "source", None) in CHAT_CHANNELS
                    and not (task.metadata or {}).get("from_contact")
                    and (task.metadata or {}).get("user_scope_id")
                ):
                    try:
                        from vaf.core import speaker_confirm as _spc
                        _ack = _spc.try_consume_channel_reply(
                            str(meta.get("user_scope_id")), str(input_text))
                    except Exception:
                        _ack = None
                    if _ack:
                        try:
                            if task.source == "telegram" and meta.get("telegram_chat_id"):
                                from vaf.core.telegram_reply import send_telegram_reply
                                send_telegram_reply(str(meta["telegram_chat_id"]), _ack)
                            elif task.source == "whatsapp" and meta.get("whatsapp_chat_jid"):
                                from vaf.core.whatsapp_reply import send_whatsapp_reply
                                send_whatsapp_reply(meta.get("username") or "admin",
                                                    str(meta["whatsapp_chat_jid"]), _ack,
                                                    user_scope_id=meta.get("user_scope_id"))
                            elif task.source == "discord":
                                from vaf.core.messaging_connections import send_to_main_messenger
                                send_to_main_messenger(str(meta.get("user_scope_id")),
                                                       meta.get("username") or "admin",
                                                       _ack, record=False)
                        except Exception as _sc_e:
                            print(f"[Headless] speaker_confirm ack send failed: {_sc_e}")
                        try:
                            if is_debug_logging_enabled():
                                from datetime import datetime as _dt
                                with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                                    f.write(f"{_dt.now().isoformat()} QUEUE_DONE session_id={task.session_id} (speaker_confirm)\n")
                        except Exception:
                            pass
                        tq.task_done()
                        continue

                # Compaction task: run session compaction (same serialized LLM as chat when local)
                is_compaction = (task.metadata or {}).get("compaction") is True
                if is_compaction:
                    from uuid import UUID
                    from vaf.memory.lanes import ChatNamespace
                    from vaf.memory.rag import run_session_compaction_sync, session_dialogue_excerpt
                    _scope = (task.metadata or {}).get("user_scope_id")
                    _chat = ChatNamespace.from_meta(task.metadata or {})
                    _turn = int((task.metadata or {}).get("turn_count", 0))
                    if _scope is not None and not isinstance(_scope, UUID):
                        try:
                            _scope = UUID(str(_scope))
                        except (ValueError, TypeError):
                            _scope = None
                    try:
                        # Load the correct session context (important for queued compaction, e.g. Telegram)
                        agent.load_session_context(task.session_id)
                        # A chat with a contact learns from the STORED transcript: the live history
                        # holds the Front Office wrapper around the person's message.
                        _conversation = session_dialogue_excerpt(task.session_id, user_label=_chat.label) if _chat else None
                        run_session_compaction_sync(agent, _scope, task.session_id, _turn,
                                                    conversation=_conversation, chat=_chat)
                    except Exception as e:
                        logging.getLogger(__name__).warning("Session compaction (queued) failed: %s", e)
                    try:
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                                f.write(f"{_dt.now().isoformat()} QUEUE_DONE session_id={task.session_id} (compaction)\n")
                    except Exception:
                        pass
                    tq.task_done()
                    continue

                # Telegram relay: contact can only send messages to the main user (no tools, safe reply only)
                meta = (task.metadata or {}) if getattr(task, "metadata", None) else {}
                if meta.get("relay") and meta.get("telegram_chat_id"):
                    try:
                        from vaf.core.telegram_reply import send_telegram_reply
                        to_user = meta.get("relay_to_username") or meta.get("username") or "the user"
                        reply = f"Got it. I'll pass that on to {to_user}. Thanks."
                        send_telegram_reply(str(meta["telegram_chat_id"]), reply)
                    except Exception:
                        pass
                    try:
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                                f.write(f"{_dt.now().isoformat()} QUEUE_DONE session_id={task.session_id} (relay)\n")
                    except Exception:
                        pass
                    tq.task_done()
                    continue

                # Lazy-load local model only when we have a real chat task (not for LOAD_SESSION etc.)
                try:
                    # Self-heal: if the live config now points at a cloud provider + key but this
                    # agent is still on a stale local/other backend, apply it BEFORE the download
                    # gate so we never pull a local GGUF when the user configured a cloud provider.
                    try:
                        _live = Config.load()
                        _lp = (_live.get("provider", "local") or "local").strip()
                        if _lp != "local" and Config.get_api_key(_lp):
                            if agent.provider != _lp or not agent.api_backend:
                                agent.reload_api_backend()
                    except Exception as _heal_e:
                        print(f"[Headless] provider self-heal failed: {_heal_e}")
                    from vaf.core import inference_profile
                    _no_local = not inference_profile.local_inference_allowed()
                    _why = turn_refusal_reason(agent, task)
                    if _why:
                        if _no_local:
                            # A switch made in another process (`vaf profile` in a
                            # terminal) left the in-process models of this one loaded.
                            from vaf.core.agent import release_disallowed_local_backends
                            release_disallowed_local_backends()
                        refuse_turn_without_backend(task, _why, session_mgr)
                        tq.task_done()
                        continue
                    if not _no_local and agent.provider == "local" and not agent.api_backend and not agent.llm and not agent.use_server:
                        from vaf.core.model_download_state import MODEL_DOWNLOAD
                        # On first use the load below downloads the model (blocking, filelock-serialized so it
                        # never races the tray). Show an immediate status so the first prompt isn't a frozen
                        # UI; the real answer streams automatically once the model is ready (auto-retry).
                        _showed_loading = False
                        if not os.path.exists(getattr(agent, "model_path", "") or "") or MODEL_DOWNLOAD.active:
                            try:
                                get_web_interface().emit_agent_message(
                                    "assistant",
                                    "Preparing the local model (downloading on first use) — I'll answer as soon as it's ready…",
                                    session_id=task.session_id,
                                )
                                _showed_loading = True
                            except Exception:
                                pass
                        agent.load_model(skip_download_check=True)
                        _loaded = bool(agent.llm or agent.use_server)
                        if _showed_loading and _loaded:
                            # Drop the placeholder; the normal response streams in its place.
                            try:
                                get_web_interface().emit_clear_last_assistant(session_id=task.session_id)
                            except Exception:
                                pass
                        if _loaded:
                            tray_ctx = TrayContext()
                            tray_ctx.set_model_loaded(True)
                            get_web_interface().push_update({
                                "type": "model_state",
                                "loaded": True,
                                "persistent": tray_ctx.is_persistent(),
                                "provider": "local"
                            })
                except Exception as e:
                    print(f"[Headless] Lazy model load failed: {e}")

                # Send initial token stats to WebUI (before processing)
                try:
                    used, total = agent.get_token_usage()
                    api_backend = getattr(agent, 'api_backend', None)
                    input_tokens = 0
                    output_tokens = 0
                    if api_backend:
                        usage = api_backend.last_request_usage
                        input_tokens = int(usage.get("input_tokens", 0))
                        output_tokens = int(usage.get("output_tokens", 0))
                        # NOTE: Do NOT overwrite `used` with session_usage here.
                        # input/output_tokens come from last_request_usage (THIS turn only) -> no cross-user mixing.
                        # get_token_usage() already returns the correct context snapshot.
                    stats = {
                        "used": used,
                        "total": total,
                        "percent": round((used / total) * 100, 1) if total else 0.0,
                        "api": bool(api_backend),
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens
                    }
                    get_web_interface().emit_stats(stats, session_id=task.session_id)
                    # Keep Context Window breakdown (system/history/tools) in sync with totals.
                    agent._broadcast_context_status()
                except Exception:
                    pass

                # Wait for local server (8080) to be ready before first chat to avoid 503 "Loading model"
                if agent.provider == "local" and getattr(agent, "use_server", False) and not waited_for_server_ready:
                    import requests as _req
                    _deadline = time.time() + 120  # max 2 min
                    while time.time() < _deadline:
                        try:
                            r = _req.get("http://127.0.0.1:8080/v1/models", timeout=3)
                            if r.status_code == 200:
                                waited_for_server_ready = True
                                break
                        except Exception:
                            pass
                        try:
                            get_web_interface().log("Model is loading, please wait...", level="info", source="System", session_id=task.session_id)
                        except Exception:
                            pass
                        time.sleep(2)
                    if not waited_for_server_ready:
                        waited_for_server_ready = True  # don't block forever

                # Swap-back belt (dedicated voice model): a healthy :8080 no
                # longer implies the MAIN model - the voice lane may have
                # swapped the one server to its own GGUF for a live call. A
                # main-agent turn must never run on the voice model, so make
                # the server hold agent.model_path again (fast no-op when it
                # already does; ~seconds when a swap is due - the caller is a
                # queued task, not an interactive keystroke). Checked per
                # task, not once: calls can start/end between tasks.
                if agent.provider == "local":
                    try:
                        from vaf.core.backend import (_loaded_model_matches,
                                                      ensure_local_model,
                                                      get_loaded_model_id)
                        import logging as _lg
                        _main_path = getattr(agent, "model_path", "") or ""
                        # Forensic line (tray_debug): proves per task what the
                        # belt saw - a silent no-swap must be diagnosable.
                        _lg.getLogger("vaf.core.headless_runner").info(
                            "swap-back belt: use_server=%s main=%s loaded=%s",
                            getattr(agent, "use_server", False),
                            os.path.basename(_main_path) if _main_path else "-",
                            get_loaded_model_id() or "-")
                        if _main_path and not _loaded_model_matches(_main_path):
                            get_web_interface().log(
                                "Switching the local model back for this task...",
                                level="info", source="System", session_id=task.session_id)
                            ensure_local_model(_main_path, reason="main-agent task")
                    except Exception as _swap_e:
                        print(f"[Headless] main-model swap-back check failed: {_swap_e}")

                # Normal Chat Step
                # If user requested stop, skip starting chat_step for this queued task.
                # NOTE: do NOT clear before checking; that would drop the stop signal.
                if tq.should_stop(task.session_id):
                    tq.clear_stop(task.session_id)
                    try:
                        get_web_interface().emit_agent_message(
                            role="assistant",
                            content="[Generation stopped by user]",
                            session_id=task.session_id
                        )
                        get_web_interface().emit_message_complete(
                            content="[Generation stopped by user]",
                            session_id=task.session_id
                        )
                    except Exception:
                        pass
                    tq.task_done()
                    continue
                # usage: chat_step(user_input, ...)
                _chat_start = time.time()
                try:
                    # Safety net: this main-runner process must NEVER carry
                    # VAF_IN_SUBAGENT_TERMINAL into a chat turn. A crashed workflow step
                    # used to leak it (=1), which made every coding_agent call run
                    # IN-PROCESS inside the turn (minutes-long blocking tool call) instead
                    # of spawning the async child — serializing all chat behind it.
                    if os.environ.get("VAF_IN_SUBAGENT_TERMINAL", "").strip():
                        os.environ.pop("VAF_IN_SUBAGENT_TERMINAL", None)
                        try:
                            append_domain_log_always(
                                "headless",
                                "[LEAK-GUARD] cleared stale VAF_IN_SUBAGENT_TERMINAL before chat turn",
                            )
                        except Exception:
                            pass
                    try:
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                                f.write(f"{_dt.now().isoformat()} QUEUE_CHAT_START session_id={task.session_id}\n")
                    except Exception:
                        pass
                    try:
                        get_web_interface().log(
                            f"Starting chat_step for session {task.session_id}...",
                            level="info",
                            source="System",
                            session_id=task.session_id
                        )
                    except Exception:
                        pass
                    # RAG: fetch memory context for this turn (sync helper)
                    memory_context = ""
                    try:
                        def _log_rag(msg: str) -> None:
                            append_domain_log("rag", msg)
                        if Config.get("memory_enabled", True):
                            from vaf.memory.lanes import ChatNamespace
                            from vaf.memory.rag import count_sources, turn_memory_context
                            from uuid import UUID
                            user_scope_id = None
                            raw = task.metadata.get("user_scope_id") if getattr(task, "metadata", None) else None
                            if raw:
                                try:
                                    user_scope_id = UUID(str(raw))
                                except (ValueError, TypeError):
                                    pass
                            # A turn inside a chat with a contact also reads what the agent learned
                            # in THAT chat; the owner's own chats read only the general lane.
                            _chat_ns = ChatNamespace.from_task(task.session_id, task.metadata or {})
                            # A CONTACT's turn (Front Office) reads the Front Office knowledge the
                            # owner handed the agent, and the owner's general memory only when the
                            # profile says so: a stranger is driving this turn.
                            _fo_turn = bool((task.metadata or {}).get("from_contact"))
                            _fo_general = True
                            if _fo_turn:
                                try:
                                    from vaf.core.front_office_profile import load_front_office_profile
                                    _fo_general = bool(load_front_office_profile(
                                        (task.metadata or {}).get("username")).get("use_general_memory"))
                                except Exception:
                                    _fo_general = False
                            try:
                                _rag_t0 = time.time()
                                if is_debug_logging_enabled():
                                    from datetime import datetime as _dt
                                    with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                                        f.write(f"{_dt.now().isoformat()} RAG_START session_id={task.session_id} query_len={len(task.input_text or '')}\n")
                            except Exception:
                                _rag_t0 = time.time()
                            memory_context = turn_memory_context(
                                task.input_text, user_scope_id=user_scope_id,
                                caller="headless", chat_key=(_chat_ns.key if _chat_ns else None),
                                front_office=_fo_turn, use_general_memory=_fo_general)
                            try:
                                _rag_dur = time.time() - _rag_t0
                                if is_debug_logging_enabled():
                                    from datetime import datetime as _dt
                                    with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                                        f.write(f"{_dt.now().isoformat()} RAG_DONE session_id={task.session_id} duration_sec={_rag_dur:.1f} snippet_count={count_sources(memory_context)}\n")
                            except Exception:
                                pass
                            snippet_count = count_sources(memory_context)
                            if snippet_count == 0:
                                _log_rag(f"RAG snippets=0 (no matching memories or DB empty) query_len={len(task.input_text or '')}")
                            else:
                                _log_rag(f"RAG snippets={snippet_count} query_len={len(task.input_text or '')}")
                        else:
                            _log_rag("RAG skipped (memory_enabled=False)")
                    except Exception as e:
                        memory_context = ""
                        append_domain_log("rag", f"RAG failed: {e}")

                    # RAG Ergebnis in Web-UI anzeigen (Trefferzahl = Snippets im System-Prompt)
                    try:
                        from vaf.memory.rag import count_sources as _count_sources
                        _rag_count = _count_sources(memory_context)
                        get_web_interface().log(
                            f"RAG: {_rag_count} hit(s) (included in system prompt for this turn).",
                            level="info",
                            source="System",
                            session_id=task.session_id,
                        )
                    except Exception:
                        pass

                    # Streaming callback for WebUI - accumulates chunks, throttled emit so UI keeps up.
                    # Supports .clear() so agent can reset buffer on empty-response retry (only retry content is shown).
                    response_parts = []
                    _last_emit_time = [0.0]  # list so nested function can assign

                    def _emit(text):
                        if not (text and text.strip()):
                            return
                        now = time.time()
                        if now - _last_emit_time[0] >= STREAM_EMIT_THROTTLE_SEC:
                            try:
                                get_web_interface().emit_agent_message(
                                    role="assistant",
                                    content=text,
                                    session_id=task.session_id
                                )
                                _last_emit_time[0] = now
                            except Exception:
                                pass

                    def webui_stream_callback(text):
                        # Cooperative stop: abort an already-running chat_step turn
                        # as soon as the next stream callback arrives.
                        if tq.should_stop(task.session_id):
                            raise _StopGenerationRequested("Stop requested by user")
                        response_parts.append(text)
                        _emit("".join(response_parts))

                    def clear_stream_buffer():
                        response_parts.clear()

                    webui_stream_callback.clear = clear_stream_buffer

                    # The source, the Front Office set, the unattended mark and the contact
                    # record were bound with the turn (_task_turn_binding).
                    _meta = (task.metadata or {}) if getattr(task, "metadata", None) else {}

                    # Sidebar documents: inject into this turn only (session history stays clean)
                    effective_input = input_text or ""
                    # When message is from a contact (front office), pass contact data so the agent can personalize
                    if _meta.get("from_contact"):
                        # The contact of THIS turn, pinned on the agent with the turn
                        # (_front_office_contact_record).
                        contact = getattr(agent, "_front_office_contact", None)
                        # No contact record: this sender reached Front Office through the reply
                        # window (the agent wrote to them first) rather than through a contact
                        # with "Can reach your assistant". Tell the model exactly that, so it
                        # neither invents a relationship nor treats them as the owner.
                        _sender_ref = str(
                            _meta.get("whatsapp_chat_jid") or _meta.get("telegram_user_id")
                            or _meta.get("discord_author_id") or ""
                        ).split("@", 1)[0].split(":", 1)[0]
                        if _meta.get("email_from"):
                            _sender_ref = str(_meta.get("email_from") or "").strip()
                        contact_block = (
                            f"Sender: {('+' + _sender_ref) if _sender_ref.isdigit() else (_sender_ref or 'unknown')} "
                            "(no contact record). Your own earlier outbound message to this sender opened "
                            "this conversation; they are a third party, not the account owner. Answer only "
                            "what concerns that conversation, reveal nothing about the owner, and report "
                            "their answer to the owner through the back-channel when it matters."
                        )
                        reply_lang_hint = ""
                        if contact:
                            # The block carries what the contact may know about their OWN
                            # record (contact_self_view): channels, language, address form,
                            # birthday, their own upcoming appointments. Never the owner's
                            # notes about them, never another contact.
                            from vaf.core.contacts_store import contact_events, contact_self_view, format_contact_self_view
                            # contact_events reads the calendar only when one exists on disk (a
                            # glance never creates a store for a message from a contact).
                            _username, _user_scope = _front_office_book_owner(task, _meta)
                            view = contact_self_view(contact, events=contact_events(contact, _username, _user_scope))
                            contact_block = format_contact_self_view(view)
                            if view.get("preferred_language"):
                                pl_code = (view["preferred_language"] or "").strip().lower()[:2]
                                _lang_names = {"de": "German", "en": "English", "tr": "Turkish", "fr": "French", "es": "Spanish", "ar": "Arabic"}
                                pl_name = _lang_names.get(pl_code) or pl_code
                                reply_lang_hint = f" REPLY IN: {pl_name} (contact preferred_language; use this language for your reply even if the message was in another language)."
                        effective_input = (
                            "[FRONT OFFICE - MESSAGE FROM A CONTACT, NOT FROM THE ACCOUNT OWNER.] "
                            "The following message was sent by a contact to your front office. "
                            "You must respond directly TO this contact (they will receive your reply). "
                            "CRITICAL: Do NOT call send_whatsapp, send_telegram, or any messaging tool - your reply text is automatically delivered to the contact. Just write your reply as normal text. "
                            "Do NOT report to the account owner (e.g. do not say 'I sent X to the contact' or 'I have sent Alice...'). "
                            "Do NOT repeat or echo the contact's message back; give a helpful reply.\n\n"
                            "Contact details (use Language / How to address when replying; the contact may ask about the appointments listed, and nothing else about the owner or other people is to be shared):\n"
                            + contact_block
                            + ("\n" + reply_lang_hint if reply_lang_hint else "")
                            + "\n\nMessage from the contact:\n\n"
                            + effective_input
                        )
                    elif getattr(task, "source", None) == "whatsapp":
                        effective_input = (
                            "[WhatsApp message.] Respond helpfully; do not repeat or echo the user's message back.\n\n"
                            + effective_input
                        )
                    try:
                        session_for_sidebar = session_mgr.load(task.session_id)
                        sidebar_docs = (getattr(session_for_sidebar, "runtime_state", None) or {}).get("sidebar_documents") or []
                        append_domain_log("rag", f"SIDEBAR_CHECK session={task.session_id} docs={len(sidebar_docs)} names={[str((d or {}).get('name','?'))[:40] for d in sidebar_docs[:3]]}")
                        from vaf.core.log_helper import log_attachment as _log_attach
                        _log_attach("AGENT_SEES", session=task.session_id, docs=len(sidebar_docs),
                            names=[str((d or {}).get('name','?'))[:60] for d in sidebar_docs[:5]],
                            content_lens=[len(str((d or {}).get('content') or '')) for d in sidebar_docs[:5]])
                        if sidebar_docs:
                            # Name AND persisted path: without the path the model
                            # has to guess it for any page-targeted read_file - the
                            # first live run spent three failed calls and a
                            # find_files discovering what this line can simply say.
                            _sidebar_entries = []
                            for d in sidebar_docs:
                                _n = str((d or {}).get("name") or "").strip()
                                if not _n:
                                    continue
                                _p = str((d or {}).get("path") or "").strip()
                                if _p and not _p.startswith("Hochgeladen"):
                                    _sidebar_entries.append(f"{_n} (path: {_p})")
                                else:
                                    _sidebar_entries.append(_n)
                            sidebar_name_list = ", ".join(_sidebar_entries[:5])
                            if len(_sidebar_entries) > 5:
                                sidebar_name_list += ", ..."
                            # Explicit anchor so the model knows these documents are active working context
                            # even when the user did not mark a specific selection in the editor/viewer.
                            context_header = (
                                "[DOCUMENT CONTEXT ACTIVE]\n"
                                "The user currently works with attached document(s) in the Web UI.\n"
                                "Use the retrieved attachment snippets below as primary context for this turn.\n"
                            )
                            if sidebar_name_list:
                                context_header += (
                                    f"Attached: {sidebar_name_list}\n"
                                    "For a specific page or range, call read_file with that "
                                    "path and first_page/last_page - never guess a path.\n"
                                )

                            # Warn the agent when any attachment exceeded the indexing size limit.
                            _max_attach_chars = int(Config.get("attachment_rag_max_chars_per_doc", 24000) or 24000)
                            _truncated_docs = [
                                d for d in sidebar_docs
                                if len(str((d or {}).get("content") or "")) > _max_attach_chars
                            ]
                            if _truncated_docs:
                                _trunc_names = ", ".join(
                                    str((d or {}).get("name") or "unknown") for d in _truncated_docs
                                )
                                _warn = (
                                    f"\n⚠️ ATTACHMENT SIZE WARNING: The following document(s) exceed the "
                                    f"{_max_attach_chars:,} character limit. "
                                    f"Content beyond the limit has been lost. "
                                    f"To process the full document use: learn_document(path=\"<path>\").\n"
                                    f"Truncated: {_trunc_names}\n"
                                )
                                for _td in _truncated_docs:
                                    _td_path = str((_td or {}).get("path") or "")
                                    _td_name = str((_td or {}).get("name") or "unknown")
                                    # Chat uploads are persisted to a real file now; only
                                    # a failed persist leaves the honest no-path literal,
                                    # and an unfollowable learn_document(path="Hochgeladen
                                    # ...") helps nobody.
                                    if _td_path and not _td_path.startswith("Hochgeladen"):
                                        _warn += f'  → {_td_name}: learn_document(path="{_td_path}")\n'
                                context_header += _warn

                            # Attachments that FIT in context are inlined IN FULL. RAG top-k retrieval is
                            # query-driven and silently drops pages the query does not semantically match
                            # (observed: "what's on page 6?" surfaced only page 1, because a page number is
                            # not its content). A document under the indexing cap is only ~a few thousand
                            # tokens, so inlining the whole thing every turn is cheap and lets the agent read
                            # every page. The RAG lane stays for documents too large to inline.
                            snippet_lines = []
                            _oversize_present = False
                            for idx, doc in enumerate(sidebar_docs, 1):
                                d_name = str((doc or {}).get("name") or f"Attachment {idx}")
                                d_content = str((doc or {}).get("content") or "").strip()
                                if not d_content:
                                    continue
                                if len(d_content) <= _max_attach_chars:
                                    snippet_lines.append(
                                        f"[Attachment {idx}] {d_name} (full document, {len(d_content)} chars)\n{d_content}"
                                    )
                                else:
                                    _oversize_present = True

                            # Hybrid retrieval lane: only for oversize attachments (full text not inlined).
                            if _oversize_present and bool(Config.get("attachment_rag_enabled", False)):
                                try:
                                    from vaf.memory.attachment_rag import search_session_attachments_sync
                                    att_hits = search_session_attachments_sync(
                                        query=(input_text or ""),
                                        session_id=str(task.session_id),
                                        user_scope_id=meta.get("user_scope_id"),
                                    )
                                    for idx, hit in enumerate(att_hits, 1):
                                        h_name = str((hit or {}).get("attachment_name") or "Attachment")
                                        h_score = float((hit or {}).get("score") or 0.0)
                                        h_text = str((hit or {}).get("text") or "").strip()
                                        if not h_text:
                                            continue
                                        snippet_lines.append(
                                            f"[Attachment Source {idx}] {h_name} (Relevance: {h_score:.0%})\n{h_text}"
                                        )
                                except Exception as e:
                                    append_domain_log("rag", f"ATTACH_SEARCH failed: {e}")

                            if not snippet_lines:
                                # Last resort: a deterministic excerpt when an oversize doc could not be
                                # inlined AND the index is not ready yet OR indexing failed.
                                fallback_chars = int(Config.get("attachment_rag_snippet_chars", 900) or 900)
                                for idx, doc in enumerate(sidebar_docs[:2], 1):
                                    d_name = str((doc or {}).get("name") or f"Attachment {idx}")
                                    d_content = str((doc or {}).get("content") or "").strip()
                                    if not d_content:
                                        continue
                                    if len(d_content) > fallback_chars:
                                        d_content = d_content[:fallback_chars].rstrip() + "\n... [fallback truncated]"
                                    snippet_lines.append(f"[Attachment Fallback {idx}] {d_name}\n{d_content}")
                                if snippet_lines:
                                    # Tell the agent why it only sees a fallback excerpt, and how to read more.
                                    snippet_lines.append(
                                        "[Note] The attachment search index returned no results (it may still be "
                                        "indexing, or indexing failed). The excerpt above may be truncated — if you "
                                        "need the full document and a local file path is available, read it directly "
                                        "with list_files / read_file."
                                    )

                            sidebar_block = "\n\n".join([context_header, "\n\n".join(snippet_lines)])
                            sidebar_block += (
                                "\n\nIf the user asks to keep knowledge from these attachments for future chats, "
                                "suggest using learn_attached_knowledge and ask explicit confirmation first."
                            )
                            if _meta.get("from_contact"):
                                effective_input = effective_input + "\n\n" + sidebar_block
                            else:
                                effective_input = sidebar_block + "\n\n" + (input_text or "")
                    except Exception as _sidebar_err:
                        append_domain_log("rag", f"SIDEBAR_CHECK_ERR session={task.session_id} err={_sidebar_err!r}")
                        pass  # no session or corrupted session file, use raw input_text

                    # Code viewer: inject current open file with numbered lines into this turn only.
                    # Content is stored in runtime_state by web_server.py and cleared here after use.
                    try:
                        session_for_cv = session_mgr.load(task.session_id)
                        cv_file = (getattr(session_for_cv, "runtime_state", None) or {}).get("code_viewer_file") or {}
                        cv_content = (cv_file.get("content") or "").strip()
                        if cv_content:
                            cv_name = cv_file.get("name") or "File"
                            cv_path = cv_file.get("path") or ""
                            cv_lines = cv_content.splitlines()
                            width = len(str(len(cv_lines)))
                            numbered = "\n".join(
                                f"{i + 1:>{width}}: {line}" for i, line in enumerate(cv_lines)
                            )
                            cv_ext = cv_path.rsplit(".", 1)[-1] if "." in cv_path else ""
                            cv_block = (
                                f"--- CURRENTLY OPEN IN CODE VIEWER: {cv_name}"
                                + (f" ({cv_path})" if cv_path else "")
                                + f" ({len(cv_lines)} lines) ---\n"
                                + f"```{cv_ext}\n{numbered}\n```\n"
                                + "---\n\n"
                            )
                            effective_input = cv_block + effective_input
                            # Clear after injection — frontend will resend next turn if viewer still open
                            session_for_cv.runtime_state.pop("code_viewer_file", None)
                            session_mgr.save(session_for_cv, sync_state=False)
                    except Exception:
                        pass

                    # Interactive browser: while this chat's window drives the sandbox
                    # browser, web_server stored the page the user is on (URL + selected
                    # text) at send time; a screenshot travelled separately through the
                    # attached-images lane. Injected into THIS turn only and cleared, the
                    # code-viewer lifecycle.
                    try:
                        session_for_bc = session_mgr.load(task.session_id)
                        bc = (getattr(session_for_bc, "runtime_state", None) or {}).get("browser_context") or {}
                        bc_url = (bc.get("url") or "").strip()
                        if bc_url:
                            bc_sel = (bc.get("selection") or "").strip()
                            bc_block = (
                                "--- USER IS IN THE INTERACTIVE BROWSER ---\n"
                                f"They are currently on this page: {bc_url}\n"
                                + (f"Text they have selected on the page:\n\"{bc_sel}\"\n" if bc_sel else "")
                                + "A screenshot of their current view is attached to this message as an image.\n"
                                # State of the world plus what can be DONE with it. Without the
                                # second half the model was told where the person is and drew no
                                # conclusion from it - measured live: asked to open a page and
                                # wait, it neither knew the person was in the browser nor that it
                                # could take that same browser over.
                                + "To act in this browser yourself - open a page, click, wait, read "
                                  "something back - call the browser_agent tool. It drives THIS "
                                  "browser: it takes over from the user for the length of the run "
                                  "and hands control back when it finishes, and it sees whatever "
                                  "the user is logged into.\n"
                                + "Taking over CONTINUES the user's session: the run is told which "
                                  "page they were on, their tab stays open and their logins stay "
                                  "live. Phrase the task as continuing there (e.g. 'continue on the "
                                  "open page and ...'), not as opening the site fresh.\n"
                                + "---\n\n"
                            )
                            effective_input = bc_block + effective_input
                            session_for_bc.runtime_state.pop("browser_context", None)
                            session_mgr.save(session_for_bc, sync_state=False)
                    except Exception:
                        pass

                    # A sub-agent window is open. Same lane as the browser block above,
                    # for the specialists that have no server-side session to read: the
                    # client says which window it has open, web_server scopes it to that
                    # session and validates the kind, and it is injected for THIS turn
                    # and cleared - so closing the window needs no event of its own.
                    #
                    # State AND affordance, the lesson the browser block records: naming
                    # where the person is, without saying what can be done about it, was
                    # measured to change nothing about the model's behaviour.
                    try:
                        session_for_sw = session_mgr.load(task.session_id)
                        sw_kind = ((getattr(session_for_sw, "runtime_state", None) or {})
                                   .get("subagent_window") or "").strip()
                        _SW = {
                            "coder": ("the Coder", "coding_agent",
                                      "writing and changing code in a project, running its "
                                      "tests and checking the page it built. If the context "
                                      "line above names a folder the user PICKED as the "
                                      "project, pass that absolute path to coding_agent as "
                                      "project_path so the run continues there instead of "
                                      "creating a fresh project"),
                            "research": ("the Researcher", "research_agent",
                                         "digging through many sources and coming back with a "
                                         "written, sourced answer"),
                            "document": ("the Document Writer", "document_agent",
                                         "drafting and editing long documents, reports and letters"),
                            "librarian": ("the Librarian", "librarian_agent",
                                          "finding its way around the user's files: searching, "
                                          "sorting, tidying and reporting what is where"),
                        }
                        if sw_kind in _SW:
                            _label, _tool, _does = _SW[sw_kind]
                            sw_detail = ((getattr(session_for_sw, "runtime_state", None) or {})
                                         .get("subagent_window_detail") or "").strip()
                            sw_block = (
                                f"--- USER HAS {_label.upper()}'S WINDOW OPEN ---\n"
                                f"They opened it themselves, so their message is most likely "
                                f"about that work even when they do not name it.\n"
                                + (f"Right now they are: {sw_detail}\n" if sw_detail else "")
                                + f"{_label} handles {_does}.\n"
                                f"To do that work, call the {_tool} tool - its run appears in "
                                f"the window they are already looking at, so they watch it "
                                f"happen rather than waiting on a silent reply.\n"
                                f"An open window does NOT mean a run is going. If one already "
                                f"is, you are told separately - then keep chatting and do not "
                                f"start a second one.\n"
                                f"---\n\n"
                            )
                            effective_input = sw_block + effective_input
                            session_for_sw.runtime_state.pop("subagent_window", None)
                            session_for_sw.runtime_state.pop("subagent_window_detail", None)
                            session_mgr.save(session_for_sw, sync_state=False)
                    except Exception:
                        pass

                    # Image viewer: while the Image Viewer is open, inject the open image's vision
                    # description into THIS turn only (so the agent can reason about the image the user
                    # is looking at). Stored in runtime_state by web_server.py and cleared here after use
                    # — same lifecycle as the code viewer, so the description stays in context exactly as
                    # long as the viewer is open.
                    try:
                        session_for_iv = session_mgr.load(task.session_id)
                        iv_ctx = (getattr(session_for_iv, "runtime_state", None) or {}).get("image_viewer_context") or {}
                        iv_desc = (iv_ctx.get("description") or "").strip()
                        if iv_desc:
                            iv_name = iv_ctx.get("name") or "image"
                            iv_block = (
                                f"--- CURRENTLY OPEN IN IMAGE VIEWER: {iv_name} (vision description) ---\n"
                                f"{iv_desc}\n"
                                f"(The user is looking at this image. For finer detail call analyze_image.)\n"
                                f"---\n\n"
                            )
                            effective_input = iv_block + effective_input
                            session_for_iv.runtime_state.pop("image_viewer_context", None)
                            session_mgr.save(session_for_iv, sync_state=False)
                    except Exception:
                        pass

                    # Marked region: inject the vision answer about the user's yellow-marked region
                    # for THIS turn only (computed in web_server when the question was sent), then clear.
                    try:
                        session_for_mr = session_mgr.load(task.session_id)
                        mr = (getattr(session_for_mr, "runtime_state", None) or {}).get("marked_region_answer") or {}
                        mr_text = (mr.get("text") or "").strip()
                        if mr_text:
                            mr_name = mr.get("name") or "image"
                            mr_block = (
                                f"--- MARKED REGION on {mr_name} (the yellow box the user drew) — "
                                f"vision answer to their question ---\n"
                                f"{mr_text}\n"
                                f"---\n\n"
                            )
                            effective_input = mr_block + effective_input
                            session_for_mr.runtime_state.pop("marked_region_answer", None)
                            session_mgr.save(session_for_mr, sync_state=False)
                    except Exception:
                        pass

                    # Session workspace + active project: inject so agent knows where to edit files.
                    # The same folders the turn was bound with (_chat_project_paths): its VAF.md
                    # is read from the edit folder (Agent._project_context_block).
                    _proj = _chat_project_paths(session_mgr, task.session_id)
                    if _proj.workspace:
                        _workspace, _edit_path = _proj.workspace, _proj.edit_path
                        proj_note = (
                            f"[SESSION WORKSPACE] All files for this chat are stored in: {_workspace}\n"
                            f"[ACTIVE PROJECT] Most recently created/edited: {_edit_path}\n"
                            f"Final deliverables belong IN the session workspace - it is the ONLY folder "
                            f"the user's UI file browser shows (remote/LAN clients cannot browse anywhere "
                            f"else). When the user says 'Projekte-Ordner' / 'projects folder', they mean "
                            f"THIS workspace, never the VAF_Projects root. write_file with a relative path "
                            f"lands here automatically; move artifacts created via shell commands here.\n"
                            f'To edit or modify: coding_agent(task="<your task>", project_path="{_edit_path}")\n\n'
                        )
                        effective_input = proj_note + effective_input
                    elif _proj.edit_path:
                        _last_proj = _proj.edit_path
                        # Fallback for sessions predating session.project_path support
                        proj_note = (
                            f"[PROJECT CONTEXT] The most recently created project is at: {_last_proj}\n"
                            f'To edit, update, or modify files from this project call: '
                            f'coding_agent(task="<your edit task>", project_path="{_last_proj}")\n\n'
                        )
                        effective_input = proj_note + effective_input

                    # Editor write tools depend on whether an editor document is open and whether
                    # selections exist; the turn was bound with the tools that hides
                    # (_editor_tool_exclusions), and both are hidden exactly when no document is
                    # open.
                    editor_has_content = "replace_editor_text" not in (_binding.excluded_tools or ())

                    try:
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                                f.write(f"{_dt.now().isoformat()} CHAT_STEP_CALL session_id={task.session_id}\n")
                    except Exception:
                        pass
                    # Do not run workflow matching for contact messages (WhatsApp/Telegram/Discord).
                    # Workflows are for the account owner in Web/CLI; contact chat should be normal LLM reply only.
                    task_source = getattr(task, "source", None) or ""
                    disable_workflows = str(task_source).lower() in CHAT_CHANNELS + ("email",)

                    # Keep WebUI sub-agents inside the WebUI panel (no host terminal popups)
                    # even after restarts where global env flags may be unset.
                    task_meta_for_env = (task.metadata or {}) if getattr(task, "metadata", None) else {}
                    origin_channel = str(task_meta_for_env.get("origin_channel") or "").strip().lower()
                    source_channel = str(task_source).strip().lower()
                    prev_webui_active = os.environ.get("VAF_WEBUI_ACTIVE")
                    force_webui_active = origin_channel == "web" or source_channel == "web"
                    # This compensation is a belt, not the mechanism. web_server.run_server sets
                    # VAF_WEBUI_ACTIVE once at startup and no longer clears it when the last
                    # browser disconnects, so the spawn decision no longer depends on a browser
                    # being attached at that instant. It is kept only for a process that starts
                    # serving without going through run_server, and it still stays out of the way
                    # of a multi-worker pool, where mutating a process-global env per turn races
                    # across workers (one worker's finally clears it mid-turn for another).
                    #
                    # The comment that used to sit here claimed web_server set the flag at
                    # startup already, citing a line that is an unrelated path allowlist. That
                    # wrong claim is exactly why disabling this under >1 worker looked harmless -
                    # and on a 2-worker install it left the real decision to a socket-lifetime
                    # flag, which is how a host terminal window opened for a web-launched
                    # sub-agent (live incident 2026-07-20).
                    _manage_webui_env = force_webui_active and total_workers <= 1
                    _task_images = task_meta_for_env.get("images") or None
                    # Whether a PERSON sent this turn and which wake it is: decided once, at the
                    # queue boundary, by the turn's binding (_task_turn_binding), and held on the
                    # agent until the task ends (end_turn).
                    _turn_is_human = _binding.is_human is True
                    _wake = _binding.wake[0] if _binding.wake else None
                    # Timer wake-turn: a timer task has no preceding user message, so without a boundary
                    # the agent's reply overwrites the previous assistant bubble (same slot + timestamp).
                    # Emit the trigger as a "wake" system-activity message (kind="timer") -> the Web UI
                    # renders it in its own LEFT-side area (not the user side), and it also serves as the
                    # boundary so the agent's reply lands in its OWN new bubble with a correct timestamp.
                    # chat_step persists the input to history itself, so this is a live-display emit only.
                    if _wake:
                        try:
                            # role="user" keeps the (proven) bubble boundary so the agent's reply lands in
                            # its own new bubble; kind (timer / process) tells the Web UI to render it as a
                            # LEFT-side wake card instead of a user bubble.
                            get_web_interface().emit_agent_message_append(
                                content=str(effective_input), session_id=task.session_id,
                                role="user", kind=_wake,
                            )
                        except Exception:
                            pass
                    # The draft note is per turn: the bind cleared the previous chat's (an early
                    # workflow exit ends a turn before chat_step writes this one's).
                    try:
                        if _manage_webui_env:
                            os.environ["VAF_WEBUI_ACTIVE"] = "1"
                        response = agent.chat_step(
                            user_input=effective_input,
                            stream_callback=webui_stream_callback,  # Stream to WebUI!
                            skip_input=False,
                            disable_workflows=disable_workflows,
                            memory_context=memory_context or None,
                            images=_task_images,
                            # Routing (workflow/skill router + variable extraction)
                            # must see the user's RAW message: effective_input
                            # carries the workspace/front-office enrichment, whose
                            # wording (coding_agent, projects, write_file) steered
                            # the router to wrong templates and leaked into
                            # extracted variables (live incident).
                            raw_user_input=(input_text or ""),
                        )
                        if tq.should_stop(task.session_id):
                            raise _StopGenerationRequested("Stop requested by user")
                    finally:
                        if _manage_webui_env:
                            if prev_webui_active is None:
                                os.environ.pop("VAF_WEBUI_ACTIVE", None)
                            else:
                                os.environ["VAF_WEBUI_ACTIVE"] = prev_webui_active
                    try:
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                                f.write(f"{_dt.now().isoformat()} CHAT_STEP_RETURNED session_id={task.session_id} response_len={len(str(response)) if response else 0}\n")
                    except Exception:
                        pass

                    # A person is back: let every paused A2A room run again. The test is
                    # composed here rather than inside chat_step because only the task
                    # still knows what it was - an automation and a TIMER both reach the
                    # agent as an ordinary turn with real text and no marker, and a
                    # repeating timer would otherwise hold the brake open forever.
                    try:
                        if _turn_is_human:
                            agent.note_human_turn()
                    except Exception:
                        pass

                    # Record this turn as "last interaction" for next turn's system prompt
                    try:
                        from vaf.core.last_interaction import update_last_interaction
                        meta = (task.metadata or {}) if getattr(task, "metadata", None) else {}
                        update_last_interaction(
                            user_scope_id=meta.get("user_scope_id"),
                            source=getattr(task, "source", "web"),
                            preview=(task.input_text or "")[:80],
                            voice=bool(meta.get("voice_lang")),
                            session_id=getattr(task, "session_id", None),
                        )
                    except Exception:
                        pass

                    # Handle async-ack markers (sub-agent dispatched, no stream output)
                    response_text = str(response) if response is not None else ""
                    _is_system_log_only = response_text.startswith("[SYSTEM_LOG_ONLY]")
                    from vaf.core.outbound_hold import TURN_ENDS_AT_DRAFT as _DRAFT_END
                    _turn_ends_at_draft = response_text.strip() == _DRAFT_END
                    # A call ended the turn (a question to the person, a draft): the answer is
                    # what the tool declared, not a text the person asked to have written.
                    _turn_closed = bool(getattr(agent, "_turn_closed_by_tool", False))
                    # The turn produced no answer (backend failure, loop-protection stop), or
                    # answered and then such a failure ended it (`failure`, the text as
                    # streamed): read NOW, before anything below runs another chat_step on the
                    # same agent.
                    _turn_status = agent.last_turn
                    if response_text.startswith("[ASYNC_ACK]"):
                        clean_ack = response_text.replace("[ASYNC_ACK]", "").strip()
                        final_text = clean_ack or response_text
                        if clean_ack:
                            get_web_interface().emit_agent_message(
                                role="assistant",
                                content=clean_ack,
                                session_id=task.session_id
                            )
                    elif _is_system_log_only:
                        # Agent already sent this as system log; do NOT add assistant bubble
                        # and do NOT send to any external channel (Telegram/WhatsApp/Discord)
                        final_text = response_text.replace("[SYSTEM_LOG_ONLY]", "").strip()
                    elif _turn_ends_at_draft:
                        # The turn stopped at a draft (vaf/core/outbound_hold.py): the card is
                        # the answer. The fixed sentence goes out as the turn's final message,
                        # which the browser shows as nothing, so the live chat and a reloaded
                        # one hold the same thing - and it must not fall into the branch below,
                        # which would answer an empty-looking turn with "No response was
                        # produced".
                        final_text = response_text
                        get_web_interface().emit_agent_message(
                            role="assistant",
                            content=final_text,
                            session_id=task.session_id
                        )
                    else:
                        # Final response broadcast: always send full content once so UI has complete message.
                        # (Streaming is throttled, so the last chunk(s) may never have been emitted.)
                        raw_final = "".join(response_parts) if response_parts else response_text

                        # SANITIZE: Remove leaked JSON fragments before showing to user
                        final_text = _strip_tool_calls_json(raw_final)

                        # If agent stopped internally (via should_stop check inside chat_step),
                        # clear the streaming bubble and emit a clean stop notice — same as the
                        # _StopGenerationRequested path — so no partial text leaks through.
                        _agent_stopped_internally = "[Generation stopped by user]" in response_text
                        if _agent_stopped_internally:
                            partial = final_text.replace("[Generation stopped by user]", "").strip()
                            get_web_interface().emit_clear_last_assistant(task.session_id)
                            final_text = (partial + "\n\n*[Generation stopped]*").strip() if partial else "*[Generation stopped]*"
                            get_web_interface().emit_agent_message(
                                role="assistant",
                                content=final_text,
                                session_id=task.session_id
                            )
                        else:
                            if not final_text or not str(final_text).strip():
                                final_text = "[Error] No response was produced. The server may have rejected the request (e.g. context too large). Try closing the Document Editor or starting a new chat."
                            get_web_interface().emit_agent_message(
                                role="assistant",
                                content=str(final_text),
                                session_id=task.session_id
                            )

                    # Emit message_complete event for Auto-TTS (skip speaking for SYSTEM_LOG_ONLY)
                    try:
                        get_web_interface().emit_message_complete(
                            content="" if (response_text.startswith("[SYSTEM_LOG_ONLY]")
                                           or _turn_ends_at_draft) else str(final_text),
                            session_id=task.session_id
                        )
                    except Exception:
                        pass

                    # ── Hallucination guard ────────────────────────────────────────────
                    # If the agent DESCRIBED calling a sub-agent tool (coding_agent,
                    # librarian_agent …) but the response has no [ASYNC_ACK] marker,
                    # no IPC task was actually registered — it hallucinated.
                    # Inject a correction and re-run chat_step once to force the real call.
                    #
                    # IMPORTANT: We require BOTH an agent-name reference AND an active-running
                    # indicator to be present. This prevents false positives when the agent is
                    # merely reporting a failure ("Der Coding Agent ist gecrasht") or telling
                    # the user it will wait.
                    _agent_name_phrases = [
                        # Exact tool names (underscore form)
                        "coding_agent", "librarian_agent", "research_agent",
                        # Natural-language aliases (EN + DE)
                        "coding agent", "sub-agent", "subagent",
                        "der sub-agent", "der subagent",
                    ]
                    _running_indicator_phrases = [
                        # English – agent described as actively running/started
                        "is now working", "working on it", "has been started",
                        "is currently working", "is running", "has started",
                        "is being created", "agent is working",
                        # German – agent described as actively running/started
                        "arbeitet gerade", "wird erstellt", "wird bearbeitet",
                        "ist gestartet", "startet gerade", "läuft gerade",
                        "ist bereits am laufen", "ist am arbeiten",
                    ]
                    # Exemption: agent explicitly said it would wait → not a hallucination
                    _wait_phrases = [
                        "warten", "warte", "ich warte", "sobald", "wait", "waiting",
                        "bereit für", "ready for",
                    ]
                    _final_lower = final_text.lower()
                    _is_async = response_text.startswith("[ASYNC_ACK]")
                    _is_sys   = response_text.startswith("[SYSTEM_LOG_ONLY]")
                    _hallucination_detected = (
                        not _is_async and not _is_sys and not _turn_closed
                        and any(p in _final_lower for p in _agent_name_phrases)
                        and any(p in _final_lower for p in _running_indicator_phrases)
                        and not any(p in _final_lower for p in _wait_phrases)
                    )
                    if _hallucination_detected:
                        try:
                            from vaf.core.subagent_ipc import get_ipc as _get_ipc
                            _ipc = _get_ipc()
                            _active = _ipc.get_active_tasks(session_id=task.session_id)
                            _pending = _ipc.get_pending_results(session_id=task.session_id)
                            if not _active and not _pending:
                                # No actual sub-agent registered → hallucination detected
                                get_web_interface().log(
                                    "Hallucination detected: agent claimed sub-agent is running "
                                    "but no IPC task found. Injecting correction.",
                                    level="warning", source="System", session_id=task.session_id
                                )
                                _correction = (
                                    "[SYSTEM CORRECTION] You just wrote that a sub-agent or the "
                                    "coding agent is working, but you never actually called the "
                                    "tool. Calling a tool means using a tool call — not writing "
                                    "text about it. You MUST now call the appropriate tool "
                                    "(e.g. coding_agent, librarian_agent) to actually start the "
                                    "task. Do NOT write another text response — call the tool."
                                )
                                # Skip correction if user already pressed stop
                                if tq.should_stop(task.session_id):
                                    tq.clear_stop(task.session_id)
                                    get_web_interface().log(
                                        "Hallucination correction skipped — user requested stop.",
                                        level="info", source="System", session_id=task.session_id
                                    )
                                else:
                                  agent.history.append({"role": "user", "content": _correction})
                                  _corr_parts = []
                                  def _corr_stream(text):
                                      if tq.should_stop(task.session_id):
                                          raise _StopGenerationRequested("Stop during hallucination correction")
                                      _corr_parts.append(text)
                                      _t = "".join(_corr_parts)
                                      if _t.strip():
                                          get_web_interface().emit_agent_message(
                                              "assistant", _t, session_id=task.session_id
                                          )
                                  agent.chat_step(
                                      user_input=_correction,
                                      stream_callback=_corr_stream,
                                      skip_input=True,   # already added above
                                      disable_workflows=True,
                                      disable_tools=False,
                                  )
                                  if _corr_parts:
                                      _corr_final = "".join(_corr_parts)
                                      get_web_interface().emit_agent_message(
                                          "assistant", _corr_final, session_id=task.session_id
                                      )
                                      get_web_interface().emit_message_complete(
                                          content=_corr_final, session_id=task.session_id
                                      )
                        except Exception as _hg_err:
                            print(f"[Headless] Hallucination guard error: {_hg_err}")
                    # ── End hallucination guard ───────────────────────────────────────

                    # When user asked for a text (e.g. "Schreib mir einen Text"), open it in Document Editor
                    # Not for a turn a call ended: a draft's text is the fixed turn-end sentence
                    # (the message it wrote is in the card), a question is no text to edit.
                    if (not response_text.startswith("[ASYNC_ACK]") and not response_text.startswith("[SYSTEM_LOG_ONLY]")
                            and not _turn_ends_at_draft and not _turn_closed):
                        try:
                            _maybe_open_draft_in_editor(
                                task.session_id or "",
                                task.input_text or "",
                                str(final_text),
                                getattr(task, "source", "web"),
                                editor_has_content=editor_has_content,
                                user_scope_id=(task.metadata or {}).get("user_scope_id"),
                            )
                        except Exception:
                            pass

                    # If this task came from Telegram/WhatsApp/Discord, send reply back.
                    # NEVER send [SYSTEM_LOG_ONLY] responses to external channels.
                    if _is_system_log_only:
                        try:
                            from vaf.core.log_helper import log_whatsapp_reply
                            log_whatsapp_reply(f"HEADLESS SYSTEM_LOG_ONLY suppressed for session={task.session_id!r} len={len(final_text)}")
                        except Exception:
                            pass
                    else:
                        # The contact pinned for THIS turn: end_turn clears it once the task ends.
                        _deliver_channel_reply(
                            task, final_text, session_mgr=session_mgr,
                            contact=getattr(agent, "_front_office_contact", None),
                            turn=_turn_status)

                    # Send token/context stats to WebUI
                    try:
                        used, total = agent.get_token_usage()
                        api_backend = getattr(agent, 'api_backend', None)
                        input_tokens = 0
                        output_tokens = 0
                        if api_backend:
                            usage = api_backend.last_request_usage
                            input_tokens = int(usage.get("input_tokens", 0))
                            output_tokens = int(usage.get("output_tokens", 0))
                            # NOTE: Do NOT overwrite `used` with session_usage here.
                            # input/output_tokens come from last_request_usage (THIS turn only) -> no cross-user mixing.
                            # get_token_usage() already returns the correct context snapshot.
                        stats = {
                            "used": used,
                            "total": total,
                            "percent": round((used / total) * 100, 1) if total else 0.0,
                            "api": bool(api_backend),
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens
                        }
                        get_web_interface().emit_stats(stats, session_id=task.session_id)
                        # Keep Context Window breakdown (system/history/tools) in sync with totals.
                        agent._broadcast_context_status()
                    except Exception:
                        pass  # Ignore stats errors

                    # === POST-CHAT PROCESSING ===
                    # Order matters for memory safety:
                    # 1. Session save (lightweight, critical for persistence)
                    # 2. Auto-capture (queues ONNX work, can be skipped on error)
                    # 3. Compaction (queues LLM work, can be skipped on error)
                    # If any step fails, we skip subsequent memory-intensive operations
                    _post_chat_ok = True

                    # 1. Save session FIRST (before any memory-intensive operations)
                    # CONTEXT EFFICIENCY: Only save clean content (no <think> blocks)
                    try:
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                                f.write(f"{_dt.now().isoformat()} SESSION_SAVE_START session_id={task.session_id}\n")
                    except Exception:
                        pass
                    try:
                        try:
                            session = session_mgr.load(task.session_id)
                        except FileNotFoundError:
                            session = new_session_for_task(task)
                        _user_input = input_text or ""
                        _clean_response, _turn_meta = _chat_turn_copy(
                            response_parts, response_text, ends_at_draft=_turn_ends_at_draft,
                            turn=_turn_status)

                        if _user_input.strip():
                            # Helper to get role/content from Message object or dict
                            def _msg_role(m):
                                return m.role if hasattr(m, 'role') else m.get("role")
                            def _msg_content(m):
                                return m.content if hasattr(m, 'content') else m.get("content", "")

                            last_user_msg = None
                            for msg in reversed(session.messages):
                                if _msg_role(msg) == "user":
                                    last_user_msg = _msg_content(msg)
                                    break

                            # What became of the chat's drafts (vaf/core/outbound_hold.py): the
                            # agent put that note right BEFORE this input, so the turn-context
                            # persistence below, which starts after the user message, cannot see
                            # it. Stored here, ahead of the user message, it survives a reload and
                            # the agent is not told the same news again - also when this input
                            # repeats the last stored one and is therefore not stored again: the
                            # note is new either way.
                            _draft_note = getattr(agent, "_turn_decision_note", None)
                            if _draft_note:
                                session.add_message(role="system", content=str(_draft_note))

                            if last_user_msg != _user_input.strip():
                                _img_meta = None
                                if _task_images:
                                    # base_description: filled in place by chat_step's one-time
                                    # vision pass — persist it so follow-up turns / reloads stay
                                    # grounded without re-running (costly) vision per turn.
                                    _img_meta = {"images": [
                                        {
                                            "name": _im.get("name", "image"),
                                            "mime_type": _im.get("mime_type", "image/jpeg"),
                                            # Prefer the on-disk path (keeps session.json lean); fall
                                            # back to inline base64 only when no file was written.
                                            **({"path": _im["path"]} if _im.get("path") else {"data": _im.get("data", "")}),
                                            **({"base_description": _im["base_description"]} if _im.get("base_description") else {}),
                                        }
                                        for _im in _task_images
                                    ]}
                                # Tag the turn with the originating channel message id (when the
                                # bridge supplied one) so a later inbound edit/delete event can find
                                # and patch exactly this turn. Purely additive; ignored elsewhere.
                                _user_meta = dict(_img_meta) if _img_meta else {}
                                _chan_msg_id = (task.metadata or {}).get("channel_message_id")
                                if _chan_msg_id:
                                    _user_meta["channel_message_id"] = str(_chan_msg_id)
                                # Voice-call delegations keep their origin across
                                # reloads: kind drives the red-bordered
                                # "voice agent" bubble in the Web UI (same
                                # persisted-kind mechanism as timer/thinking).
                                _msg_kind = ("voice_delegation"
                                             if (task.metadata or {}).get("origin_channel") == "voice_call"
                                             else None)
                                session.add_message(role="user", content=_user_input.strip(),
                                                    metadata=(_user_meta or None), kind=_msg_kind)
                                # Increment persistent user_turn_count in runtime_state
                                if not hasattr(session, 'runtime_state') or session.runtime_state is None:
                                    session.runtime_state = {}
                                session.runtime_state["user_turn_count"] = session.runtime_state.get("user_turn_count", 0) + 1
                                # Persist this turn's context artifacts from the live agent
                                # history — either the raw tool scaffolding (assistant tool_calls
                                # + role:"tool" results) or, after the turn-end squash, the
                                # "[Context: ...]" summary that records each tool's outcome/error.
                                # This keeps the agent aware of what it did (and which errors it
                                # hit) across session reloads. Plain assistant text is saved
                                # separately below as _clean_response, so it is skipped here.
                                try:
                                    # Idempotency guard against history duplication: a re-sent identical
                                    # prompt (plus a concurrent session reload on the shared agent) can make
                                    # turn_context_messages_since_last_user() re-select an EARLIER turn's
                                    # assistant(tool_calls)/role:tool block and append it verbatim — writing
                                    # DUPLICATE tool_call_ids that later 400 providers like DeepSeek
                                    # ("messages with role 'tool' must follow a message with 'tool_calls'").
                                    # Track CALL ids and RESULT ids in SEPARATE spaces: an assistant's
                                    # tool_calls[].id and its paired role:tool's tool_call_id are the SAME id
                                    # (that equality IS the link), so one shared set would skip every fresh
                                    # result as a "duplicate" of its own call and orphan the tool_use. Skip a
                                    # role:tool only if its id already exists as a RESULT; skip an assistant
                                    # only if ALL its call ids already exist as CALLS. Also dedupe in-batch.
                                    _call_ids = set()
                                    _result_ids = set()
                                    _existing_ctx = set()
                                    for _em in session.messages:
                                        _eid = getattr(_em, "tool_call_id", None)
                                        if _eid:
                                            _result_ids.add(_eid)
                                        for _etc in (getattr(_em, "tool_calls", None) or []):
                                            _etcid = (_etc or {}).get("id")
                                            if _etcid:
                                                _call_ids.add(_etcid)
                                        _ec = str(getattr(_em, "content", "") or "")
                                        if _ec.lstrip().startswith("[Context:"):
                                            _existing_ctx.add(_ec.strip())
                                    for _tm in turn_context_messages_since_last_user(getattr(agent, "history", []) or [], _user_input):
                                        _role = _tm.get("role")
                                        _content = str(_tm.get("content") or "")
                                        _tcid = _tm.get("tool_call_id")
                                        _tcids = [(_c or {}).get("id") for _c in (_tm.get("tool_calls") or []) if (_c or {}).get("id")]
                                        if _role == "tool" and _tcid and _tcid in _result_ids:
                                            continue
                                        if _role == "assistant" and _tcids and all(_id in _call_ids for _id in _tcids):
                                            continue
                                        if (_role == "system"
                                                and _content.lstrip().startswith("[Context:")
                                                and _content.strip() in _existing_ctx):
                                            continue
                                        session.add_message(
                                            role=_role,
                                            content=_content,
                                            tool_calls=_tm.get("tool_calls"),
                                            tool_call_id=_tm.get("tool_call_id"),
                                            name=_tm.get("name"),
                                        )
                                        if _tcid:
                                            _result_ids.add(_tcid)
                                        for _id in _tcids:
                                            _call_ids.add(_id)
                                        if _content.lstrip().startswith("[Context:"):
                                            _existing_ctx.add(_content.strip())
                                except Exception as _ctx_e:
                                    # Never silent: a turn whose steps are missing from the saved
                                    # chat has to be explainable from the logs afterwards.
                                    append_domain_log("headless", f"turn context not persisted for "
                                                      f"{task.session_id}: {type(_ctx_e).__name__}: {_ctx_e}")
                                if _clean_response:
                                    session.add_message(role="assistant", content=_clean_response,
                                                        metadata=_turn_meta)
                                session_mgr.save(session)
                                if is_debug_logging_enabled():
                                    logging.getLogger(__name__).debug(f"Session saved: +1 user, +1 assistant for {task.session_id}, turn_count={session.runtime_state.get('user_turn_count', 0)}")
                            elif _clean_response:
                                last_assistant_msg = None
                                for msg in reversed(session.messages):
                                    if _msg_role(msg) == "assistant":
                                        last_assistant_msg = _msg_content(msg)
                                        break
                                if last_assistant_msg != _clean_response:
                                    session.add_message(role="assistant", content=_clean_response,
                                                        metadata=_turn_meta)
                                # sync_state=True ensures the ContextManager state is saved to runtime_state
                                session_mgr.save(session, sync_state=True)
                    except Exception as e:
                        logging.getLogger(__name__).warning(f"Failed to save session: {e}")
                        _post_chat_ok = False  # Skip memory-intensive operations
                        gc.collect()  # Defensive GC on error
                    try:
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                                f.write(f"{_dt.now().isoformat()} SESSION_SAVE_END session_id={task.session_id} ok={_post_chat_ok}\n")
                    except Exception:
                        pass

                    # 2. Auto-capture: DISABLED - causes 13GB+ memory spikes
                    # The memory leak occurs during pipeline.ingest() ~13 seconds after EMBED_DONE
                    # TODO: Investigate asyncpg/SQLAlchemy memory leak in auto_capture_memory()
                    if False and _post_chat_ok:  # HARD DISABLED
                        try:
                            if is_debug_logging_enabled():
                                from datetime import datetime as _dt
                                with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                                    f.write(f"{_dt.now().isoformat()} AUTO_CAPTURE_START session_id={task.session_id}\n")
                        except Exception:
                            pass
                        try:
                            from vaf.memory.rag import run_auto_capture_sync
                            from uuid import UUID
                            _scope = (task.metadata or {}).get("user_scope_id") if getattr(task, "metadata", None) else None
                            if _scope is not None and not isinstance(_scope, UUID):
                                try:
                                    _scope = UUID(str(_scope))
                                except (ValueError, TypeError):
                                    _scope = None
                            if _scope is not None:
                                _assistant_text = "".join(response_parts) if response_parts else str(response_text or "")
                                run_auto_capture_sync(input_text or "", _assistant_text, _scope)
                        except Exception as e:
                            logging.getLogger(__name__).debug(f"Auto-capture skipped: {e}")
                            _post_chat_ok = False
                            gc.collect()
                        try:
                            if is_debug_logging_enabled():
                                from datetime import datetime as _dt
                                with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                                    f.write(f"{_dt.now().isoformat()} AUTO_CAPTURE_END session_id={task.session_id}\n")
                        except Exception:
                            pass

                    # 3. Session compaction. The owner's own chats (Web, Telegram, WhatsApp, Discord)
                    #    learn into the general lane; a chat with a contact learns into that chat's
                    #    own namespace, which no other lane ever reads (vaf/memory/lanes.py). The
                    #    Front Office answer and the learning are one gate: the agent may answer here.
                    if _post_chat_ok:
                        try:
                            if is_debug_logging_enabled():
                                from datetime import datetime as _dt
                                with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                                    f.write(f"{_dt.now().isoformat()} COMPACTION_CHECK_START session_id={task.session_id}\n")
                        except Exception:
                            pass
                        try:
                            from uuid import UUID
                            from vaf.memory.lanes import ChatNamespace
                            from vaf.core.config import Config
                            _task_meta = (task.metadata or {}) if getattr(task, "metadata", None) else {}
                            if Config.get("memory_enabled", True) and Config.get("memory_compaction_enabled", True):
                                # CRITICAL: Use PERSISTENT turn_count from session.runtime_state
                                try:
                                    _session_for_count = session_mgr.load(task.session_id)
                                    _runtime = getattr(_session_for_count, 'runtime_state', None) or {}
                                    turn_count = _runtime.get("user_turn_count", 0)
                                except Exception:
                                    turn_count = len([m for m in agent.history if _msg_role(m) == "user"])
                                _scope = (task.metadata or {}).get("user_scope_id") if getattr(task, "metadata", None) else None
                                if _scope is not None and not isinstance(_scope, UUID):
                                    try:
                                        _scope = UUID(str(_scope))
                                    except (ValueError, TypeError):
                                        _scope = None
                                _chat_ns = ChatNamespace.from_task(task.session_id, _task_meta)
                                tq.add(
                                    task.session_id,
                                    "__COMPACTION__",
                                    source="web",
                                    priority=15,
                                    metadata={"compaction": True, "user_scope_id": str(_scope) if _scope else None,
                                              "turn_count": turn_count, **(_chat_ns.as_meta() if _chat_ns else {})},
                                )
                        except Exception as e:
                            logging.getLogger(__name__).warning("Session compaction failed: %s", e)
                        try:
                            if is_debug_logging_enabled():
                                from datetime import datetime as _dt
                                with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                                    f.write(f"{_dt.now().isoformat()} COMPACTION_CHECK_END session_id={task.session_id}\n")
                        except Exception:
                            pass

                    # Result is already added to history and broadcast to WebUI by agent logic
                    _duration = time.time() - _chat_start
                    try:
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                                f.write(f"{_dt.now().isoformat()} QUEUE_CHAT_END session_id={task.session_id} duration_sec={_duration:.1f}\n")
                    except Exception:
                        pass
                    print("[Headless] Task complete.")

                    # Memory cleanup: clear response parts list
                    if 'response_parts' in dir():
                        response_parts.clear()

                except _StopGenerationRequested:
                    try:
                        tq.clear_stop(task.session_id)
                    except Exception:
                        pass
                    # Capture whatever partial content streamed before the stop
                    _partial = ""
                    try:
                        if "response_parts" in dir():
                            _partial = "".join(response_parts).strip()
                            response_parts.clear()
                    except Exception:
                        pass
                    try:
                        # Clear the streaming bubble so no partial text leaks through
                        # (chunks already queued in the asyncio loop arrive before this,
                        # then this wipes them, then the final message appears cleanly)
                        get_web_interface().emit_clear_last_assistant(task.session_id)
                        final_stop_content = (_partial + "\n\n*[Generation stopped]*").strip() if _partial else "*[Generation stopped]*"
                        get_web_interface().emit_agent_message(
                            role="assistant",
                            content=final_stop_content,
                            session_id=task.session_id,
                        )
                        get_web_interface().emit_message_complete(
                            content=final_stop_content,
                            session_id=task.session_id,
                        )
                    except Exception:
                        pass
                    try:
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                                f.write(f"{_dt.now().isoformat()} QUEUE_CHAT_STOPPED session_id={task.session_id}\n")
                    except Exception:
                        pass
                except Exception as e:
                    _duration = time.time() - _chat_start
                    try:
                        if is_debug_logging_enabled():
                            from datetime import datetime as _dt
                            err_preview = str(e).replace("\n", " ")[:120]
                            with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                                f.write(f"{_dt.now().isoformat()} QUEUE_CHAT_FAIL session_id={task.session_id} duration_sec={_duration:.1f} error={repr(err_preview)}\n")
                    except Exception:
                        pass
                    print(f"[Headless] Error during chat step: {e}")
                    traceback.print_exc()
                    try:
                        tb = traceback.format_exc()
                        get_web_interface().log(
                            f"Chat_step failed for session {task.session_id}: {e}",
                            level="error",
                            source="System",
                            session_id=task.session_id
                        )
                        get_web_interface().log(
                            f"Traceback (chat_step):\n{tb}",
                            level="error",
                            source="System",
                            session_id=task.session_id
                        )
                    except Exception:
                        pass
                    try:
                        # Close the turn for the browser: without this, a failed
                        # chat_step leaves the stop button and loader armed until
                        # the user acts (every other exit path emits this). Empty
                        # content on purpose - the error is in the timeline via
                        # new_log, and empty skips the Auto-TTS read-out.
                        get_web_interface().emit_message_complete(
                            content="",
                            session_id=task.session_id,
                        )
                    except Exception:
                        pass
                    # A messenger turn answers its failure there too. A contact's turn gets
                    # a fixed sentence, never the exception text; the contact is the one
                    # pinned for THIS turn (still set: end_turn clears it once the task ends).
                    _send_turn_error_reply(
                        task, e, contact=getattr(agent, "_front_office_contact", None))
                try:
                    if is_debug_logging_enabled():
                        from datetime import datetime as _dt
                        with open(get_dated_log_path("queue", "log"), "a", encoding="utf-8") as f:
                            f.write(f"{_dt.now().isoformat()} QUEUE_DONE session_id={task.session_id} (chat)\n")
                except Exception:
                    pass
                tq.task_done()
            else:
                # Background maintenance runs only on primary worker to avoid duplicates.
                if worker_id != 1:
                    time.sleep(0.05)
                    continue
                # Check for sub-agent results and summarize for WebUI. Driven by the
                # <ipc-notification> push (react the instant a sub-agent finishes) with the
                # ~1s interval kept as a reliable fallback if a push was ever missed.
                now = time.time()
                # Rooms: deliver what other agents said, as a synthetic turn that is
                # bound to the mode the LOCAL user granted for that room and to the
                # account whose room it is, in a conversation of its own
                # (_room_turn_binding): never on the chat this worker served last. The
                # cursor advances only afterwards, so an interruption costs a repeat
                # rather than a lost message.
                # Rooms this machine HOLDS check in on members that have drifted
                # off - per peer, addressed to that one, so a check-in never costs
                # the whole room a turn. Host-side by construction: is_host is keyed
                # on the tenant, so an invited guest can never emit one.
                if now - last_ping_sweep >= PING_SWEEP_SECONDS:
                    last_ping_sweep = now
                    try:
                        _room_ping_sweep()
                    except Exception as _ping_err:
                        append_domain_log_always(
                            "headless", f"[ROOM] check-in sweep failed: {_ping_err}")

                if now - last_room_check >= ROOM_POLL_SECONDS:
                    last_room_check = now
                    if now - last_room_scopes >= ROOM_SCOPES_TTL:
                        last_room_scopes = now
                        try:
                            from vaf.core.a2a.room import local_room_tenants
                            room_scopes = local_room_tenants()
                        except Exception:
                            room_scopes = []
                    _room_wake = None
                    try:
                        _room_wake = agent.collect_room_wake(scopes=room_scopes or None)
                    except Exception:
                        _room_wake = None
                    if _room_wake:
                        # And the same for a SHELL this turn might open. An agent has
                        # its own handle in a room, but `vaf a2a` acts as the machine
                        # owner by design - so an agent that reached for a shell instead
                        # of its own tool wrote under its user's name, and the task board
                        # recorded its work as the user's. Measured live: eight reports
                        # in a row.
                        #
                        # Bound to THIS room, not just to this turn, because a coder
                        # subprocess can outlive the turn: a later call in another room
                        # does not match the id and falls back to the ordinary answer.
                        # Restored on every exit path - a marker left standing is how one
                        # wrong mode used to leak into every later run.
                        from vaf.core.a2a.room import (ROOM_ACTOR_ENV, participant_key,
                                                       room_actor_value)
                        _prev_actor = os.environ.get(ROOM_ACTOR_ENV)
                        try:
                            try:
                                os.environ[ROOM_ACTOR_ENV] = room_actor_value(
                                    _room_wake["room_id"],
                                    participant_key("agent", _room_wake.get("scope")))
                            except Exception:
                                pass
                            _room_started = False
                            try:
                                # The turn RUNS AS the account whose room it is, in a
                                # conversation of its own: without this the prompt, the
                                # memory seed, the workspace and the tool set are built for
                                # whoever was bound last, on that person's chat. An account
                                # that no longer stands gets no room turn (TurnRefused).
                                # resolve_scope_identity and bind_turn are module-level
                                # names. Importing one of them HERE would make it
                                # function-local for the whole of run_headless_agent, and
                                # the queue lane's earlier call would raise
                                # UnboundLocalError on every turn (it happened once).
                                _room_scope = _room_wake.get("scope")
                                if _room_scope not in room_identities:
                                    room_identities[_room_scope] = resolve_scope_identity(_room_scope)
                                bind_turn(agent, _room_turn_binding(_room_wake, room_identities[_room_scope]))
                                _room_started = True
                            except Exception as _bind_err:
                                append_domain_log_always(
                                    "headless", f"[ROOM] turn not started: {_bind_err}")
                            if _room_started:
                                # A room turn is a turn, so it gets what a turn gets. This was
                                # the half that was missing: the chat lane fills `memory_context`
                                # a thousand lines up and the room lane filled nothing, so the
                                # agent answered its own room knowing none of what it had ever
                                # been told. Same primitive, same account, and the query is what
                                # was just SAID rather than the instructions wrapped around it.
                                _room_memory = ""
                                try:
                                    from uuid import UUID as _UUID
                                    from vaf.memory.rag import turn_memory_context
                                    _rs = _room_wake.get("scope")
                                    _room_memory = turn_memory_context(
                                        _room_wake.get("query") or "",
                                        user_scope_id=(_UUID(str(_rs)) if _rs else None),
                                        caller="room")
                                except Exception:
                                    _room_memory = ""
                                try:
                                    _hist_before = len(getattr(agent, "history", []) or [])
                                    _room_answer = agent.chat_step(
                                        user_input=_room_wake["prompt"],
                                        skip_input=False,
                                        disable_workflows=True,
                                        disable_tools=False,
                                        memory_context=_room_memory or None,
                                    )
                                    # The turn's words must not evaporate. The prompt
                                    # tells the model to answer via room_send, but when
                                    # it answers in plain text instead there is nobody
                                    # on the other end of that text - a synthetic drain
                                    # turn has no chat window - and the room saw an
                                    # agent think for a minute and say nothing. Live
                                    # incident: a direct question in the room, a
                                    # finished 264-character answer, zero tool calls,
                                    # and the answer vanished; the owner read it as the
                                    # agent being dead. If the turn ran no room_send,
                                    # its text IS the answer and is delivered as one.
                                    deliver_stray_room_answer(
                                        agent, _room_wake, _room_answer, _hist_before)
                                except Exception as _room_err:
                                    append_domain_log_always(
                                        "headless", f"[ROOM] delivery failed: {_room_err}"
                                    )
                                else:
                                    # AFTER the turn and only when it ran: learning from a turn
                                    # that failed would store half a conversation as fact. Its
                                    # own interval decides whether anything happens, exactly as
                                    # on the chat lane - this is a nudge, not a schedule.
                                    try:
                                        _room_wake["learn"]()
                                    except Exception as _learn_err:
                                        append_domain_log_always(
                                            "headless", f"[ROOM] learning skipped: {_learn_err}")
                        finally:
                            if _prev_actor is None:
                                os.environ.pop(ROOM_ACTOR_ENV, None)
                            else:
                                os.environ[ROOM_ACTOR_ENV] = _prev_actor
                            end_turn(agent)
                            try:
                                _room_wake["advance"]()
                            except Exception:
                                pass
                _pushed = False
                try:
                    _pushed = take_result_notification()
                except Exception:
                    _pushed = False
                if _pushed or (now - last_subagent_check >= 1.0):
                    last_subagent_check = now
                    _drain_claims = set()
                    _drain_session = None   # the chat whose result is being processed
                    try:
                        # Drain EVERY session's results here (not just the runner's stale
                        # "current" one) — each is routed by its own session_id below via
                        # load_session_context, so a push for any session reacts at once.
                        pending_results = agent._check_subagent_results(all_sessions=True)
                        # A result that names no chat is delivered into none: it stays pending,
                        # logged once (_split_results_by_chat).
                        pending_results, _no_chat = _split_results_by_chat(pending_results)
                        for _rt in _no_chat:
                            if _rt.task_id not in _unrouted_results:
                                _unrouted_results.add(_rt.task_id)
                                append_domain_log_always(
                                    "headless",
                                    f"[DRAIN] no chat for this result: it stays pending "
                                    f"(agent={_rt.agent_type} task={_rt.task_id} "
                                    f"room={getattr(_rt, 'room_id', None) or '-'})")
                        # Claim each result's session before touching it: this drain runs
                        # OUTSIDE the TaskQueue, so without the claim its load_session_context
                        # + summary chat_step would race a worker's concurrent chat turn on the
                        # same session (parallel_main_workers>1). A busy session's results stay
                        # pending and are picked up on a later pass once it frees up.
                        if pending_results:
                            _kept_results = []
                            for _rt in pending_results:
                                _rs = str(_rt.session_id or "")
                                if _rs in _drain_claims or tq.try_claim_session(_rs):
                                    if _rs:
                                        _drain_claims.add(_rs)
                                    _kept_results.append(_rt)
                            pending_results = _kept_results
                        if pending_results:
                            found_results_text = []
                            any_needs_retry = False
                            cancelled_sessions = set()
                            for result_task in pending_results:
                                # Ensure agent context is aligned with result session
                                session_id = _result_chat(result_task)
                                _drain_session = session_id
                                if session_id:
                                    agent.load_session_context(session_id)
                                    subagent_last_activity[session_id] = now
                                err_text = str(getattr(result_task, "error", "") or "")
                                err_lower = err_text.lower()
                                is_user_cancelled = (
                                    result_task.status == "failed"
                                    and (
                                        "[user_cancelled]" in err_lower
                                        or "stopped/cancelled by user via stop button" in err_lower
                                        or "stopped by user via stop button" in err_lower
                                        or "cancelled by user via stop button" in err_lower
                                    )
                                )
                                if agent._process_subagent_result(result_task):
                                    any_needs_retry = True
                                
                                # Ensure WebUI opens the Sub-Agent window even if no active task was seen.
                                # workflow:* tasks belong to the WorkflowRuntime panel — skip ONLY the
                                # SubAgentWindow update, but still allow found_results_text to be populated.
                                _is_workflow_task = str(getattr(result_task, 'agent_type', '') or '').startswith('workflow:')
                                sid = session_id
                                if sid and not _is_workflow_task:
                                    status_label = "Completed"
                                    if is_user_cancelled:
                                        status_label = "Stopped/Cancelled"
                                    elif result_task.status == "failed":
                                        status_label = "Failed"
                                    elif result_task.status == "timeout":
                                        status_label = "Timed out"
                                    presence = "idle" if is_user_cancelled else ("error" if result_task.status in ("failed", "timeout") else "idle")
                                    provider, model = _get_subagent_model_info()
                                    step_status = "timeout" if is_user_cancelled else ("completed" if result_task.status in ("completed", "failed", "timeout") else "running")
                                    steps = [{
                                        "id": result_task.task_id,
                                        "title": result_task.agent_type.replace("_", " ").title(),
                                        "description": result_task.task_description,
                                        "status": step_status,
                                        "actions": []
                                    }]
                                    subagent_last_steps[sid] = steps
                                    open_subagent_sessions.add(sid)
                                    subagent_last_activity[sid] = now
                                    get_web_interface()._push_session_update(sid, {
                                        "type": "subagent_update",
                                        "agentName": "Sub-Agent",
                                        "status": status_label,
                                        "presence": presence,
                                        "provider": provider,
                                        "model": model,
                                        "file": "",
                                        "code": "",
                                        "steps": steps
                                    })

                                if result_task.status == "completed":
                                    try:
                                        output_text = str(result_task.result or "")
                                        if output_text:
                                            max_len = 8000
                                            if len(output_text) > max_len:
                                                output_text = output_text[:max_len] + "\n... [Output Truncated]\n"
                                            if session_id:
                                                get_web_interface()._push_session_update(session_id, {
                                                "type": "subagent_output",
                                                "taskId": result_task.task_id,
                                                "agentType": result_task.agent_type,
                                                "status": "completed",
                                                "output": output_text
                                                })
                                    except Exception:
                                        pass
                                    found_results_text.append(
                                        f"Sub-Agent '{result_task.agent_type}' completed:\n{result_task.result}"
                                    )
                                elif is_user_cancelled:
                                    try:
                                        if session_id:
                                            cancelled_sessions.add(session_id)
                                            get_web_interface()._push_session_update(session_id, {
                                                "type": "subagent_output",
                                                "taskId": result_task.task_id,
                                                "agentType": result_task.agent_type,
                                                "status": "timeout",
                                                "output": "Sub-Agent stopped/cancelled by user."
                                            })
                                    except Exception:
                                        pass
                                elif result_task.status == "failed":
                                    try:
                                        error_text = str(result_task.error or "")
                                        if error_text:
                                            if session_id:
                                                get_web_interface()._push_session_update(session_id, {
                                                    "type": "subagent_output",
                                                    "taskId": result_task.task_id,
                                                    "agentType": result_task.agent_type,
                                                    "status": "failed",
                                                    "output": error_text
                                                })
                                    except Exception:
                                        pass
                                    found_results_text.append(
                                        f"Sub-Agent '{result_task.agent_type}' FAILED:\n{result_task.error}"
                                    )
                                elif result_task.status == "timeout":
                                    try:
                                        if session_id:
                                            get_web_interface()._push_session_update(session_id, {
                                                "type": "subagent_output",
                                                "taskId": result_task.task_id,
                                                "agentType": result_task.agent_type,
                                                "status": "timeout",
                                                "output": "Sub-Agent timed out."
                                            })
                                    except Exception:
                                        pass
                                    found_results_text.append(
                                        f"Sub-Agent '{result_task.agent_type}' TIMEOUT."
                                    )

                            if cancelled_sessions:
                                try:
                                    for sid in cancelled_sessions:
                                        tq.clear_stop(sid)
                                except Exception:
                                    pass
                            # The summary turn runs on the chat the results were loaded for (the
                            # last result's own chat, never whatever the worker holds), as that
                            # chat's owner with its source (_drain_turn_binding). No chat known:
                            # no summary turn, because a reply emitted without a session reaches
                            # every connected browser. A chat that is gone, or an account without
                            # access, gets none either.
                            _drain_bound = False
                            if found_results_text or any_needs_retry:
                                _drain_sid = _drain_session
                                if not _drain_sid:
                                    append_domain_log_always(
                                        "headless", "[DRAIN] no chat for this result: no summary turn")
                                else:
                                    try:
                                        bind_turn(agent, _drain_turn_binding(session_mgr, _drain_sid))
                                        _drain_bound = True
                                    except TurnRefused as _refused:
                                        append_domain_log_always(
                                            "headless", f"[DRAIN] no summary turn for {_drain_sid}: "
                                                        f"{_refused}")
                            if _drain_bound:
                                user_lang = "auto"
                                for msg in reversed(agent.history):
                                    if msg.get("role") == "user":
                                        user_lang = agent._detect_user_language(msg.get("content", ""))
                                        break

                                native_lang = agent.LANGUAGE_NAMES_NATIVE.get(user_lang, user_lang)
                                # Coding/workflow results can be ~2000 chars — keep them whole so path+files survive.
                                _is_coding_result = any(
                                    str(getattr(_pt, 'agent_type', '') or '').startswith(('coding_agent', 'workflow:'))
                                    for _pt in pending_results
                                )
                                _per_limit = 4000 if _is_coding_result else 1000
                                combined_results = "\n\n---\n\n".join(r[:_per_limit] for r in found_results_text) if found_results_text else ""

                                if any_needs_retry and getattr(agent, "_pending_user_question", None):
                                    # Ask-first invariant (incident 2026-07-13): the agent asked the user a
                                    # question that is still unanswered - the retry command below drove
                                    # repeated unconsented re-delegation. Report status instead; a retry can
                                    # happen after the user replies.
                                    if user_lang == "de":
                                        instruction_prompt = (
                                            "Das Sub-Agent-Ergebnis erfüllt die Anfrage nicht, aber du wartest gerade "
                                            "auf die Antwort des Nutzers auf deine offene Frage.\n"
                                            "Starte KEINEN neuen Versuch und keine neuen Aktionen. Fasse kurz und ehrlich "
                                            "zusammen, was das Ergebnis war, und warte auf die Antwort. ANTWORTE AUF DEUTSCH."
                                        )
                                    else:
                                        instruction_prompt = (
                                            "The sub-agent result did not fulfill the request, but you are currently "
                                            "waiting for the user's answer to your open question.\n"
                                            "Do NOT retry and do not start new actions. Briefly and honestly summarize "
                                            "what the result was, then wait for their reply."
                                        )
                                elif any_needs_retry:
                                    if user_lang == "de":
                                        instruction_prompt = (
                                            "Der Sub-Agent-Ergebnis erfüllt die Anfrage des Benutzers NICHT.\n"
                                            "Du MUSST den Sub-Agent SOFORT erneut mit der genauen Aufgabe aufrufen, die in der Background Intelligence oben steht.\n"
                                            "Fasse NICHT zusammen. Rufe das Tool JETZT auf."
                                        )
                                    else:
                                        instruction_prompt = (
                                            "The sub-agent result did NOT fulfill the user's request.\n"
                                            "You MUST retry immediately by calling the sub-agent again with the exact task specified in the Background Intelligence above.\n"
                                            "Do NOT summarize. Call the tool now."
                                        )
                                elif _is_coding_result:
                                    # Coding/workflow task: agent MUST relay file info before anything else.
                                    if user_lang == "de":
                                        instruction_prompt = (
                                            f"Der Coding Agent / Workflow hat folgendes Ergebnis geliefert:\n\n"
                                            f"{combined_results}\n\n"
                                            f"**Antworte dem User JETZT auf DEUTSCH** — in dieser Reihenfolge:\n"
                                            f"1. Wo die Datei(en) liegen (vollständiger Pfad)\n"
                                            f"2. Dateiname(n) und Größe\n"
                                            f"3. Wie er/sie es öffnet oder nutzt\n"
                                            f"Erst DANACH darfst du kurz auf Qualitätsprobleme hinweisen — aber nur wenn wirklich nötig.\n"
                                            f"Rufe KEINE Tools auf bevor du geantwortet hast. Wiederhole NICHT deine vorherige Nachricht - "
                                            f"antworte NUR auf dieses Ergebnis. ANTWORTE AUSSCHLIESSLICH AUF DEUTSCH."
                                        )
                                    else:
                                        instruction_prompt = (
                                            f"The coding agent / workflow delivered the following result:\n\n"
                                            f"{combined_results}\n\n"
                                            f"**Reply to the user NOW in {native_lang}** — in this exact order:\n"
                                            f"1. Where the file(s) are located (full path)\n"
                                            f"2. File name(s) and size\n"
                                            f"3. How to open or use it\n"
                                            f"Only AFTER that may you briefly mention quality issues — and only if truly necessary.\n"
                                            f"Do NOT call any tools before replying. Do NOT repeat your previous message - "
                                            f"respond ONLY to this result. RESPOND EXCLUSIVELY IN {native_lang.upper()}."
                                        )
                                elif user_lang == "de":
                                    instruction_prompt = (
                                        "Hier sind die Ergebnisse der Sub-Agenten:\n\n"
                                        f"{combined_results}\n\n"
                                        "Bitte erstelle eine KURZE ZUSAMMENFASSUNG dieser Ergebnisse für den Benutzer auf DEUTSCH.\n"
                                        "Konzentriere dich auf den Inhalt (was wurde gefunden/getan).\n"
                                        "Bleib prägnant aber informativ.\n"
                                        "Du kannst `read_file` nutzen, wenn du den Inhalt sehen musst.\n"
                                        "Schreibe NUR die fertige Nachricht an den Nutzer - keine Überlegungen, "
                                        "kein Meta-Kommentar, kein Denkprozess.\n"
                                        "Wiederhole NICHT deine vorherige Nachricht. ANTWORTE AUSSCHLIESSLICH AUF DEUTSCH."
                                    )
                                else:
                                    instruction_prompt = (
                                        "The sub-agent(s) have completed their tasks.\n\n"
                                        f"**RESULTS:**\n{combined_results}\n\n"
                                        f"Please provide a BRIEF SUMMARY of these results for the user in {native_lang}.\n"
                                        "Focus on the content (what was found/done).\n"
                                        "Keep it concise but informative.\n"
                                        "You may use `read_file` if you need to see the content before summarizing.\n"
                                        "Write ONLY the final user-facing message - no deliberation, no meta "
                                        "commentary, no thinking out loud.\n"
                                        f"Do NOT repeat your previous message. RESPOND EXCLUSIVELY IN {native_lang.upper()}."
                                    )

                                response_parts = []

                                def webui_stream_callback(text):
                                    response_parts.append(text)
                                    full_text = "".join(response_parts)
                                    if full_text.strip():
                                        get_web_interface().emit_agent_message(
                                            "assistant",
                                            full_text,
                                            session_id=getattr(agent, "current_session_id", None)
                                        )

                                # A SYNTHETIC background turn, bound so (_drain_turn_binding): the
                                # ask-first gate keys on it, and chat_step must not treat the
                                # instruction prompt as a real user message (it would clear the
                                # pending-question latch). Ended on EVERY exit path (Rule 4.5).
                                drain_summary_text = ""
                                try:
                                    # Capture the return value: chat_step returns the FINAL answer with
                                    # reasoning stripped (AGENT_LOOP design) - the messenger summary is
                                    # based on it, never on the raw stream buffer (which carried untagged
                                    # chain-of-thought to Telegram on 2026-07-13). The WebUI lane keeps
                                    # using response_parts: the browser already saw that stream (4.2).
                                    drain_summary_text = agent.chat_step(
                                        user_input=instruction_prompt,
                                        stream_callback=webui_stream_callback,
                                        skip_input=False,
                                        disable_workflows=True,
                                        disable_tools=False
                                    )
                                finally:
                                    end_turn(agent)
                                # What chat_step left about this turn: read before anything else
                                # runs a turn on the agent, like the chat lane.
                                _drain_status = agent.last_turn

                                # POST-CHAT: Emit final message + save session
                                # (mirrors main chat path – ensures browser sees the response even
                                # if WebSocket dropped during streaming, and persists it for reload)
                                _result_sid = getattr(agent, "current_session_id", None)
                                # The stored copy is the cleaned stream, marked so no later
                                # context reads a failure back (session.model_text).
                                _clean_result, _drain_meta = _drain_turn_copy(
                                    response_parts, turn=_drain_status)

                                if _clean_result:
                                    # 1. Final emit – guarantees browser has complete message
                                    try:
                                        get_web_interface().emit_agent_message(
                                            "assistant",
                                            _clean_result,
                                            session_id=_result_sid
                                        )
                                    except Exception:
                                        pass
                                    # 2. message_complete – closes streaming bubble, triggers TTS
                                    try:
                                        get_web_interface().emit_message_complete(
                                            content=_clean_result,
                                            session_id=_result_sid
                                        )
                                    except Exception:
                                        pass
                                    # 3. Save to session so browser reload / reconnect shows the response
                                    try:
                                        if _result_sid:
                                            _saved_sess = session_mgr.load(_result_sid)
                                            _saved_sess.add_message(
                                                role="assistant", content=_clean_result,
                                                metadata=_drain_meta)
                                            session_mgr.save(_saved_sess)
                                    except Exception:
                                        pass
                                else:
                                    # DELIVERY GUARANTEE: the model produced no usable summary
                                    # (think-only stream / rehash stripped to nothing). The result
                                    # must NEVER be silently dropped — the user watched a stream
                                    # that then vanished on reload and the thread never
                                    # acknowledged the sub-agent's work. Deliver a deterministic
                                    # message built from the raw result instead.
                                    _fb_head = ("Der Sub-Agent ist fertig. Ergebnis:"
                                                if user_lang == "de" else "The sub-agent finished. Result:")
                                    _fb_body = (combined_results or "").strip()[:2000] or (
                                        "(no textual result payload)" if user_lang != "de" else "(kein Text-Ergebnis)")
                                    _fb_msg = f"{_fb_head}\n\n{_fb_body}"
                                    try:
                                        get_web_interface().emit_agent_message("assistant", _fb_msg, session_id=_result_sid)
                                        get_web_interface().emit_message_complete(content=_fb_msg, session_id=_result_sid)
                                    except Exception:
                                        pass
                                    try:
                                        if _result_sid:
                                            _saved_sess = session_mgr.load(_result_sid)
                                            _saved_sess.add_message(role="assistant", content=_fb_msg)
                                            session_mgr.save(_saved_sess)
                                    except Exception:
                                        pass

                                # Send subagent summary to Telegram/Discord if this session originated from there.
                                # Same sanitizer chain as the normal reply path (the old hand-copied short
                                # chain here let 1034 chars of untagged chain-of-thought reach Telegram),
                                # and a DETERMINISTIC fallback instead of the old noise placeholder: the
                                # drain owns delivery (4.3) - it must send something useful, never noise
                                # and never nothing.
                                try:
                                    sid = getattr(agent, "current_session_id", None)
                                    if sid:
                                        session = session_mgr.load(sid)
                                        out = _prepare_channel_outbound(drain_summary_text or "")
                                        if not out:
                                            out = _prepare_channel_outbound("".join(response_parts))
                                        if not out:
                                            _mb_head = ("Der Sub-Agent ist fertig. Ergebnis:"
                                                        if user_lang == "de" else "The sub-agent finished. Result:")
                                            _mb_body = (combined_results or "").strip()[:2000] or (
                                                "(kein Text-Ergebnis)" if user_lang == "de" else "(no textual result payload)")
                                            out = _prepare_channel_outbound(f"{_mb_head}\n\n{_mb_body}") or _mb_head
                                        chat_id = session.metadata.get("telegram_chat_id")
                                        if chat_id:
                                            from vaf.core.telegram_reply import send_telegram_reply
                                            send_telegram_reply(str(chat_id), out)
                                        else:
                                            discord_channel_id = session.metadata.get("discord_channel_id")
                                            if discord_channel_id:
                                                from vaf.core.discord_reply import send_discord_reply
                                                send_discord_reply(str(discord_channel_id), out)
                                            else:
                                                whatsapp_chat_jid = session.metadata.get("whatsapp_chat_jid")
                                                if whatsapp_chat_jid:
                                                    from vaf.core.whatsapp_reply import send_whatsapp_reply
                                                    send_whatsapp_reply(
                                                        session.metadata.get("username") or "admin",
                                                        str(whatsapp_chat_jid),
                                                        out,
                                                        user_scope_id=session.metadata.get("user_scope_id") and str(session.metadata["user_scope_id"]) or None,
                                                    )
                                except Exception as e:
                                    logging.getLogger(__name__).warning(
                                        "Failed to send subagent summary to Telegram/Discord: %s", e
                                    )
                    except Exception as e:
                        import traceback as _tb
                        print(f"[Headless] Sub-agent result processing error: {e}")
                        # Emit a fallback notification so the browser is not left waiting
                        drain_failure_notice(_drain_session, e)
                    finally:
                        # Always release drain claims so a skipped/failed pass can never
                        # deadlock the session's queued chat turns.
                        for _cs in _drain_claims:
                            try:
                                tq.release_session_claim(_cs)
                            except Exception:
                                pass

                # Periodically update Sub-Agent window state for WebUI
                if now - last_subagent_ui_update >= 1.0:
                    last_subagent_ui_update = now
                    try:
                        from vaf.core.subagent_ipc import get_ipc
                        ipc = get_ipc()
                        active_tasks = ipc.get_active_tasks()
                        # Skip workflow:* tasks — they are already displayed in the
                        # VAFWorkflowRuntime panel and must not open the SubAgentWindow.
                        active_tasks = [t for t in active_tasks if not str(getattr(t, 'agent_type', '') or '').startswith('workflow:')]
                        if active_tasks:
                            tasks_by_session = {}
                            for task in active_tasks:
                                # Shown in the task's own chat only: a task that names none
                                # (a room's, an automation's) has no window to show it in.
                                sid = _result_chat(task)
                                if not sid:
                                    continue
                                tasks_by_session.setdefault(sid, []).append(task)

                            for sid, tasks in tasks_by_session.items():
                                subagent_last_activity[sid] = now
                                open_subagent_sessions.add(sid)
                                provider, model = _get_subagent_model_info()
                                steps = []
                                heartbeat_ages = []
                                for task in tasks:
                                    status = "running"
                                    if task.status == "completed":
                                        status = "completed"
                                    elif task.status == "pending":
                                        status = "pending"
                                    try:
                                        hb = getattr(task, "last_heartbeat", None)
                                        if hb:
                                            from datetime import datetime as _dt
                                            age = max(0.0, (now - _dt.fromisoformat(hb).timestamp()))
                                            heartbeat_ages.append(int(age))
                                    except Exception:
                                        pass
                                    steps.append({
                                        "id": task.task_id,
                                        "title": task.agent_type.replace("_", " ").title(),
                                        "description": task.task_description,
                                        "status": status,
                                        "actions": []
                                    })

                                subagent_last_steps[sid] = steps
                                hb_status = ""
                                if heartbeat_ages:
                                    hb_status = f" (heartbeat {min(heartbeat_ages)}s ago)"
                                get_web_interface()._push_session_update(sid, {
                                    "type": "subagent_update",
                                    "agentName": "Sub-Agent",
                                    "status": f"Running sub-agent tasks...{hb_status}",
                                    "presence": "online",
                                    "provider": provider,
                                    "model": model,
                                    "file": "",
                                    "code": "",
                                    "steps": steps
                                })
                        else:
                            # Keep window visible briefly after completion to avoid flicker
                            now_ts = time.time()
                            for sid in list(open_subagent_sessions):
                                last_seen = subagent_last_activity.get(sid, 0.0)
                                provider, model = _get_subagent_model_info()
                                if now_ts - last_seen <= 15.0:
                                    get_web_interface()._push_session_update(sid, {
                                        "type": "subagent_update",
                                        "agentName": "Sub-Agent",
                                        "status": "Completed",
                                        "presence": "idle",
                                        "provider": provider,
                                        "model": model,
                                        "file": "",
                                        "code": "",
                                        "steps": subagent_last_steps.get(sid, [])
                                    })
                                else:
                                    get_web_interface()._push_session_update(sid, {
                                        "type": "subagent_update",
                                        "agentName": "Sub-Agent",
                                        "status": "Idle",
                                        "presence": "idle",
                                        "provider": provider,
                                        "model": model,
                                        "file": "",
                                        "code": "",
                                        "steps": []
                                    })
                                    open_subagent_sessions.discard(sid)
                                    subagent_last_activity.pop(sid, None)
                                    subagent_last_steps.pop(sid, None)
                    except Exception as e:
                        print(f"[Headless] Sub-agent UI update error: {e}")

                # Sleep briefly to avoid busy loop
                time.sleep(0.1)

                # Periodic memory check
                _check_and_cleanup_memory()

        except Exception as e:
            print(f"[Headless] Loop error: {e}")
            time.sleep(1)
        finally:
            # Every exit path of every lane, the early `continue`s included, ends with the
            # markers neutral: what runs between two turns (the drain's result handling,
            # the room sweep) is no turn and gates and routes as none.
            end_turn(agent)

def _handle_command(cmd_str, agent, session_mgr):
    """Handle system commands from Web UI."""
    try:
        parts = cmd_str.strip().split(":")
        if len(parts) < 2: return
        
        cmd_type = parts[1]
        
        if cmd_type == "NEW_SESSION":
            # Agent/Session logic is handled by Web Server mostly, 
            # but we might need to reset agent state
            agent.init_chat()
            
        elif cmd_type == "LOAD_SESSION" and len(parts) >= 3:
            sid = parts[2].strip()
            # Explicitly load the session context (history + runtime state)
            agent.load_session_context(sid)
            print(f"[Headless] LOAD_SESSION: Switched agent context to {sid}")

        elif cmd_type == "RENAME_SESSION":
            # Format: __CMD__:RENAME_SESSION:id:new_name. The disk write
            # already happened (SessionManager.rename in the WS handler);
            # this repairs the copy THIS worker may hold in memory, so a
            # later full-object save cannot write the old name back over
            # the rename - the same repair the interactive lanes carry.
            # This branch was missing: the command was silently swallowed.
            try:
                cmd_parts = cmd_str.split(":", 3)
                sid = cmd_parts[2].strip()
                new_name = cmd_parts[3].strip()
                cur = getattr(session_mgr, "_current", None)
                if cur is not None and str(getattr(cur, "id", "")) == sid:
                    cur.name = new_name
            except Exception:
                pass

        elif cmd_type == "RELOAD_CONFIG":
            # The backend swap belongs to the agent, not to whoever happens to receive the
            # command: Agent.reload_api_backend does it with the swap lock, the sub-agent
            # pin and embedded guards, the embedded-key path (_build_api_backend), the
            # event-sink reattach, the local teardown (stop_server, so the GGUF leaves
            # VRAM), the tokenizer reset and the model-name refresh. This runner used to
            # reimplement it and had none of that. force=True because RELOAD_CONFIG does
            # not know WHICH key changed - without it a pure API-key change is a no-op.
            #
            # Broadcast, because this command arrives on ONE worker's command loop while
            # the pool may hold several agents; re-applying only to `agent` left the rest
            # of the pool on the previous key until a restart.
            from vaf.core.config import Config
            from vaf.core.agent import reload_all_api_backends
            agent.config = Config.load()   # non-provider keys, as before
            old_provider = getattr(agent, "provider", "local")
            changed = reload_all_api_backends(force=True)
            print(f"[Headless] RELOAD_CONFIG: old={old_provider} "
                  f"new={getattr(agent, 'provider', 'local')} agents_changed={changed}")
            if changed:
                # Reflect the (possibly) new backend in the UI. Gated on the agent's own
                # verdict rather than a provider comparison, so a key-only swap counts too.
                try:
                    used, total = agent.get_token_usage()
                    stats = {
                        "used": used,
                        "total": total,
                        "percent": round((used / total) * 100, 1) if total else 0.0,
                        "api": bool(getattr(agent, 'api_backend', False))
                    }
                    get_web_interface().emit_stats(stats)
                    print(f"[Headless] Stats updated: api={stats['api']}")
                except Exception:
                    pass
            
    except Exception as e:
        print(f"[Headless] Command error: {e}")
