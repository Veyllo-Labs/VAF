# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import json
import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Union
from dataclasses import dataclass, field, asdict
from pathlib import Path

def _to_user_tz(dt: datetime) -> datetime:
    """Convert a tz-aware (UTC-stored) working-memory timestamp to the user's
    timezone for PROMPT display. Stored stamps are UTC ("...Z"), but the model
    read them as wall-clock ("10:03" for a 12:03 local event) - off-by-tz
    times in the working-memory block misdate the model's own recent actions
    (timezone SSOT: vaf/core/user_time). Naive stamps and any error pass
    through unchanged."""
    try:
        if dt.tzinfo is not None:
            from vaf.core.user_time import resolve_user_timezone
            tz = resolve_user_timezone(None)
            if tz is not None:
                return dt.astimezone(tz)
    except Exception:
        pass
    return dt


# Main Context Constants
MAIN_CONTEXT_DIR = ".vaf/main"
USER_INTENT_FILE = "user_intent.md"
TEAM_STATE_FILE = "team_state.json"
WORKING_MEMORY_FILE = "working_memory.json"
RESULTS_DIR = "results"
SUBAGENT_VALIDATION_FILE = "subagent_validation.json"

# Team state: a stuck entry (e.g. a sub-agent that crashed without reporting) is pruned
# after this wall-clock TTL as a safety net.
TEAM_STATE_TTL_SECONDS = 3 * 3600  # 3 hours
# A finished (completed/failed) team entry is shown as "done HH:MM" for this many main-agent
# turns, then removed from the team list entirely.
TEAM_DONE_PRUNE_TURNS = 3

# Working memory: max entries per list (notes, plan, tasks); oldest dropped when exceeded
WORKING_MEMORY_MAX_ENTRIES = 500
# Tasks with status "done" are removed after this many seconds
WORKING_MEMORY_TASKS_DONE_TTL_SECONDS = 12 * 3600  # 12 hours

@dataclass
class SubAgentState:
    agent_type: str
    task_id: str
    status: str  # running, completed, failed, needs_clarification
    current_task: str
    last_update: float = field(default_factory=time.time)
    result_summary: Optional[str] = None
    clarification_question: Optional[str] = None
    result_file: Optional[str] = None
    # Set when the agent reaches a terminal status; drives the "done HH:MM" label and
    # the turn-based prune. prune_in_turns < 0 means "active" (not counting down).
    completed_at: Optional[float] = None
    prune_in_turns: int = -1

@dataclass
class TeamState:
    active_agents: Dict[str, SubAgentState] = field(default_factory=dict)
    completed_tasks: List[str] = field(default_factory=list)
    last_updated: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {
            "active_agents": {k: asdict(v) for k, v in self.active_agents.items()},
            "completed_tasks": self.completed_tasks,
            "last_updated": self.last_updated
        }

    @classmethod
    def from_dict(cls, data: Dict) -> 'TeamState':
        agents = {}
        for k, v in data.get("active_agents", {}).items():
            agents[k] = SubAgentState(**v)
        return cls(
            active_agents=agents,
            completed_tasks=data.get("completed_tasks", []),
            last_updated=data.get("last_updated", time.time())
        )

class MainPersistenceManager:
    """
    Manages the persistent context for the Main Agent.
    Implements the Hybrid Architecture: Memory Blocks + JSON State + Tiered Results.
    """

    def __init__(self, base_dir: str, session_id: Optional[str] = None):
        self.base_dir = Path(base_dir)
        self.session_id = (str(session_id).strip() or None) if session_id else None
        if self.session_id:
            # Per-session isolation: each conversation gets its own intent/plan/tasks/notes/team so
            # the live state never leaks across chats or users. Falls back to the legacy global
            # .vaf/main/ when no session is known (e.g. single-shot CLI), preserving old behavior.
            self.context_dir = self.base_dir / MAIN_CONTEXT_DIR / "sessions" / self._safe_session_dir(self.session_id)
        else:
            self.context_dir = self.base_dir / MAIN_CONTEXT_DIR
        self.results_dir = self.context_dir / RESULTS_DIR

        # Ensure directory structure exists
        self.context_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)

        self._init_files()

    @staticmethod
    def _safe_session_dir(session_id: str) -> str:
        """Sanitize a session id into a single safe path segment (prevents traversal):
        non-allowed chars -> '_', dot-runs collapsed (no '..'), no leading/trailing punctuation."""
        import re as _re
        safe = _re.sub(r"[^A-Za-z0-9_.-]", "_", str(session_id).strip())
        safe = _re.sub(r"\.+", ".", safe)          # collapse dot runs so no '..' survives
        safe = safe.strip("._-") or "default"      # no leading/trailing punctuation
        return safe[:128]

    def _init_files(self):
        """Initialize empty context files if they don't exist."""
        # User Intent (The North Star)
        intent_path = self.context_dir / USER_INTENT_FILE
        if not intent_path.exists():
            intent_path.write_text("# USER INTENT\n\n(No intent defined yet.)", encoding="utf-8")
            
        # Team State (The Orchestration Layer)
        team_path = self.context_dir / TEAM_STATE_FILE
        if not team_path.exists():
            initial_state = TeamState()
            self._save_json(team_path, initial_state.to_dict())
            
        # Working Memory (The Scratchpad)
        mem_path = self.context_dir / WORKING_MEMORY_FILE
        if not mem_path.exists():
            self._save_json(mem_path, {"notes": [], "plan": [], "tasks": []})

    def _save_json(self, path: Path, data: Dict):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def _load_json(self, path: Path, default: Any = None) -> Any:
        if not path.exists():
            return default
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return default

    # --- USER INTENT ---
    def get_user_intent(self) -> str:
        return (self.context_dir / USER_INTENT_FILE).read_text(encoding="utf-8")

    def update_user_intent(self, content: str):
        (self.context_dir / USER_INTENT_FILE).write_text(content, encoding="utf-8")

    # --- SUBAGENT VALIDATION RETRY COUNT ---
    def _get_validation_data(self) -> dict:
        """Load full subagent_validation.json. Merges with defaults."""
        data = self._load_json(self.context_dir / SUBAGENT_VALIDATION_FILE, {})
        if not isinstance(data, dict):
            data = {}
        return data

    def get_validation_retry_count(self) -> int:
        """Get the current validation retry count (consecutive </false> results)."""
        data = self._get_validation_data()
        return int(data.get("retry_count", 0))

    def increment_validation_retry_count(self) -> int:
        """Increment and save the retry count. Returns the new value. Preserves intent/goal."""
        data = self._get_validation_data()
        count = int(data.get("retry_count", 0)) + 1
        data["retry_count"] = count
        self._save_json(self.context_dir / SUBAGENT_VALIDATION_FILE, data)
        return count

    def reset_validation_retry_count(self) -> None:
        """Reset the retry count to 0 (e.g. when user sends new message). Preserves intent/goal."""
        data = self._get_validation_data()
        data["retry_count"] = 0
        self._save_json(self.context_dir / SUBAGENT_VALIDATION_FILE, data)

    def write_subagent_delegation_intent(self, intent: str, goal: str, agent_type: str) -> None:
        """
        Write user intent and delegation goal BEFORE sub-agent invocation.
        Resets retry_count to 0 (fresh delegation). Called from execute_tool.

        Stored BOTH in the legacy top-level slot (last delegation, backward compatible)
        AND keyed per agent_type under "by_agent" — so with chat-while-subagent-runs, a
        research spawn from light chat cannot clobber the running coder's intent that its
        result validation depends on.
        """
        from datetime import datetime
        data = self._get_validation_data()
        _intent = (intent or "").strip()
        _goal = (goal or "").strip()
        _atype = (agent_type or "").strip()
        data["intent"] = _intent
        data["goal"] = _goal
        data["agent_type"] = _atype
        data["retry_count"] = 0
        data["updated_at"] = datetime.now().isoformat()
        if _atype:
            by_agent = data.get("by_agent") or {}
            by_agent[_atype] = {
                "intent": _intent,
                "goal": _goal,
                "updated_at": data["updated_at"],
            }
            data["by_agent"] = by_agent
        self._save_json(self.context_dir / SUBAGENT_VALIDATION_FILE, data)

    def get_subagent_delegation_intent(self, agent_type: str = None) -> Optional[Dict[str, Any]]:
        """
        Return the intent/goal we wrote before the last sub-agent call.
        Used during validation. Returns None if no delegation intent stored.

        With agent_type, prefer that agent's own slot (immune to a later delegation of a
        DIFFERENT agent type overwriting the top-level fields); fall back to the legacy
        top-level slot for pre-existing files — but never to another agent's delegation.
        """
        data = self._get_validation_data()
        _atype_req = (agent_type or "").strip()
        if _atype_req:
            per = (data.get("by_agent") or {}).get(_atype_req) or {}
            p_intent = (per.get("intent") or "").strip()
            p_goal = (per.get("goal") or "").strip()
            if p_intent or p_goal:
                return {"intent": p_intent, "goal": p_goal, "agent_type": _atype_req}
        intent = (data.get("intent") or "").strip()
        goal = (data.get("goal") or "").strip()
        stored_type = (data.get("agent_type") or "").strip()
        if not intent and not goal:
            return None
        if _atype_req and stored_type and stored_type != _atype_req:
            # The top-level slot belongs to a DIFFERENT agent's delegation — do not
            # validate this agent's result against it.
            return None
        return {"intent": intent, "goal": goal, "agent_type": stored_type}

    # --- TEAM STATE ---
    def get_team_state(self) -> TeamState:
        data = self._load_json(self.context_dir / TEAM_STATE_FILE, {})
        state = TeamState.from_dict(data)
        # Prune entries older than TTL so prompt and file stay recent-only
        now = time.time()
        state.active_agents = {
            k: v for k, v in state.active_agents.items()
            if (now - getattr(v, "last_update", 0)) <= TEAM_STATE_TTL_SECONDS
        }
        state.last_updated = now
        self._save_json(self.context_dir / TEAM_STATE_FILE, state.to_dict())
        return state

    def update_subagent_status(self, task_id: str, agent_type: str, status: str, 
                               details: str = None, question: str = None, result_summary: str = None):
        """
        Updates the status of a sub-agent task.
        Critical for the 'Needs Clarification' protocol.
        """
        state = self.get_team_state()
        now = time.time()
        is_terminal = status in ("completed", "failed")

        # Create or Update agent state
        agent_key = f"{agent_type}_{task_id[:8]}"

        if agent_key not in state.active_agents:
            state.active_agents[agent_key] = SubAgentState(
                agent_type=agent_type,
                task_id=task_id,
                status=status,
                current_task=details or ("Done." if is_terminal else "Starting..."),
            )
        agent = state.active_agents[agent_key]
        agent.status = status
        agent.last_update = now
        if details:
            agent.current_task = details
        if question:
            agent.clarification_question = question
        if result_summary:
            agent.result_summary = result_summary

        # A finished agent gets a completion timestamp and starts its turn-based prune
        # countdown, so it shows as "done HH:MM" for a few turns and is then removed.
        # An agent that goes back to running (re-used key) clears the countdown.
        if is_terminal:
            if agent.completed_at is None:
                agent.completed_at = now
            agent.prune_in_turns = TEAM_DONE_PRUNE_TURNS
        else:
            agent.completed_at = None
            agent.prune_in_turns = -1

        state.last_updated = now
        self._save_json(self.context_dir / TEAM_STATE_FILE, state.to_dict())

    def tick_team_state(self) -> None:
        """Advance the team list by one main-agent turn: count down finished entries and
        drop those whose grace period (TEAM_DONE_PRUNE_TURNS) has elapsed. Called once per
        user turn so a completed sub-agent lingers as "done HH:MM" briefly, then disappears."""
        state = self.get_team_state()
        changed = False
        for key in list(state.active_agents.keys()):
            agent = state.active_agents[key]
            if agent.completed_at is None:
                continue                      # still active — never pruned by turn count
            agent.prune_in_turns -= 1
            changed = True
            if agent.prune_in_turns <= 0:
                del state.active_agents[key]
        if changed:
            state.last_updated = time.time()
            self._save_json(self.context_dir / TEAM_STATE_FILE, state.to_dict())

    def save_subagent_result_full(self, task_id: str, content: str) -> str:
        """
        Saves the FULL result to the tiered storage.
        Returns the relative path to be stored in the context summary.
        """
        filename = f"{task_id}_result.md"
        path = self.results_dir / filename
        path.write_text(content, encoding="utf-8")
        return str(path.relative_to(self.base_dir))

    # --- WORKING MEMORY ---

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    @staticmethod
    def _normalize_wm_entry(entry: Union[str, Dict], now_iso: str) -> Dict:
        """Normalize a note/plan entry to {t, text}. Accepts string (legacy) or dict."""
        if isinstance(entry, dict) and "text" in entry:
            t = entry.get("t") or now_iso
            return {"t": t, "text": str(entry["text"])}
        return {"t": now_iso, "text": str(entry)}

    @staticmethod
    def _format_wm_list(entries: List) -> str:
        """Format notes/plan list for prompt display: 'YYYY-MM-DD HH:MM - text' or '(no date) - text'."""
        lines = []
        for entry in entries:
            if isinstance(entry, dict) and "text" in entry:
                text = entry["text"]
                t = entry.get("t")
                if t:
                    try:
                        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
                        dt = _to_user_tz(dt)
                        lines.append(f"{dt.strftime('%Y-%m-%d %H:%M')} - {text}")
                    except (ValueError, TypeError):
                        lines.append(f"(no date) - {text}")
                else:
                    lines.append(f"(no date) - {text}")
            else:
                lines.append(f"(no date) - {entry}")
        return "\n".join(lines) if lines else "(empty)"

    @staticmethod
    def _parse_iso_to_ts(iso_str: Any) -> float:
        """Parse ISO timestamp to Unix timestamp for comparison. Returns 0 on failure."""
        if not iso_str:
            return 0.0
        try:
            s = str(iso_str).replace("Z", "+00:00")
            return datetime.fromisoformat(s).timestamp()
        except (ValueError, TypeError):
            return 0.0

    def _prune_done_tasks(self, mem: Dict) -> bool:
        """Remove tasks with status 'done' and ts older than 12h. Mutates mem. Returns True if changed."""
        tasks = mem.get("tasks", [])
        if not tasks:
            return False
        now_ts = time.time()
        cutoff = now_ts - WORKING_MEMORY_TASKS_DONE_TTL_SECONDS
        kept = []
        for t in tasks:
            if not isinstance(t, dict):
                kept.append(t)
                continue
            status = (t.get("status") or "pending").lower()
            if status != "done":
                kept.append(t)
                continue
            ts = self._parse_iso_to_ts(t.get("ts"))
            if ts >= cutoff:
                kept.append(t)
        if len(kept) != len(tasks):
            mem["tasks"] = kept
            return True
        return False

    def get_working_memory(self) -> Dict:
        mem = self._load_json(self.context_dir / WORKING_MEMORY_FILE, {})
        if not isinstance(mem, dict):
            mem = {}
        if self._prune_done_tasks(mem):
            self._save_json(self.context_dir / WORKING_MEMORY_FILE, mem)
        return mem

    @staticmethod
    def _wm_text(entry: Any) -> str:
        """Text of a working-memory entry (dict with 'text', or a bare value)."""
        return str(entry.get("text", "")) if isinstance(entry, dict) else str(entry)

    @classmethod
    def _wm_norm(cls, text: Any) -> str:
        """Normalized dedupe key: collapsed whitespace, casefolded. '' for blank."""
        return " ".join(cls._wm_text(text).split()).casefold()

    @classmethod
    def _dedupe_notes_plan(cls, entries: List[Any]) -> List[Any]:
        """Drop entries whose normalized text repeats an earlier one (keep first)."""
        seen: set = set()
        out: List[Any] = []
        for e in entries:
            k = cls._wm_norm(e)
            if k and k in seen:
                continue
            seen.add(k)
            out.append(e)
        return out

    @classmethod
    def _dedupe_tasks(cls, tasks: List[Any]) -> List[Any]:
        """Collapse tasks with the same normalized text (keep first); the kept one is 'done' if ANY
        duplicate was done, so cleaning up a polluted list never silently un-finishes a step."""
        order: List[str] = []
        by_key: Dict[str, Any] = {}
        for t in tasks:
            k = cls._wm_norm(t)
            if not k:
                continue
            if k not in by_key:
                by_key[k] = dict(t) if isinstance(t, dict) else {"text": str(t), "status": "pending"}
                order.append(k)
            else:
                st = (t.get("status") if isinstance(t, dict) else None) or "pending"
                if str(st).lower() == "done":
                    by_key[k]["status"] = "done"
        return [by_key[k] for k in order]

    def update_working_memory(
        self,
        notes: Optional[List[Any]] = None,
        plan: Optional[List[Any]] = None,
        add_notes: Optional[List[str]] = None,
        add_plan: Optional[List[str]] = None,
        tasks: Optional[List[Dict]] = None,
        add_task: Optional[str] = None,
        mark_task_done: Optional[int] = None,
        mark_all_done: bool = False,
    ):
        mem = self.get_working_memory()
        now_iso = self._now_iso()

        notes_list = list(mem.get("notes", []))
        plan_list = list(mem.get("plan", []))
        tasks_list = list(mem.get("tasks", []))

        # Append-with-dedupe: a model that loses track re-adds the same note/plan/task many times
        # (observed: the same task appended 5x in one turn), polluting working memory. Skip an
        # append whose normalized text already exists.
        if add_notes:
            seen = {self._wm_norm(e) for e in notes_list}
            for item in add_notes:
                k = self._wm_norm(item)
                if k and k not in seen:
                    notes_list.append({"t": now_iso, "text": str(item)})
                    seen.add(k)
        if add_plan:
            seen = {self._wm_norm(e) for e in plan_list}
            for item in add_plan:
                k = self._wm_norm(item)
                if k and k not in seen:
                    plan_list.append({"t": now_iso, "text": str(item)})
                    seen.add(k)
        if notes is not None:
            notes_list = self._dedupe_notes_plan([self._normalize_wm_entry(e, now_iso) for e in notes])
        if plan is not None:
            plan_list = self._dedupe_notes_plan([self._normalize_wm_entry(e, now_iso) for e in plan])

        if add_task is not None and str(add_task).strip():
            k = self._wm_norm(add_task)
            if k and k not in {self._wm_norm(t) for t in tasks_list}:
                tasks_list.append({
                    "text": str(add_task).strip(),
                    "status": "pending",
                    "ts": now_iso,
                })
        if mark_task_done is not None and 0 <= mark_task_done < len(tasks_list):
            t = tasks_list[mark_task_done]
            if isinstance(t, dict):
                t = dict(t)
                t["status"] = "done"
                t["ts"] = now_iso
                tasks_list[mark_task_done] = t
        # Bulk completion: the user says "mark everything done". Without this the model has to loop
        # mark_task_done by index and tends to lose track (observed). Mark every pending task done
        # in one call.
        if mark_all_done:
            for _i, _t in enumerate(tasks_list):
                if isinstance(_t, dict):
                    if str(_t.get("status") or "pending").lower() != "done":
                        _t = dict(_t)
                        _t["status"] = "done"
                        _t["ts"] = now_iso
                        tasks_list[_i] = _t
                else:
                    tasks_list[_i] = {"text": str(_t), "status": "done", "ts": now_iso}
        if tasks is not None:
            rebuilt = []
            for e in tasks:
                if isinstance(e, dict) and e.get("text") is not None:
                    st = (e.get("status") or "pending").lower()
                    rebuilt.append({
                        "text": str(e["text"]),
                        "status": "done" if st == "done" else "pending",
                        "ts": e.get("ts") or now_iso,
                    })
                else:
                    rebuilt.append({"text": str(e), "status": "pending", "ts": now_iso})
            tasks_list = self._dedupe_tasks(rebuilt)

        notes_list = notes_list[-WORKING_MEMORY_MAX_ENTRIES:]
        plan_list = plan_list[-WORKING_MEMORY_MAX_ENTRIES:]
        tasks_list = tasks_list[-WORKING_MEMORY_MAX_ENTRIES:]

        mem["notes"] = notes_list
        mem["plan"] = plan_list
        mem["tasks"] = tasks_list
        self._save_json(self.context_dir / WORKING_MEMORY_FILE, mem)

    def _format_tasks_list(self, tasks: List) -> str:
        """Format tasks for prompt: '[i] YYYY-MM-DD HH:MM [pending] text'.
        The leading [i] is the index to pass to update_working_memory(mark_task_done=i)."""
        lines = []
        for i, t in enumerate(tasks):
            if not isinstance(t, dict):
                lines.append(f"[{i}] (no date) [pending] {t}")
                continue
            text = t.get("text", "")
            status = (t.get("status") or "pending").lower()
            ts = t.get("ts")
            if ts:
                try:
                    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                    dt = _to_user_tz(dt)
                    lines.append(f"[{i}] {dt.strftime('%Y-%m-%d %H:%M')} [{status}] {text}")
                except (ValueError, TypeError):
                    lines.append(f"[{i}] (no date) [{status}] {text}")
            else:
                lines.append(f"[{i}] (no date) [{status}] {text}")
        return "\n".join(lines) if lines else "(empty)"

    @staticmethod
    def _current_step(tasks: List) -> Optional[tuple]:
        """Derive the current plan step = the FIRST task whose status is pending.
        Returns (index, text, done_count, total_count) or None when no pending task exists.
        Deriving (instead of storing an index) gives auto-advance for free and cannot desync
        from the task list (e.g. when done tasks are pruned after their TTL)."""
        if not isinstance(tasks, list) or not tasks:
            return None
        total = len(tasks)
        done_count = 0
        first_pending = None
        for i, t in enumerate(tasks):
            status = (t.get("status") if isinstance(t, dict) else None) or "pending"
            if str(status).lower() == "done":
                done_count += 1
            elif first_pending is None:
                text = (t.get("text", "") if isinstance(t, dict) else str(t)) or ""
                first_pending = (i, str(text).strip())
        if first_pending is None:
            return None
        return (first_pending[0], first_pending[1], done_count, total)

    # --- CONTEXT INJECTION HELPER ---
    def build_context_injection(self) -> str:
        """
        Builds the context string to be injected into the Main Agent's prompt.
        Reads live data from files.
        """
        intent = self.get_user_intent()
        team = self.get_team_state()
        memory = self.get_working_memory()
        
        # Format Team State for LLM. A finished agent renders as "done HH:MM" (or
        # "failed HH:MM") so the main agent can SEE it has stopped and stop waiting on it;
        # it lingers a few turns (TEAM_DONE_PRUNE_TURNS) then tick_team_state() removes it.
        team_str = "No active agents."
        if team.active_agents:
            lines = []
            for k, v in team.active_agents.items():
                done_time = ""
                if v.completed_at:
                    try:
                        done_time = " " + datetime.fromtimestamp(v.completed_at).strftime("%H:%M")
                    except Exception:
                        done_time = ""
                if v.status == "running":
                    status_icon, status_label = "🟢", "running"
                elif v.status == "failed":
                    status_icon, status_label = "🔴", f"failed{done_time}"
                elif v.status == "needs_clarification":
                    status_icon, status_label = "🟡", "needs clarification"
                else:  # completed
                    status_icon, status_label = "✅", f"done{done_time}"
                line = f"{status_icon} **{v.agent_type}** (ID: {v.task_id[:8]})\n   Status: {status_label}"
                # Only show the live "Doing:" line while the agent is actually working.
                if v.current_task and v.completed_at is None:
                    line += f"\n   Doing: {v.current_task}"
                if v.clarification_question: line += f"\n   ❓ QUESTION: {v.clarification_question}"
                if v.result_summary: line += f"\n   Result: {v.result_summary[:100]}..."
                lines.append(line)
            team_str = "\n".join(lines)

        notes_fmt = self._format_wm_list(memory.get("notes", []))
        plan_fmt = self._format_wm_list(memory.get("plan", []))
        tasks_fmt = self._format_tasks_list(memory.get("tasks", []))

        # Per-turn focus line. Two cases (steps live in tasks, never in plan):
        #  - a task is pending  -> focus the model on the first pending step (follow the plan step by
        #    step, mark each done before the next).  Kill-switch: plan_step_reminder_enabled.
        #  - a plan but NO tasks -> the agent put its approach in plan but never broke it into trackable
        #    steps, so nothing is enforced. Tell it to add tasks. Kill-switch:
        #    plan_without_tasks_reminder_enabled.
        # Silent otherwise (no nagging on plain chat).
        step_reminder = ""
        try:
            from vaf.core.config import Config
            _tasks = memory.get("tasks", [])
            _plan = memory.get("plan", [])
            step = self._current_step(_tasks) if Config.get("plan_step_reminder_enabled", True) else None
            if step is not None:
                _idx, _text, _done, _total = step
                step_reminder = (
                    f">> CURRENT STEP {_done + 1}/{_total}: \"{_text}\" — finish THIS step, then "
                    f"call update_working_memory(mark_task_done={_idx}) before starting another.\n\n"
                )
            elif _plan and not _tasks and Config.get("plan_without_tasks_reminder_enabled", True):
                step_reminder = (
                    ">> You have a plan but no tasks. Steps belong in tasks, not the plan — break it "
                    "into concrete steps with update_working_memory(add_task=\"...\") so each one is "
                    "tracked and kept on course.\n\n"
                )
        except Exception:
            step_reminder = ""

        return f"""
# 🧠 MAIN AGENT CONTEXT (Live System State)

<user_intent>
{intent}
</user_intent>

<team_state>
{team_str}
</team_state>

<working_memory>
{step_reminder}Notes (facts worth remembering):
{notes_fmt}

Plan (your high-level approach):
{plan_fmt}

Tasks (the concrete steps that carry out the plan; pending/done, done removed after 12h):
{tasks_fmt}
</working_memory>

Use the update_working_memory tool to keep these current; they persist across turns and appear here.
- plan = your high-level approach (a line or two: how you will tackle the intent). Keep it short and stable; replace it when the approach changes. The plan gate only needs this approach, not a full step list.
- tasks = the concrete, ordered steps that carry out the plan. add_task to add a step, mark_task_done(index) when it is finished; the current step is shown above and done tasks drop after 12h. This is where multi-step work is tracked and kept on course — put the steps here, not in plan. (If you set a plan but no tasks, you'll be reminded to break it into tasks.)
- notes = facts/observations worth remembering; add_notes to append.
- On a new user task, reset what no longer applies (e.g. update_working_memory(plan=[], notes=[])).

Long-term memories about the user/system (from memory_save/RAG) are injected as "Memory context" when relevant to the query; use them to answer questions like "what do you remember about me?" and use memory_save to save new facts.
"""
