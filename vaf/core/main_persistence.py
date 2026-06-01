import json
import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Union
from dataclasses import dataclass, field, asdict
from pathlib import Path

# Main Context Constants
MAIN_CONTEXT_DIR = ".vaf/main"
USER_INTENT_FILE = "user_intent.md"
TEAM_STATE_FILE = "team_state.json"
WORKING_MEMORY_FILE = "working_memory.json"
RESULTS_DIR = "results"
SUBAGENT_VALIDATION_FILE = "subagent_validation.json"

# Team state: entries older than this are pruned (per session / recent-only in prompt)
TEAM_STATE_TTL_SECONDS = 3 * 3600  # 3 hours

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

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir)
        self.context_dir = self.base_dir / MAIN_CONTEXT_DIR
        self.results_dir = self.context_dir / RESULTS_DIR
        
        # Ensure directory structure exists
        self.context_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        
        self._init_files()

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
        """
        from datetime import datetime
        data = self._get_validation_data()
        data["intent"] = (intent or "").strip()
        data["goal"] = (goal or "").strip()
        data["agent_type"] = (agent_type or "").strip()
        data["retry_count"] = 0
        data["updated_at"] = datetime.now().isoformat()
        self._save_json(self.context_dir / SUBAGENT_VALIDATION_FILE, data)

    def get_subagent_delegation_intent(self) -> Optional[Dict[str, Any]]:
        """
        Return the intent/goal we wrote before the last sub-agent call.
        Used during validation. Returns None if no delegation intent stored.
        """
        data = self._get_validation_data()
        intent = (data.get("intent") or "").strip()
        goal = (data.get("goal") or "").strip()
        agent_type = (data.get("agent_type") or "").strip()
        if not intent and not goal:
            return None
        return {"intent": intent, "goal": goal, "agent_type": agent_type}

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
        
        # Create or Update agent state
        agent_key = f"{agent_type}_{task_id[:8]}"
        
        if agent_key not in state.active_agents:
            state.active_agents[agent_key] = SubAgentState(
                agent_type=agent_type,
                task_id=task_id,
                status=status,
                current_task=details or "Starting..."
            )
        else:
            agent = state.active_agents[agent_key]
            agent.status = status
            agent.last_update = time.time()
            if details: agent.current_task = details
            if question: agent.clarification_question = question
            if result_summary: agent.result_summary = result_summary
        
        # Move to completed list if done
        if status in ["completed", "failed"]:
            # Optionally keep in active list for a bit or move to history
            # For now, we flag it but keep it so Main Agent sees the result
            pass

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
        """Format notes/plan list for prompt display: 'YYYY-MM-DD HH:MM - text' or '(ohne Datum) - text'."""
        lines = []
        for entry in entries:
            if isinstance(entry, dict) and "text" in entry:
                text = entry["text"]
                t = entry.get("t")
                if t:
                    try:
                        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
                        lines.append(f"{dt.strftime('%Y-%m-%d %H:%M')} - {text}")
                    except (ValueError, TypeError):
                        lines.append(f"(ohne Datum) - {text}")
                else:
                    lines.append(f"(ohne Datum) - {text}")
            else:
                lines.append(f"(ohne Datum) - {entry}")
        return "\n".join(lines) if lines else "(leer)"

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

    def update_working_memory(
        self,
        notes: Optional[List[Any]] = None,
        plan: Optional[List[Any]] = None,
        add_notes: Optional[List[str]] = None,
        add_plan: Optional[List[str]] = None,
        tasks: Optional[List[Dict]] = None,
        add_task: Optional[str] = None,
        mark_task_done: Optional[int] = None,
    ):
        mem = self.get_working_memory()
        now_iso = self._now_iso()

        notes_list = list(mem.get("notes", []))
        plan_list = list(mem.get("plan", []))
        tasks_list = list(mem.get("tasks", []))

        if add_notes:
            for item in add_notes:
                notes_list.append({"t": now_iso, "text": str(item)})
        if add_plan:
            for item in add_plan:
                plan_list.append({"t": now_iso, "text": str(item)})
        if notes is not None:
            notes_list = [self._normalize_wm_entry(e, now_iso) for e in notes]
        if plan is not None:
            plan_list = [self._normalize_wm_entry(e, now_iso) for e in plan]

        if add_task is not None and str(add_task).strip():
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
        if tasks is not None:
            tasks_list = []
            for e in tasks:
                if isinstance(e, dict) and e.get("text") is not None:
                    st = (e.get("status") or "pending").lower()
                    tasks_list.append({
                        "text": str(e["text"]),
                        "status": "done" if st == "done" else "pending",
                        "ts": e.get("ts") or now_iso,
                    })
                else:
                    tasks_list.append({"text": str(e), "status": "pending", "ts": now_iso})

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
                lines.append(f"[{i}] (ohne Datum) [pending] {t}")
                continue
            text = t.get("text", "")
            status = (t.get("status") or "pending").lower()
            ts = t.get("ts")
            if ts:
                try:
                    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                    lines.append(f"[{i}] {dt.strftime('%Y-%m-%d %H:%M')} [{status}] {text}")
                except (ValueError, TypeError):
                    lines.append(f"[{i}] (ohne Datum) [{status}] {text}")
            else:
                lines.append(f"[{i}] (ohne Datum) [{status}] {text}")
        return "\n".join(lines) if lines else "(leer)"

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
        
        # Format Team State for LLM
        team_str = "No active agents."
        if team.active_agents:
            lines = []
            for k, v in team.active_agents.items():
                status_icon = "🟢" if v.status == "running" else "🔴" if v.status == "failed" else "🟡" if v.status == "needs_clarification" else "✅"
                line = f"{status_icon} **{v.agent_type}** (ID: {v.task_id[:8]})\n   Status: {v.status}"
                if v.current_task: line += f"\n   Doing: {v.current_task}"
                if v.clarification_question: line += f"\n   ❓ QUESTION: {v.clarification_question}"
                if v.result_summary: line += f"\n   Result: {v.result_summary[:100]}..."
                lines.append(line)
            team_str = "\n".join(lines)

        notes_fmt = self._format_wm_list(memory.get("notes", []))
        plan_fmt = self._format_wm_list(memory.get("plan", []))
        tasks_fmt = self._format_tasks_list(memory.get("tasks", []))

        # Current-step reminder: focus the model on the first pending task each turn so it
        # follows its plan step by step (mark each done before the next). Silent when there is no
        # pending task (no nagging on plain chat). Kill-switch: plan_step_reminder_enabled.
        step_reminder = ""
        try:
            from vaf.core.config import Config
            if Config.get("plan_step_reminder_enabled", True):
                step = self._current_step(memory.get("tasks", []))
                if step is not None:
                    _idx, _text, _done, _total = step
                    step_reminder = (
                        f">> CURRENT STEP {_done + 1}/{_total}: \"{_text}\" — finish THIS step, then "
                        f"call update_working_memory(mark_task_done={_idx}) before starting another.\n\n"
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
{step_reminder}Notes:
{notes_fmt}

Plan:
{plan_fmt}

Tasks (pending/done; done removed after 12h):
{tasks_fmt}
</working_memory>

Use the update_working_memory tool to save notes, plan, and tasks; they persist across turns and appear here.
- Use notes/plan to set the full list (replaces existing), or add_notes/add_plan to append. On a new user task or after completing a task, replace or clear notes/plan so working memory does not grow without bound (e.g. update_working_memory(notes=[], plan=[]) when done).
- Tasks: add_task to add a step (pending), mark_task_done(index) to mark done; done tasks are automatically removed after 12 hours. Pending = in progress or waiting on something. For multi-step work, record each step as a task (add_task) so progress is tracked and the current step is shown above.

Long-term memories about the user/system (from memory_save/RAG) are injected as "Memory context" when relevant to the query; use them to answer questions like "what do you remember about me?" and use memory_save to save new facts.
"""
