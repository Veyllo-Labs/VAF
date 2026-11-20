# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
VAF Automation System - Time-based task automation
Cross-Platform: Windows, macOS, Linux

Features:
- Schedule tasks at specific times (daily, weekly, hourly)
- Model can create automations via coding_agent
- Clarification prompts for incomplete tasks
- Animated terminal execution
"""
import os
import sys
import json
import math
import uuid
import subprocess
import threading
import time
import copy
from contextlib import contextmanager
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Any, Callable, Tuple
from dataclasses import dataclass, field, asdict
from enum import Enum
from vaf.core.identity_binding import bind_identity, resolve_scope_identity
from vaf.core.channels import ALL_SEND_TOOLS, CHAT_CHANNELS
from vaf.core.log_helper import append_domain_log, append_domain_log_always
from vaf.core.subagent_ipc import get_current_session_id, set_current_session_id


# ═══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

class Frequency(str, Enum):
    """Task execution frequency."""
    ONCE = "once"
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    # Not a clock at all: the task runs when something happens in a room it names
    # (`AutomationTask.trigger`, vaf/core/automation_triggers.py). It has no next
    # clock run, and the scheduler registers no job for it.
    ON_EVENT = "on_event"


# On-disk format tag of a task file. Written on every save; the loader keeps reading
# untagged files, which is what every task written before the tag existed is. Pinned
# as a literal in tests/test_persisted_format_tags.py.
AUTOMATION_FORMAT = "automation-1-4e9b27"


# The weekday names a lane may be given, to the English lowercase name the clock rule reads.
_WEEKDAY_NAMES = {
    "montag": "monday", "dienstag": "tuesday", "mittwoch": "wednesday",
    "donnerstag": "thursday", "freitag": "friday", "samstag": "saturday", "sonntag": "sunday",
    "monday": "monday", "tuesday": "tuesday", "wednesday": "wednesday",
    "thursday": "thursday", "friday": "friday", "saturday": "saturday", "sunday": "sunday",
}
# The words that say off or on, after folding (`read_enabled`). Anything else is not
# understood, and an automation is then off: a proposal or a request that meant "off" in a
# word this list lacks must not leave a task running on a schedule.
_ENABLED_FALSE = frozenset({"false", "0", "no", "off", "", "nein", "aus", "falsch",
                            "disabled", "disable", "inactive", "inaktiv"})
_ENABLED_TRUE = frozenset({"true", "1", "yes", "on", "ja", "an", "ein", "wahr", "enabled",
                           "enable", "active", "aktiv"})
# The longest text that is folded. Every word above has at most 8 letters; folding works
# character by character, and an unbounded value (the WebSocket admits frames of many MB)
# held the event loop for seconds: 1.32 s for 1 MB, 12.85 s for 10 MB (measured).
_ENABLED_FOLD_MAX = 64


def read_enabled(value: Any) -> Optional[bool]:
    """True or False for a value that says on or off, None for one that says neither. Text is
    folded first (`agent_origin.folded_text`: compatibility forms such as a full-width
    "ｆａｌｓｅ" become plain letters, and invisible characters such as a zero-width space
    are dropped), then stripped and lower-cased; it is never `bool()`-ed, because
    `bool("false")` is True. A bool is itself, a number is off when 0; NaN and infinity say
    neither. Text longer than `_ENABLED_FOLD_MAX` is not folded and says neither: no word is
    that long, and folding it would cost time in proportion to its length."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        if len(value) > _ENABLED_FOLD_MAX:
            return None
        from vaf.core.agent_origin import folded_text

        word = folded_text(value).strip().lower()
        if word in _ENABLED_TRUE:
            return True
        if word in _ENABLED_FALSE:
            return False
    return None


def coerce_schedule_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
    """The schedule fields of an automation as the clock rule and the scheduler read them, the
    one coercion every lane goes through (`AutomationTask.__post_init__` and
    `AutomationManager.update`). A copy of `fields`, changed only where a key is present and
    not None:

    - `day`: a whole number from 1 to 31 (`int()`, so "15" and 15.0 are read, then clamped);
      dropped when it does not parse. A string day used to reach `datetime.replace`, which
      raised `TypeError` inside `list()`, and so in the scheduler's start and refresh: one
      approved proposal disarmed every account's automations.
    - `enabled`: a bool, read by `read_enabled` (text folded and parsed, never `bool()`-ed,
      which kept "false" on). A value that says neither on nor off is False: it fails
      closed, so an automation meant to be switched off in an unknown word stays off.
    - `weekday`: stripped and lower-cased, a German name mapped to the English one; "" is
      None; anything that is not text is dropped.

    `time` is left as it is: each lane formats it its own way, and `calculate_next_run`
    answers None for one it cannot read."""
    out = dict(fields)
    day = out.get("day")
    if day is not None:
        try:
            out["day"] = max(1, min(31, int(day.strip() if isinstance(day, str) else day)))
        except (TypeError, ValueError, OverflowError):
            del out["day"]
    enabled = out.get("enabled")
    if enabled is not None:
        out["enabled"] = read_enabled(enabled) is True
    weekday = out.get("weekday")
    if weekday is not None:
        if isinstance(weekday, str):
            word = weekday.strip().lower()
            out["weekday"] = _WEEKDAY_NAMES.get(word, word) or None
        else:
            del out["weekday"]
    return out


@dataclass
class AutomationTask:
    """A scheduled automation task."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = ""
    description: str = ""
    prompt: str = ""  # The prompt to send to VAF (legacy, for backwards compatibility)
    workflow_steps: List[Dict[str, Any]] = field(default_factory=list)  # Structured workflow steps (n8n-like)
    frequency: str = "daily"
    time: str = "06:00"  # HH:MM format
    weekday: Optional[str] = None  # For weekly: monday, tuesday, etc.
    day: Optional[int] = None  # For monthly: 1-31
    enabled: bool = True
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_run: Optional[str] = None
    # Local calendar day (YYYY-MM-DD) when the task last completed successfully; persisted in JSON
    # so "Done (today)" survives app restarts until the calendar advances.
    last_completed_local_date: Optional[str] = None
    # next_run is now calculated dynamically - no longer stored
    # Kept for backwards compatibility when loading old files
    next_run: Optional[str] = None
    output_path: Optional[str] = None  # Where to save results
    output_format: str = "markdown"  # markdown, json, txt

    # Task parameters (filled by clarification)
    parameters: Dict[str, Any] = field(default_factory=dict)

    # User isolation: scope automations to specific users
    user_scope_id: Optional[str] = None

    # What makes an ON_EVENT task due: {"kind": "room_message" | "room_reaction",
    # "room_id": ..., "match"?: ..., "emoji"?: ..., "from"?: ..., "cursor"?: lamport}.
    # Read only through vaf.core.automation_triggers.read_trigger, never trusted raw.
    trigger: Optional[Dict[str, Any]] = None

    # Which task this is beyond its id: set by `AutomationManager.create`, never changed by an
    # update, and carried into the trash and back with the file. An id can be given to a NEW
    # task while a run of the old one is still going (a retried approval creates its planned
    # id again after the first task was deleted), and a run's bookkeeping must not land on
    # that new task. None on a record written before the field existed: two Nones are taken
    # for the same task, as every write treated them before.
    incarnation: Optional[str] = None

    def __post_init__(self):
        # The data-model boundary: every constructor and every file loaded through
        # from_dict pass here, so a record written before the coercion (a day of "15")
        # is read as the clock rule needs it. A day or weekday that does not parse becomes
        # None; `enabled` is on only when it says on (it fails closed), and an explicit
        # None stays off, as it read before.
        fields = coerce_schedule_fields(
            {"day": self.day, "enabled": self.enabled, "weekday": self.weekday})
        self.day = fields.get("day")
        self.weekday = fields.get("weekday")
        self.enabled = fields["enabled"] if self.enabled is not None else False
        self.incarnation = _incarnation(self.incarnation)
        # A scope names a directory and an account, so it is text or nothing. A number, a
        # list or a bool (a record edited by hand or by another program) made every path of
        # the task raise, and the clock tried it again on every tick. None is "no scope of
        # its own": a loader stamps the scope of the directory the file lives in, and only a
        # file in the root directory, which the machine owner's store holds, stays a root
        # task there.
        if not isinstance(self.user_scope_id, str):
            self.user_scope_id = None

    def to_dict(self) -> Dict:
        """Convert to dict, excluding next_run (calculated dynamically)."""
        data = asdict(self)
        # Don't save next_run - it's calculated dynamically
        if "next_run" in data:
            del data["next_run"]
        data["format"] = AUTOMATION_FORMAT
        return data
    
    @property
    def next_run_datetime(self) -> Optional[datetime]:
        """The next clock run, or None when the record carries no clock rule this code
        can read. Never raises: see calculate_next_run."""
        return self.calculate_next_run()
    
    @property
    def next_run_iso(self) -> str:
        """The next clock run as an ISO string, empty when there is none. The browser
        already treats an absent value as "no date", so empty is the honest wire form."""
        when = self.calculate_next_run()
        return when.isoformat() if when else ""

    @property
    def next_run_label(self) -> str:
        """`YYYY-MM-DD HH:MM` for a line a person reads, or "-" when there is no next
        clock run. The one place the format lives: it used to be spelled out at eight
        call sites, and every one of them would have raised on a record with no clock."""
        when = self.calculate_next_run()
        return when.strftime("%Y-%m-%d %H:%M") if when else "-"

    @property
    def schedule_label(self) -> str:
        """WHEN this task runs, as a person reads it: `daily at 07:15`, or for an
        event-driven task what has to happen where. The one place the phrase lives;
        it used to be `{frequency} at {time}` at five call sites, every one of which
        would have printed `on_event at ` for a task that has no clock."""
        if self.frequency == Frequency.ON_EVENT:
            from vaf.core.automation_triggers import trigger_label
            return trigger_label(self.trigger)
        return f"{self.frequency} at {self.time}"
    
    @classmethod
    def from_dict(cls, data: Dict) -> "AutomationTask":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
    
    def calculate_next_run(self, after: Optional[datetime] = None) -> Optional[datetime]:
        """Next execution time, interpreted in the OWNER's timezone, returned as a SERVER-local
        naive datetime. `after` (naive server-local) is the moment to count from; now when omitted.

        Wall-clock times (self.time) are the user's local times, so "now" is taken in the owner's
        timezone (user_identity.timezone, the single source of truth; server-local when unset). The
        result is converted back to a naive server-local datetime so sort/min over tasks and
        comparisons with naive datetime.now() never mix aware+naive (which would raise).

        This is also the scheduler's clock (AutomationManager._schedule_task): what a list or the
        calendar shows as the next run is the moment the task fires.

        None when the record carries no clock rule this code can read: an empty or malformed
        `time`, an hour or minute out of range, a day no month has, or a frequency
        this version does not know. NEVER an exception. A record is data that arrived from a
        file or a caller, and the boundary coerces rather than trusts it: one record with an
        unreadable time used to make list() raise, and list() is what the Web UI automations
        list, the CLI listing and the thinking-mode start gate all read, so a single bad file
        took every automation surface down for every user. The create path admits exactly
        such a record, because the interval check steps aside whenever the time has no colon.
        """
        from vaf.core.user_time import resolve_user_timezone, user_now
        try:
            hour, minute = (int(part) for part in str(self.time or "").split(":"))
        except (TypeError, ValueError):
            return None
        username = _resolve_username(self.user_scope_id)
        if after is None:
            now = user_now(username)
        else:
            tz = resolve_user_timezone(username)
            now = after.astimezone(tz) if tz else after
        try:
            next_time = self._next_clock_time(now, hour, minute)
        except (ValueError, OverflowError, TypeError, AttributeError) as e:
            # TypeError and AttributeError too: a day or weekday of a type the calendar
            # cannot take (set past the coercion) must not make list() raise. Logged with
            # the id only, so a task that is never armed can be found.
            append_domain_log("backend", f"[AUTOMATION] no next run for {self.id}: "
                                         f"{type(e).__name__}")
            return None
        return _to_server_local_naive(next_time) if next_time else None

    def _next_clock_time(self, now: datetime, hour: int, minute: int) -> Optional[datetime]:
        """The clock rule itself, one branch per frequency; None for a frequency with no
        clock rule. May raise on values a calendar refuses; the caller turns that into None."""
        if self.frequency == Frequency.ON_EVENT:
            # Due when something happens, never by the clock.
            return None
        if self.frequency == Frequency.ONCE:
            # Run once: today at the specified time. If already passed, schedule for tomorrow.
            next_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if next_time <= now:
                next_time += timedelta(days=1)
        elif self.frequency == Frequency.HOURLY:
            next_time = now.replace(minute=minute, second=0, microsecond=0)
            if next_time <= now:
                next_time += timedelta(hours=1)
        elif self.frequency == Frequency.DAILY:
            next_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if next_time <= now:
                next_time += timedelta(days=1)
        elif self.frequency == Frequency.WEEKLY:
            weekdays = {
                "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                "friday": 4, "saturday": 5, "sunday": 6
            }
            target_day = weekdays.get(self.weekday.lower(), 0) if self.weekday else 0
            next_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            days_ahead = target_day - now.weekday()
            if days_ahead < 0 or (days_ahead == 0 and next_time <= now):
                days_ahead += 7
            next_time += timedelta(days=days_ahead)
        elif self.frequency == Frequency.MONTHLY:
            # The next month that HAS the day: the 31st skips the thirty-day months and February
            # rather than having no next run while the current month is one of them.
            target_day = self.day or 1
            year, month = now.year, now.month
            for _ in range(13):
                try:
                    next_time = now.replace(year=year, month=month, day=target_day, hour=hour,
                                            minute=minute, second=0, microsecond=0)
                except ValueError:
                    next_time = None
                if next_time is not None and next_time > now:
                    break
                year, month = (year + 1, 1) if month == 12 else (year, month + 1)
            else:
                return None
        else:
            # Not "now": a frequency with no clock rule has no next clock run. Answering
            # "now" sorted such a record first and told the thinking-mode gate that
            # something was due this instant, every time it asked.
            return None
        return next_time


def _same_clock_rule(old: AutomationTask, new: AutomationTask) -> bool:
    """Whether two copies of a task fire at the same slots: the same task (its incarnation)
    with the same schedule fields and owner (whose time zone the slots are counted in)."""
    return all(getattr(old, name) == getattr(new, name)
               for name in ("incarnation", "user_scope_id", "frequency", "time", "weekday", "day"))


# The frequencies the clock arms (`_schedule_task`) and runs (`_clock_record_verdict`): one
# set, so a record whose frequency the arming refuses is never run from an older copy either.
_CLOCK_FREQUENCIES = (Frequency.HOURLY, Frequency.DAILY, Frequency.WEEKLY,
                      Frequency.MONTHLY, Frequency.ONCE)


@dataclass
class _ClockJob:
    """A clock task the running scheduler has armed: when it is due next (naive server-local,
    from AutomationTask.calculate_next_run) and whether it ends after it has fired."""
    task: AutomationTask
    due: datetime
    once: bool = False


# ═══════════════════════════════════════════════════════════════════════════════
# TIME INTERVAL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

MIN_AUTOMATION_INTERVAL_MINUTES = 10


def _safe_filename_stem(name: str, max_len: int = 50) -> str:
    """A filesystem-safe, length-bounded stem from an automation name.

    An automation's `name` is often the WHOLE task prompt (the create_scheduled_task workflow sets
    name = task_description), so building an output filename as ``f"{name}_{date}.html"`` produced a path
    component well over the OS limit -> "[Errno 36] File name too long". This slugifies (keeps word chars
    + dash, collapses whitespace to '_') and truncates so ``<stem>_<date>.<ext>`` always fits comfortably.
    """
    import re as _re
    s = _re.sub(r"\s+", "_", (name or "").strip())
    s = _re.sub(r"[^\w\-]", "", s, flags=_re.UNICODE)   # keep unicode word chars (umlauts) + dash
    s = s.strip("._-")[:max_len].strip("._-")
    return s or "automation"


def _to_server_local_naive(dt: datetime) -> datetime:
    """Convert a (possibly tz-aware) datetime to a naive SERVER-local datetime, so it stays
    comparable with naive datetime.now() and sortable alongside other naive task times."""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone().replace(tzinfo=None)


def _resolve_username(user_scope_id: Optional[str]) -> str:
    """Resolve the account username for a task's ``user_scope_id``.

    SECURITY (cross-user leak): a non-admin scope must resolve to its OWN account username, never
    the literal "admin". The username keys the per-user workspace + messenger lookups, so handing a
    non-admin "admin" would deliver their automation result to the LOCAL ADMIN's messenger.

    THE RULE IS NO LONGER WRITTEN HERE. It was right in this file and right in thinking_mode,
    and wrong in the dispatcher that every tool call actually passes through - a rule that
    existed twice and still failed where it mattered. It lives in
    ``config.resolve_caller_username`` now, so the assigner and the workflow engine answer the
    same question the same way. Automations may pay for the account lookup: one read of the
    account directory per listing (the listings open `account_directory_once`), one per tool
    call would not be.
    """
    from vaf.core.config import resolve_caller_username
    return resolve_caller_username(None, user_scope_id, allow_lookup=True)


# Outbound send tools a workflow step can use for in-run delivery. When one of
# them already reached the user, the post-run messenger push is skipped (the Web
# UI trace and the notification still happen) - otherwise every workflow with a
# delivery step would message the user twice.
_SEND_STEP_TOOLS = frozenset(ALL_SEND_TOOLS) | frozenset({
    "send_to_user", "send_mail", "reply_mail", "forward_mail",
})


# Grace window after a prompt-run timeout: the bounded worker is ABANDONED, not
# killed, and live runs finished 77-140s late and then delivered fine (2026-07-13,
# twice). Waiting a bounded grace turns "timeout + junk push + late zombie message"
# into one normal, complete delivery. Only timed-out runs pay this wait.
_TIMEOUT_GRACE_SECONDS = 300.0


def _wait_for_abandoned_run(chat_done: dict, grace_seconds: float = _TIMEOUT_GRACE_SECONDS,
                            poll: float = 2.0) -> bool:
    """Wait up to ``grace_seconds`` for the abandoned prompt-run worker to set its
    completion flag (the worker sets chat_done['done'] after chat_step returns).
    True = the run finished; treat it as a normal completion. Never raises."""
    try:
        deadline = time.monotonic() + max(0.0, float(grace_seconds))
        while time.monotonic() < deadline:
            if chat_done.get("done"):
                return True
            time.sleep(max(0.1, float(poll)))
        return bool(chat_done.get("done"))
    except Exception:
        return False


# Success PREFIX the send tools emit when a document went out with the message
# ("Message and document <name> sent to the user via <channel>."). Prefix-keyed,
# so the end-of-turn squash's snippet truncation cannot hide it; a text-only
# success reads "Message sent to the user via ..." and never matches.
_DOCUMENT_SUCCESS_PREFIX = "message and document"


def _iter_agent_history_send_successes(history):
    """Yield the result text of every confirmed send-tool success in a prompt run's history.

    Prompt-based automations deliver in-run via tool calls. TWO history shapes
    must be recognized: live role='tool' entries (turn still running), and the
    END-OF-TURN SQUASH - chat_step consolidates all intermediate messages into
    one '[Context: tools used this turn]' system note with '- <tool> -> OK:
    <snippet>' lines (agent.py turn finalize). The dedup reads the history
    AFTER the turn ended, so the squashed form is the one that matters (live
    2026-07-13 15:52: a real send_to_user delivery was missed and the user got
    the push on top).
    """
    try:
        from vaf.core.context import TURN_CONTEXT_PREFIX as _CTX_PREFIX
    except Exception:
        _CTX_PREFIX = "[Context:"
    for m in history or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = str(m.get("content") or "")
        if (
            role == "tool"
            and m.get("name") in _SEND_STEP_TOOLS
            and "sent to the user via" in content.lower()
        ):
            yield content
        elif role == "system" and content.startswith(_CTX_PREFIX):
            for line in content.splitlines():
                line = line.strip()
                if not line.startswith("- ") or " → OK: " not in line:
                    continue
                name, _, rest = line[2:].partition(" → OK: ")
                if name.strip() in _SEND_STEP_TOOLS and "sent to the user via" in rest.lower():
                    yield rest


def _delivered_via_agent_history(history) -> bool:
    """True if a send tool confirmed delivery during a prompt-based agent run.

    Same conservative contract as _delivered_via_send_step: only an explicit
    send-tool SUCCESS suppresses the post-run push - a duplicate message beats
    a lost one.
    """
    return any(True for _ in _iter_agent_history_send_successes(history))


def _file_delivered_via_agent_history(history) -> bool:
    """True if an in-run send during a prompt run already carried a document attachment."""
    return any(
        _DOCUMENT_SUCCESS_PREFIX in s.lower() for s in _iter_agent_history_send_successes(history)
    )


def _iter_send_step_successes(step_results):
    """Yield the result text of every send step that confirmed delivery to the user.

    Keys on the shared success phrase of the send tools ("sent to the user via
    ...").
    """
    for sr in step_results or []:
        if sr.get("tool") in _SEND_STEP_TOOLS and sr.get("status") == "success":
            result = str(sr.get("result") or "")
            if "sent to the user via" in result.lower():
                yield result


def _delivered_via_send_step(step_results) -> bool:
    """True if a send step in this run confirmed delivery to the user.

    Deliberately conservative: an unrecognized or failed send result keeps the
    post-run push ON - a duplicate message beats a lost one.
    """
    return any(True for _ in _iter_send_step_successes(step_results))


def _file_delivered_via_send_step(step_results) -> bool:
    """True if a send step already delivered a document attachment in-run."""
    return any(
        _DOCUMENT_SUCCESS_PREFIX in s.lower() for s in _iter_send_step_successes(step_results)
    )


def _push_result_to_web_ui(
    task: "AutomationTask", status: str, summary: str, output_file: Optional[str] = None,
    deliver_messenger: bool = True, file_delivered_in_run: bool = False,
) -> bool:
    """Push automation result to Web UI and, if configured, to the user's messenger.

    Delivers to BOTH channels so the user always sees the result regardless of
    whether they have Telegram/WhatsApp/Discord configured.
    When ``output_file`` is given (the automation produced a file), it is referenced in the Web UI
    message AND delivered as an attachment on the messenger.
    ``deliver_messenger=False`` skips only the messenger TEXT push - used when a workflow
    send step or the prompt run's agent already delivered the content in-run (no double
    delivery). A produced ``output_file`` is then still sent as an attachment-only follow-up,
    UNLESS ``file_delivered_in_run`` says an in-run send already carried a document: in the
    prompt lane the file is written after the run, so an in-run send can never have attached
    it (live 2026-08-21: a weather automation's Telegram text arrived without the produced
    report file).
    Returns True if at least the Web UI push succeeded.

    VAF's own file link in ``summary`` (a workflow names its folder and report with one) is
    shown as the path it opens before anything is cut, stored, shown or sent
    (``own_links_as_labels``), so the stored chat copy holds what the browser showed.
    """
    from vaf.core.terminal_text import own_links_as_labels
    summary = own_links_as_labels(summary)
    status_icon = "\u2705" if status == "success" else "\u274c"
    msg = f"{status_icon} **Automation: {task.name}**\n\n{(summary or '').strip()[:1200]}"
    # Web UI gets a note about the saved file (the messenger receives the file itself as an
    # attachment, so its text stays clean \u2014 the local path means nothing to a Telegram user).
    web_msg = msg + (f"\n\n\U0001F4CE Gespeichert: {output_file}" if output_file else "")
    web_ok = False

    # 1. Web UI — deliver as a chat message to the user's active session.
    try:
        from vaf.core.web_interface import get_web_interface
        from vaf.core.session import SessionManager
        wi = get_web_interface()
        sm = SessionManager()
        _target_scope = str(task.user_scope_id or "")

        # Priority 1: session with an active WebSocket connection for this user.
        # This ensures the message lands in the tab the user is currently looking at,
        # not in some historically-sorted session file from hours ago.
        sid = None
        if wi and wi.connection_sessions:
            live_sids = [
                sess_id
                for ws, sess_id in list(wi.connection_sessions.items())
                if str(wi.connection_users.get(ws) or "") == _target_scope
            ]
            if live_sids:
                sid = live_sids[-1]  # most recently subscribed connection

        # Priority 2: most recently modified web session on disk (mtime-sorted).
        if not sid:
            all_sessions = sm.list(limit=10, user_scope_id=task.user_scope_id)
            web_sessions = [
                s for s in all_sessions
                if (s.get("metadata") or {}).get("source") not in ("thinking",) + CHAT_CHANNELS
            ]
            if web_sessions:
                sid = web_sessions[0]["id"]
            else:
                new_session = sm.create(user_scope_id=task.user_scope_id, metadata={"source": "automation_result"})
                sid = new_session.id

        # The kind is persisted too, not only emitted: after a reload the transcript
        # has nothing else to tell a proactive message from the tail of the turn
        # before it, and one it cannot recognise gets folded into that turn. The
        # append goes through the background primitive so a live agent on this
        # session rebuilds from the file instead of answering the user's reaction
        # to a result it never saw.
        sm.append_background_message(sid, web_msg, kind="automation")
        if wi:
            # Append as a standalone bubble - automation results are proactive and
            # have no live agent turn, so the streaming update path would overwrite
            # the previous reply instead of showing a new "done" message.
            wi.emit_agent_message_append(web_msg, session_id=sid, kind="automation")
            wi.emit_session_unread(sid)
        web_ok = True
    except Exception as _e:
        append_domain_log("backend", f"[AUTOMATION] Web UI delivery failed: {_e}")

    # 2. Messenger - send proactively if a main messenger is configured, via the ONE canonical
    # "reach the user on their main channel" helper (single source of truth, shared with thinking
    # mode). It resolves the per-channel chat id/jid, attaches the output file (Telegram/WhatsApp/
    # Discord all support it), and never raises. The username is resolved to the TASK OWNER (not the
    # local admin) so a per-user automation result never leaks onto the admin's messenger.
    # With the text push suppressed (in-run send), a produced file still goes out: empty text makes
    # the adapter deliver attachment-only (just the document with a filename caption) - otherwise
    # the file would reach nobody on the messenger, since only THIS push can attach it.
    push_file_only = bool(output_file) and not deliver_messenger and not file_delivered_in_run
    if deliver_messenger or push_file_only:
        try:
            from vaf.core.messaging_connections import send_to_main_messenger
            username = _resolve_username(task.user_scope_id)
            send_to_main_messenger(
                task.user_scope_id, username,
                msg if deliver_messenger else "",
                file_path=output_file,
            )
        except Exception as _e:
            append_domain_log("backend", f"[AUTOMATION] Messenger delivery failed: {_e}")

    # 3. Notification store — surface the result in the WebUI NotificationsModal (kind 'automation',
    # already understood by the frontend) so it appears alongside other proactive results.
    try:
        from vaf.core.user_notifications import append_notification
        append_notification(
            task.user_scope_id,
            kind="automation",
            title=f"Automation: {task.name}",
            status=status,
            summary=(summary or "").strip()[:500],
            task_name=task.name,
        )
    except Exception:
        pass

    return web_ok


def _minutes_since_midnight(time_str: str) -> int:
    """Parse HH:MM and return minutes since midnight (0-1439)."""
    if not time_str or ":" not in time_str:
        return 0
    parts = time_str.strip().split(":")
    try:
        h = int(parts[0] or 0)
        m = int(parts[1] or 0) if len(parts) > 1 else 0
        return max(0, min(1439, h * 60 + m))
    except (ValueError, TypeError):
        return 0


def _min_gap_minutes(t1: str, t2: str) -> int:
    """Minimum gap in minutes on the 24h circle between two HH:MM times."""
    a = _minutes_since_midnight(t1)
    b = _minutes_since_midnight(t2)
    diff = abs(a - b)
    return min(diff, 1440 - diff)


def _parse_last_run_local_date(last_run: Optional[str]) -> Optional[date]:
    """Return the calendar date of last_run in local time, or None if missing/invalid."""
    if not last_run or not str(last_run).strip():
        return None
    s = str(last_run).strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt.date()
    except (ValueError, TypeError, OSError):
        return None


def _last_effective_completion_local_date(task: AutomationTask) -> Optional[date]:
    """Prefer persisted local completion date; fall back to parsing last_run for older JSON files."""
    raw = (task.last_completed_local_date or "").strip()
    if raw:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            pass
    return _parse_last_run_local_date(task.last_run)


def _room_trigger_binding(task, trigger: Dict[str, Any]):
    """The turn binding of a prompt-lane run a room message started: a room turn of its own.

    The prompt is the user's, but the words steering the run are whoever spoke in the room, so
    the run is bounded the way the agent's own room turn is (`Agent._room_mode_gate_decision`):
    the room's mode for the account's agent decides the writes, a trigger that is not the
    user's words alone reads only the room, the web and the room's shared folder, and its
    system prompt carries no profile (`Agent._answers_somebody_else`). Bound to the room's
    conversation of its own, so its working memory is that room's. Unattended, as every
    automation is: an outward send it was told to make is not held for a person. Who spoke is
    the room's answer (`Room.wake_authority`), the one the wake uses. A room that cannot be
    read binds the closed marker: `observe`, not the user's words, no shared folder."""
    from vaf.core.a2a.room import Room, conversation_key, derive_peer_id, participant_key
    from vaf.core.turn_binding import TurnBinding, room_turn_marker

    scope = str(getattr(task, "user_scope_id", "") or "")
    room_id = str((trigger or {}).get("room_id") or "").strip()
    if not scope or not room_id:
        raise ValueError("a room-triggered run needs its account and its room")
    rows = [r for r in ((trigger or {}).get("frames") or []) if isinstance(r, dict)]
    handle = derive_peer_id(participant_key("agent", scope), room_id)
    marker: Dict[str, Any] = {
        "room_id": room_id, "mode": "observe",
        "query": " ".join(str(r.get("text") or "").strip() for r in rows)[:300].strip(),
    }
    try:
        room = Room.open(room_id)
        marker.update(room.wake_authority(scope, [str(r.get("from") or "") for r in rows]))
        marker["mode"] = room.mode_of(handle)
        marker["shared_folder"] = str(room.workspace_dir(create=False) or "")
    except Exception:
        marker = {k: marker[k] for k in ("room_id", "query")}
        marker["mode"] = "observe"
    return TurnBinding(identity=resolve_scope_identity(scope), source="web",
                       conversation=conversation_key(room_id, handle), room_id=room_id,
                       room_turn=room_turn_marker(marker), unattended=True)


def _revoked_run_note(task) -> str:
    """What a run of an account whose access was taken away mid-run answers: logged, and
    nothing is delivered (vaf.core.revocation). One text for both lanes of run_task."""
    msg = (f"[REVOKED] Automation '{task.name}' ({task.id}) stopped: its account's "
           f"access was taken away; nothing is delivered.")
    append_domain_log_always("backend", msg)
    return msg


# The four ways a run can end. `paused` is a workflow that handed a step to a background helper:
# the drain resumes the remaining steps without the automation's context, so nothing could stamp
# the run later, and it counts as done now (a daily task would otherwise read "Due" forever).
RUN_STATUSES = ("ok", "paused", "failed", "timed_out")
_COMPLETED_STATUSES = frozenset({"ok", "paused"})

# What a failed turn delivers when it left no text of its own.
NO_ANSWER_NOTE = "Die Automation hat keine Antwort erhalten."


@dataclass(frozen=True)
class RunOutcome:
    """How one automation run ended, decided once per run (`AutomationManager.run_task`).

    Every consumer reads this one value: the stamp of `last_run` and
    `last_completed_local_date` (`completed` only), the run log, the delivery to chat,
    messenger and notification and the thinking workspace (`wire_status`), the legacy output
    file (`ok` only) and `vaf automation run`, which holds its window and exits 1 when the run
    did not complete (`on_outcome`). A one-time task is consumed by its run whatever the
    outcome, and no outcome is retried: a failed run may already have acted (sent a message),
    and a recurring task's next slot is its retry.

    `text` is what the person is shown; `report` is what `run_task` returns when it differs (a
    workflow's full execution report beside its short summary). `status` is coerced at the
    boundary: anything that is not one of `RUN_STATUSES` is `failed`, so an unknown value never
    counts as done."""
    status: str
    text: str = ""
    report: str = ""

    def __post_init__(self):
        status = self.status if isinstance(self.status, str) else ""
        object.__setattr__(self, "status", status if status in RUN_STATUSES else "failed")
        object.__setattr__(self, "text", "" if self.text is None else str(self.text))
        object.__setattr__(self, "report", "" if self.report is None else str(self.report))

    @property
    def completed(self) -> bool:
        """The run counts as done: it stamps `last_run` (ok and paused)."""
        return self.status in _COMPLETED_STATUSES

    @property
    def wire_status(self) -> str:
        """The status the run log, the delivery and the thinking workspace carry."""
        return "success" if self.completed else "error"

    @property
    def returned(self) -> str:
        """What `run_task` returns, and what the run log and the workspace summarise."""
        return self.report or self.text


def _prompt_outcome(answer, turn, stream_text: Callable[[], str], *, timed_out: bool,
                    timeout_note: str = "") -> RunOutcome:
    """The outcome of a prompt run, from what the turn RETURNED and how it ended.

    `answer` is `chat_step`'s return value, `turn` the agent's `last_turn` read right after it
    (never needed for a timed-out run, whose worker was abandoned). The turn is judged the way
    every host judges one (`returned_turn_copy`): `turn.failed` (the backend failed, loop
    protection stopped it) or a `[SYSTEM_LOG_ONLY]` head (the empty retries ran out) is
    `failed`, with the turn's own text, or `NO_ANSWER_NOTE` when it has none. A turn whose
    rounds answered, or that acknowledged a delegation (`[ASYNC_ACK]`), delivers its return
    value: every answer of the turn, joined by the engine, without the control head.

    Only a turn that neither failed nor answered (a turn closed by a tool, a status line)
    delivers `stream_text()`, the cleaned stream with its history fallback; that path is
    always `ok`. Never reads the stream to decide a status."""
    if timed_out:
        return RunOutcome("timed_out", timeout_note)
    from vaf.core.completion import split_control_head
    from vaf.core.session import FAILED_TURN_KEY, TurnStatus, returned_turn_copy
    from vaf.markers import ASYNC_ACK
    if not isinstance(turn, TurnStatus):
        turn = TurnStatus()
    copy, meta = returned_turn_copy(answer, turn)
    text = copy.strip()
    if isinstance(meta, dict) and meta.get(FAILED_TURN_KEY):
        return RunOutcome("failed", text or NO_ANSWER_NOTE)
    answered = any(r.answer for r in turn.rounds) or split_control_head(answer)[0] == ASYNC_ACK
    if answered and text:
        return RunOutcome("ok", text)
    return RunOutcome("ok", stream_text() or "")


def _stamp_successful_run(task: AutomationTask) -> None:
    """Record completion time and local calendar day on `task` (written by `_record_successful_run`)."""
    from vaf.core.user_time import user_now
    now = datetime.now()
    task.last_run = now.isoformat()
    # "local date" = the OWNER's calendar day (their timezone), so daily "done today" checks
    # match the user's day, not the server's.
    task.last_completed_local_date = user_now(_resolve_username(task.user_scope_id)).date().isoformat()


def _incarnation(value: Any) -> Optional[str]:
    """An incarnation as the task carries it: text, or None for none. The one reading for a
    loaded task and for a record compared against it, so a value a file holds as a number
    (written by hand or by another program) still names its own task."""
    return None if value is None else str(value)


def _trigger_rule(trigger: Any) -> Optional[Dict[str, Any]]:
    """A trigger without its cursor: the rule a hit was matched against."""
    if not isinstance(trigger, dict):
        return None
    return {key: value for key, value in trigger.items() if key != "cursor"}


def _frame_lamport(frame: Any) -> Optional[int]:
    """A room frame's position, or None for an object that carries none."""
    try:
        value = getattr(frame, "lamport", None)
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _stored_cursor(trigger: Any) -> int:
    """The cursor a stored trigger carries, 0 for none or one that is not a number."""
    try:
        return int((trigger or {}).get("cursor") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def _without_cursor(trigger: Any) -> Any:
    """A trigger without a read position: what a task that has read nothing carries."""
    if not isinstance(trigger, dict) or "cursor" not in trigger:
        return trigger
    return {key: value for key, value in trigger.items() if key != "cursor"}


def _carried_cursor(stored: Any, given: Any) -> Any:
    """The trigger an update writes, with the read position it keeps. Positions count per
    room: in the same room the higher of the stored and the given one stays, so an edit that
    sends the trigger again (a prompt edit through a lane that rebuilds the trigger) does not
    move the position back for another scheduler process that has not read this far; in
    another room none stays, the old room's number means nothing there."""
    if not isinstance(given, dict):
        return given
    bare = _without_cursor(given)
    if not isinstance(stored, dict) or stored.get("room_id") != given.get("room_id"):
        return bare
    cursor = max(_stored_cursor(stored), _stored_cursor(given))
    return dict(bare, cursor=cursor) if cursor > 0 else bare


def _cursor_patch(fired: Dict[str, Any]) -> Callable[[Dict[str, Any]], bool]:
    """The change that writes a fired trigger's cursor onto a task record, and only when the
    record is still the event task whose rule fired and has not read this far already
    (False, nothing to write, otherwise):

    - its frequency is still `on_event`: a task changed to a clock schedule keeps its old
      trigger dict, and must not start with room context;
    - its trigger is still the rule that fired, every field but the cursor alike: a cursor
      belongs to the rule whose frames it consumed, and a rule watching another room, or
      with another text or emoji, was never asked about them;
    - its stored cursor is below the hit's newest frame: never back, so a writer that read
      further first (another process) is not overwritten and its frames do not run twice.

    Afterwards `patch.applied` says whether the cursor was written, `patch.stored` is the
    cursor the record carried, and `patch.reason` names why nothing was written."""
    rule = _trigger_rule(fired)
    newest = _stored_cursor(fired)

    def patch(record: Dict[str, Any]) -> bool:
        current = record.get("trigger")
        if str(record.get("frequency") or "") != Frequency.ON_EVENT.value:
            patch.reason = "no longer an event task"
            return False
        if _trigger_rule(current) != rule:
            patch.reason = "rule changed"
            return False
        patch.stored = _stored_cursor(current)
        if patch.stored >= newest:
            patch.reason = "already read this far"
            return False
        record["trigger"] = dict(current, cursor=newest)
        patch.applied = True
        return True

    patch.applied, patch.stored, patch.reason = False, 0, "task gone or created anew"
    return patch


# Bounded per-automation run log. Until this existed the record carried `last_run`, stamped ONLY on
# success, so a job that errored every morning and a machine that was switched off for a week left
# byte-identical traces - and nothing could honestly be said about either. Kept small and beside the
# task file, in the same per-user directory, so it inherits the task's isolation.
_RUN_LOG_FORMAT = "autorun-1-7c41d9"
_RUN_LOG_MAX = 50


def _run_log_path(task_path: Path) -> Path:
    return task_path.with_suffix(".runs.json")


def _task_files(directory: Path) -> List[Path]:
    """The task files of one automation directory: `<id>.json`, never a `<id>.runs.json` run log.

    Every reader globbed `*.json`, and the run log that arrived later matches that pattern too.
    Loaded as a task, a run log became a nameless daily 06:00 automation (every field defaulted)
    with a new random id on every load - so deleting it in the list did nothing, it came back
    after each restart, and the scheduler armed it to run an empty prompt (measured live)."""
    try:
        return sorted(p for p in directory.glob("*.json") if not p.name.endswith(".runs.json"))
    except OSError:
        return []


def _read_task_record(path: Path, strict: bool = False) -> Optional[Dict[str, Any]]:
    """A task file's record, or None when the file is not one. A record without an id is refused:
    AutomationTask would invent one per load, and a task whose id changes can never be found to
    be edited or deleted.

    `strict`: only a missing file answers None. A file that is there but cannot be read (any
    other `OSError`), does not parse (a manual edit half written) or holds no task record raises
    instead, for a caller that must not take a passing state of the file for a deleted task."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        if strict:
            raise
        return None
    if not isinstance(data, dict) or not str(data.get("id") or "").strip():
        if strict:
            raise ValueError("not a task record")
        return None
    return data


# Every write of a task file holds the lock of the directory it lives in,
# `<dir>/.automations.lock` (neither `_task_files` nor the slot count reads it). One lock per
# directory, not per task: a holder keeps it for well under a millisecond for a task of a few
# KB (0.33 ms median for lock, read, change and atomic write of a 4 KB task, measured), and
# about 5 ms more per MB of task file (1 MB 5 ms, 64 MB 311 ms), so two tasks of one account
# never wait on each other for long, create, delete, restore and the trash move need no per-id
# bookkeeping, and a filelock that leaves its file behind leaves one per directory rather than
# one per task id ever written. Different accounts are different directories.
_STORE_LOCK_NAME = ".automations.lock"
# How long a writer waits for another one before it answers "busy" (as the handoff's decision
# lock waits). The WebSocket waits on its event loop, so a wait is bounded by this.
_STORE_LOCK_TIMEOUT_S = 5.0
# Per thread: resolved directory -> how deep this thread holds its store lock. A second
# FileLock instance on a path the same thread already holds waits out the whole timeout
# (filelock is not reentrant across instances, measured), and `_save_task` takes the lock
# inside `_mutate_record`.
_STORE_LOCK_HELD = threading.local()
# Used only when the filelock package cannot be imported: one in-process lock per directory.
_FALLBACK_STORE_LOCKS: Dict[str, threading.Lock] = {}
_FALLBACK_STORE_LOCKS_GUARD = threading.Lock()


class AutomationStoreBusy(OSError):
    """Another writer held the automation store's lock past the timeout. An `OSError`, so a
    lane that answers a store it could not write (an approved handoff answers "not saved" and
    keeps its claim for the next approve) answers this one the same way."""


def _acquire_store_lock(directory: Path) -> Callable[[], None]:
    """Take the kernel lock of one store directory; the function that releases it. Raises
    `AutomationStoreBusy` after `_STORE_LOCK_TIMEOUT_S`, or `OSError("automation store could
    not be locked")` when the lock file cannot be opened. Both texts are fixed: the WebSocket
    answers with the error's text, and the filelock error names the data path."""
    from vaf.core.secure_store import _get_filelock_cls

    timeout = max(0.0, float(_STORE_LOCK_TIMEOUT_S))
    lock_cls = _get_filelock_cls()
    if lock_cls is None:
        with _FALLBACK_STORE_LOCKS_GUARD:
            fallback = _FALLBACK_STORE_LOCKS.setdefault(str(directory), threading.Lock())
        if not fallback.acquire(timeout=timeout):
            raise AutomationStoreBusy("automation store is busy")
        return fallback.release
    from filelock import Timeout

    try:
        # A missing directory is created even for a lane that only reads or removes (a late run
        # stamp for an account whose folder is gone): an empty folder holds no task for any
        # loader, and one lock path for every lane leaves no lockless branch to reason about.
        directory.mkdir(parents=True, exist_ok=True)
        lock = lock_cls(str(directory / _STORE_LOCK_NAME), timeout=timeout)
        lock.acquire()
    except Timeout:
        # A `Timeout` is an `OSError` whose text names the lock path.
        raise AutomationStoreBusy("automation store is busy") from None
    except OSError as e:
        append_domain_log("backend", f"[AUTOMATION] store lock not taken: {e!r}")
        raise OSError("automation store could not be locked") from None
    return lock.release


def scoped_task_record(user_scope_id: str, task_id: str) -> Optional[Dict[str, Any]]:
    """One task of an account's own store, read strictly: None when there is no such file,
    `OSError` or `ValueError` when there is one that cannot be read or is not that task's
    record. For recovery code that must not take an unreadable task for a missing one:
    `_read_task_record`, the loaders' reader, skips such a file, so `get()` answers None for
    a task that is on disk. `task_id` must already be one plain entry (the caller checks)."""
    from vaf.core.platform import Platform

    path = Platform.vaf_dir() / "automations" / user_scope_id / f"{task_id}.json"
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or str(data.get("id") or "").strip() != task_id:
        raise ValueError("not the task's record")
    return data


def load_run_log(task_path: Path) -> List[Dict[str, Any]]:
    """Newest-last run records, or [] when there are none.

    Tolerates a file without the format tag: a store that refuses to read what an earlier version
    wrote turns an upgrade into data loss."""
    try:
        with open(_run_log_path(task_path), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    runs = data.get("runs")
    return runs if isinstance(runs, list) else []


def append_run_log(task_path: Path, *, status: str, started_at: str, duration_seconds: float,
                   summary: str = "") -> None:
    """Record one outcome. Never raises: a bookkeeping write must not fail an automation.

    Only beside a task that still exists: a one-time task is deleted when its run ends, before its
    outcome is recorded, and a log written then was an orphan the loaders used to take for a task."""
    try:
        if not Path(task_path).exists():
            return
        runs = load_run_log(task_path)
        runs.append({
            "status": "success" if status == "success" else "error",
            "at": started_at,
            "seconds": round(float(duration_seconds), 1),
            "summary": (summary or "").strip()[:200] or None,
        })
        path = _run_log_path(task_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"format": _RUN_LOG_FORMAT, "runs": runs[-_RUN_LOG_MAX:]},
                      f, indent=2, ensure_ascii=False)
        tmp.replace(path)
    except Exception as e:
        try:
            append_domain_log("backend", f"[AUTOMATION] run log not written for {task_path.name}: {e}")
        except Exception:
            pass


# How long a task may go without a successful run before that is worth mentioning, per frequency.
# Two periods, so a single missed occurrence is never reported: an automation that runs daily and
# has not completed for two days is a real observation, one that missed this morning is noise.
_REVIEW_GRACE_HOURS = {
    Frequency.HOURLY: 2,
    Frequency.DAILY: 48,
    Frequency.WEEKLY: 24 * 14,
    Frequency.MONTHLY: 24 * 62,
}


def _review_grace_hours(task: "AutomationTask") -> Optional[float]:
    """None for ONCE and anything unrecognised - a one-shot task that has run is simply done."""
    try:
        return float(_REVIEW_GRACE_HOURS[Frequency(task.frequency)])
    except (KeyError, ValueError, TypeError):
        return None


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse a stored timestamp as NAIVE local time.

    Everything this module compares against is `datetime.now()`, which is naive. A stored value
    carrying an offset (hand-edited file, an import, a future writer) would come back aware, and
    subtracting the two raises TypeError rather than returning a wrong answer - so it would take
    out the whole findings pass, not just one finding. Convert to local and drop the offset."""
    try:
        dt = datetime.fromisoformat(str(value)) if value else None
    except (ValueError, TypeError):
        return None
    if dt is not None and dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _prompt_overlap(a: str, b: str) -> float:
    """Word-level Jaccard, the same crude measure create-time duplicate detection already uses."""
    import re
    wa = {w for w in re.findall(r"\w+", (a or "").lower()) if len(w) > 3}
    wb = {w for w in re.findall(r"\w+", (b or "").lower()) if len(w) > 3}
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def review_findings(tasks: List["AutomationTask"], now: Optional[datetime] = None,
                    run_logs: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> List[Dict[str, Any]]:
    """Checkable observations about a user's existing automations, computed in CODE.

    Every finding here is derivable from the stored record, and nothing else is offered. That is a
    deliberate limit, not a shortcut: an automation's PROMPT reads like evidence while saying nothing
    about how the job actually behaved, so letting a model read prompts and invent "improvements" is
    the same fabrication surface the whole background-run design exists to close.

    What the record cannot support, and what a caller must therefore never claim: that an automation
    FAILED, errored, is broken, ran too long, or produced poor output. `last_run` is stamped only on
    success (`_stamp_successful_run`), so "no successful run since <date>" is byte-identical to "VAF
    was switched off for a week". The wording of every finding below stays inside that limit.

    `run_logs` maps task id to that task's recorded outcomes (see `load_run_log`). It is optional
    because the log only starts existing when a task next runs: with it, "errored on its last N runs"
    becomes sayable; without it the findings simply stay at what the task record alone supports.

    Returns dicts with a stable `key` (id + kind) so a caller can de-duplicate across runs by the
    FINDING rather than by phrasing, plus `kind`, `task_id`, `task_name` and a factual `detail`.
    """
    run_logs = run_logs or {}
    now = now or datetime.now()
    out: List[Dict[str, Any]] = []

    def add(task, kind, detail):
        out.append({
            "key": f"{task.id}:{kind}",
            "kind": kind,
            "task_id": task.id,
            "task_name": task.name or task.id,
            "detail": detail,
        })

    by_slot: Dict[tuple, List["AutomationTask"]] = {}
    for task in tasks:
        created = _parse_iso(task.created_at)
        last = _parse_iso(task.last_run)
        grace = _review_grace_hours(task)

        if not task.enabled:
            if created and (now - created).total_seconds() > 30 * 86400:
                add(task, "disabled_and_old",
                    f"disabled since it was created on {created.date().isoformat()}"
                    if not last else f"disabled, last successful run {last.date().isoformat()}")
            continue

        if grace is not None:
            if last is None:
                if created and (now - created).total_seconds() > grace * 3600:
                    add(task, "never_completed",
                        f"created {created.date().isoformat()}, scheduled {task.frequency} at "
                        f"{task.time}, and has never recorded a successful run")
            elif (now - last).total_seconds() > grace * 3600:
                add(task, "no_recent_success",
                    f"scheduled {task.frequency} at {task.time}; the last recorded successful run "
                    f"was {last.date().isoformat()}")

        recent_runs = (run_logs.get(task.id) or [])[-5:]
        if len(recent_runs) >= 3 and all(r.get("status") == "error" for r in recent_runs):
            last_at = (recent_runs[-1].get("at") or "")[:10]
            add(task, "repeated_errors",
                f"its last {len(recent_runs)} recorded runs all ended with an error"
                + (f", most recently on {last_at}" if last_at else ""))

        # NO "dead output path" finding. It was here and does not survive contact with what
        # run_task actually does: it resolves folder aliases ("Desktop", "downloads") against the
        # home directory, and it CREATES a missing output directory before writing. So a plain
        # `Path(output_path).exists()` check flags a relative alias that works perfectly, and an
        # absolute path that the next run would simply create. The one case that is a real problem -
        # a path VAF cannot create, e.g. on a drive that is no longer mounted - makes the run FAIL,
        # and a failed run is recorded in the run log, where `repeated_errors` reports it from
        # evidence instead of from a guess.

        by_slot.setdefault((str(task.frequency), str(task.time)), []).append(task)

    for (freq, when), group in by_slot.items():
        if len(group) > 1:
            names = ", ".join(t.name or t.id for t in group)
            add(group[0], "slot_collision",
                f"{len(group)} automations are scheduled {freq} at the same time {when}: {names}")

    enabled = [t for t in tasks if t.enabled]
    for i, a in enumerate(enabled):
        for b in enabled[i + 1:]:
            if _prompt_overlap(a.prompt, b.prompt) > 0.6:
                add(a, "near_duplicate",
                    f"its instructions overlap heavily with '{b.name or b.id}' ({b.id})")
                break

    return out


def _briefing_family_name(name: str) -> bool:
    """True if the automation name looks like a morning/briefing job (several languages)."""
    n = (name or "").lower()
    return any(
        k in n
        for k in (
            "briefing",
            "morgenbrief",
            "morning brief",
            "morgen ",  # e.g. "guten morgen" style titles
        )
    )


def _same_automation_family_for_catchup(name_a: str, name_b: str) -> bool:
    """Whether two tasks count as the same 'job' when avoiding duplicate same-day catch-up runs."""
    a, b = (name_a or "").strip().lower(), (name_b or "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    return _briefing_family_name(a) and _briefing_family_name(b)


def format_daily_calendar_status(task: AutomationTask) -> str:
    """
    Agent-readable status: whether today's expected run is done, pending, or in progress.
    For daily tasks uses local date and scheduled HH:MM; checks automation lock for running state.
    "Done (today)" uses ``last_completed_local_date`` (and falls back to ``last_run``), both loaded
    from the task JSON—so the status survives process restarts until the calendar day changes.
    """
    try:
        from vaf.core.lock_manager import LockManager

        if LockManager.is_locked(f"automation_{task.id}"):
            return "In progress"
    except Exception:
        pass

    # All "today"/slot checks below are in the OWNER's timezone (single source of truth).
    from vaf.core.user_time import user_now
    _now = user_now(_resolve_username(task.user_scope_id))

    freq = (task.frequency or "").strip().lower()
    if freq != "daily":
        done_d = _last_effective_completion_local_date(task)
        today_d = _now.date()
        if done_d == today_d:
            return "Done (today)"
        return f"Next: {task.next_run_label}"

    today_d = _now.date()
    done_d = _last_effective_completion_local_date(task)
    if done_d == today_d:
        return "Done (today)"

    try:
        parts = (task.time or "06:00").split(":")
        hh, mm = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        slot_t = _now.replace(hour=hh, minute=mm, second=0, microsecond=0).time()
    except (ValueError, TypeError, IndexError):
        return "Due (not yet run today)"

    if _now.time() < slot_t:
        return "Scheduled (later today)"
    return "Due (not yet run today)"


# Max users that may book the same time slot (same HH:MM + frequency). Enforced globally.
MAX_USERS_PER_SLOT = 3
SUGGESTED_SLOT_GAP_MINUTES = 15


def _slot_occupancy(base_dir: Path) -> Dict[tuple, set]:
    """
    Scan all automation tasks under base_dir (root + user UUID subdirs) and return
    (time, frequency) -> set of user_scope_ids (or "__global__" for root tasks).
    Only enabled tasks are counted.
    """
    if not base_dir.exists():
        return {}
    occupancy: Dict[tuple, set] = {}
    # Root-level tasks (legacy/admin)
    for filepath in _task_files(base_dir):
        try:
            data = _read_task_record(filepath)
            if data is None or not data.get("enabled", True):
                continue
            t = data.get("time") or "06:00"
            freq = data.get("frequency") or "daily"
            if ":" in t:
                key = (t, freq)
                occupancy.setdefault(key, set()).add("__global__")
        except Exception:
            continue
    # Per-user subdirs
    for subdir in base_dir.iterdir():
        if not subdir.is_dir():
            continue
        try:
            uuid.UUID(subdir.name)
        except (ValueError, TypeError):
            continue
        for filepath in _task_files(subdir):
            try:
                data = _read_task_record(filepath)
                if data is None or not data.get("enabled", True):
                    continue
                t = data.get("time") or "06:00"
                freq = data.get("frequency") or "daily"
                if ":" in t:
                    key = (t, freq)
                    occupancy.setdefault(key, set()).add(subdir.name)
            except Exception:
                continue
    return occupancy


# ═══════════════════════════════════════════════════════════════════════════════
# AUTOMATION MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

class AutomationManager:
    """Manages automation tasks and scheduling."""

    def __init__(self, storage_dir: str = None, user_scope_id: Optional[str] = None):
        self.user_scope_id = user_scope_id

        if storage_dir:
            self.storage_dir = Path(storage_dir)
            self.base_dir = self.storage_dir
        else:
            # OS-unabhängiger Pfad
            from vaf.core.platform import Platform
            base_dir = Platform.vaf_dir() / "automations"
            if user_scope_id:
                # Per-user isolation: store automations in user-specific subdirectory
                self.storage_dir = base_dir / user_scope_id
                self.base_dir = base_dir
            else:
                # Legacy/admin: global automations directory (also aggregates all user dirs for CLI/scheduler)
                self.storage_dir = base_dir
                self.base_dir = base_dir

        self.storage_dir.mkdir(parents=True, exist_ok=True)
        # Trash directory (system-independent)
        self.trash_dir = self.storage_dir / "trash"
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        
        # OS-unabhängiger Pfad für letzte Ausführungszeit
        from vaf.core.platform import Platform
        self.last_run_file = Platform.vaf_dir() / "last_automation_run.json"
        
        self.tasks: Dict[str, AutomationTask] = {}
        self._scheduler_thread: Optional[threading.Thread] = None
        self._running = False
        self._clock_jobs: Dict[str, _ClockJob] = {}
        # (task id, incarnation) -> the slot the tick claimed, until the next swap.
        self._clock_claims: Dict[Tuple[Any, Optional[str]], datetime] = {}
        self._clock_lock = threading.Lock()
        self._create_readme()
        self._load_tasks()

    def _log_scheduler_event(self, message: str) -> None:
        """Write scheduler diagnostics only when debug logging is enabled."""
        append_domain_log("backend", f"[AUTOMATION_SCHEDULER] {message}")

    def _run_scheduled_task(self, task: AutomationTask, *,
                            trigger: Optional[Dict[str, Any]] = None) -> str:
        """Wrapper for scheduled executions: runs in a background thread (no terminal).

        Running without a terminal (new_terminal=False) keeps the automation in the
        same process, which means _push_result_to_web_ui can reach the live Web UI
        WebSocket and deliver the result directly — no subprocess isolation needed.
        """
        self._log_scheduler_event(
            f"TRIGGER task_id={task.id} name={task.name!r} frequency={task.frequency} time={task.time}"
        )

        def _execute():
            # Say out loud that this run belongs to NOBODY's web session, before any tool
            # is dispatched. A scheduled run has no browser tab behind it; without this
            # declaration the thread starts with an empty context and every session lookup
            # answers with whatever a live chat turn last set - which is how one tenant's
            # automation wrote its deliverable into another tenant's workspace and pushed
            # the path into their browser. "Nobody" is the honest answer, and the cost of
            # it is only that this run has no live view and no Stop button, which is
            # correct for something nobody is watching.
            set_current_session_id(None)
            try:
                # A clock run passes exactly what it always passed: the trigger is
                # named only when there is one, so a stand-in for run_task that
                # predates the parameter keeps working on the lane it stands in for.
                extra = {"trigger": trigger} if trigger is not None else {}
                result = self.run_task(task, new_terminal=False, **extra)
                preview = (result or "").replace("\n", " ")[:200]
                self._log_scheduler_event(
                    f"COMPLETED task_id={task.id} result_preview={preview!r}"
                )
            except Exception as e:
                self._log_scheduler_event(
                    f"ERROR task_id={task.id} name={task.name!r} error={e!r}"
                )

        import threading as _threading
        _threading.Thread(target=_execute, daemon=True, name=f"automation-{task.id}").start()
        return f"Automation '{task.name}' started in background"
    
    def _fire_room_triggers(self) -> int:
        """Run every event-driven task whose room said something matching since the
        last tick. Returns how many were started.

        A hit runs only after its cursor is on disk, so a restart in the middle of a run
        cannot fire the same frames twice; a run that then fails is a run that failed,
        recorded in the run log like any other. The per-task run lock in run_task keeps a
        trigger that fires again while the previous run is still going from overlapping it.

        The cursor is committed onto the record as it is on disk, under the store lock
        (`_cursor_patch`), which is also where the hit is judged against the task as it
        is now: a task gone or created anew under the id, no longer an event task, or
        already read this far (another process) gets nothing written and no run; a task
        switched off gets its cursor written and no run. A task whose rule changed on disk
        is rewound (below) instead of dropped: the scheduler's copy was older, and the
        frames the watch read past may match the rule on disk. The run gets the
        task as it is on disk, never the scheduler's older copy, and only the frames past
        the cursor the record carried.

        When the commit cannot be made (the store busy past its timeout, not writable, the
        file not readable), nothing is held in memory: the watch is rewound to where the
        hit's read started (`RoomTriggerWatch.rewind`), so the next tick reports the same
        frames again and judges them afresh (membership, rule, the task as it is by then).
        Once one store directory is busy in a tick, its other hits are rewound without a
        second wait, so a busy store costs one timeout per tick, not one per hit. Each
        hit's run is started on its own: one that fails to start does not touch the others.
        """
        from vaf.core.automation_triggers import RoomTriggerWatch, trigger_context

        watch = getattr(self, "_trigger_watch", None)
        if watch is None:
            watch = self._trigger_watch = RoomTriggerWatch()

        def rewind(hit, why: str) -> None:
            self._log_scheduler_event(f"TRIGGER_REWOUND task_id={hit.task.id} reason={why!r}")
            back = getattr(watch, "rewind", None)
            if back is not None:
                back(hit.task.id, getattr(hit, "since", None))

        busy = set()
        fired = 0
        for hit in watch.tick(self.list(enabled_only=True)):
            task = hit.task
            directory = str(self._path_for_task(task).parent)
            if directory in busy:
                rewind(hit, "store busy")
                continue
            # The scheduler's copy is not changed here: the record on disk decides.
            patch = _cursor_patch(dict(task.trigger or {}, cursor=int(hit.newest)))
            try:
                current = self._mutate_record(task, patch, same_incarnation=True, strict=True)
            except AutomationStoreBusy:
                busy.add(directory)
                rewind(hit, "store busy")
                continue
            except Exception as e:
                rewind(hit, f"not committed: {e!r}")
                continue
            if current is not None and patch.reason == "rule changed":
                # The scheduler's copy was older than the rule on disk, and the watch read
                # past frames only the old rule was asked about. The commit put the current
                # record into the cache, so the next report reads them again under the rule
                # on disk (another room or a task created anew is set again by the watch).
                rewind(hit, "rule changed")
                continue
            if current is None or not patch.applied:
                self._log_scheduler_event(
                    f"TRIGGER_DROPPED task_id={task.id} reason={patch.reason!r}")
                continue
            if not current.enabled:
                self._log_scheduler_event(f"TRIGGER_DROPPED task_id={task.id} reason='disabled'")
                continue
            frames = [f for f in hit.frames if _frame_lamport(f) is None
                      or _frame_lamport(f) > patch.stored]
            if not frames:
                continue
            try:
                self._log_scheduler_event(
                    f"TRIGGER_EVENT task_id={current.id} name={current.name!r} frames={len(frames)}")
                self._run_scheduled_task(current, trigger=trigger_context(current.trigger, frames,
                                                                          hit.labels))
                fired += 1
            except Exception as e:
                # The cursor is committed, so these frames are consumed: a run that cannot
                # start is a run that failed, and the other hits go on.
                self._log_scheduler_event(f"TRIGGER_RUN_NOT_STARTED task_id={current.id} error={e!r}")
        return fired

    def _create_readme(self):
        """Create README in automations folder if it doesn't exist."""
        readme_path = self.storage_dir / "README.md"
        
        if readme_path.exists():
            return
        
        readme_content = """# 🤖 VAF Automations

This folder contains your scheduled automation tasks.

## How It Works

1. **Create**: Use `vaf automation create` or ask VAF to create one
2. **View**: Check Settings → ⚡ Automations or `vaf automation list`
3. **Start**: Run `vaf automation start` to activate the scheduler
4. **Manage**: Enable/disable in Settings, delete manually

## Files

- `*.json` - Automation task definitions
- Each file = one scheduled task

## Two Types of Automations

### 1. One-Step Automation (Prompt-Based)
Simple automation with just a prompt. The LLM processes the request and generates clean output.

**Format:**
```json
{
  "id": "abc123",
  "name": "daily_news",
  "prompt": "Create a summary of today's tech news",
  "workflow_steps": [],
  "frequency": "daily",
  "time": "06:00",
  "enabled": true,
  "output_path": "~/Desktop"
}
```

**Features:**
- Simple prompt-based execution
- Clean output (internal thinking is filtered out)
- Best for: Simple tasks, content generation, summaries

### 2. Multi-Step Automation (Workflow-Based)
Advanced automation with n8n-like workflow steps. Each step is clearly visible and executed sequentially.

**Format:**
```json
{
  "id": "abc123",
  "name": "research_and_save",
  "prompt": "",
  "workflow_steps": [
    {
      "tool": "web_search",
      "args": {"query": "Python best practices", "max_results": 5},
      "description": "Search for information",
      "output": "research_data"
    },
    {
      "tool": "write_file",
      "args": {"path": "research_{date}.md", "content": "{research_data}"},
      "description": "Save results to file",
      "output": "saved_file"
    }
  ],
  "frequency": "daily",
  "time": "06:00",
  "enabled": true
}
```

**Features:**
- Clear step-by-step execution (like n8n nodes)
- Each step shows: tool name, description, status, result
- Output chaining: Step outputs become inputs for next steps
- Best for: Complex multi-step tasks, data pipelines, file operations

## Task Format

```json
{
  "id": "abc123",
  "name": "daily_news",
  "prompt": "Create a summary of today's tech news",
  "workflow_steps": [],  // Empty = one-step, populated = multi-step
  "frequency": "daily",
  "time": "06:00",
  "enabled": true,
  "output_path": "~/Desktop",
  "output_format": "html",  // html, markdown, json, txt
  "parameters": {
    "city": "Berlin"
  }
}
```

## Frequencies

- `hourly` - Every hour at :MM
- `daily` - Every day at HH:MM
- `weekly` - Every week on weekday at HH:MM
- `monthly` - Every month on day at HH:MM

## Commands

```bash
vaf automation list          # View all
vaf automation create        # Create new (interactive)
vaf automation run <id>      # Run manually
vaf automation start         # Start scheduler daemon
vaf automation enable <id>   # Enable task
vaf automation disable <id>  # Disable task
vaf automation delete <id>   # Delete task
```

## Tips

- Automations are **enabled by default** when created
- Disable in Settings without deleting
- To delete: remove the .json file or use CLI
- Scheduler must be running for timed execution
- **One-step automations**: Clean output, no internal thinking
- **Multi-step automations**: Each step is visible and tracked

---
*Generated by VAF - Veyllo Agentic Framework*
"""
        readme_path.write_text(readme_content, encoding='utf-8')
    
    def reload_tasks(self):
        """Reload all tasks from storage (useful after manual file edits)."""
        self.tasks.clear()
        self._load_tasks()
    
    def _is_uuid_dir(self, name: str) -> bool:
        """Return True if name looks like a UUID (user scope subdir)."""
        try:
            uuid.UUID(name)
            return True
        except (ValueError, TypeError):
            return False

    def _load_tasks(self):
        """Load all tasks from storage. When manager is global (no user_scope_id), also load from user subdirs."""
        # Load from current storage_dir (global dir or this user's dir)
        for filepath in _task_files(self.storage_dir):
            try:
                data = _read_task_record(filepath)
                if data is None:
                    continue
                task = AutomationTask.from_dict(data)
                # Ensure task has scope when loaded from user-scoped manager
                if self.user_scope_id and not task.user_scope_id:
                    task.user_scope_id = self.user_scope_id
                self.tasks[task.id] = task
            except Exception:
                continue
        # When global manager: also load from each user scope subdir so CLI/scheduler see all
        if self.user_scope_id is None and self.base_dir.exists():
            for subdir in self.base_dir.iterdir():
                if subdir.is_dir() and self._is_uuid_dir(subdir.name):
                    for filepath in _task_files(subdir):
                        try:
                            data = _read_task_record(filepath)
                            if data is None:
                                continue
                            task = AutomationTask.from_dict(data)
                            if not task.user_scope_id:
                                task.user_scope_id = subdir.name
                            self.tasks[task.id] = task
                        except Exception:
                            continue
    
    def _path_for_task(self, task: AutomationTask) -> Path:
        """Return the filesystem path where this task is or should be stored."""
        if task.user_scope_id:
            return self.base_dir / task.user_scope_id / f"{task.id}.json"
        return self.storage_dir / f"{task.id}.json"

    def _save_task(self, task: AutomationTask):
        """Save a task to storage. When task has user_scope_id, save to that user's dir (for global manager).

        Encoded in full first, then temp file + replace: a truncating write that failed half
        way (a full disk, a value that does not encode) left an empty or cut-off file, which
        `_read_task_record` skips, so the automation was gone from every later load. Plain
        JSON (`encrypt=False`): every reader of this store parses it with `json.load`.

        Writes the WHOLE object, so it holds the store lock and is called only where the
        object is the current record: a new task (`create`) and `_mutate_record`, which built
        it from the file under that same lock. Writing back an object loaded earlier puts back
        every field another lane changed since (tests/test_automation_store_lock.py pins the
        two callers)."""
        from vaf.core.data_files import write_json_atomic

        path = self._path_for_task(task)
        with self._store_lock(path.parent):
            write_json_atomic(path, task.to_dict(), encrypt=False, indent=2)

    @staticmethod
    @contextmanager
    def _store_lock(directory: Path):
        """Hold the lock of one store directory (`.automations.lock` in it), or raise
        `AutomationStoreBusy` (waited past `_STORE_LOCK_TIMEOUT_S`) or `OSError` (the lock
        file could not be opened), both with a fixed text.

        A kernel lock (`flock`, `msvcrt.locking` through filelock), as the handoff's decision
        lock: the operating system releases it when its holder dies, and two threads of one
        process exclude each other because each acquisition opens its own descriptor.
        Reentrant within one thread: only the outermost entry takes the lock.

        A LEAF: while it is held, code reads the task record, changes it, writes it, and moves
        or unlinks that task's files, nothing else. It never refreshes the scheduler, mirrors
        into the thinking workspace or takes the run lock, so every lock order that exists is
        "another lock, then this one" (the handoff's decision lock, the scheduler's manager
        lock, the run lock) and none can be reversed. Pinned by a static guard.

        Named boundaries: without filelock only the threads of this process are serialized.
        Fifteen modules hold a file lock by hand, with different failure semantics; a shared
        helper would be its own consolidation, so this one keeps the decision lock's shape."""
        try:
            key = str(Path(directory).resolve())
        except (OSError, RuntimeError):
            key = os.path.abspath(str(directory))
        depth = getattr(_STORE_LOCK_HELD, "depth", None)
        if depth is None:
            depth = _STORE_LOCK_HELD.depth = {}
        if depth.get(key):
            depth[key] += 1
            try:
                yield
            finally:
                depth[key] -= 1
            return
        release = _acquire_store_lock(Path(key))
        depth[key] = 1
        try:
            yield
        finally:
            depth.pop(key, None)
            release()

    def _mutate_record(self, task: AutomationTask,
                       change: Callable[[Dict[str, Any]], Optional[bool]], *,
                       same_incarnation: bool = False,
                       strict: bool = False) -> Optional[AutomationTask]:
        """Apply `change` to the task's record AS IT IS ON DISK NOW, under the store lock,
        and write it; the task built from the written record (also put into the cache).

        `task` names the record (its id and its path); its other fields are not written.
        A lane's manager, and the object a run captured when it fired, can be minutes old,
        and writing that object back put back every field another lane had changed since
        (measured: 200 of 200 two-thread trials lost a field, a run switched a task that
        was switched off during it back on).

        None when the file is gone or is not this task's record (deleted, moved to the
        trash, unreadable): nothing is written, so a deleted task is never brought back and
        a file this code cannot read is never overwritten. `change` returning False means
        "nothing to write": the current record is returned unwritten.

        `same_incarnation`: write only onto the very task `task` was loaded as (its
        `incarnation`), for the bookkeeping of something that task started (a run, a
        trigger hit). None, with nothing written, when the id now names a task created anew.

        `strict`: a file that is there but cannot be read raises `OSError` instead of being
        taken for a task that is gone.

        The cache is changed inside the lock, in the order the file changes: published after
        it, an update could put a task back into `get()` that a delete on the same manager
        removed in between."""
        path = self._path_for_task(task)
        tasks = getattr(self, "tasks", None)
        if tasks is None:
            tasks = {}
        with self._store_lock(path.parent):
            record = _read_task_record(path, strict=strict)
            if record is None or str(record.get("id")).strip() != str(task.id):
                tasks.pop(task.id, None)
                return None
            if same_incarnation and _incarnation(record.get("incarnation")) != task.incarnation:
                tasks[task.id] = self._task_from_record(record, task, path)
                return None
            write = change(record) is not False
            fresh = self._task_from_record(record, task, path)
            if write:
                self._save_task(fresh)
            tasks[fresh.id] = fresh
        return fresh

    def _task_from_record(self, record: Dict[str, Any], task: AutomationTask,
                          path: Path) -> AutomationTask:
        """The task a record read from `path` is. It keeps its path: the scope comes from
        where the task lives, as `_load_tasks` stamps it on a file that does not carry one,
        and the id is the task's own. Every caller has matched the record's id to it read
        stripped, so an id written with spaces around it or as a number names this task; kept
        as the record spells it, a write went to a second file (`" abc123 .json"`) and the
        cache got a second key."""
        fresh = AutomationTask.from_dict(record)
        fresh.id = task.id
        fresh.user_scope_id = (task.user_scope_id or fresh.user_scope_id
                               or getattr(self, "user_scope_id", None))
        if self._path_for_task(fresh) != path:
            fresh.user_scope_id = task.user_scope_id
        return fresh

    def _record_successful_run(self, task: AutomationTask) -> Optional[AutomationTask]:
        """Stamp a successful run on the captured `task` (its mirror and labels read it) and
        write ONLY the two run fields onto the current record. A time, switch, prompt or name
        changed while the run went keeps its change, and a task deleted or moved to the trash
        during its run stays gone. Written only onto the task that ran (`same_incarnation`):
        a task created anew under the same id meanwhile has not run, and the stamp would show
        it as done today. A bookkeeping write must not fail the automation: an error (a busy
        or unwritable store) is logged, as `append_run_log` does."""
        _stamp_successful_run(task)
        stamp = {"last_run": task.last_run,
                 "last_completed_local_date": task.last_completed_local_date}
        try:
            return self._mutate_record(task, lambda record: record.update(stamp),
                                       same_incarnation=True)
        except Exception as e:
            append_domain_log("backend", f"[AUTOMATION] run of {task.id} not recorded: {e!r}")
            return None

    def _sync_workspace_automation_state(
        self,
        task: AutomationTask,
        run_status: str = "",
        summary: str = "",
        event: str = "automation_sync",
    ) -> None:
        """Best-effort bridge: mirror automation lifecycle state into Thinking Workspace."""
        try:
            from vaf.core.thinking_workspace import sync_automation_status_to_workspace

            payload = {
                "id": task.id,
                "name": task.name,
                "description": task.description,
                "frequency": task.frequency,
                "time": task.time,
                "enabled": task.enabled,
                "last_run": task.last_run,
                "last_completed_local_date": task.last_completed_local_date,
                "next_run": task.next_run_iso,
            }
            sync_automation_status_to_workspace(
                user_scope_id=task.user_scope_id,
                automation_data=payload,
                run_status=run_status,
                summary=summary,
                event=event,
            )
        except Exception:
            pass
    
    def create(self, task: AutomationTask) -> AutomationTask:
        """Create a new automation task: a copy of `task` with a new `incarnation`, also when
        its id was carried by a task before (a retried approval creates its planned id
        again). Returns the copy, the task as stored. The caller's object is left as it is:
        it may be one a run already holds (a task from `get()` reused for its replacement),
        and that run must still know it as the task it was."""
        # next_run is calculated dynamically - no need to store it
        task = copy.deepcopy(task)
        task.incarnation = uuid.uuid4().hex[:12]
        # A new task has read nothing yet: a cursor carried in (a stored task reused for its
        # replacement) would make the watch start there and run frames said before it existed.
        task.trigger = _without_cursor(task.trigger)
        path = self._path_for_task(task)
        with self._store_lock(path.parent):
            self._save_task(task)
            self.tasks[task.id] = task
        self._sync_workspace_automation_state(task, event="automation_created")

        # Arm the new task with the running scheduler immediately (no restart needed). The web/agent
        # create path constructs a FRESH AutomationManager whose own scheduler is not running, so arming
        # via this instance (self._running) alone is skipped and the task would never fire until the next
        # restart (and a one-time task whose time passes meanwhile would never fire at all).
        # refresh_scheduler_from_disk re-reads disk and rebuilds the jobs on the process-global running
        # scheduler instead — the same mechanism update() already uses. Best-effort: the task is persisted
        # regardless, so a scheduler hiccup must never fail creation. The self._running fallback covers the
        # CLI flow where this instance IS the (about-to-start) scheduler.
        try:
            if not refresh_scheduler_from_disk(origin=f"create:{task.id}") and self._running:
                self._schedule_task(task)
        except Exception as e:
            self._log_scheduler_event(f"CREATE_ARM_FAILED task_id={task.id} error={e!r}")

        return task

    def should_skip_daily_catch_up_run(self, new_task: AutomationTask) -> tuple[bool, str]:
        """
        Avoid immediate post-create runs when another automation in the same family
        already completed today (local date)—e.g. second 'Morgenbriefing' after one ran at 07:00.
        """
        if str(new_task.frequency or "").lower() != "daily":
            return False, ""
        from vaf.core.user_time import user_now
        today = user_now(_resolve_username(new_task.user_scope_id)).date()
        for peer in self.list():
            if peer.id == new_task.id:
                continue
            if not peer.enabled:
                continue
            if str(peer.frequency or "").lower() != "daily":
                continue
            if _last_effective_completion_local_date(peer) != today:
                continue
            if _same_automation_family_for_catchup(new_task.name, peer.name):
                return True, (
                    f"Another automation '{peer.name}' ({peer.id}) in the same family already "
                    f"ran today ({peer.last_run}); skipping immediate catch-up for '{new_task.name}'."
                )
        return False, ""
    
    def check_can_create_automation(self, new_time: str = None, new_frequency: str = None) -> tuple[bool, Optional[str]]:
        """
        Check if a new automation can be created. Enforces a minimum interval of
        MIN_AUTOMATION_INTERVAL_MINUTES between any two automations.
        
        Args:
            new_time: Time of the new automation (HH:MM format)
            new_frequency: Frequency of the new automation (daily, hourly, etc.)
        
        Returns: (can_create, error_message)
        """
        if not new_time or ":" not in new_time:
            return (True, None)
        existing_tasks = self.list(enabled_only=True)
        for task in existing_tasks:
            gap = _min_gap_minutes(task.time, new_time)
            if gap < MIN_AUTOMATION_INTERVAL_MINUTES:
                error_msg = (
                    f"ERROR: Automation time too close to an existing one.\n\n"
                    f"Another automation '{task.name}' ({task.id}) runs at {task.time}. "
                    f"The chosen time ({new_time}) is only {gap} minute(s) apart. "
                    f"Choose a time at least {MIN_AUTOMATION_INTERVAL_MINUTES} minutes apart (e.g. {task.time} → use 06:10 if the other is 06:00).\n\n"
                    f"**Solution:** Pick a different time in HH:MM format, or update the existing automation with `update_automation`."
                )
                return (False, error_msg)
        # Global cap: max 3 users per (time, frequency) slot
        occupancy = _slot_occupancy(self.base_dir)
        slot = (new_time, (new_frequency or "daily"))
        users_at_slot = occupancy.get(slot, set())
        effective_scope = self.user_scope_id or "__global__"
        if effective_scope not in users_at_slot and len(users_at_slot) >= MAX_USERS_PER_SLOT:
            return (
                False,
                f"Too many other users have already booked this time slot ({new_time}). "
                f"Please choose another slot at least {SUGGESTED_SLOT_GAP_MINUTES} minutes apart."
            )
        return (True, None)

    def check_can_update_automation(
        self, task_id: str, new_time: str, new_frequency: str = None
    ) -> tuple[bool, Optional[str]]:
        """
        Check if an automation's time can be updated. Same 10-minute minimum
        interval rule as create; excludes the task being updated.
        
        Returns: (can_update, error_message)
        """
        if not new_time or ":" not in new_time:
            return (True, None)
        existing_tasks = self.list(enabled_only=True)
        for task in existing_tasks:
            if task.id == task_id:
                continue
            gap = _min_gap_minutes(task.time, new_time)
            if gap < MIN_AUTOMATION_INTERVAL_MINUTES:
                error_msg = (
                    f"ERROR: New time too close to another automation.\n\n"
                    f"Automation '{task.name}' ({task.id}) runs at {task.time}. "
                    f"The new time ({new_time}) is only {gap} minute(s) apart. "
                    f"Choose a time at least {MIN_AUTOMATION_INTERVAL_MINUTES} minutes apart."
                )
                return (False, error_msg)
        # Global cap: max 3 users per (time, frequency) slot
        occupancy = _slot_occupancy(self.base_dir)
        slot = (new_time, (new_frequency or "daily"))
        users_at_slot = occupancy.get(slot, set())
        effective_scope = self.user_scope_id or "__global__"
        if effective_scope not in users_at_slot and len(users_at_slot) >= MAX_USERS_PER_SLOT:
            return (
                False,
                f"Too many other users have already booked this time slot ({new_time}). "
                f"Please choose another slot at least {SUGGESTED_SLOT_GAP_MINUTES} minutes apart."
            )
        return (True, None)
    
    def update(self, task_id: str, **kwargs) -> Optional[AutomationTask]:
        """Update an existing task: the given fields, written onto the record as it is on disk
        now (`_mutate_record`), so another lane's edit of a different field made since this
        manager loaded the task stays. Two edits of the same field: the one written last
        stands. The task as written, a NEW object (the cached one is not changed in place);
        None when there is no such task, or it was deleted or became unreadable meanwhile."""
        cached = self.tasks.get(task_id)
        if cached is None:
            return None
        # The schedule fields are coerced here (every update lane, the WebSocket, the agent's
        # tool, an approved handoff and the CLI, reaches the store through this call). Only
        # stored fields are applied, never the identity: an `id` or `user_scope_id` would
        # move the file, an `incarnation` would make a run's stamp miss its own task, and a
        # property name is not a field.
        fields = AutomationTask.__dataclass_fields__
        changes = {key: value for key, value in coerce_schedule_fields(kwargs).items()
                   if key in fields and key not in ("id", "user_scope_id", "incarnation")}
        def apply(record: Dict[str, Any]) -> None:
            stored = record.get("trigger")
            record.update(changes)
            if "trigger" in changes:
                # The read position follows the room, never moves back within it.
                record["trigger"] = _carried_cursor(stored, changes["trigger"])

        task = self._mutate_record(cached, apply)
        if task is None:
            return None

        # After the store lock is released: the mirror and the scheduler refresh take locks
        # of their own (`_store_lock` is a leaf).
        self._sync_workspace_automation_state(task, event="automation_updated")
        # Keep the live scheduler in sync with on-disk changes (e.g. updated time).
        try:
            refresh_scheduler_from_disk(origin=f"update:{task.id}")
        except Exception:
            pass
        return task
    
    def delete(self, task_id: str, permanent: bool = False) -> bool:
        """Delete a task (moves to trash by default, or permanently if specified)."""
        task = self.tasks.get(task_id)
        if task is None:
            return False
        return self._delete(task, permanent=permanent)

    def _remove_spent_once(self, task: AutomationTask) -> bool:
        """Remove a one-time task its run consumed, and only that task: one created anew under
        the same id while the run went (a retried approval creates its planned id again) has
        not run and stays. False when nothing was removed.

        Never raises: it is the bookkeeping after a run, and a store that is busy or cannot be
        written must not turn a run that did its work into an error (or skip the agent's
        shutdown after it). The failure is logged; the task then stays on disk, the named
        boundary in docs/platform/AUTOMATIONS.md."""
        try:
            return self._delete(task, permanent=True, only_this_incarnation=True)
        except Exception as e:
            append_domain_log("backend", f"[AUTOMATION] spent one-time {task.id} not removed: {e!r}")
            return False

    def _delete(self, task: AutomationTask, *, permanent: bool,
                only_this_incarnation: bool = False) -> bool:
        task_id = task.id
        filepath = self._path_for_task(task)
        trash_path = self.trash_dir / f"{task_id}.json"

        # Under the store lock: a change that took the lock first is written before the file
        # goes, and one that comes after finds no file and writes nothing (`_mutate_record`).
        # The cache follows the file inside the lock, in the same order.
        with self._store_lock(filepath.parent):
            if only_this_incarnation:
                record = _read_task_record(filepath)
                if record is None or str(record.get("id")).strip() != str(task_id) \
                        or _incarnation(record.get("incarnation")) != task.incarnation:
                    return False
            if filepath.exists():
                if permanent:
                    # Permanent deletion
                    filepath.unlink()
                else:
                    # Move to trash
                    import shutil
                    shutil.move(str(filepath), str(trash_path))

            # The run log lives beside the task file, so it has to follow it - otherwise a
            # deleted automation leaves its history orphaned in the storage directory forever,
            # and a restore comes back without the record the review rung reads. Tolerates a
            # missing log: it only starts existing once the task has actually run.
            _log_src = _run_log_path(filepath)
            if _log_src.exists():
                try:
                    if permanent:
                        _log_src.unlink()
                    else:
                        import shutil
                        shutil.move(str(_log_src), str(_run_log_path(trash_path)))
                except OSError as _e:
                    append_domain_log("backend", f"[AUTOMATION] run log not moved for {task_id}: {_e}")
            self.tasks.pop(task_id, None)

        try:
            task.enabled = False
            self._sync_workspace_automation_state(task, event="automation_deleted")
        except Exception:
            pass
        return True
    
    def move_to_trash(self, task_id: str) -> bool:
        """Move a task to trash (recoverable deletion)."""
        return self.delete(task_id, permanent=False)
    
    def restore_from_trash(self, task_id: str) -> bool:
        """Restore a task from trash."""
        trash_path = self.trash_dir / f"{task_id}.json"
        if not trash_path.exists():
            return False
        
        try:
            # Load task from trash (preserves user_scope_id so we restore to correct dir)
            with open(trash_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            task = AutomationTask.from_dict(data)
            
            # Move back to storage (user scope dir if task has user_scope_id)
            filepath = self._path_for_task(task)
            filepath.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            with self._store_lock(filepath.parent):
                # Never over a live task of the same id: the move replaced it silently, and
                # an approval retry creates its planned id again.
                if filepath.exists():
                    append_domain_log("backend", f"[AUTOMATION] {task_id} not restored: a task "
                                                 f"with this id is in the store")
                    return False
                shutil.move(str(trash_path), str(filepath))
                _log_trash = _run_log_path(trash_path)
                if _log_trash.exists():
                    try:
                        shutil.move(str(_log_trash), str(_run_log_path(filepath)))
                    except OSError as _e:
                        append_domain_log("backend", f"[AUTOMATION] run log not restored for {task_id}: {_e}")
                # Back into the cache inside the lock, in the order the file changed.
                self.tasks[task_id] = task
        except Exception:
            return False

        # After the store lock is released (`_store_lock` is a leaf): the mirror that the
        # delete set to switched off shows the task's real state again, and the running
        # scheduler arms it as after a create. Every lane mutates through a fresh manager, so
        # without the refresh the restored task stayed unarmed (and a restored event task
        # out of the scheduler's list) until the next create, update or restart. Best-effort:
        # the task is back on disk either way.
        self._sync_workspace_automation_state(task, event="automation_restored")
        try:
            if not refresh_scheduler_from_disk(origin=f"restore:{task_id}") and self._running:
                self._schedule_task(task)
        except Exception as e:
            self._log_scheduler_event(f"RESTORE_ARM_FAILED task_id={task_id} error={e!r}")
        return True
    
    def list_trash(self) -> List[AutomationTask]:
        """List all tasks in trash."""
        tasks = []
        for filepath in _task_files(self.trash_dir):
            try:
                data = _read_task_record(filepath)
                if data is None:
                    continue
                task = AutomationTask.from_dict(data)
                # Same stamp as `_load_tasks`: a scoped manager's trash holds only its own
                # scope's tasks, and a record written without a scope still belongs to it.
                if self.user_scope_id and not task.user_scope_id:
                    task.user_scope_id = self.user_scope_id
                tasks.append(task)
            except Exception:
                continue
        return tasks
    
    def empty_trash(self) -> int:
        """Permanently delete all tasks in trash. Returns number of deleted tasks."""
        count = 0
        for filepath in self.trash_dir.glob("*.json"):
            try:
                filepath.unlink()
                count += 1
            except Exception:
                continue
        return count
    
    def get(self, task_id: str) -> Optional[AutomationTask]:
        """Get a task by ID."""
        return self.tasks.get(task_id)
    
    def list(self, enabled_only: bool = False) -> List[AutomationTask]:
        """List all tasks, next run first. The sort names the account behind every task
        (its time zone decides the next run) from ONE read of the account directory; a
        caller that reads each task's next run again opens the same block around both."""
        from vaf.core.tool_dispatch import account_directory_once
        tasks = list(self.tasks.values())
        if enabled_only:
            tasks = [t for t in tasks if t.enabled]
        # A record with no readable clock sorts LAST and raises nothing: every surface
        # reads this listing, so one bad file must not take them all down.
        def _key(task: AutomationTask):
            when = task.next_run_datetime
            return (when is None, when or datetime.min)
        with account_directory_once():
            return sorted(tasks, key=_key)
    
    def _get_last_run_time(self) -> Optional[datetime]:
        """Get the timestamp of the last automation run."""
        if not self.last_run_file.exists():
            return None
        
        try:
            with open(self.last_run_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                last_run_str = data.get("last_run")
                if last_run_str:
                    return datetime.fromisoformat(last_run_str)
        except Exception:
            return None
        return None
    
    def _save_last_run_time(self):
        """Save the current time as the last automation run time."""
        try:
            data = {
                "last_run": datetime.now().isoformat()
            }
            with open(self.last_run_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass
    
    def _extract_clean_answer(self, history: List[Dict], clean: Callable[[str], str]) -> str:
        """The newest assistant text of the run, cleaned by the rule the stream got.

        `clean` is the agent's own reply cleaner (`Agent._clean_reasoning`): only the
        reasoning the model marked and leaked tool-call JSON go, plain text is the answer as
        written, in any language. Used when nothing unmarked reached the stream: the reply was
        all marked reasoning, or its last words went to the history only (a background-task
        acknowledgement, a stop note). The walk goes newest first and passes over a message the
        cleaner leaves empty, so a run that ended on a reasoning-only round still finds the
        answer it gave before it.

        Reasoning is never returned with only its tags removed: that text IS the reasoning.
        The engine's own placeholders for a round without text ("Thinking...", "[Empty]") are
        passed over like an empty message: status, not an answer. Nothing found gives "", which
        the delivery shows as "Completed".
        """
        from vaf.core.completion import HISTORY_PLACEHOLDERS
        for msg in reversed(history or []):
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if not isinstance(content, str) or not content:
                continue
            text = (clean(content) or "").strip()
            if text and text not in HISTORY_PLACEHOLDERS:
                return text
        return ""
    
    def _check_cooldown(self) -> tuple[bool, Optional[float]]:
        """
        Check if enough time has passed since the last automation run.
        Returns: (can_run, seconds_remaining)
        """
        MIN_COOLDOWN_SECONDS = 180  # 3 minutes
        
        last_run_time = self._get_last_run_time()
        if last_run_time is None:
            # No previous run, allow execution
            return (True, None)
        
        time_since_last = datetime.now() - last_run_time
        seconds_passed = time_since_last.total_seconds()
        
        if seconds_passed >= MIN_COOLDOWN_SECONDS:
            return (True, None)
        else:
            seconds_remaining = MIN_COOLDOWN_SECONDS - seconds_passed
            return (False, seconds_remaining)
    
    def run_task(self, task: AutomationTask, callback: Callable = None, new_terminal: bool = True,
                 trigger: Optional[Dict[str, Any]] = None, *,
                 on_outcome: Optional[Callable[["RunOutcome"], None]] = None) -> str:
        """
        Execute an automation task.
        
        Args:
            task: The automation task to run
            callback: Optional callback for progress updates
            new_terminal: If True, run in a new terminal window (default: True)
            trigger: For an event-driven run, what triggered it (automation_triggers.trigger_context):
                appended to the prompt on the prompt lane, offered as template variables on the
                workflow lane. Never stored on the task. Forces the in-process lane, because a
                spawned terminal would run the task without it.
            on_outcome: Called once with the run's `RunOutcome`, right after its run log is
                written; never for a run that was skipped, locked, revoked or handed to a
                terminal. It cannot fail the run.
        """
        if trigger is not None:
            new_terminal = False
        # Nothing runs on a schedule for an account without access - deactivated, deleted,
        # or revoked in this process (vaf.core.revocation.account_stands).
        from vaf.core.revocation import account_stands, is_revoked
        if not account_stands(task.user_scope_id):
            msg = f"[SKIPPED] Automation '{task.name}' ({task.id}): its account has no access."
            append_domain_log_always("backend", msg)
            return msg
        from vaf.cli.ui import UI
        from vaf.core.platform import Platform
        from vaf.core.lock_manager import LockManager
        import sys
        import subprocess
        
        # 🔒 SINGLETON PROTECTION: Prevent same automation from running twice
        lock_id = f"automation_{task.id}"
        if not LockManager.acquire(lock_id):
            msg = f"[LOCK] Automation '{task.name}' ({task.id}) is already running. Skipping."
            append_domain_log_always("backend", msg)
            return msg

        # Start of THIS run, for the run log below. `last_run` on the task cannot serve: it is
        # stamped only on success, which is the whole gap the log closes.
        _run_started = datetime.now()

        # Speichere aktuelle Zeit als letzte Ausführung (für Cooldown beim Erstellen neuer Automatisierungen)
        # WICHTIG: Cooldown wird nur beim ERSTELLEN geprüft, nicht bei geplanten Ausführungen!
        self._save_last_run_time()
        
        # Silent execution - don't show notifications in main terminal to keep input area free
        # If new_terminal is True, open in new terminal window
        if new_terminal:
            # Build command to run automation
            # Use 'vaf automation run <id>' command
            vaf_cmd = f'"{sys.executable}" -m vaf.main automation run {task.id}'
            
            # Try to open in new terminal
            title = f"VAF Automation: {task.name}"
            # Release lock before spawning new terminal, because the NEW process will acquire its own lock!
            LockManager.release(lock_id)
            
            # The child's session comes from an EXPLICIT env entry, like every other spawn
            # site. Inheritance is not a fallback here: the parent's environment is shared
            # by every lane in this process, so an inherited value is whichever session
            # happened to be set last, and this was the one caller still relying on it.
            _auto_env = {"VAF_AGENT_TYPE": "automation"}
            _auto_sid = get_current_session_id()
            if _auto_sid:
                _auto_env["VAF_SESSION_ID"] = _auto_sid

            if Platform.open_new_terminal(vaf_cmd, title=title, extra_env=_auto_env):
                # Silent - don't show notification in main terminal
                return f"Automation '{task.name}' started in new terminal window"
            else:
                # Fallback: run in background thread if new terminal fails (never block!)
                # Silent - don't show notification in main terminal
                import threading
                def run_in_background():
                    try:
                        self.run_task(task, callback=callback, new_terminal=False)
                    except Exception as e:
                        # Silent - only log to debug, don't show in main terminal
                        # Background execution errors are silently ignored
                        pass
                thread = threading.Thread(target=run_in_background, daemon=True)
                thread.start()
                return f"Automation '{task.name}' started in background thread (new terminal unavailable)"
        
        # How the run ended, decided once per lane (or by the exception handler) and read by
        # every consumer below: the stamp, the output file, the run log, the delivery.
        outcome: Optional[RunOutcome] = None
        # True on the workflow lane when it succeeded: its write_file step already saved the
        # output, so the legacy output file is not written a second time.
        workflow_saved_file = False
        # Final path of the saved output file (if any). Initialised here so it survives to the
        # delivery call after the try/finally; set only when a file is actually written, or
        # when a successful workflow's last step produced one.
        saved_output_path: Optional[str] = None
        # True when the run already delivered to the user in-run (confirmed by the tool
        # result): a workflow send step, or a send tool of the prompt-based agent. Then the
        # post-run messenger text is not pushed a second time; survives to the delivery below.
        delivered_in_run = False
        # Companion: True when that in-run send already carried a document attachment
        # (then the post-run file follow-up must not send a second one).
        file_delivered_in_run = False
        # True when a prompt run hit the time limit AND did not finish within the
        # grace window: no partial stream as result, no legacy file wrap - the user
        # gets one honest timeout note instead (live 2026-07-13: the half stream was
        # wrapped into a junk HTML and pushed, then the zombie delivered again).
        prompt_timeout_unresolved = False
        # The agent a room-triggered run bound as a room turn; its turn is ended below on
        # every exit, so the room and conversation it set do not outlive the run.
        _room_bound_agent = None

        try:
            # ... (Rest of the method logic) ...

            # Set environment variables for non-interactive automation mode
            # This prevents user prompts during automation execution.
            # Both are process-global, so the PRIOR value is remembered and put back in the
            # cleanup below. Without that, VAF_NONINTERACTIVE stayed set for the life of the
            # process after the first automation, and a live chat turn - which reads it at
            # agent.py:6330 and :6464, not only at construction - stopped asking the human
            # it was waiting for. The comment there already said "keep it if it was set
            # before"; nothing recorded what "before" was.
            import os
            _prior_noninteractive = os.environ.get("VAF_NONINTERACTIVE")
            os.environ["VAF_NONINTERACTIVE"] = "1"
            os.environ["VAF_IN_AUTOMATION"] = "1"
            
            # If workflow_steps exist, use workflow engine (n8n-like)
            if task.workflow_steps and len(task.workflow_steps) > 0:
                from vaf.workflows.engine import WorkflowEngine, WorkflowStep
                from vaf.core.agent import Agent

                # Initialize agent to get tools
                agent = Agent(verbose=False, run_kind="automation")
                agent.load_model()
                agent.init_chat()
                # Background run: this agent must stay SILENT — its tool_update emits must never broadcast
                # into a live user's chat (a scheduled automation has no own web session, so the global
                # session fallback would otherwise route its tool bubbles to whoever is the active web user).
                agent._background_run = True
                # User isolation: the run adopts the task owner's scope and account name.
                bind_identity(agent, resolve_scope_identity(task.user_scope_id))

                # Get all available tools
                all_tools = {**agent.tools}
                
                # Load additional tools that might be needed
                try:
                    from vaf.tools.filesystem import WriteFileTool, ReadFileTool, ListFilesTool
                    from vaf.tools.search import WebSearchTool
                    from vaf.tools.coder import CodingAgentTool
                    from vaf.tools.librarian import LibrarianTool
                    from vaf.tools.python_sandbox import PythonSandboxTool
                    
                    all_tools["write_file"] = WriteFileTool()
                    all_tools["read_file"] = ReadFileTool()
                    all_tools["list_files"] = ListFilesTool()
                    all_tools["web_search"] = WebSearchTool()
                    all_tools["coding_agent"] = CodingAgentTool()
                    all_tools["librarian_agent"] = LibrarianTool()
                    all_tools["python_sandbox"] = PythonSandboxTool()
                except ImportError:
                    pass
                
                # Convert workflow_steps to WorkflowStep objects
                steps = []
                date_str = datetime.now().strftime("%Y-%m-%d")
                for i, step_def in enumerate(task.workflow_steps):
                    # Replace date placeholder in paths (both {date} and {{date}})
                    step_args = step_def.get("args", {}).copy()
                    if "path" in step_args:
                        path_str = str(step_args["path"])
                        # Replace both single and double braces
                        path_str = path_str.replace("{{date}}", date_str).replace("{date}", date_str)
                        step_args["path"] = path_str
                    
                    steps.append(WorkflowStep(
                        tool=step_def["tool"],
                        args_template=step_args,
                        input_template=step_def.get("input", ""),
                        output_name=step_def.get("output", f"step_{i+1}"),
                        description=step_def.get("description", f"Execute {step_def['tool']}")
                    ))
                
                # Execute workflow with detailed step tracking
                step_results = []
                def workflow_callback(event, step, current, total):
                    if event == "start":
                        step_info = f"⚙️ Step {current}/{total}: {step.tool}"
                        if step.description:
                            step_info += f" - {step.description}"
                        step_results.append({
                            "step": current,
                            "tool": step.tool,
                            "description": step.description,
                            "status": "running"
                        })
                        if callback:
                            callback(f"\n{step_info}\n")
                    elif event == "success":
                        step_results[-1]["status"] = "success"
                        step_results[-1]["result"] = str(step.result)[:200] if step.result else "Completed"
                    elif event == "error":
                        step_results[-1]["status"] = "failed"
                        step_results[-1]["error"] = str(step.error) if step.error else "Unknown error"
                
                engine = WorkflowEngine(
                    all_tools,
                    callback=workflow_callback,
                    user_scope_id=task.user_scope_id,
                    username=agent._current_username,
                    authorize=getattr(agent, "_tool_authorizer", None),
                )
                # Add 'date' to workflow defaults so {date} can be resolved in templates
                engine._workflow_defaults = {"date": date_str}
                engine._workflow_name = task.name
                variables = dict(task.parameters)
                if trigger is not None:
                    from vaf.core.automation_triggers import trigger_variables
                    variables.update(trigger_variables(trigger))
                # Stopped like the prompt lane when the account's access is taken away
                # mid-run: between steps and inside a step's bounded wait.
                workflow_result = engine.execute(
                    steps, variables=variables,
                    check_stop=lambda: is_revoked(task.user_scope_id))

                if getattr(workflow_result, "paused", False):
                    # PAUSED, NOT FAILED: a step handed off to an async sub-agent, so the run
                    # is still alive and its remaining steps continue when the drain resumes
                    # it. The old code fell through to the failure branch below and wrote
                    # "# Workflow Failed / Error: None" into the automation report (error is
                    # None precisely because nothing failed).
                    _agent_t = getattr(workflow_result, "waiting_for_agent", "") or "a background helper"
                    _task_t = str(getattr(workflow_result, "waiting_for_task", "") or "?")
                    result = (
                        f"# Workflow Still Running\n\n"
                        f"Step {len([s for s in step_results if s.get('status') == 'success']) + 1} "
                        f"of {len(steps)} handed off to {_agent_t} [Task: {_task_t}].\n"
                        f"Nothing failed. The remaining steps continue on their own and the "
                        f"result is delivered when the helper is done.\n\n"
                    )
                    result += "## Steps so far:\n"
                    for i, step_result in enumerate(step_results, 1):
                        result += f"{i}. {step_result['tool']} - {step_result['status']}\n"
                    outcome = RunOutcome("paused", result)
                elif workflow_result.success:
                    # MULTI-STEP AUTOMATION: Build detailed output showing each step
                    result_parts = ["# Workflow Execution Report\n"]
                    result_parts.append(f"**Automation:** {task.name}\n")
                    result_parts.append(f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    result_parts.append(f"**Steps:** {len(steps)}\n\n")
                    
                    for i, step_result in enumerate(step_results, 1):
                        status_icon = "✅" if step_result["status"] == "success" else "❌" if step_result["status"] == "failed" else "⚙️"
                        result_parts.append(f"## {status_icon} Step {i}: {step_result['tool']}\n")
                        if step_result.get("description"):
                            result_parts.append(f"**Description:** {step_result['description']}\n")
                        result_parts.append(f"**Status:** {step_result['status'].upper()}\n")
                        if step_result.get("result"):
                            result_parts.append(f"**Result:** {step_result['result']}\n")
                        if step_result.get("error"):
                            result_parts.append(f"**Error:** {step_result['error']}\n")
                        result_parts.append("\n")
                    
                    if workflow_result.final_output:
                        result_parts.append("---\n")
                        result_parts.append("## Final Output\n")
                        result_parts.append(str(workflow_result.final_output))
                    
                    result = "".join(result_parts)
                    # IMPORTANT: If workflow has write_file step, it already saved the output
                    # Skip legacy output_path saving to avoid duplicate files
                    workflow_saved_file = True
                    # A short, friendly summary for the person instead of the raw technical
                    # report. final_output is the LAST step's result string - only treat it as
                    # a saved file when it actually IS one, otherwise the Web UI printed tool
                    # chatter like "Gespeichert: Message sent to the user via Telegram." (live
                    # 2026-07-13).
                    saved_path = str(workflow_result.final_output or "").strip()
                    saved_output_path = (saved_path if (saved_path and os.path.isfile(saved_path))
                                         else None)
                    short_summary = (
                        f"✅ **{task.name}** ist fertig!\n\n"
                        + (f"{saved_output_path}\n\n" if saved_output_path else "")
                        + f"Alle {len(steps)} Schritte erfolgreich abgeschlossen."
                    ).strip()
                    outcome = RunOutcome("ok", short_summary, report=result)
                    # A send step already reached the user: no second messenger push.
                    delivered_in_run = _delivered_via_send_step(step_results)
                    file_delivered_in_run = _file_delivered_via_send_step(step_results)
                else:
                    result = f"# Workflow Failed\n\n**Error:** {workflow_result.error}\n\n"
                    result += "## Steps Executed:\n"
                    for i, step_result in enumerate(step_results, 1):
                        status_icon = "✅" if step_result["status"] == "success" else "❌"
                        result += f"{status_icon} Step {i}: {step_result['tool']} - {step_result['status']}\n"
                    outcome = RunOutcome("failed", result)

                agent.shutdown()
            else:
                # Legacy: Use prompt-based execution (backwards compatibility)
                # ONE-STEP AUTOMATION: Simple prompt, clean output only
                prompt = task.prompt
                for key, value in task.parameters.items():
                    prompt = prompt.replace(f"{{{key}}}", str(value))
                if trigger is not None:
                    from vaf.core.automation_triggers import prompt_with_trigger
                    prompt = prompt_with_trigger(prompt, trigger)
                
                # Import agent and run
                from vaf.core.agent import Agent

                agent = Agent(verbose=False, run_kind="automation")
                agent.load_model()
                # A run a room message started is a room turn: bound before its prompt is
                # built (`bind_turn` starts the conversation of its own), so the prompt, the
                # tools and the working memory are the room turn's (_room_trigger_binding).
                if trigger is not None:
                    from vaf.core.turn_binding import bind_turn
                    bind_turn(agent, _room_trigger_binding(task, trigger))
                    _room_bound_agent = agent
                else:
                    agent.init_chat()
                # RUNAWAY GUARD: a prompt-based automation fallback must NOT recursively spawn workflows or
                # more automations — that is what flooded /api/subagent/stream and required a process kill.
                # Strip the recursive spawners (ordinary content sub-agents like coding_agent stay and are
                # each already time-bounded by run_bounded). Belt-and-suspenders with the chat_step timeout below.
                try:
                    for _t in ("create_automation", "create_agent_workflow", "execute_workflow"):
                        if isinstance(getattr(agent, "tools", None), dict):
                            agent.tools.pop(_t, None)
                except Exception:
                    pass
                # Background run: stay SILENT — tool_update emits must never broadcast into a live user's
                # chat (no own web session -> the global session fallback would route tool bubbles to the
                # active web user; observed as an automation leaking into a LAN client's chat).
                agent._background_run = True
                # So calendar and create_automation tools use the correct user (same scope as this task).
                # A room-triggered run is already bound to it, with the room's markers.
                if trigger is None:
                    bind_identity(agent, resolve_scope_identity(task.user_scope_id))

                # Tell the agent it is the same agent, just running an automation in the background.
                if os.environ.get("VAF_IN_AUTOMATION", "").strip() in ("1", "true", "yes"):
                    automation_notice = (
                        "\n\n## AUTOMATION MODE\n"
                        "You are the **main agent**. You are currently **executing an automation in the background** (autonomous / selbständig). "
                        "Act on your own to complete the task given in the user message below.\n"
                        "This runs silently: the user sees only your final result, not your steps. "
                        "If you need the user, use `ask_user` — but this is a HIGH BAR: ONLY for a genuine blocker "
                        "or an important clarification you truly cannot resolve on your own, NEVER for status "
                        "('starting', 'working on it'). If you can proceed on a reasonable assumption, do so and "
                        "note the assumption in your result instead of asking. When you do call `ask_user`, your "
                        "full working context is handed to the user's main agent, which continues the task after "
                        "they reply — so ask once, clearly, then stop."
                    )
                    if agent.history and agent.history[0].get("role") == "system":
                        agent.history[0]["content"] = (agent.history[0]["content"] or "") + automation_notice

                # Capture the stream untouched. The reasoning strip after the run removes whole
                # marked blocks, so it needs every block whole: a chunk with its tags stripped keeps
                # the reasoning TEXT and loses the marks that tell it apart from the answer, which is
                # how a model's reasoning was once delivered as the result.
                raw_parts = []
                def capture(text):
                    raw_parts.append(text)
                    if callback:
                        callback(text)

                # What this account already knows, before the model answers.
                memory_context = ""
                try:
                    from uuid import UUID as _UUID
                    from vaf.memory.rag import turn_memory_context
                    task_scope = None
                    if task.user_scope_id:
                        try:
                            task_scope = _UUID(str(task.user_scope_id))
                        except (ValueError, TypeError):
                            task_scope = None
                    memory_context = turn_memory_context(
                        prompt, user_scope_id=task_scope, caller="automation")
                except Exception:
                    memory_context = ""

                # RUNAWAY GUARD: bound the whole fallback turn so a stuck/looping provider (e.g. a stateful
                # gateway 400ing every tool turn) can never run unbounded. On timeout the caller is freed and
                # the automation lock released; the abandoned worker cannot keep spawning recursive workflows
                # because those tools were stripped above.
                _auto_to = 600.0
                # Only a failure to IMPORT the bounded runner falls back to a plain call. An
                # exception of the turn itself is the run's failure: answering it with a second,
                # unbounded turn ran the model twice and could repeat what the first one did.
                try:
                    from vaf.core.bounded_run import (
                        run_bounded as _run_bounded,
                        TIMEOUT_PREFIX as _TO_PREFIX,
                        STOPPED_PREFIX as _ST_PREFIX,
                    )
                except Exception:
                    _run_bounded = None
                try:
                    from vaf.core.config import Config as _CfgAuto
                    _auto_to = float(_CfgAuto.get("automation_run_timeout_seconds", 600) or 600)
                except Exception:
                    _auto_to = 600.0
                # The turn's return value and how it ended are what decide the run
                # (`_prompt_outcome`); `done` is set however the turn ends, also when it raised,
                # so the grace wait below does not outwait a turn that already failed.
                _chat_done = {"done": False, "answer": None, "error": None}

                def _do_chat():
                    try:
                        _chat_done["answer"] = agent.chat_step(
                            prompt, stream_callback=capture, memory_context=memory_context or None)
                    except Exception as exc:
                        _chat_done["error"] = exc
                        raise
                    finally:
                        _chat_done["done"] = True
                    return True

                _turn_error: Optional[BaseException] = None
                try:
                    if _run_bounded is None:
                        _do_chat()
                    else:
                        _bounded_ret = _run_bounded(
                            _do_chat, timeout=_auto_to, label="automation_prompt_run",
                            stop_check=lambda: is_revoked(task.user_scope_id))
                        # The old code ignored this return value entirely - a timed-out run was
                        # indistinguishable from a finished one, so the half stream became the
                        # "result" while the abandoned worker delivered again later.
                        if isinstance(_bounded_ret, str) and _bounded_ret.startswith(_TO_PREFIX):
                            append_domain_log_always(
                                "backend",
                                f"[AUTOMATION] '{task.name}' hit the {int(_auto_to)}s time limit - "
                                f"waiting up to {int(_TIMEOUT_GRACE_SECONDS)}s grace for the abandoned run to finish.",
                            )
                            if _wait_for_abandoned_run(_chat_done):
                                append_domain_log_always(
                                    "backend",
                                    f"[AUTOMATION] '{task.name}' finished within the grace window - "
                                    f"judging it like a turn that ended in time.",
                                )
                                _turn_error = _chat_done["error"]
                            else:
                                prompt_timeout_unresolved = True
                                append_domain_log_always(
                                    "backend",
                                    f"[AUTOMATION] '{task.name}' still unfinished after the grace window - "
                                    f"delivering a timeout note (no partial result, no file wrap).",
                                )
                        elif isinstance(_bounded_ret, str) and _bounded_ret.startswith(_ST_PREFIX):
                            # The account's access was taken away mid-run: no grace wait, no
                            # partial result (and no delivery, below).
                            prompt_timeout_unresolved = True
                except Exception as exc:
                    _turn_error = exc

                def _stream_result() -> str:
                    # A turn without an answer of its own: the cleaned stream. The SAME canonical
                    # cleaner the live chat uses (agent._clean_reasoning) removes whole marked
                    # reasoning blocks (content included), so reasoning is stripped structurally
                    # and in any language, not by phrase heuristics. When nothing unmarked reached
                    # the stream, the newest assistant message of the run that the same cleaner
                    # leaves non-empty is the result, else nothing.
                    text = ""
                    try:
                        text = (agent._clean_reasoning("".join(raw_parts)) or "").strip()
                    except Exception:
                        text = ""
                    if not text:
                        try:
                            text = self._extract_clean_answer(agent.history, agent._clean_reasoning)
                        except Exception:
                            text = ""
                    return text

                if _turn_error is not None:
                    outcome = RunOutcome("failed", f"Error: {_turn_error}")
                else:
                    outcome = _prompt_outcome(
                        _chat_done["answer"],
                        None if prompt_timeout_unresolved else agent.last_turn,
                        _stream_result,
                        timed_out=prompt_timeout_unresolved,
                        timeout_note=(
                            f"Zeitlimit überschritten: Die Automation '{task.name}' war nach "
                            f"{int(_auto_to)}s und der Nachfrist noch nicht fertig. Es gibt kein "
                            f"Teilergebnis, und was der Lauf danach noch erledigt, wird nicht als "
                            f"Ergebnis zugestellt."
                        ),
                    )
                # A send tool of this run already reached the user: no second messenger push.
                delivered_in_run = _delivered_via_agent_history(getattr(agent, "history", None))
                file_delivered_in_run = _file_delivered_via_agent_history(
                    getattr(agent, "history", None))

                agent.shutdown()

            # Access taken away while the run went: no output file, no "ran successfully"
            # stamp (the schedule would count it as done), no delivery, on either lane. The
            # `finally` below still releases the lock and consumes a one-time task.
            if is_revoked(task.user_scope_id):
                return _revoked_run_note(task)

            # Save output if path specified, and only for an answer: never for a failure, a
            # timeout or a paused workflow (wrapping a timeout note or a half stream into an
            # output file produced the junk HTML attachments of 2026-07-13), and never a second
            # time for a workflow whose own step already saved it.
            if task.output_path and outcome.status == "ok" and not workflow_saved_file:
                result = outcome.text
                # Resolve path properly (handle "Desktop", "Documents", etc.)
                output_path_str = str(task.output_path).strip()
                output_path = Path(output_path_str).expanduser()
                
                # If path doesn't exist and looks like a folder alias, try to resolve it
                if not output_path.exists():
                    from vaf.core.platform import Platform
                    home = Path.home()
                    output_lower = output_path_str.lower()
                    
                    # Common folder aliases (cross-platform)
                    folder_aliases = {
                        "desktop": home / "Desktop",
                        "documents": home / "Documents",
                        "downloads": home / "Downloads",
                        "pictures": home / "Pictures",
                        "videos": home / "Videos",
                        "music": home / "Music",
                    }
                    
                    # German folder names (Windows often uses these)
                    if Platform.is_windows():
                        german_mappings = {
                            "desktop": ["Desktop", "Arbeitsplatz"],
                            "documents": ["Documents", "Dokumente"],
                            "pictures": ["Pictures", "Bilder"],
                            "videos": ["Videos"],
                            "music": ["Music", "Musik"],
                            "downloads": ["Downloads", "Herunterladen"],
                        }
                        
                        for key, variants in german_mappings.items():
                            for variant in variants:
                                path = home / variant
                                if path.exists():
                                    folder_aliases[key] = path
                    
                    # Check if output_path_str matches any alias
                    for alias, alias_path in folder_aliases.items():
                        if alias in output_lower or output_path_str.lower() == alias:
                            if alias_path.exists():
                                output_path = alias_path
                                break
                
                # Create filename with date. Use a sanitized, length-bounded stem — task.name can be the
                # whole prompt, which produced "[Errno 36] File name too long".
                date_str = datetime.now().strftime("%Y-%m-%d")
                _stem = _safe_filename_stem(task.name)
                if task.output_format == "html":
                    filename = f"{_stem}_{date_str}.html"
                    # If result already contains HTML structure, use it directly
                    # Otherwise, wrap it in a basic HTML structure
                    if result.strip().startswith("<!DOCTYPE") or result.strip().startswith("<html"):
                        content = result
                    else:
                        content = f"""<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{task.name}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            max-width: 800px;
            margin: 0 auto;
            padding: 20px;
            line-height: 1.6;
            background: #f5f5f5;
        }}
        .container {{
            background: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        h1 {{
            color: #333;
            border-bottom: 2px solid #4CAF50;
            padding-bottom: 10px;
        }}
        .meta {{
            color: #666;
            font-size: 0.9em;
            margin-bottom: 20px;
        }}
        pre {{
            background: #f4f4f4;
            padding: 15px;
            border-radius: 4px;
            overflow-x: auto;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>{task.name}</h1>
        <div class="meta">Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
        <div>
{result}
        </div>
    </div>
</body>
</html>"""
                elif task.output_format == "markdown":
                    filename = f"{_stem}_{date_str}.md"
                    content = f"# {task.name}\n\n*Generated: {datetime.now().isoformat()}*\n\n{result}"
                elif task.output_format == "json":
                    filename = f"{_stem}_{date_str}.json"
                    content = json.dumps({"task": task.name, "date": date_str, "content": result}, indent=2)
                else:
                    filename = f"{_stem}_{date_str}.txt"
                    content = result
                
                output_file = output_path / filename
                # Ensure parent directory exists (don't try to create Desktop itself)
                if not output_path.exists():
                    try:
                        output_path.mkdir(parents=True, exist_ok=True)
                    except PermissionError:
                        # Desktop might have permission issues on Windows, try Documents as fallback
                        from vaf.core.platform import Platform
                        if Platform.is_windows():
                            fallback = Path.home() / "Documents"
                            if fallback.exists():
                                output_path = fallback
                                output_file = output_path / filename
                                UI.warning(f"Could not write to Desktop, using Documents instead: {output_file}")
                
                output_file.write_text(content, encoding='utf-8')
                saved_output_path = str(output_file)

                UI.success(f"Output saved: {output_file}")
            
            # Update task (local completion date persists across restarts), only for a run that
            # counts as done: a failed or timed-out run stamped here read "Done (today)", was
            # skipped by the catch-up and hid from the review findings.
            # Only the run's own fields, onto the record as it is now: a time or switch
            # changed while the run went stays, a task deleted meanwhile stays deleted.
            if outcome.completed:
                self._record_successful_run(task)

            # If frequency is ONCE, delete after run, whatever the outcome (einmalig = einmalig).
            if task.frequency == Frequency.ONCE and self._remove_spent_once(task):
                append_domain_log_always("backend", f"Automation '{task.name}' ({task.id}) frequency is 'once' - deleted after run.")

            agent.shutdown()

        except Exception as e:
            outcome = RunOutcome("failed", f"Error: {e}")
            UI.error(f"Automation failed: {e}")
        finally:
            if _room_bound_agent is not None:
                try:
                    if prompt_timeout_unresolved:
                        # The run was abandoned, not finished: its worker may still be
                        # calling tools on this agent, and ending the turn would clear
                        # the room marker those calls are gated by. The agent is this
                        # run's own and never reused, so its markers stay with it; only
                        # this thread's room and conversation are cleared (the worker
                        # runs on a copy of the context, `run_bounded`).
                        from vaf.core.subagent_ipc import (set_current_conversation_id,
                                                           set_current_room_id)
                        set_current_room_id(None)
                        set_current_conversation_id(None)
                    else:
                        from vaf.core.turn_binding import end_turn
                        end_turn(_room_bound_agent)
                except Exception:
                    pass
            # 🔓 RELEASE LOCK
            try:
                from vaf.core.lock_manager import LockManager
                LockManager.release(f"automation_{task.id}")
            except Exception:
                pass

            # einmalig = einmalig: a one-time automation is consumed by its first
            # firing. Remove it here so it is gone whether execution succeeded OR
            # raised. The success/early-return paths above already delete it; this
            # finally only kicks in when the run errored out before reaching them —
            # otherwise the file lingers and the scheduler re-registers and re-runs
            # it on the next refresh/restart (showing up as "next run tomorrow").
            # Only the task that ran: one created anew under its id meanwhile stays.
            if task.frequency == Frequency.ONCE:
                try:
                    if self._remove_spent_once(task):
                        append_domain_log_always(
                            "backend",
                            f"Automation '{task.name}' ({task.id}) frequency is 'once' - removed after run attempt.",
                        )
                except Exception:
                    pass

            # Clean up environment variables: put back exactly what was there.
            # NAMED BOUNDARY on the shape: two automations running at once still share these
            # two process-global names, so a save/restore pair can interleave. Closing that
            # means carrying the flags per run the way the session id now is, which is a
            # change of its own; this one stops the permanent leak, which is the part that
            # outlives every run and silently disarms a human's confirmation prompt.
            import os
            os.environ.pop("VAF_IN_AUTOMATION", None)
            if _prior_noninteractive is None:
                os.environ.pop("VAF_NONINTERACTIVE", None)
            else:
                os.environ["VAF_NONINTERACTIVE"] = _prior_noninteractive
        
        if is_revoked(task.user_scope_id):
            return _revoked_run_note(task)
        if outcome is None:
            # Every lane decides its outcome; a run that reaches here without one did not
            # complete, and it never counts as done.
            outcome = RunOutcome("failed", NO_ANSWER_NOTE)

        # The one place both halves of an outcome are recorded, for both lanes: `last_run`
        # records only the completed runs, so without this an errored run leaves no trace.
        try:
            append_run_log(
                self._path_for_task(task),
                status=outcome.wire_status,
                started_at=_run_started.isoformat(),
                duration_seconds=(datetime.now() - _run_started).total_seconds(),
                summary=outcome.returned[:200],
            )
        except Exception:
            pass
        if on_outcome is not None:
            try:
                on_outcome(outcome)
            except Exception:
                pass

        # Deliver result to user via Web UI chat + messenger.
        # Only one delivery path - no duplicate notification + chat message.
        try:
            # For prompt-based tasks the result IS the summary (already clean text).
            # saved_output_path is None for chat-only runs (no file produced) -> text-only delivery.
            # A confirmed in-run send suppresses only the messenger TEXT push (live 2026-07-14:
            # the calendar check messaged the user twice); Web UI + notification stay, and a
            # produced file is still delivered attachment-only - it is written post-run, so the
            # in-run send cannot have carried it (live 2026-08-21: the report file never
            # reached the messenger).
            _push_result_to_web_ui(
                task, outcome.wire_status, outcome.text or "Completed",
                output_file=saved_output_path,
                deliver_messenger=not delivered_in_run,
                file_delivered_in_run=file_delivered_in_run,
            )
        except Exception:
            pass
        try:
            self._sync_workspace_automation_state(
                task,
                run_status=outcome.wire_status,
                summary=outcome.returned[:1000],
                event="automation_run",
            )
        except Exception:
            pass

        # Always return cleanly
        return outcome.returned
    
    def start_scheduler(self):
        """Start the background scheduler."""
        if self._running:
            self._log_scheduler_event("START_SKIPPED reason='already running'")
            return

        # One scheduler per process. A second manager instance starting "its" scheduler
        # arms every task a second time and spins up a second loop thread - every task
        # then fires twice and only the run lock prevents double execution (live
        # 2026-07-13: double TRIGGER on every automation, when the clock registry was
        # still module-global). Only the process-wide singleton may pump the scheduler. Deliberately read
        # WITHOUT _scheduler_manager_lock: ensure_scheduler_started() calls this
        # method while holding that (non-reentrant) lock - taking it here would
        # deadlock. The bare reference read is atomic under the GIL, and this guard
        # is defense-in-depth (the primary fix routes callers through the ensure
        # helper in the first place).
        global _scheduler_manager
        _process_sm = _scheduler_manager
        if (_process_sm is not None and _process_sm is not self
                and getattr(_process_sm, "_running", False)):
            self._log_scheduler_event(
                "START_SKIPPED reason='process scheduler already running on another manager instance'"
            )
            return
        
        self._running = True
        # A start arms every task afresh.
        self._clock_lock = getattr(self, "_clock_lock", None) or threading.Lock()
        self._clock_jobs = {}
        self._clock_claims = {}
        from vaf.core.tool_dispatch import account_directory_once
        with account_directory_once():
            enabled_tasks = self.list(enabled_only=True)
            self._log_scheduler_event(
                f"START task_count={len(enabled_tasks)} storage_dir={str(self.storage_dir)!r}"
            )
            # Arm every task from the same directory read the sort used.
            for task in enabled_tasks:
                self._schedule_task(task)
        
        def scheduler_loop():
            self._log_scheduler_event("LOOP_STARTED")
            while self._running:
                # On its own try like the lanes below: the clock reads task files, and no
                # file may end the loop that also delivers every account's reminders and
                # room triggers.
                try:
                    self._run_due_clock_jobs()
                except Exception as e:
                    self._log_scheduler_event(f"CLOCK_TICK_FAILED error={e!r}")
                # One-shot reminders (narrow lane, see vaf/core/reminders.py): a
                # reminder is stored data delivered verbatim - no agent run. Fired
                # here so only the process singleton ever delivers them.
                try:
                    from vaf.core.reminders import fire_due_reminders
                    fire_due_reminders()
                except Exception:
                    pass
                # Calendar reminders (vaf/core/calendar_sync.py): the same narrow lane for
                # the events in the VAF calendar - stored data, composed deterministically,
                # delivered on the main channel; no agent run, no tools.
                try:
                    from vaf.core.calendar_sync import fire_due_calendar_reminders
                    fire_due_calendar_reminders()
                except Exception:
                    pass
                # Event-driven automations: the "is it due" decision for a room event,
                # asked on the same tick the reminders ride, so only the process
                # singleton ever fires one (vaf/core/automation_triggers.py).
                try:
                    self._fire_room_triggers()
                except Exception:
                    pass
                time.sleep(30)  # Check every 30 seconds
            self._log_scheduler_event("LOOP_STOPPED")
        
        self._scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
        self._scheduler_thread.start()
    
    def stop_scheduler(self):
        """Stop the background scheduler."""
        self._log_scheduler_event("STOP_REQUESTED")
        self._running = False
        self._clear_clock_jobs()

    def _clear_clock_jobs(self) -> None:
        with self._clock_lock:
            self._clock_jobs.clear()
    
    def _schedule_task(self, task: AutomationTask,
                       into: Optional[Dict[str, _ClockJob]] = None):
        """Add a task to the scheduler: its clock job goes into the armed jobs, or into
        `into` (a refresh builds the new jobs apart and swaps them in at once)."""
        if not task.enabled:
            self._log_scheduler_event(
                f"REGISTER_SKIPPED task_id={task.id} name={task.name!r} reason='disabled'"
            )
            return
        
        if task.frequency == Frequency.ON_EVENT:
            # No clock job. The "is it due" question for an event is asked on every
            # tick of the scheduler loop (_fire_room_triggers), not registered here.
            self._log_scheduler_event(
                f"REGISTERED_EVENT task_id={task.id} name={task.name!r} when={task.schedule_label!r}"
            )
            return

        if task.frequency not in _CLOCK_FREQUENCIES:
            self._log_scheduler_event(
                f"REGISTER_SKIPPED task_id={task.id} name={task.name!r} reason='unsupported frequency {task.frequency}'"
            )
            return

        if task.frequency == Frequency.ONCE and (task.last_run or task.last_completed_local_date):
            # einmalig = einmalig: never re-arm a one-time task that has already
            # fired. Guards against a lingering file being re-registered after a
            # restart or scheduler refresh, which would otherwise run it again.
            self._log_scheduler_event(
                f"REGISTER_SKIPPED task_id={task.id} name={task.name!r} "
                f"reason='once already ran' last_run={task.last_run}"
            )
            try:
                self.delete(task.id, permanent=True)
            except Exception:
                pass
            return

        # The clock is the task's own next run, the one the lists and the calendar show,
        # computed in the owner's timezone with the standard library (user_time.user_now,
        # zoneinfo) and asked again after every run. A third-party scheduler used to keep a
        # second clock here, and it needed pytz for any timezone: pytz was never a dependency,
        # so once an owner had a timezone set, registering the first task raised and the
        # scheduler did not start at all (measured: "No module named 'pytz'" on every start).
        due = task.calculate_next_run()
        if due is None:
            self._log_scheduler_event(
                f"REGISTER_SKIPPED task_id={task.id} name={task.name!r} reason='no readable clock rule' "
                f"when={task.schedule_label!r}"
            )
            return
        job = _ClockJob(task=task, due=due, once=task.frequency == Frequency.ONCE)
        if into is not None:
            into[task.id] = job
        else:
            with self._clock_lock:
                self._clock_jobs[task.id] = job
        self._log_scheduler_event(
            f"REGISTERED task_id={task.id} name={task.name!r} when={task.schedule_label!r} "
            f"next={due:%Y-%m-%d %H:%M}"
        )

    def _swap_clock_jobs(self, fresh: Dict[str, _ClockJob]) -> None:
        """Put the jobs a refresh armed from disk in place of the armed ones, in one step
        under `_clock_lock`, and give a pending slot back to each task whose record still has
        the same incarnation and the same clock rule.

        A refresh arms every task at its next run counted from now, and every create, update
        and restore of ANY task refreshes. Measured before this: a daily task due seconds
        before another account's task was updated, created or restored, and not yet reached
        by the tick, was moved to the next day and did not run that day.

        What is kept is decided here, from the jobs armed at this moment and the time now,
        under the lock the tick claims a job with: a slot that came due while the refresh
        read the store is kept too. A task whose rule changed is armed by its new rule, and
        one that is gone or switched off stays unarmed (only a job the refresh armed again
        gets a slot back).

        A slot the tick claimed is never handed back. The new jobs are armed while the old
        ones are in force, so a job armed before its slot can arrive after the tick claimed
        that slot on the old job (measured: two runs of the same slot, daily and one-time).
        Every claim since the last swap is recorded (`_clock_claims`, by id and
        incarnation); a new job due at or before its task's claimed slot gets the next slot
        after it instead, and a one-time task, which has none, is not armed again. The
        record is emptied here: refreshes run one at a time, so the next one arms after
        this swap and counts from a time past every claim recorded so far."""
        kept, claimed = [], []
        with self._clock_lock:
            now = datetime.now()
            claims = self._clock_claims
            for task_id, job in list(fresh.items()):
                slot = claims.get((task_id, job.task.incarnation))
                if slot is not None and job.due <= slot:
                    nxt = None if job.once else job.task.calculate_next_run(after=slot)
                    if nxt is None:
                        del fresh[task_id]
                    else:
                        job.due = nxt
                    claimed.append(task_id)
                    continue
                old = self._clock_jobs.get(task_id)
                if (old is not None and old.due <= now and old.due < job.due
                        and _same_clock_rule(old.task, job.task)):
                    job.due = old.due
                    kept.append(task_id)
            self._clock_jobs.clear()
            self._clock_jobs.update(fresh)
            claims.clear()
        for task_id in kept:
            self._log_scheduler_event(f"KEPT_DUE task_id={task_id} reason='slot pending at refresh'")
        for task_id in claimed:
            self._log_scheduler_event(f"CLAIM_KEPT task_id={task_id} reason='slot already claimed'")

    def _clock_record_verdict(self, task: AutomationTask
                              ) -> Tuple[str, str, Optional[AutomationTask]]:
        """Whether a due clock job may run, judged against the task's record on disk now:
        ("run", "", the task as it is on disk), ("drop", reason, None) or
        ("skip", reason, None).

        The scheduler holds its own copy of every task, and every lane deletes, moves to the
        trash or switches off through a manager of its own (another process too: the
        terminal while the web app runs), so the copy outlives the task. Measured before this
        check: a daily task deleted in the app or by the agent ran its full prompt at its
        next time, and was armed again for the day after, until a refresh or restart.

        - drop: the file is gone or names another task, the id now names a task created anew
          (another `incarnation`), the task is switched off, it is no longer a clock task
          (an event task, or a frequency `_schedule_task` does not arm), or it is a one-time
          task that has already run (the rule `_schedule_task` arms by: its removal after
          the run failed, and it must not run a second time);
        - skip: the file is there but cannot be read as a record (a manual edit half
          written), so this one slot is not run and the task is not taken for deleted;
        - run: the task to run is the record on disk (its scope from where it lives, as the
          trigger lane's run has it), not the scheduler's older copy. Its id is the one the
          job is armed under (`_task_from_record`): the record's may be spelled differently
          (spaces, a number) and still name this task, and the id keys the armed jobs, the
          run lock and the file.

        Read without the store lock: every write is a temp file put in place, so a read sees a
        whole record."""
        try:
            path = self._path_for_task(task)
            record = _read_task_record(path, strict=True)
            if record is None or str(record.get("id")).strip() != str(task.id):
                return "drop", "task gone", None
            if _incarnation(record.get("incarnation")) != task.incarnation:
                return "drop", "created anew", None
            current = self._task_from_record(record, task, path)
        except Exception:
            return "skip", "record unreadable", None
        if not current.enabled:
            return "drop", "disabled", None
        if current.frequency not in _CLOCK_FREQUENCIES:
            return "drop", "no longer a clock task", None
        if current.frequency == Frequency.ONCE and (current.last_run
                                                    or current.last_completed_local_date):
            return "drop", "once already ran", None
        return "run", "", current

    def _run_due_clock_jobs(self, now: Optional[datetime] = None) -> None:
        """Run every armed clock task that is due, then arm it for its next run (a one-time
        task ends). Called on the scheduler loop's tick; `now` is naive server-local.

        A job is claimed before its run starts: under `_clock_lock` it must still be the job
        registered for its id (a refresh during the tick replaces every job, and the job it
        put there is the one that counts), and its next slot is set in the same step. So a
        replaced job never starts a run, and a refresh that comes after the claim sees the
        slot already taken and cannot hand it out a second time."""
        now = now or datetime.now()
        with self._clock_lock:
            due = [job for job in self._clock_jobs.values() if job.due <= now]
        for job in sorted(due, key=lambda j: j.due):
            task_id = job.task.id  # the key it is armed under
            try:
                self._run_due_clock_job(job, task_id, now)
            except Exception as e:
                # One job never stops the others: it gives up this slot like a skip.
                self._log_scheduler_event(f"CLOCK_JOB_FAILED task_id={task_id} error={e!r}")
                failed = None
                with self._clock_lock:
                    # Still due: not claimed yet, so its slot is not taken twice.
                    if self._clock_jobs.get(task_id) is job and job.due <= now:
                        try:
                            self._claim(job, task_id, now)
                        except Exception as again:
                            # Its next slot cannot be found either: the job ends (fails
                            # closed) rather than stay due and fail on every tick, ahead of
                            # every job due after it. A refresh or restart arms it again.
                            del self._clock_jobs[task_id]
                            failed = again
                if failed is not None:
                    self._log_scheduler_event(
                        f"CLOCK_DROPPED task_id={task_id} reason='no next slot' error={failed!r}")

    def _claim(self, job: _ClockJob, task_id: Any, now: datetime) -> None:
        """Take a due job's slot: record the claim, then set its next slot, or end the job
        when it has none (a one-time task). Called under `_clock_lock`."""
        self._clock_claims[(task_id, job.task.incarnation)] = job.due
        # Counted from the tick. The run starts in a thread of its own; a run still going
        # when its next slot comes is refused by the task's run lock.
        nxt = None if job.once else job.task.calculate_next_run(after=max(now, datetime.now()))
        if nxt is None:
            del self._clock_jobs[task_id]
        else:
            job.due = nxt

    def _run_due_clock_job(self, job: _ClockJob, task_id: Any, now: datetime) -> None:
        verdict, reason, current = self._clock_record_verdict(job.task)
        with self._clock_lock:
            if self._clock_jobs.get(task_id) is not job:
                return  # a refresh re-armed or removed it meanwhile
            if verdict == "drop":
                del self._clock_jobs[task_id]
            else:
                if verdict == "run":
                    # The record on disk is the task from here on: its next slot is counted
                    # from its own rule, and the next run is its own again.
                    job.task = current
                    job.once = current.frequency == Frequency.ONCE
                self._claim(job, task_id, now)
        if verdict == "drop":
            self._log_scheduler_event(f"CLOCK_DROPPED task_id={task_id} reason={reason!r}")
        elif verdict == "skip":
            self._log_scheduler_event(f"CLOCK_SKIPPED task_id={task_id} reason={reason!r}")
        else:
            try:
                self._run_scheduled_task(current)
            except Exception as e:
                self._log_scheduler_event(f"RUN_FAILED task_id={task_id} error={e!r}")


class AutomationAccess:
    """Which automations one caller may see, read, change or delete: the one ownership rule
    every network lane asks (the WebSocket commands, the agent's automation tools, the
    thinking review, the next-run lookup, the calendar's daily-check route).

    Why a rule and not just a scoped manager: `AutomationManager()` without a scope loads the
    root directory AND every `automations/<scope>/` directory, so it holds every account's
    tasks, and a task in the root directory runs as the machine owner. A lane that falls back
    to it on a miss hands any caller who knows (or guesses) an 8-character id another
    account's automation.

    The rule:
    - The caller is an admin when `is_admin_identity(role, scope)` holds (the role claim or
      the machine owner's scope). A NAMELESS caller (scope None or "") is an admin as well:
      that is the machine owner itself (the CLI, an embedder that passes no scope), the same
      convention as the tools' store chooser and the file jail. A scope that is present but
      blank after stripping is corrupt, not absent, so it is never an admin and sees nothing:
      it must not fail toward the global store.
    - An admin sees what its automation list shows: root tasks (no scope), its own scope and
      the machine owner's scope (system automations such as the daily calendar check). It
      does NOT see another account's tasks: those run with that account's credentials, so an
      admin rewriting one would make that account act on words it never wrote.
    - Anyone else sees only the tasks of its own non-empty scope.
    - `get` answers a foreign id exactly like a missing one, `(None, None)`: ids are names,
      not capabilities, and a lane must not become an existence oracle.
    - A record whose stored scope carries surrounding whitespace is seen by nobody (see
      `sees`): the rule would read it stripped while the store and the scheduler use the
      raw string.
    - `trash()` lists the trashed tasks the caller sees, each with the manager whose trash
      holds it, across every store its tasks can live in (`_stores`: the own scope's, and for
      an admin the machine owner's and the root trash too), so a restore runs where the
      delete put the task.

    Named boundaries:
    - The CLI (`vaf automation ...`), the TUI screens and the scheduler use the global manager
      on purpose: they are the machine owner at the terminal, who can read the store directly,
      and the scheduler runs every task as its own owner. No network caller reaches them.
    - When an admin's task lives in the global manager, the 10-minute interval check runs on
      that manager, so its refusal can quote another account's task name to an admin. That is
      the existing machine-wide scheduling rule, admin-only.
    - The WebSocket never carries a scopeless connection (`websocket_endpoint` closes a
      remote socket without context and gives a localhost one the machine owner's scope), so
      the nameless-admin rule is reached there only through that local-admin default.
    - Not on the public facade: `vaf/__init__.py` exports no automation store, so a rule
      without its store would have nothing to apply to. Exporting both is one decision
      about putting the scheduler on the public surface.
    """

    def __init__(self, user_scope_id: Optional[str], user_role: Optional[str] = None) -> None:
        from vaf.core.config import get_local_admin_scope_id, is_admin_identity
        self.own = str(user_scope_id or "").strip()
        self._local = str(get_local_admin_scope_id() or "").strip()
        if user_scope_id is None or user_scope_id == "":
            self.is_admin = True
        elif not self.own:
            self.is_admin = False
        else:
            self.is_admin = bool(is_admin_identity(user_role, self.own))

    def sees(self, task: Any) -> bool:
        """Whether this caller may see (and therefore act on) `task`.

        A record whose stored scope carries surrounding whitespace (`" <scope>"`, or a
        scope that is only a no-break space) is visible to nobody: this rule would read it
        stripped, as the machine owner's or a root task, while `_path_for_task` and the
        scheduler use the raw value, so a change would land in a directory named by that
        string and the run would carry it as its scope. Every lane stamps the scope from the signed identity, so such a
        record is never written by VAF; it is refused rather than guessed at."""
        raw = getattr(task, "user_scope_id", None)
        if raw is not None and str(raw) != str(raw).strip():
            return False
        ts = str(raw or "").strip()
        if self.is_admin:
            return ts == "" or ts == self.own or (bool(self._local) and ts == self._local)
        return bool(self.own) and ts == self.own

    def list(self, *, enabled_only: bool = False) -> List[AutomationTask]:
        """Every task this caller sees, in the manager's order (next run first)."""
        if self.is_admin:
            return [t for t in AutomationManager().list(enabled_only=enabled_only) if self.sees(t)]
        if not self.own:
            # Never construct a manager here: `AutomationManager(user_scope_id="")` is the
            # global one.
            return []
        return [t for t in AutomationManager(user_scope_id=self.own).list(enabled_only=enabled_only)
                if self.sees(t)]

    def get(self, task_id: str) -> Tuple[Optional[AutomationManager], Optional[AutomationTask]]:
        """`(manager, task)` for a task this caller sees, else `(None, None)` - for a foreign
        id and a missing id alike. The manager is the one that holds the task, so an
        interval check and the save run where they ran before."""
        task_id = str(task_id or "").strip()
        if not task_id:
            return None, None
        for make in self._stores():
            mgr = make()
            task = mgr.get(task_id)
            if task is not None and self.sees(task):
                return mgr, task
        return None, None

    def trash(self) -> List[Tuple[AutomationManager, AutomationTask]]:
        """Every trashed task this caller sees, each with the manager whose trash holds it
        (so a restore runs on that manager). Read from every store in `_stores()`: the own
        scope's trash, and for an admin also the machine owner's scoped trash (where the
        machine owner's own deletes land) and the root trash. An account's earlier deletes
        stay listable and restorable wherever they landed."""
        out: List[Tuple[AutomationManager, AutomationTask]] = []
        ids: set = set()
        for make in self._stores():
            mgr = make()
            for task in mgr.list_trash():
                if task.id not in ids and self.sees(task):
                    ids.add(task.id)
                    out.append((mgr, task))
        return out

    def _stores(self) -> List[Callable[[], AutomationManager]]:
        """The stores this caller's tasks can live in, own scope first. For an admin also
        the machine owner's scoped store (when it is not the admin's own) and the global
        one: the machine owner deletes its own tasks into its scoped trash, which neither
        the admin's own trash nor the root trash holds. Never the global store for a
        non-admin, and none at all for a blank scope."""
        stores: List[Callable[[], AutomationManager]] = []
        if self.own:
            stores.append(lambda: AutomationManager(user_scope_id=self.own))
        if self.is_admin:
            if self._local and self._local != self.own:
                stores.append(lambda: AutomationManager(user_scope_id=self._local))
            stores.append(AutomationManager)
        return stores


def get_next_automation_run_utc(user_scope_id: Optional[str]) -> Optional[datetime]:
    """
    Return the next run time (minimum across the enabled automations this user sees).
    Used by thinking mode to skip starting if an automation runs within the buffer window.
    The set is `AutomationAccess(user_scope_id).list()`, the one the automation list shows:
    for the machine owner the root tasks and its own scope, for anyone else its own scope.
    """
    from vaf.core.tool_dispatch import account_directory_once
    with account_directory_once():
        tasks = AutomationAccess(user_scope_id).list(enabled_only=True)
        upcoming = [when for when in (t.next_run_datetime for t in tasks) if when is not None]
    return min(upcoming) if upcoming else None


# ═══════════════════════════════════════════════════════════════════════════════
# CLARIFICATION HELPER
# ═══════════════════════════════════════════════════════════════════════════════

class AutomationClarifier:
    """Helps clarify incomplete automation requests."""
    
    # Required parameters for common task types
    REQUIRED_PARAMS = {
        "weather": ["city"],
        "news": ["category"],  # tech, politics, sports, all
        "stock": ["symbol"],
        "reminder": ["message"],
        "email_summary": ["email_account"],
        "backup": ["source_path", "destination_path"],
    }
    
    # Questions to ask for each parameter
    QUESTIONS = {
        "city": "Für welche Stadt soll das Wetter abgerufen werden?",
        "category": "Welche Nachrichten-Kategorie? (tech, politik, sport, wirtschaft, alle)",
        "symbol": "Welches Aktiensymbol? (z.B. AAPL, GOOGL, MSFT)",
        "message": "Was soll die Erinnerung sagen?",
        "email_account": "Welcher E-Mail-Account soll zusammengefasst werden?",
        "source_path": "Welcher Ordner soll gesichert werden?",
        "destination_path": "Wohin soll die Sicherung gespeichert werden?",
        "time": "Um welche Uhrzeit? (Format: HH:MM, z.B. 06:00)",
        "frequency": "Wie oft? (täglich, wöchentlich, monatlich)",
        "output_path": "Wohin soll das Ergebnis gespeichert werden? (z.B. ~/Desktop)",
    }
    
    @classmethod
    def detect_task_type(cls, prompt: str) -> Optional[str]:
        """Detect the type of automation from the prompt."""
        prompt_lower = prompt.lower()
        
        if any(w in prompt_lower for w in ["wetter", "weather", "temperatur"]):
            return "weather"
        if any(w in prompt_lower for w in ["nachrichten", "news", "headlines"]):
            return "news"
        if any(w in prompt_lower for w in ["aktie", "stock", "börse"]):
            return "stock"
        if any(w in prompt_lower for w in ["erinner", "remind", "alarm"]):
            return "reminder"
        if any(w in prompt_lower for w in ["email", "mail", "inbox"]):
            return "email_summary"
        if any(w in prompt_lower for w in ["backup", "sicher", "kopie"]):
            return "backup"
        
        return None
    
    @classmethod
    def get_missing_params(cls, task_type: str, existing_params: Dict) -> List[str]:
        """Get list of missing required parameters."""
        required = cls.REQUIRED_PARAMS.get(task_type, [])
        return [p for p in required if p not in existing_params]
    
    @classmethod
    def extract_params(cls, prompt: str) -> Dict[str, Any]:
        """Extract parameters from a prompt."""
        import re
        params = {}
        prompt_lower = prompt.lower()
        
        # Extract city (common German/international cities)
        cities = ["berlin", "hamburg", "münchen", "köln", "frankfurt", "düsseldorf",
                  "stuttgart", "london", "paris", "new york", "tokyo", "wien", "zürich"]
        for city in cities:
            if city in prompt_lower:
                params["city"] = city.title()
                break
        
        # Extract time (HH:MM pattern)
        time_match = re.search(r'(\d{1,2})[:\.](\d{2})', prompt)
        if time_match:
            hour = int(time_match.group(1))
            minute = time_match.group(2)
            params["time"] = f"{hour:02d}:{minute}"
        
        # Extract frequency
        if any(w in prompt_lower for w in ["täglich", "daily", "jeden tag"]):
            params["frequency"] = "daily"
        elif any(w in prompt_lower for w in ["wöchentlich", "weekly", "jede woche"]):
            params["frequency"] = "weekly"
        elif any(w in prompt_lower for w in ["stündlich", "hourly", "jede stunde"]):
            params["frequency"] = "hourly"
        elif any(w in prompt_lower for w in ["monatlich", "monthly", "jeden monat"]):
            params["frequency"] = "monthly"
        
        # Extract output path
        if "desktop" in prompt_lower:
            params["output_path"] = str(Path.home() / "Desktop")
        elif "dokumente" in prompt_lower or "documents" in prompt_lower:
            params["output_path"] = str(Path.home() / "Documents")
        elif "downloads" in prompt_lower:
            params["output_path"] = str(Path.home() / "Downloads")
        
        # Extract news category
        if "tech" in prompt_lower:
            params["category"] = "tech"
        elif "politik" in prompt_lower or "politics" in prompt_lower:
            params["category"] = "politics"
        elif "sport" in prompt_lower:
            params["category"] = "sports"
        elif "wirtschaft" in prompt_lower or "business" in prompt_lower:
            params["category"] = "business"
        
        return params
    
    @classmethod
    def build_clarification_prompt(cls, task_type: str, missing_params: List[str]) -> str:
        """Build a clarification prompt for missing parameters."""
        questions = []
        for param in missing_params:
            if param in cls.QUESTIONS:
                questions.append(f"• {cls.QUESTIONS[param]}")
        
        if questions:
            return "Um die Automatisierung zu erstellen, brauche ich noch ein paar Infos:\n\n" + "\n".join(questions)
        return ""


# ═══════════════════════════════════════════════════════════════════════════════
# CLI COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

import typer

automation_app = typer.Typer(help="Manage scheduled automations")

_manager: Optional[AutomationManager] = None
_scheduler_manager: Optional[AutomationManager] = None
_scheduler_manager_lock = threading.Lock()

def get_manager() -> AutomationManager:
    global _manager
    if _manager is None:
        _manager = AutomationManager()
    return _manager


def ensure_scheduler_started(origin: str = "unknown") -> tuple[AutomationManager, bool]:
    """
    Ensure the process-wide automation scheduler is running exactly once.

    Returns:
        (manager, started_now)
    """
    global _scheduler_manager
    with _scheduler_manager_lock:
        if _scheduler_manager is None:
            _scheduler_manager = AutomationManager()

        thread_alive = bool(
            _scheduler_manager._scheduler_thread and _scheduler_manager._scheduler_thread.is_alive()
        )
        if _scheduler_manager._running and thread_alive:
            _scheduler_manager._log_scheduler_event(
                f"ENSURE_SKIPPED origin={origin!r} reason='already running'"
            )
            return _scheduler_manager, False

        if _scheduler_manager._running and not thread_alive:
            _scheduler_manager._log_scheduler_event(
                f"ENSURE_RECOVER origin={origin!r} reason='thread not alive'"
            )
            _scheduler_manager._running = False
            try:
                _scheduler_manager._clear_clock_jobs()
            except Exception:
                pass

        _scheduler_manager.reload_tasks()
        _scheduler_manager._log_scheduler_event(f"ENSURE_START origin={origin!r}")
        _scheduler_manager.start_scheduler()
        return _scheduler_manager, True


def refresh_scheduler_from_disk(origin: str = "unknown") -> bool:
    """
    If the process-wide scheduler is running, reload tasks from disk and
    rebuild all schedule jobs so updates (like changed HH:MM) apply immediately.

    Returns:
        True if a running scheduler was refreshed, False otherwise.
    """
    global _scheduler_manager
    with _scheduler_manager_lock:
        if _scheduler_manager is None:
            return False

        thread_alive = bool(
            _scheduler_manager._scheduler_thread and _scheduler_manager._scheduler_thread.is_alive()
        )
        if not (_scheduler_manager._running and thread_alive):
            return False

        from vaf.core.tool_dispatch import account_directory_once
        _scheduler_manager._log_scheduler_event(f"REFRESH_START origin={origin!r}")
        _scheduler_manager.reload_tasks()
        # The armed jobs stay in force while the store is read (the tick may claim one
        # meanwhile); the new ones are built apart and swapped in at once.
        fresh: Dict[str, _ClockJob] = {}
        with account_directory_once():
            for task in _scheduler_manager.list(enabled_only=True):
                _scheduler_manager._schedule_task(task, into=fresh)
            _scheduler_manager._swap_clock_jobs(fresh)
            _scheduler_manager._log_scheduler_event(
                f"REFRESH_DONE origin={origin!r} task_count={len(_scheduler_manager.list(enabled_only=True))}"
            )
        return True


@automation_app.command("list")
def list_automations():
    """List all automation tasks."""
    from vaf.core.terminal_text import TerminalConsole
    from rich.table import Table
    from rich.text import Text
    
    from vaf.core.tool_dispatch import account_directory_once

    console = TerminalConsole()
    manager = get_manager()
    # The sort and every row's next run name the task's account from one directory read.
    with account_directory_once():
        tasks = manager.list()

        if not tasks:
            console.print("[yellow]No automations configured.[/yellow]")
            console.print("\n[dim]Create one with: vaf automation create[/dim]")
            return

        table = Table(title="⚡ Scheduled Automations", show_header=True)
        table.add_column("ID", style="cyan")
        table.add_column("Name")
        table.add_column("Frequency")
        table.add_column("Time")
        table.add_column("Next Run")
        table.add_column("Status")

        for task in tasks:
            status = "[green]●[/green] Active" if task.enabled else "[red]○[/red] Disabled"
            next_run = task.next_run_label

            table.add_row(
                task.id,
                Text(task.name[:20]),
                task.frequency,
                task.time or task.schedule_label,
                next_run,
                status
            )

    console.print(table)


@automation_app.command("create")
def create_automation(
    name: str = typer.Option(..., "--name", "-n", prompt="Task name"),
    prompt: str = typer.Option(..., "--prompt", "-p", prompt="What should VAF do?"),
    frequency: str = typer.Option("daily", "--frequency", "-f", help="daily, weekly, hourly, monthly"),
    time: str = typer.Option("06:00", "--time", "-t", help="Execution time (HH:MM)"),
    output: str = typer.Option(None, "--output", "-o", help="Output directory"),
    on_room: str = typer.Option("", "--on-room", help="Run when something happens in this agent room (its id) instead of by the clock"),
    on_match: str = typer.Option("", "--on-match", help="With --on-room: only a message containing this text"),
    on_reaction: str = typer.Option("", "--on-reaction", help="With --on-room: a reaction instead of a message; an emoji, or 'any'"),
    on_from: str = typer.Option("", "--on-from", help="With --on-room: only frames from this peer handle"),
    on_frame: str = typer.Option("", "--on-frame", help="With --on-reaction: only a reaction on this message id"),
):
    """Create a new automation task."""
    from vaf.core.terminal_text import TerminalConsole
    
    console = TerminalConsole()
    manager = get_manager()
    
    # Extract and clarify parameters
    clarifier = AutomationClarifier()
    task_type = clarifier.detect_task_type(prompt)
    params = clarifier.extract_params(prompt)
    
    if task_type:
        missing = clarifier.get_missing_params(task_type, params)
        if missing:
            console.print(f"\n[yellow]{clarifier.build_clarification_prompt(task_type, missing)}[/yellow]\n")
            
            for param in missing:
                question = clarifier.QUESTIONS.get(param, f"Value for {param}?")
                value = typer.prompt(question)
                params[param] = value
    
    # An event trigger replaces the clock: no time, no clock job, due when the room says so.
    trigger = None
    if on_room:
        from vaf.core.automation_triggers import read_trigger
        trigger = read_trigger({"kind": "room_reaction" if on_reaction else "room_message",
                                "room_id": on_room, "match": on_match,
                                "emoji": "" if on_reaction.strip().lower() in ("any", "*") else on_reaction,
                                "from": on_from, "on_frame": on_frame})
        if trigger is None:
            console.print("[red]--on-room needs a room id that could be one (letters, digits, - _ .)[/red]")
            raise typer.Exit(1)
        frequency, time = Frequency.ON_EVENT.value, ""

    # Create task
    task = AutomationTask(
        name=name,
        prompt=prompt,
        frequency=frequency,
        time=time,
        output_path=output or str(Path.home() / "Desktop"),
        parameters=params,
        trigger=trigger,
    )
    
    task = manager.create(task)
    
    console.print(f"\n[green]✓ Automation created![/green]")
    console.print(f"  [dim]ID:[/dim] {task.id}")
    console.print(f"  [dim]When:[/dim] {task.schedule_label}")
    console.print(f"  [dim]Next run:[/dim] {task.next_run_label}")
    console.print(f"\n[dim]Start scheduler with: vaf automation start[/dim]")


@automation_app.command("run")
def run_automation(
    task_id: str = typer.Argument(..., help="Task ID to run")
):
    """Manually run an automation task."""
    from vaf.core.terminal_text import TerminalConsole, escape_markup
    
    console = TerminalConsole()
    manager = get_manager()
    
    # This command is ALSO what run_task spawns into a terminal window (new_terminal=True
    # builds "vaf automation run <id>"). That window used to be held open by the `; exec bash`
    # tail the Linux spawn appended; now that the tail is gone, the child owns its own window
    # and must say so explicitly, or a manual automation run would flash past unreadably.
    from vaf.cli.autoclose import finish_terminal

    task = manager.get(task_id)
    if not task:
        console.print(f"[red]Task not found: {task_id}[/red]")
        finish_terminal(success=False)

    console.print(f"\n[cyan]⚡ Running: {escape_markup(task.name)}[/cyan]\n")

    def print_output(text):
        # The run's output is the model's text: printed as written, never as markup.
        console.print(text, end="", markup=False, emoji=False)

    # The run's own verdict (`RunOutcome`): a run that failed or ran out of time holds the
    # window and exits 1 like a crash does, and so does a run that ended without one (skipped,
    # locked, its account's access taken away).
    outcomes = []
    ok = True
    try:
        # Don't open ANOTHER terminal - we may already be running inside one.
        manager.run_task(task, callback=print_output, new_terminal=False,
                         on_outcome=outcomes.append)
    except Exception as exc:
        ok = False
        console.print(f"\n[red]Automation failed: {escape_markup(exc)}[/red]")
    console.print("\n")
    finish_terminal(success=ok and bool(outcomes) and outcomes[0].completed)


@automation_app.command("delete")
def delete_automation(
    task_id: str = typer.Argument(..., help="Task ID to delete"),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation")
):
    """Delete an automation task."""
    from vaf.core.terminal_text import TerminalConsole, escape_markup
    
    console = TerminalConsole()
    manager = get_manager()
    
    task = manager.get(task_id)
    if not task:
        console.print(f"[red]Task not found: {task_id}[/red]")
        raise typer.Exit(1)
    
    if not force:
        confirm = typer.confirm(f"Delete automation '{task.name}'?")
        if not confirm:
            console.print("[yellow]Cancelled.[/yellow]")
            return
    
    manager.delete(task_id)
    console.print(f"[green]✓ Deleted: {escape_markup(task.name)}[/green]")


@automation_app.command("start")
def start_scheduler():
    """Start the automation scheduler daemon."""
    from vaf.core.terminal_text import TerminalConsole, escape_markup
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text
    
    console = TerminalConsole()
    
    manager = get_manager()
    tasks = manager.list(enabled_only=True)
    
    if not tasks:
        console.print("[yellow]No enabled automations to run.[/yellow]")
        return
    
    console.print(f"\n[cyan]⚡ Starting VAF Automation Scheduler[/cyan]")
    console.print(f"[dim]Running {len(tasks)} task(s). Press Ctrl+C to stop.[/dim]\n")
    
    for task in tasks:
        console.print(f"  • {escape_markup(task.name)} @ {task.time} ({task.frequency})")
    
    console.print()
    
    manager.start_scheduler()
    
    try:
        # Keep running with status updates
        while True:
            time.sleep(60)
            # Could add live status display here
    except KeyboardInterrupt:
        manager.stop_scheduler()
        console.print("\n[yellow]Scheduler stopped.[/yellow]")


@automation_app.command("enable")
def enable_automation(task_id: str = typer.Argument(..., help="Task ID")):
    """Enable an automation task."""
    manager = get_manager()
    task = manager.update(task_id, enabled=True)
    if task:
        print(f"✓ Enabled: {task.name}")
    else:
        print(f"Task not found: {task_id}")


@automation_app.command("disable")
def disable_automation(task_id: str = typer.Argument(..., help="Task ID")):
    """Disable an automation task."""
    manager = get_manager()
    task = manager.update(task_id, enabled=False)
    if task:
        print(f"✓ Disabled: {task.name}")
    else:
        print(f"Task not found: {task_id}")


@automation_app.command("reload")
def reload_automations():
    """Reload all automations from disk (useful after manual file edits)."""
    from vaf.core.terminal_text import TerminalConsole
    
    console = TerminalConsole()
    manager = get_manager()
    
    console.print("[cyan]Reloading automations...[/cyan]")
    manager.reload_tasks()
    
    tasks = manager.list()
    console.print(f"[green]✓ Reloaded {len(tasks)} automation(s)[/green]")
    
    # next_run is now calculated dynamically, so no corrections needed

