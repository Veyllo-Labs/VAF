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
import uuid
import subprocess
import threading
import time
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, field, asdict
from enum import Enum
from vaf.core.log_helper import append_domain_log, append_domain_log_always

# Cross-platform scheduler
try:
    import schedule
    HAS_SCHEDULE = True
except ImportError:
    HAS_SCHEDULE = False


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
    
    def to_dict(self) -> Dict:
        """Convert to dict, excluding next_run (calculated dynamically)."""
        data = asdict(self)
        # Don't save next_run - it's calculated dynamically
        if "next_run" in data:
            del data["next_run"]
        return data
    
    @property
    def next_run_datetime(self) -> datetime:
        """Get the next run time (calculated dynamically)."""
        return self.calculate_next_run()
    
    @property
    def next_run_iso(self) -> str:
        """Get the next run time as ISO string (calculated dynamically)."""
        return self.calculate_next_run().isoformat()
    
    @classmethod
    def from_dict(cls, data: Dict) -> "AutomationTask":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
    
    def calculate_next_run(self) -> datetime:
        """Next execution time, interpreted in the OWNER's timezone, returned as a SERVER-local
        naive datetime.

        Wall-clock times (self.time) are the user's local times, so "now" is taken in the owner's
        timezone (user_identity.timezone — single source of truth; server-local when unset). The
        result is converted back to a naive server-local datetime so sort/min over tasks and
        comparisons with naive datetime.now() never mix aware+naive (which would raise).
        """
        from vaf.core.user_time import user_now
        now = user_now(_resolve_username(self.user_scope_id))
        hour, minute = map(int, self.time.split(":"))

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
            target_day = self.day or 1
            next_time = now.replace(day=target_day, hour=hour, minute=minute, second=0, microsecond=0)
            if next_time <= now:
                # Move to next month
                if now.month == 12:
                    next_time = next_time.replace(year=now.year + 1, month=1)
                else:
                    next_time = next_time.replace(month=now.month + 1)
        else:
            next_time = now
        return _to_server_local_naive(next_time)


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
    non-admin "admin" would deliver their automation result to the LOCAL ADMIN's messenger. Mirrors
    the thinking-mode resolver: admin only for the admin scope (or empty scope = single-user/local),
    a synthetic ``scope_<hex>`` for an unknown scope, never "admin" for a non-admin scope.
    """
    try:
        from vaf.core.config import get_local_admin_username, get_local_admin_scope_id
        admin_user = get_local_admin_username() or "admin"
        if not user_scope_id or str(user_scope_id).strip() == str(get_local_admin_scope_id()).strip():
            return admin_user
        from vaf.core.thinking_mode import _resolve_username_for_scope
        resolved = _resolve_username_for_scope(user_scope_id)
        return resolved or ("scope_" + str(user_scope_id).replace("-", "")[:8])
    except Exception:
        return "admin"


# Outbound send tools a workflow step can use for in-run delivery. When one of
# them already reached the user, the post-run messenger push is skipped (the Web
# UI trace and the notification still happen) - otherwise every workflow with a
# delivery step would message the user twice.
_SEND_STEP_TOOLS = frozenset({
    "send_to_user", "send_telegram", "send_whatsapp", "send_discord", "send_slack", "send_mail",
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


def _delivered_via_agent_history(history) -> bool:
    """True if a send tool confirmed delivery during a prompt-based agent run.

    Prompt-based automations deliver in-run via tool calls. TWO history shapes
    must be recognized: live role='tool' entries (turn still running), and the
    END-OF-TURN SQUASH - chat_step consolidates all intermediate messages into
    one '[Context: tools used this turn]' system note with '- <tool> -> OK:
    <snippet>' lines (agent.py turn finalize). The dedup reads the history
    AFTER the turn ended, so the squashed form is the one that matters (live
    2026-07-13 15:52: a real send_to_user delivery was missed and the user got
    the push on top). Same conservative contract as _delivered_via_send_step:
    only an explicit send-tool SUCCESS suppresses the post-run push - a
    duplicate message beats a lost one.
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
            return True
        if role == "system" and content.startswith(_CTX_PREFIX):
            for line in content.splitlines():
                line = line.strip()
                if not line.startswith("- ") or " → OK: " not in line:
                    continue
                name, _, rest = line[2:].partition(" → OK: ")
                if name.strip() in _SEND_STEP_TOOLS and "sent to the user via" in rest.lower():
                    return True
    return False


def _delivered_via_send_step(step_results) -> bool:
    """True if a send step in this run confirmed delivery to the user.

    Keys on the shared success phrase of the send tools ("sent to the user via
    ..."). Deliberately conservative: an unrecognized or failed send result
    keeps the post-run push ON - a duplicate message beats a lost one.
    """
    for sr in step_results or []:
        if (
            sr.get("tool") in _SEND_STEP_TOOLS
            and sr.get("status") == "success"
            and "sent to the user via" in str(sr.get("result") or "").lower()
        ):
            return True
    return False


def _push_result_to_web_ui(
    task: "AutomationTask", status: str, summary: str, output_file: Optional[str] = None,
    deliver_messenger: bool = True,
) -> bool:
    """Push automation result to Web UI and, if configured, to the user's messenger.

    Delivers to BOTH channels so the user always sees the result regardless of
    whether they have Telegram/WhatsApp/Discord configured.
    When ``output_file`` is given (the automation produced a file), it is referenced in the Web UI
    message AND delivered as an attachment on the messenger.
    ``deliver_messenger=False`` skips only the messenger push - used when a workflow
    send step already delivered the content in-run (no double delivery).
    Returns True if at least the Web UI push succeeded.
    """
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
                if (s.get("metadata") or {}).get("source") not in ("thinking", "telegram", "discord", "whatsapp")
            ]
            if web_sessions:
                sid = web_sessions[0]["id"]
            else:
                new_session = sm.create(user_scope_id=task.user_scope_id, metadata={"source": "automation_result"})
                sid = new_session.id

        loaded = sm.load(sid)
        if loaded:
            loaded.add_message(role="assistant", content=web_msg)
            sm.save(loaded)
        if wi:
            # Append as a standalone bubble — automation results are proactive and
            # have no live agent turn, so the streaming update path would overwrite
            # the previous reply instead of showing a new "done" message.
            wi.emit_agent_message_append(web_msg, session_id=sid)
            wi.emit_session_unread(sid)
        web_ok = True
    except Exception as _e:
        append_domain_log("backend", f"[AUTOMATION] Web UI delivery failed: {_e}")

    # 2. Messenger — send proactively if a main messenger is configured, via the ONE canonical
    # "reach the user on their main channel" helper (single source of truth, shared with thinking
    # mode). It resolves the per-channel chat id/jid, attaches the output file (Telegram/WhatsApp/
    # Discord all support it), and never raises. The username is resolved to the TASK OWNER (not the
    # local admin) so a per-user automation result never leaks onto the admin's messenger.
    if deliver_messenger:
        try:
            from vaf.core.messaging_connections import send_to_main_messenger
            username = _resolve_username(task.user_scope_id)
            send_to_main_messenger(task.user_scope_id, username, msg, file_path=output_file)
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


def _stamp_successful_run(task: AutomationTask) -> None:
    """Record completion time and local calendar day (survives restarts via _save_task)."""
    from vaf.core.user_time import user_now
    now = datetime.now()
    task.last_run = now.isoformat()
    # "local date" = the OWNER's calendar day (their timezone), so daily "done today" checks
    # match the user's day, not the server's.
    task.last_completed_local_date = user_now(_resolve_username(task.user_scope_id)).date().isoformat()


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
        return f"Next: {task.next_run_datetime.strftime('%Y-%m-%d %H:%M')}"

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
    for filepath in base_dir.glob("*.json"):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not data.get("enabled", True):
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
        for filepath in subdir.glob("*.json"):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not data.get("enabled", True):
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
        self._create_readme()
        self._load_tasks()

    def _log_scheduler_event(self, message: str) -> None:
        """Write scheduler diagnostics only when debug logging is enabled."""
        append_domain_log("backend", f"[AUTOMATION_SCHEDULER] {message}")

    def _run_scheduled_task(self, task: AutomationTask) -> str:
        """Wrapper for scheduled executions: runs in a background thread (no terminal).

        Running without a terminal (new_terminal=False) keeps the automation in the
        same process, which means _push_result_to_web_ui can reach the live Web UI
        WebSocket and deliver the result directly — no subprocess isolation needed.
        """
        self._log_scheduler_event(
            f"TRIGGER task_id={task.id} name={task.name!r} frequency={task.frequency} time={task.time}"
        )

        def _execute():
            try:
                result = self.run_task(task, new_terminal=False)
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
        for filepath in self.storage_dir.glob("*.json"):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
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
                    for filepath in subdir.glob("*.json"):
                        try:
                            with open(filepath, 'r', encoding='utf-8') as f:
                                data = json.load(f)
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
        """Save a task to storage. When task has user_scope_id, save to that user's dir (for global manager)."""
        filepath = self._path_for_task(task)
        if task.user_scope_id:
            filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(task.to_dict(), f, indent=2, ensure_ascii=False)

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
        """Create a new automation task."""
        # next_run is calculated dynamically - no need to store it
        self.tasks[task.id] = task
        self._save_task(task)
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
        """Update an existing task."""
        if task_id not in self.tasks:
            return None
        
        task = self.tasks[task_id]
        for key, value in kwargs.items():
            if hasattr(task, key):
                setattr(task, key, value)
        
        # next_run is calculated dynamically - no need to store it
        self._save_task(task)
        self._sync_workspace_automation_state(task, event="automation_updated")
        # Keep the live scheduler in sync with on-disk changes (e.g. updated time).
        try:
            refresh_scheduler_from_disk(origin=f"update:{task.id}")
        except Exception:
            pass
        return task
    
    def delete(self, task_id: str, permanent: bool = False) -> bool:
        """Delete a task (moves to trash by default, or permanently if specified)."""
        if task_id not in self.tasks:
            return False
        
        task = self.tasks[task_id]
        filepath = self._path_for_task(task)
        trash_path = self.trash_dir / f"{task_id}.json"
        
        if filepath.exists():
            if permanent:
                # Permanent deletion
                filepath.unlink()
            else:
                # Move to trash
                import shutil
                shutil.move(str(filepath), str(trash_path))
        
        del self.tasks[task_id]
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
            if task.user_scope_id:
                filepath.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.move(str(trash_path), str(filepath))
            
            # Add back to tasks
            self.tasks[task_id] = task
            return True
        except Exception:
            return False
    
    def list_trash(self) -> List[AutomationTask]:
        """List all tasks in trash."""
        tasks = []
        for filepath in self.trash_dir.glob("*.json"):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                task = AutomationTask.from_dict(data)
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
        """List all tasks."""
        tasks = list(self.tasks.values())
        if enabled_only:
            tasks = [t for t in tasks if t.enabled]
        return sorted(tasks, key=lambda t: t.next_run_datetime)
    
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
    
    def _extract_clean_answer(self, raw_response: str, history: List[Dict]) -> str:
        """
        Extract only the final clean answer from agent response.
        Removes internal thinking, tool calls, and formatting artifacts.
        
        Args:
            raw_response: Raw response string from agent
            history: Agent history to extract final answer from
            
        Returns:
            Clean final answer without internal thinking
        """
        import re
        
        # If we have history, try to extract the final assistant message
        if history:
            # Find the last assistant message that's not just tool calls
            for msg in reversed(history):
                if msg.get('role') == 'assistant' and msg.get('content'):
                    content = str(msg.get('content', ''))
                    
                    # Remove all XML tags (thinking, tool calls, etc.)
                    clean = re.sub(r'<[^>]*>', '', content)
                    # Remove Rich markup tags
                    clean = re.sub(r'\[/?[^\]]+\]', '', clean)
                    # Remove tool_call blocks
                    clean = re.sub(r'<tool_call>.*?</tool_call>', '', clean, flags=re.DOTALL)
                    # Remove code blocks (but keep content if it's the answer)
                    # Only remove if it's clearly a tool call format
                    clean = re.sub(r'```json\s*\{[^}]*"name"[^}]*\}\s*```', '', clean, flags=re.DOTALL)
                    
                    # Remove common "thinking" patterns
                    thinking_patterns = [
                        r'Okay.*?let.*?me',
                        r'First.*?I.*?need',
                        r'Wait.*?the.*?user',
                        r'I.*?should.*?use',
                        r'Let.*?me.*?check',
                    ]
                    for pattern in thinking_patterns:
                        clean = re.sub(pattern, '', clean, flags=re.IGNORECASE | re.DOTALL)
                    
                    # If we have substantial content after cleaning, use it
                    if len(clean.strip()) > 50:
                        return clean.strip()
        
        # Fallback: Clean the raw response
        clean = raw_response
        
        # Remove Rich markup
        clean = re.sub(r'\[/?[^\]]+\]', '', clean)
        # Remove XML tags
        clean = re.sub(r'<[^>]*>', '', clean)
        # Remove tool calls
        clean = re.sub(r'<tool_call>.*?</tool_call>', '', clean, flags=re.DOTALL)
        # Remove thinking blocks
        clean = re.sub(r'</?think>', '', clean, flags=re.IGNORECASE)
        clean = re.sub(r'</?redacted_reasoning>', '', clean, flags=re.IGNORECASE)
        
        # Remove leading "thinking" patterns
        lines = clean.split('\n')
        filtered_lines = []
        skip_thinking = True
        for line in lines:
            line_lower = line.lower().strip()
            # Skip obvious thinking lines
            if skip_thinking and any(pattern in line_lower for pattern in [
                'okay', 'first', 'wait', 'let me', 'i need', 'i should',
                'tool_call', 'thinking', 'reasoning'
            ]):
                continue
            # Once we hit real content, stop skipping
            if len(line.strip()) > 10 and not any(c in line_lower for c in ['[', ']', '<', '>']):
                skip_thinking = False
            if not skip_thinking or len(line.strip()) > 0:
                filtered_lines.append(line)
        
        result = '\n'.join(filtered_lines).strip()
        
        # If result is still mostly thinking artifacts, return a fallback
        if len(result) < 50 or result.count('[') > result.count('\n'):
            return raw_response.strip()  # Return original if cleaning removed too much
        
        return result
    
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
    
    def run_task(self, task: AutomationTask, callback: Callable = None, new_terminal: bool = True) -> str:
        """
        Execute an automation task.
        
        Args:
            task: The automation task to run
            callback: Optional callback for progress updates
            new_terminal: If True, run in a new terminal window (default: True)
        """
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
            
            if Platform.open_new_terminal(vaf_cmd, title=title):
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
        
        result = ""
        # Final path of the saved output file (if any). Initialised here so it survives to the
        # delivery call after the try/finally; set only when a file is actually written.
        saved_output_path: Optional[str] = None
        # True when the prompt-based agent already delivered to the user in-run via a
        # send tool (confirmed by the tool result). Drives the same double-delivery
        # dedup the workflow lane has; survives to the delivery call below.
        prompt_delivered_in_run = False
        # True when a prompt run hit the time limit AND did not finish within the
        # grace window: no partial stream as result, no legacy file wrap - the user
        # gets one honest timeout note instead (live 2026-07-13: the half stream was
        # wrapped into a junk HTML and pushed, then the zombie delivered again).
        prompt_timeout_unresolved = False

        try:
            # ... (Rest of the method logic) ...

            # Set environment variables for non-interactive automation mode
            # This prevents user prompts during automation execution
            import os
            os.environ["VAF_NONINTERACTIVE"] = "1"
            os.environ["VAF_IN_AUTOMATION"] = "1"
            
            # If workflow_steps exist, use workflow engine (n8n-like)
            if task.workflow_steps and len(task.workflow_steps) > 0:
                from vaf.workflows.engine import WorkflowEngine, WorkflowStep
                from vaf.core.agent import Agent
                from vaf.core.config import get_local_admin_scope_id, get_local_admin_username

                # Initialize agent to get tools
                agent = Agent(verbose=False, run_kind="automation")
                agent.load_model()
                agent.init_chat()
                # Background run: this agent must stay SILENT — its tool_update emits must never broadcast
                # into a live user's chat (a scheduled automation has no own web session, so the global
                # session fallback would otherwise route its tool bubbles to whoever is the active web user).
                agent._background_run = True
                # User isolation: workflow runs with task owner's scope (tools + memory)
                agent._current_user_scope_id = task.user_scope_id
                if not task.user_scope_id or str(task.user_scope_id).strip() == str(get_local_admin_scope_id()).strip():
                    agent._current_username = get_local_admin_username()
                else:
                    # SECURITY (cross-user leak): a non-admin scope must resolve to its OWN account
                    # username, never the literal "admin" — the username keys UserWorkspace
                    # (~/.vaf/users/<username>), which feeds the system-prompt <user_context> and the
                    # username-keyed calendar/contacts/mail tools. Reuse the thinking-mode resolver so a
                    # non-admin automation can never inject the admin's identity/profile.
                    from vaf.core.thinking_mode import _resolve_username_for_scope
                    _resolved = _resolve_username_for_scope(task.user_scope_id)
                    agent._current_username = _resolved or ("scope_" + str(task.user_scope_id).replace("-", "")[:8])

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
                )
                # Add 'date' to workflow defaults so {date} can be resolved in templates
                engine._workflow_defaults = {"date": date_str}
                engine._workflow_name = task.name
                workflow_result = engine.execute(steps, variables=task.parameters)

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
                    workflow_saved_file = False
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
                else:
                    result = f"# Workflow Failed\n\n**Error:** {workflow_result.error}\n\n"
                    result += "## Steps Executed:\n"
                    for i, step_result in enumerate(step_results, 1):
                        status_icon = "✅" if step_result["status"] == "success" else "❌"
                        result += f"{status_icon} Step {i}: {step_result['tool']} - {step_result['status']}\n"
                    workflow_saved_file = False
                
                agent.shutdown()
                
                # Skip legacy output saving if workflow already saved the file
                if workflow_saved_file:
                    # Workflow's write_file step already saved the output.
                    _stamp_successful_run(task)
                    self._save_task(task)
                    self._sync_workspace_automation_state(
                        task,
                        run_status="success",
                        summary=(result or "")[:1000],
                        event="automation_run",
                    )
                    # Delete ONCE tasks here too (early-return path would skip the
                    # ONCE-delete block further below).
                    if task.frequency == Frequency.ONCE:
                        self.delete(task.id, permanent=True)
                    # Build a short, friendly bot summary instead of the raw technical report.
                    # final_output is the LAST step's result string - only treat it as a saved
                    # file when it actually IS one, otherwise the Web UI printed tool chatter
                    # like "Gespeichert: Message sent to the user via Telegram." (live 2026-07-13).
                    saved_path = str(workflow_result.final_output or "").strip()
                    real_file = saved_path if (saved_path and os.path.isfile(saved_path)) else None
                    short_summary = (
                        f"✅ **{task.name}** ist fertig!\n\n"
                        + (f"{real_file}\n\n" if real_file else "")
                        + f"Alle {len(steps)} Schritte erfolgreich abgeschlossen."
                    ).strip()
                    _push_result_to_web_ui(
                        task, "success", short_summary, output_file=real_file,
                        deliver_messenger=not _delivered_via_send_step(step_results),
                    )
                    return result
            else:
                # Legacy: Use prompt-based execution (backwards compatibility)
                # ONE-STEP AUTOMATION: Simple prompt, clean output only
                prompt = task.prompt
                for key, value in task.parameters.items():
                    prompt = prompt.replace(f"{{{key}}}", str(value))
                
                # Import agent and run
                from vaf.core.agent import Agent
                import re
                
                agent = Agent(verbose=False, run_kind="automation")
                agent.load_model()
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
                from vaf.core.config import get_local_admin_scope_id, get_local_admin_username
                agent._current_user_scope_id = task.user_scope_id
                if not task.user_scope_id or str(task.user_scope_id).strip() == str(get_local_admin_scope_id()).strip():
                    agent._current_username = get_local_admin_username()
                else:
                    # SECURITY (cross-user leak): a non-admin scope must resolve to its OWN account
                    # username, never the literal "admin" — the username keys UserWorkspace
                    # (~/.vaf/users/<username>), which feeds the system-prompt <user_context> and the
                    # username-keyed calendar/contacts/mail tools. Reuse the thinking-mode resolver so a
                    # non-admin automation can never inject the admin's identity/profile.
                    from vaf.core.thinking_mode import _resolve_username_for_scope
                    _resolved = _resolve_username_for_scope(task.user_scope_id)
                    agent._current_username = _resolved or ("scope_" + str(task.user_scope_id).replace("-", "")[:8])

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

                # Capture response. We keep TWO buffers:
                #  - raw_parts: the untouched stream, so a <think>...</think> block stays INTACT for the
                #    final reasoning strip. Stripping the tags per-chunk (as below) would only remove the
                #    markers and leave the reasoning TEXT, which the heuristic cleaner cannot then detect
                #    (esp. non-English reasoning) — that is exactly how a CoT preamble leaked into a result.
                #  - response_parts: the legacy tag-stripped buffer, kept only as a fallback.
                raw_parts = []
                response_parts = []
                def capture(text):
                    raw_parts.append(text)
                    # Filter out internal thinking tags and formatting
                    # Remove Rich markup tags like [white dim]...[/]
                    filtered = re.sub(r'\[/?[^\]]+\]', '', text)
                    # Remove thinking/reasoning tags
                    filtered = re.sub(r'</?think>', '', filtered, flags=re.IGNORECASE)
                    filtered = re.sub(r'</?redacted_reasoning>', '', filtered, flags=re.IGNORECASE)
                    # Remove tool_call XML tags
                    filtered = re.sub(r'<tool_call>.*?</tool_call>', '', filtered, flags=re.DOTALL)
                    if filtered.strip():
                        response_parts.append(filtered)
                    if callback:
                        callback(text)

                # RAG: fetch memory context for this turn (pre-injection, before LLM)
                memory_context = ""
                try:
                    from vaf.core.config import Config
                    if Config.get("memory_enabled", True):
                        from vaf.memory.rag import run_memory_search_sync
                        from uuid import UUID as _UUID
                        k = int(Config.get("memory_rag_k", 5))
                        k = max(1, min(20, k))
                        # Use task's user_scope_id for scoped RAG search
                        task_scope = None
                        if task.user_scope_id:
                            try:
                                task_scope = _UUID(str(task.user_scope_id))
                            except (ValueError, TypeError):
                                pass
                        memory_context = run_memory_search_sync(
                            query=prompt, k=k, user_scope_id=task_scope, caller="automation"
                        )
                except Exception:
                    memory_context = ""

                # RUNAWAY GUARD: bound the whole fallback turn so a stuck/looping provider (e.g. a stateful
                # gateway 400ing every tool turn) can never run unbounded. On timeout the caller is freed and
                # the automation lock released; the abandoned worker cannot keep spawning recursive workflows
                # because those tools were stripped above.
                _auto_to = 600.0
                try:
                    from vaf.core.bounded_run import (
                        run_bounded as _run_bounded,
                        TIMEOUT_PREFIX as _TO_PREFIX,
                        STOPPED_PREFIX as _ST_PREFIX,
                    )
                    from vaf.core.config import Config as _CfgAuto
                    _auto_to = float(_CfgAuto.get("automation_run_timeout_seconds", 600) or 600)
                    _chat_done = {"done": False}

                    def _do_chat():
                        agent.chat_step(prompt, stream_callback=capture, memory_context=memory_context or None)
                        _chat_done["done"] = True
                        return True

                    _bounded_ret = _run_bounded(_do_chat, timeout=_auto_to, label="automation_prompt_run")
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
                                f"treating it as a normal completion.",
                            )
                        else:
                            prompt_timeout_unresolved = True
                            append_domain_log_always(
                                "backend",
                                f"[AUTOMATION] '{task.name}' still unfinished after the grace window - "
                                f"delivering an honest timeout note (no partial result, no file wrap).",
                            )
                    elif isinstance(_bounded_ret, str) and _bounded_ret.startswith(_ST_PREFIX):
                        # User-initiated stop: no grace wait, but the same honest handling.
                        prompt_timeout_unresolved = True
                except Exception:
                    # Fallback to a plain call if run_bounded is unavailable for any reason.
                    agent.chat_step(prompt, stream_callback=capture, memory_context=memory_context or None)

                if prompt_timeout_unresolved:
                    result = (
                        f"Error: Zeitlimit überschritten - die Automation '{task.name}' war nach "
                        f"{int(_auto_to)}s plus Nachfrist noch nicht fertig. Es wird bewusst kein "
                        f"Teilergebnis zugestellt; falls der Lauf im Hintergrund doch noch fertig "
                        f"wird, meldet er sich selbst."
                    )
                else:
                    raw_result = "".join(response_parts)

                    # Deliver only the final clean answer. Use the SAME canonical cleaner the live chat uses
                    # (agent._clean_reasoning): it removes whole <think>...</think> blocks (content included),
                    # so reasoning is stripped structurally and language-agnostically — not by brittle, English-
                    # only phrase heuristics. Fall back to the legacy extractor only if that yields nothing.
                    result = ""
                    try:
                        result = (agent._clean_reasoning("".join(raw_parts)) or "").strip()
                    except Exception:
                        result = ""
                    if not result:
                        result = self._extract_clean_answer(raw_result, agent.history)

                agent.shutdown()
            
            # Save output if path specified. NEVER on an unresolved timeout: wrapping the
            # honest error note (or a half stream) into an output file produced the junk
            # HTML attachments of 2026-07-13.
            if task.output_path and not prompt_timeout_unresolved:
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
            
            # Update task (local completion date persists across restarts)
            _stamp_successful_run(task)
            # next_run is calculated dynamically - no need to store it
            self._save_task(task)

            # If frequency is ONCE, delete after run
            if task.frequency == Frequency.ONCE:
                # Use permanent=True because it's a planned one-time run, not a manual deletion
                self.delete(task.id, permanent=True)
                append_domain_log_always("backend", f"Automation '{task.name}' ({task.id}) frequency is 'once' - deleted after run.")

            prompt_delivered_in_run = _delivered_via_agent_history(getattr(agent, "history", None))
            agent.shutdown()
            
        except Exception as e:
            result = f"Error: {e}"
            UI.error(f"Automation failed: {e}")
        finally:
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
            if task.frequency == Frequency.ONCE and task.id in self.tasks:
                try:
                    self.delete(task.id, permanent=True)
                    append_domain_log_always(
                        "backend",
                        f"Automation '{task.name}' ({task.id}) frequency is 'once' - removed after run attempt.",
                    )
                except Exception:
                    pass

            # Clean up environment variables
            import os
            os.environ.pop("VAF_IN_AUTOMATION", None)
            # Keep VAF_NONINTERACTIVE if it was set before
        
        # Deliver result to user via Web UI chat + messenger.
        # Only one delivery path — no duplicate notification + chat message.
        try:
            status = "error" if (result or "").strip().startswith("Error:") else "success"
            # For prompt-based tasks the result IS the summary (already clean text).
            # saved_output_path is None for chat-only runs (no file produced) -> text-only delivery.
            # A confirmed in-run send suppresses only the messenger push (live 2026-07-14:
            # the calendar check messaged the user twice); Web UI + notification stay.
            _push_result_to_web_ui(
                task, status, result or "Completed", output_file=saved_output_path,
                deliver_messenger=not prompt_delivered_in_run,
            )
        except Exception:
            pass
        try:
            self._sync_workspace_automation_state(
                task,
                run_status=status,
                summary=(result or "")[:1000],
                event="automation_run",
            )
        except Exception:
            pass

        # Always return cleanly
        return result
    
    def start_scheduler(self):
        """Start the background scheduler."""
        if not HAS_SCHEDULE:
            self._log_scheduler_event("START_FAILED reason='schedule package missing'")
            raise ImportError("'schedule' package required. Install: pip install schedule")

        if self._running:
            self._log_scheduler_event("START_SKIPPED reason='already running'")
            return

        # The `schedule` registry is MODULE-GLOBAL while _running is per-instance:
        # a second manager instance starting "its" scheduler re-registers every job
        # into the same global registry (without a clear) and spins up a second
        # loop thread - every task then fires twice and only the run lock prevents
        # double execution (live 2026-07-13: double TRIGGER on every automation).
        # Only the process-wide singleton may pump the scheduler. Deliberately read
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
        enabled_tasks = self.list(enabled_only=True)
        self._log_scheduler_event(
            f"START task_count={len(enabled_tasks)} storage_dir={str(self.storage_dir)!r}"
        )
        
        def scheduler_loop():
            self._log_scheduler_event("LOOP_STARTED")
            while self._running:
                schedule.run_pending()
                # One-shot reminders (narrow lane, see vaf/core/reminders.py): a
                # reminder is stored data delivered verbatim - no agent run. Fired
                # here so only the process singleton ever delivers them.
                try:
                    from vaf.core.reminders import fire_due_reminders
                    fire_due_reminders()
                except Exception:
                    pass
                time.sleep(30)  # Check every 30 seconds
            self._log_scheduler_event("LOOP_STOPPED")
        
        # Schedule all enabled tasks
        for task in enabled_tasks:
            self._schedule_task(task)
        
        self._scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
        self._scheduler_thread.start()
    
    def stop_scheduler(self):
        """Stop the background scheduler."""
        self._log_scheduler_event("STOP_REQUESTED")
        self._running = False
        schedule.clear()
    
    def _schedule_task(self, task: AutomationTask):
        """Add a task to the scheduler."""
        if not task.enabled:
            self._log_scheduler_event(
                f"REGISTER_SKIPPED task_id={task.id} name={task.name!r} reason='disabled'"
            )
            return
        
        # Owner timezone (IANA name) so wall-clock times fire in the USER's zone, not the server's.
        # schedule's Job.at(time, tz) handles DST; tz=None -> server-local (unchanged behavior).
        from vaf.core.user_time import resolve_user_timezone_name, user_now
        _owner_user = _resolve_username(task.user_scope_id)
        _tz = resolve_user_timezone_name(_owner_user)

        # Run in new terminal window by default
        job_func = lambda t=task: self._run_scheduled_task(t)

        if task.frequency == Frequency.HOURLY:
            schedule.every().hour.at(f":{task.time.split(':')[1]}", _tz).do(job_func)
            self._log_scheduler_event(
                f"REGISTERED task_id={task.id} name={task.name!r} frequency=hourly time={task.time}"
            )
        
        elif task.frequency == Frequency.DAILY:
            schedule.every().day.at(task.time, _tz).do(job_func)
            self._log_scheduler_event(
                f"REGISTERED task_id={task.id} name={task.name!r} frequency=daily time={task.time}"
            )
        
        elif task.frequency == Frequency.WEEKLY:
            weekday = task.weekday or "monday"
            getattr(schedule.every(), weekday).at(task.time, _tz).do(job_func)
            self._log_scheduler_event(
                f"REGISTERED task_id={task.id} name={task.name!r} frequency=weekly weekday={weekday} time={task.time}"
            )
        
        elif task.frequency == Frequency.MONTHLY:
            # Monthly is trickier - check daily and run if day matches
            def monthly_check(t=task, _u=_owner_user):
                _cur_day = user_now(_u).day  # day-of-month in the owner's timezone
                if _cur_day == (t.day or 1):
                    self._run_scheduled_task(t)
                else:
                    self._log_scheduler_event(
                        f"MONTHLY_SKIP task_id={t.id} name={t.name!r} expected_day={t.day or 1} current_day={_cur_day}"
                    )
            schedule.every().day.at(task.time, _tz).do(monthly_check)
            self._log_scheduler_event(
                f"REGISTERED task_id={task.id} name={task.name!r} frequency=monthly day={task.day or 1} time={task.time}"
            )

        elif task.frequency == Frequency.ONCE:
            # einmalig = einmalig: never re-arm a one-time task that has already
            # fired. Guards against a lingering file being re-registered after a
            # restart or scheduler refresh, which would otherwise run it again.
            if task.last_run or task.last_completed_local_date:
                self._log_scheduler_event(
                    f"REGISTER_SKIPPED task_id={task.id} name={task.name!r} "
                    f"reason='once already ran' last_run={task.last_run}"
                )
                try:
                    self.delete(task.id, permanent=True)
                except Exception:
                    pass
                return
            # Run exactly once at the specified time (today if still in the future,
            # tomorrow if the time has already passed).  Returning schedule.CancelJob
            # from the callback removes the job automatically after it fires.
            def once_job(t=task):
                self._run_scheduled_task(t)
                return schedule.CancelJob
            schedule.every().day.at(task.time, _tz).do(once_job)
            self._log_scheduler_event(
                f"REGISTERED task_id={task.id} name={task.name!r} frequency=once time={task.time}"
            )

        else:
            self._log_scheduler_event(
                f"REGISTER_SKIPPED task_id={task.id} name={task.name!r} reason='unsupported frequency {task.frequency}'"
            )


def get_next_automation_run_utc(user_scope_id: Optional[str]) -> Optional[datetime]:
    """
    Return the next run time (minimum across all visible automations for this user).
    Used by thinking mode to skip starting if an automation runs within the buffer window.
    For local admin, includes root automations (same merge as get_automations in web_server).
    """
    from vaf.core.config import get_local_admin_scope_id
    tasks: List[AutomationTask] = []
    mgr = AutomationManager(user_scope_id=user_scope_id) if user_scope_id else AutomationManager()
    tasks.extend(mgr.list(enabled_only=True))
    local_scope = get_local_admin_scope_id()
    if user_scope_id and str(user_scope_id).strip() == str(local_scope).strip():
        root_mgr = AutomationManager()
        seen = {t.id for t in tasks}
        for t in root_mgr.list(enabled_only=True):
            if t.id not in seen:
                tasks.append(t)
                seen.add(t.id)
    if not tasks:
        return None
    return min(t.next_run_datetime for t in tasks)


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
                schedule.clear()
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

        _scheduler_manager._log_scheduler_event(f"REFRESH_START origin={origin!r}")
        _scheduler_manager.reload_tasks()
        schedule.clear()
        for task in _scheduler_manager.list(enabled_only=True):
            _scheduler_manager._schedule_task(task)
        _scheduler_manager._log_scheduler_event(
            f"REFRESH_DONE origin={origin!r} task_count={len(_scheduler_manager.list(enabled_only=True))}"
        )
        return True


@automation_app.command("list")
def list_automations():
    """List all automation tasks."""
    from rich.console import Console
    from rich.table import Table
    
    console = Console()
    manager = get_manager()
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
        next_run = task.next_run_datetime.strftime("%Y-%m-%d %H:%M")
        
        table.add_row(
            task.id,
            task.name[:20],
            task.frequency,
            task.time,
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
    output: str = typer.Option(None, "--output", "-o", help="Output directory")
):
    """Create a new automation task."""
    from rich.console import Console
    
    console = Console()
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
    
    # Create task
    task = AutomationTask(
        name=name,
        prompt=prompt,
        frequency=frequency,
        time=time,
        output_path=output or str(Path.home() / "Desktop"),
        parameters=params
    )
    
    task = manager.create(task)
    
    console.print(f"\n[green]✓ Automation created![/green]")
    console.print(f"  [dim]ID:[/dim] {task.id}")
    console.print(f"  [dim]Next run:[/dim] {task.next_run_datetime.strftime('%Y-%m-%d %H:%M')}")
    console.print(f"\n[dim]Start scheduler with: vaf automation start[/dim]")


@automation_app.command("run")
def run_automation(
    task_id: str = typer.Argument(..., help="Task ID to run")
):
    """Manually run an automation task."""
    from rich.console import Console
    
    console = Console()
    manager = get_manager()
    
    task = manager.get(task_id)
    if not task:
        console.print(f"[red]Task not found: {task_id}[/red]")
        raise typer.Exit(1)
    
    console.print(f"\n[cyan]⚡ Running: {task.name}[/cyan]\n")
    
    def print_output(text):
        console.print(text, end="")
    
    # Don't open new terminal when called directly from CLI
    result = manager.run_task(task, callback=print_output, new_terminal=False)
    console.print("\n")


@automation_app.command("delete")
def delete_automation(
    task_id: str = typer.Argument(..., help="Task ID to delete"),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation")
):
    """Delete an automation task."""
    from rich.console import Console
    
    console = Console()
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
    console.print(f"[green]✓ Deleted: {task.name}[/green]")


@automation_app.command("start")
def start_scheduler():
    """Start the automation scheduler daemon."""
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text
    
    console = Console()
    
    if not HAS_SCHEDULE:
        console.print("[red]Missing dependency: schedule[/red]")
        console.print("[dim]Install with: pip install schedule[/dim]")
        raise typer.Exit(1)
    
    manager = get_manager()
    tasks = manager.list(enabled_only=True)
    
    if not tasks:
        console.print("[yellow]No enabled automations to run.[/yellow]")
        return
    
    console.print(f"\n[cyan]⚡ Starting VAF Automation Scheduler[/cyan]")
    console.print(f"[dim]Running {len(tasks)} task(s). Press Ctrl+C to stop.[/dim]\n")
    
    for task in tasks:
        console.print(f"  • {task.name} @ {task.time} ({task.frequency})")
    
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
    from rich.console import Console
    
    console = Console()
    manager = get_manager()
    
    console.print("[cyan]Reloading automations...[/cyan]")
    manager.reload_tasks()
    
    tasks = manager.list()
    console.print(f"[green]✓ Reloaded {len(tasks)} automation(s)[/green]")
    
    # next_run is now calculated dynamically, so no corrections needed

