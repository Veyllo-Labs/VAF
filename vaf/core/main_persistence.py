import json
import os
import time
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict
from pathlib import Path

# Main Context Constants
MAIN_CONTEXT_DIR = ".vaf/main"
USER_INTENT_FILE = "user_intent.md"
TEAM_STATE_FILE = "team_state.json"
WORKING_MEMORY_FILE = "working_memory.json"
RESULTS_DIR = "results"

# Team state: entries older than this are pruned (per session / recent-only in prompt)
TEAM_STATE_TTL_SECONDS = 3 * 3600  # 3 hours

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
            self._save_json(mem_path, {"notes": [], "plan": []})

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
    def get_working_memory(self) -> Dict:
        return self._load_json(self.context_dir / WORKING_MEMORY_FILE, {})

    def update_working_memory(
        self,
        notes: Optional[List[str]] = None,
        plan: Optional[List[str]] = None,
        add_notes: Optional[List[str]] = None,
        add_plan: Optional[List[str]] = None,
    ):
        mem = self.get_working_memory()
        notes_list = mem.get("notes", [])
        plan_list = mem.get("plan", [])
        if add_notes:
            notes_list = notes_list + list(add_notes)
        if add_plan:
            plan_list = plan_list + list(add_plan)
        if notes is not None:
            notes_list = notes
        if plan is not None:
            plan_list = plan
        mem["notes"] = notes_list
        mem["plan"] = plan_list
        self._save_json(self.context_dir / WORKING_MEMORY_FILE, mem)

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
            
        return f"""
# 🧠 MAIN AGENT CONTEXT (Live System State)

<user_intent>
{intent}
</user_intent>

<team_state>
{team_str}
</team_state>

<working_memory>
Notes: {json.dumps(memory.get('notes', []), ensure_ascii=False)}
Plan: {json.dumps(memory.get('plan', []), ensure_ascii=False)}
</working_memory>

Use the update_working_memory tool to save notes and plan; they persist across turns and appear here. Use notes/plan to set the full list, or add_notes/add_plan to append without replacing.

Long-term memories about the user/system (from memory_store/RAG) are injected as "Memory context" when relevant to the query; use them to answer questions like "what do you remember about me?" and use memory_store to save new facts.
"""
