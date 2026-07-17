# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
VAF Session Management - Save and restore conversations
Provides persistent storage for chat sessions
"""
import json
import uuid
import gzip
import random
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field, asdict, fields

# ═══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Message:
    """A single message in a conversation."""
    role: str  # user, assistant, system, tool
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    tool_calls: Optional[List[Dict]] = None
    tool_results: Optional[Dict] = None
    metadata: Optional[Dict] = None
    # Tool-call linkage: assistant messages carry `tool_calls`; the matching
    # role:"tool" result carries `tool_call_id` (+ `name`). Persisting these keeps
    # the agent aware of its own tool calls and their results across reloads.
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    # Proactive-bubble tag ("thinking" / "nudge" / "timer") that drives the per-bubble
    # agent-avatar animation in the Web UI. Persisted so the animation survives a reload /
    # chat-switch (to_dict omits it when None; from_dict tolerates old sessions without it).
    kind: Optional[str] = None

    def to_dict(self) -> Dict:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: Dict) -> "Message":
        # Filter to known dataclass fields so legacy/unknown keys in stored
        # sessions don't raise TypeError (backward compatibility).
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Session:
    """A conversation session with runtime state persistence."""
    id: str = field(default_factory=lambda: _generate_session_id())
    name: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    model: str = ""
    project_path: str = ""
    messages: List[Message] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    # Runtime state persistence (NEW)
    runtime_state: Dict[str, Any] = field(default_factory=dict)
    state_version: str = "1.0"
    
    def add_message(self, role: str, content: str, **kwargs) -> Message:
        """Add a message to the session."""
        msg = Message(role=role, content=content, **kwargs)
        self.messages.append(msg)
        self.updated_at = datetime.now().isoformat()
        return msg
    
    def update_runtime_state(self, provider_name: str, state: Dict[str, Any]) -> None:
        """
        Update runtime state for a specific provider.
        
        Args:
            provider_name: Name of the state provider (e.g., 'sandbox', 'context')
            state: State dictionary from the provider
        """
        if "providers" not in self.runtime_state:
            self.runtime_state["providers"] = {}
        
        self.runtime_state["providers"][provider_name] = {
            "state": state,
            "updated_at": datetime.now().isoformat()
        }
        self.updated_at = datetime.now().isoformat()
    
    def get_provider_state(self, provider_name: str) -> Optional[Dict[str, Any]]:
        """
        Get runtime state for a specific provider.
        
        Args:
            provider_name: Name of the state provider
            
        Returns:
            Provider state dictionary or None if not found
        """
        providers = self.runtime_state.get("providers", {})
        provider_data = providers.get(provider_name, {})
        return provider_data.get("state")
    
    def get_history(self, limit: int = None) -> List[Dict]:
        """Get message history for API calls.

        Preserves tool-call linkage (`tool_calls` on assistant messages,
        `tool_call_id`/`name` on role:"tool" results) so restored history keeps
        valid tool_use/tool_result pairs.
        """
        messages = self.messages[-limit:] if limit else self.messages
        out: List[Dict] = []
        for m in messages:
            entry: Dict[str, Any] = {"role": m.role, "content": m.content}
            if getattr(m, "tool_calls", None):
                entry["tool_calls"] = m.tool_calls
            if getattr(m, "tool_call_id", None):
                entry["tool_call_id"] = m.tool_call_id
            if getattr(m, "name", None):
                entry["name"] = m.name
            # Carry attached images (+ their persisted base_description) for user turns so
            # restored history keeps multi-turn vision grounding; stored in metadata["images"].
            if m.role == "user":
                _imgs = (getattr(m, "metadata", None) or {}).get("images")
                if _imgs:
                    entry["images"] = _imgs
            out.append(entry)
        return out
    
    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "model": self.model,
            "project_path": self.project_path,
            "messages": [m.to_dict() for m in self.messages],
            "metadata": self.metadata,
            "runtime_state": self.runtime_state,
            "state_version": self.state_version,
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> "Session":
        messages = [Message.from_dict(m) for m in data.get("messages", [])]
        
        # Automatic migration for old sessions without runtime_state
        runtime_state = data.get("runtime_state", {})
        state_version = data.get("state_version", "1.0")
        
        return cls(
            id=data.get("id", _generate_session_id()),
            name=data.get("name", ""),
            created_at=data.get("created_at", datetime.now().isoformat()),
            updated_at=data.get("updated_at", datetime.now().isoformat()),
            model=data.get("model", ""),
            project_path=data.get("project_path", ""),
            messages=messages,
            metadata=data.get("metadata", {}),
            runtime_state=runtime_state,
            state_version=state_version,
        )
    
    def summary(self) -> str:
        """Generate a short summary of the session."""
        if not self.messages:
            return "Empty session"
        
        # Find first user message
        for msg in self.messages:
            if msg.role == "user":
                content = msg.content[:50]
                return content + "..." if len(msg.content) > 50 else content
        
        return f"{len(self.messages)} messages"


def turn_context_messages_since_last_user(history: List[Dict], user_input: str) -> List[Dict]:
    """Extract the per-turn context artifacts of the latest turn from an agent
    history (OpenAI-style dicts: role/content/tool_calls/tool_call_id/name).

    Returns, in order, the messages that capture what the agent DID this turn and
    that appear AFTER the last user message matching ``user_input`` (falling back
    to the most recent user message):

      * assistant messages carrying ``tool_calls`` and their ``role:"tool"``
        results (when the raw tool scaffolding is still present), and
      * the ``role:"system"`` ``[Context: ...]`` summary that replaces those
        steps once the turn-end squash has run (the common case).

    Plain assistant text is intentionally skipped — it is persisted separately as
    the cleaned final response, avoiding duplication.
    """
    if not history:
        return []
    target = (user_input or "").strip()
    start = None
    fallback = None
    for i in range(len(history) - 1, -1, -1):
        if history[i].get("role") == "user":
            if fallback is None:
                fallback = i
            if target and str(history[i].get("content") or "").strip() == target:
                start = i
                break
    if start is None:
        start = fallback
    if start is None:
        return []
    out: List[Dict] = []
    for m in history[start + 1:]:
        role = m.get("role")
        content = str(m.get("content") or "")
        if (role == "assistant" and m.get("tool_calls")) or role == "tool":
            out.append(m)
        elif role == "system" and content.lstrip().startswith("[Context:"):
            out.append(m)
    return out


def _generate_session_id() -> str:
    """
    Generate a human-friendly session ID: <color><6 digits>
    Examples: yellow012345, red654321

    Collisions are unlikely, but we still try a few times against the default storage dir.
    """
    colors = ("yellow", "red", "blue", "green", "purple", "cyan", "orange")
    sessions_dir = Path.home() / ".vaf" / "sessions"

    for _ in range(20):
        color = random.choice(colors)
        digits = f"{random.randint(0, 999_999):06d}"
        sid = f"{color}{digits}"
        # Avoid collisions with existing session files
        if not (sessions_dir / f"{sid}.json").exists() and not (sessions_dir / f"{sid}.json.gz").exists():
            return sid

    # Fallback (extremely unlikely)
    return f"{random.choice(colors)}{random.randint(0, 999_999):06d}"


# ═══════════════════════════════════════════════════════════════════════════════
# SESSION MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

class SessionManager:
    """Manages session storage and retrieval with runtime state support."""
    
    def __init__(self, storage_dir: str = None, state_registry=None):
        if storage_dir:
            self.storage_dir = Path(storage_dir)
        else:
            self.storage_dir = Path.home() / ".vaf" / "sessions"
        
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._current: Optional[Session] = None
        self.state_registry = state_registry  # Optional StateRegistry for state management
    
    @property
    def current(self) -> Optional[Session]:
        """Get current active session."""
        return self._current
    
    def new(self, name: str = None, model: str = "", project_path: str = "", user_scope_id: str = None) -> Session:
        """Create a new session."""
        metadata = {}
        if user_scope_id:
            metadata["user_scope_id"] = user_scope_id
            
        session = Session(
            name=name or f"Session {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            model=model,
            project_path=project_path,
            metadata=metadata
        )
        self._current = session
        return session
    
    def save(self, session: Session = None, compress: bool = False, sync_state: bool = True) -> Path:
        """
        Save a session to disk.
        
        Args:
            session: Session to save (defaults to current session)
            compress: Whether to compress with gzip
            sync_state: Whether to capture state from registry before saving
            
        Returns:
            Path to saved session file
        """
        session = session or self._current
        if not session:
            raise ValueError("No session to save")
        
        # Capture state from registry if available
        if sync_state and self.state_registry and self.state_registry.is_enabled():
            try:
                from vaf.core.session_state import StateSnapshot
                snapshot = self.state_registry.capture_snapshot()
                # Preserve non-provider runtime keys (e.g. sidebar_documents, editor_selections)
                # while refreshing provider snapshot fields (timestamp/schema_version/providers).
                existing_runtime = dict(session.runtime_state or {})
                snapshot_runtime = snapshot.to_dict()
                merged_runtime = dict(existing_runtime)
                merged_runtime.update(snapshot_runtime)
                session.runtime_state = merged_runtime
            except Exception as e:
                import logging
                logging.error(f"Failed to capture state before save: {e}")
        
        # Update timestamp
        session.updated_at = datetime.now().isoformat()
        
        # Determine file path
        filename = f"{session.id}.json"
        if compress:
            filename += ".gz"
        
        filepath = self.storage_dir / filename
        
        # Serialize
        data = json.dumps(session.to_dict(), indent=2, ensure_ascii=False)
        # Strip lone Unicode surrogates (e.g. from PDF emoji extracted by PyPDF2).
        # json.dumps(ensure_ascii=False) produces them as literal surrogate codepoints
        # which UTF-8 cannot encode, causing UnicodeEncodeError on file write.
        data = data.encode("utf-8", errors="replace").decode("utf-8")

        # Atomic write: write to a hidden temp file then rename so a crash/interrupt
        # never leaves a 0-byte or half-written session file on disk.
        tmp_filepath = self.storage_dir / f".{session.id}.tmp"
        try:
            if compress:
                with gzip.open(tmp_filepath, 'wt', encoding='utf-8') as f:
                    f.write(data)
            else:
                with open(tmp_filepath, 'w', encoding='utf-8') as f:
                    f.write(data)
            tmp_filepath.replace(filepath)  # atomic rename on POSIX
        except Exception:
            try:
                tmp_filepath.unlink()
            except Exception:
                pass
            raise

        return filepath
    
    def load(self, session_id: str, restore_state: bool = True) -> Session:
        """
        Load a session by ID.
        
        Args:
            session_id: ID of session to load
            restore_state: Whether to restore state to registry after loading
            
        Returns:
            Loaded Session instance
        """
        # Try both compressed and uncompressed
        for ext in [".json", ".json.gz"]:
            filepath = self.storage_dir / f"{session_id}{ext}"
            if filepath.exists():
                if ext.endswith('.gz'):
                    with gzip.open(filepath, 'rt', encoding='utf-8') as f:
                        data = json.load(f)
                else:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                
                session = Session.from_dict(data)
                self._current = session
                
                # Restore state to registry if available
                if restore_state and self.state_registry and self.state_registry.is_enabled():
                    try:
                        from vaf.core.session_state import StateSnapshot
                        if session.runtime_state:
                            snapshot = StateSnapshot.from_dict(session.runtime_state)
                            self.state_registry.restore_snapshot(snapshot)
                    except Exception as e:
                        import logging
                        logging.error(f"Failed to restore state after load: {e}")
                
                return session
        
        raise FileNotFoundError(f"Session not found: {session_id}")
    
    def list(self, limit: int = 50, user_scope_id: str = None) -> List[Dict]:
        """
        List all sessions, optionally filtered by user_scope_id.
        
        Args:
            limit: Maximum sessions to return
            user_scope_id: Optional ID to filter by. If provided, returns:
                          1. Sessions matching this user_scope_id
                          2. Sessions without any user_scope_id (legacy/local admin)
        """
        sessions = []
        
        # Normalize user_scope_id for comparison
        target_scope = str(user_scope_id).strip() if user_scope_id else None
        
        for filepath in sorted(self.storage_dir.glob("*.json*"), 
                               key=lambda p: p.stat().st_mtime, 
                               reverse=True):
            if len(sessions) >= limit:
                break
                
            try:
                if filepath.suffix == '.gz':
                    with gzip.open(filepath, 'rt', encoding='utf-8') as f:
                        data = json.load(f)
                else:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                
                meta = data.get("metadata") or {}
                if meta.get("hidden_from_list"):
                    continue  # Hide from list (e.g. thinking session "removed" by user); GC can delete later
                
                # Filter by user_scope_id if provided
                if target_scope:
                    session_scope = meta.get("user_scope_id")
                    # Show if: matches OR session has no scope (legacy)
                    if session_scope and str(session_scope).strip() != target_scope:
                        continue
                
                sessions.append({
                    "id": data.get("id"),
                    "name": data.get("name"),
                    "created_at": data.get("created_at"),
                    "updated_at": data.get("updated_at"),
                    "model": data.get("model"),
                    "message_count": len(data.get("messages", [])),
                    "summary": Session.from_dict(data).summary(),
                    "metadata": meta,
                })
            except Exception:
                continue
        
        return sessions

    def hide(self, session_id: str) -> bool:
        """Mark session as hidden from list (e.g. thinking session). Does not delete; GC can delete later."""
        try:
            session = self.load(session_id)
            if not session.metadata:
                session.metadata = {}
            session.metadata["hidden_from_list"] = True
            self.save(session, sync_state=False)
            return True
        except FileNotFoundError:
            return False

    def save_thinking_run(
        self,
        user_scope_id: Optional[str],
        run_id: str,
        started_at: str,
        ended_at: str,
        messages_list: List[Dict[str, Any]],
    ) -> str:
        """
        Save a thinking-mode run as a session so it appears in the Web UI chat list.
        user_scope_id: scope key (string); started_at/ended_at: ISO datetime strings.
        messages_list: list of {"role", "content", "tool_calls": [names]} (e.g. from run log).
        Returns the session id (e.g. thinking_<scope>_<run_id>).
        """
        scope_key = str(user_scope_id).strip() if user_scope_id else "default"
        safe_key = "".join(c if c.isalnum() or c in "-_" else "_" for c in scope_key)[:32]
        sid = f"thinking_{safe_key}_{run_id}"
        name = f"Thinking mode {started_at[:16].replace('T', ' ')}"
        messages = []
        for m in messages_list or []:
            role = m.get("role") or "assistant"
            content = m.get("content") or ""
            tools = m.get("tool_calls")
            if role == "assistant" and tools:
                # Assistant message without "[Tools: ...]" suffix
                messages.append(Message(role="assistant", content=(content or "").strip() or "(no content)", timestamp=started_at))
                # One tool message per tool so UI shows real tool names (not "Unknown Tool")
                for i, tool_name in enumerate(tools):
                    messages.append(Message(
                        role="tool",
                        content="(completed)",
                        timestamp=started_at,
                        metadata={"toolName": str(tool_name), "toolId": f"thinking-{run_id}-{i}", "toolStatus": "completed"},
                    ))
            else:
                content_plain = (content or "").strip()
                if tools:
                    content_plain += "\n\n[Tools: " + ", ".join(str(t) for t in tools) + "]"
                messages.append(Message(role=role, content=content_plain or "(no content)", timestamp=started_at))
        session = Session(
            id=sid,
            name=name,
            created_at=started_at,
            updated_at=ended_at,
            model="",
            project_path="",
            messages=messages,
            metadata={"source": "thinking", "user_scope_id": user_scope_id},
        )
        self.save(session, sync_state=False)
        return sid

    def append_to_thinking_session(
        self,
        user_scope_id: Optional[str],
        run_id: str,
        started_at: str,
        ended_at: str,
        messages_list: List[Dict[str, Any]],
    ) -> str:
        """
        Append a thinking-mode run to the daily session (one session per user per day).
        If no session exists for today, creates one. Otherwise appends a separator + new messages.
        Returns the session id.
        """
        scope_key = str(user_scope_id).strip() if user_scope_id else "default"
        safe_key = "".join(c if c.isalnum() or c in "-_" else "_" for c in scope_key)[:32]
        today = datetime.now().strftime("%Y%m%d")
        sid = f"thinking_{safe_key}_{today}"

        # Build messages for this run
        new_messages = []
        for m in messages_list or []:
            role = m.get("role") or "assistant"
            content = m.get("content") or ""
            tools = m.get("tool_calls")
            if role == "assistant" and tools:
                new_messages.append(Message(role="assistant", content=(content or "").strip() or "(no content)", timestamp=started_at))
                for i, tool_name in enumerate(tools):
                    new_messages.append(Message(
                        role="tool",
                        content="(completed)",
                        timestamp=started_at,
                        metadata={"toolName": str(tool_name), "toolId": f"thinking-{run_id}-{i}", "toolStatus": "completed"},
                    ))
            else:
                content_plain = (content or "").strip()
                if tools:
                    content_plain += "\n\n[Tools: " + ", ".join(str(t) for t in tools) + "]"
                new_messages.append(Message(role=role, content=content_plain or "(no content)", timestamp=started_at))

        # Try to load existing daily session
        existing = None
        try:
            existing = self.load(sid)
        except (FileNotFoundError, Exception):
            existing = None

        if existing and existing.messages:
            # Append separator + new run messages
            separator = Message(
                role="system",
                content=f"--- Thinking run {run_id} ({started_at[:16].replace('T', ' ')}) ---",
                timestamp=started_at,
            )
            existing.messages.append(separator)
            existing.messages.extend(new_messages)
            existing.updated_at = ended_at
            existing.name = f"Thinking mode {today[:4]}-{today[4:6]}-{today[6:8]}"
            self.save(existing, sync_state=False)
        else:
            # Create new daily session
            name = f"Thinking mode {today[:4]}-{today[4:6]}-{today[6:8]}"
            session = Session(
                id=sid,
                name=name,
                created_at=started_at,
                updated_at=ended_at,
                model="",
                project_path="",
                messages=new_messages,
                metadata={"source": "thinking", "user_scope_id": user_scope_id},
            )
            self.save(session, sync_state=False)

        return sid

    def append_thinking_run_to_session(
        self,
        session_id: str,
        run_id: str,
        started_at: str,
        ended_at: str,
        messages_list: List[Dict[str, Any]],
    ) -> None:
        """
        Append a thinking-mode run to an existing session (e.g. web-default).
        Use this so thinking output appears in the same chat as the user's web session.
        Creates the session if it does not exist.
        """
        # Build messages for this run (same format as append_to_thinking_session)
        new_messages = []
        for m in messages_list or []:
            role = m.get("role") or "assistant"
            content = m.get("content") or ""
            tools = m.get("tool_calls")
            if role == "assistant" and tools:
                new_messages.append(Message(role="assistant", content=(content or "").strip() or "(no content)", timestamp=started_at))
                for i, tool_name in enumerate(tools):
                    new_messages.append(Message(
                        role="tool",
                        content="(completed)",
                        timestamp=started_at,
                        metadata={"toolName": str(tool_name), "toolId": f"thinking-{run_id}-{i}", "toolStatus": "completed"},
                    ))
            else:
                content_plain = (content or "").strip()
                if tools:
                    content_plain += "\n\n[Tools: " + ", ".join(str(t) for t in tools) + "]"
                new_messages.append(Message(role=role, content=content_plain or "(no content)", timestamp=started_at))

        existing = None
        try:
            existing = self.load(session_id)
        except (FileNotFoundError, Exception):
            existing = None

        if existing and existing.messages is not None:
            separator = Message(
                role="system",
                content=f"--- Thinking run {run_id} ({started_at[:16].replace('T', ' ')}) ---",
                timestamp=started_at,
            )
            existing.messages.append(separator)
            existing.messages.extend(new_messages)
            existing.updated_at = ended_at
            self.save(existing, sync_state=False)
        else:
            name = f"Chat {session_id}" if not existing else existing.name
            session = Session(
                id=session_id,
                name=name,
                created_at=started_at,
                updated_at=ended_at,
                model=existing.model if existing else "",
                project_path=existing.project_path if existing else "",
                messages=new_messages,
                metadata=dict(existing.metadata) if existing and existing.metadata else {},
            )
            self.save(session, sync_state=False)

    def delete(self, session_id: str) -> bool:
        """Delete a session.

        Also removes the session's workspace folder (VAF_Projects/<uid8>/<sid>/)
        if it exists but is EMPTY (no visible files/folders - e.g. the WebUI
        eagerly created it when the chat was opened and nothing was ever
        saved into it, or a workflow's own scratch-file cleanup left an empty
        shell behind). A workspace holding real content is never touched -
        only the session record is removed and the files stay on disk; the
        chat's title is saved into the workspace label first so the surviving
        folder keeps a human name in the Data Explorer instead of falling
        back to the raw session-id folder name.
        """
        _cleanup_empty_session_workspace(session_id)
        _preserve_workspace_title(self, session_id)

        deleted = False

        for ext in [".json", ".json.gz"]:
            filepath = self.storage_dir / f"{session_id}{ext}"
            if filepath.exists():
                filepath.unlink()
                deleted = True

        if self._current and self._current.id == session_id:
            self._current = None

        return deleted
    
    def rename(self, session_id: str, new_name: str) -> bool:
        """Rename a session."""
        try:
            session = self.load(session_id)
            session.name = new_name
            self.save(session)
            return True
        except FileNotFoundError:
            return False

    def cleanup_empty(self, exclude_session_id: str = None) -> int:
        """
        Delete sessions that are empty or contain only system/internal messages.
        Prevents accumulation of 'New Chat' sessions with no user interaction.
        
        Args:
            exclude_session_id: Optional session ID to exclude from cleanup (e.g., current active session)
        
        Returns: Number of deleted sessions.
        """
        count = 0
        deleted_ids = []
        
        # Iterate over all sessions
        # We use list() to iterate over a static list while modifying file system
        try:
            # Re-implement list logic inline to avoid full object loading overhead if possible,
            # but we need to inspect content, so use load() safely
            
            all_files = list(self.storage_dir.glob("*.json")) + list(self.storage_dir.glob("*.json.gz"))
            
            # De-duplicate IDs (handle .json and .gz for same ID)
            unique_ids = set()
            for p in all_files:
                unique_ids.add(p.name.split('.')[0])
                
            for sid in unique_ids:
                # Skip excluded session (e.g., current active session)
                if exclude_session_id and sid == exclude_session_id:
                    continue
                
                try:
                    # Load session
                    session = self.load(sid)
                    
                    # Check criteria:
                    # 1. No messages at all
                    # 2. Only system messages (role='system')
                    # 3. Only internal tool messages? (usually linked to user prompt, so role='user' check is enough)
                    
                    has_user_interaction = False
                    for msg in session.messages:
                        if msg.role == "user":
                            has_user_interaction = True
                            break
                    
                    if not has_user_interaction:
                        # Delete it (it's a Lehre-Chat - empty teaching session)
                        self.delete(sid)
                        count += 1
                        deleted_ids.append(sid)
                        
                except Exception:
                    continue
                    
        except Exception as e:
            print(f"Cleanup error: {e}")
            
        return count
    
    def export(self, session: Session = None, format: str = "markdown") -> str:
        """Export a session to a formatted string."""
        session = session or self._current
        if not session:
            raise ValueError("No session to export")
        
        if format == "markdown":
            return self._export_markdown(session)
        elif format == "json":
            return json.dumps(session.to_dict(), indent=2, ensure_ascii=False)
        else:
            raise ValueError(f"Unknown format: {format}")
    
    def _export_markdown(self, session: Session) -> str:
        """Export session as Markdown."""
        lines = []
        lines.append(f"# {session.name}")
        lines.append("")
        lines.append(f"**Session ID:** {session.id}")
        lines.append(f"**Created:** {session.created_at}")
        lines.append(f"**Model:** {session.model or 'Unknown'}")
        lines.append("")
        lines.append("---")
        lines.append("")
        
        for msg in session.messages:
            role = msg.role.upper()
            timestamp = msg.timestamp[:19] if msg.timestamp else ""
            
            lines.append(f"### {role}")
            if timestamp:
                lines.append(f"*{timestamp}*")
            lines.append("")
            lines.append(msg.content)
            lines.append("")
            
            if msg.tool_calls:
                lines.append("**Tool Calls:**")
                lines.append("```json")
                lines.append(json.dumps(msg.tool_calls, indent=2))
                lines.append("```")
                lines.append("")
        
        return "\n".join(lines)
    
    def search(self, query: str, limit: int = 10) -> List[Dict]:
        """Search sessions by content."""
        query_lower = query.lower()
        results = []
        
        for filepath in self.storage_dir.glob("*.json*"):
            try:
                if filepath.suffix == '.gz':
                    with gzip.open(filepath, 'rt', encoding='utf-8') as f:
                        data = json.load(f)
                else:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                
                # Search in messages
                for msg in data.get("messages", []):
                    if query_lower in msg.get("content", "").lower():
                        results.append({
                            "session_id": data.get("id"),
                            "session_name": data.get("name"),
                            "match": msg.get("content")[:100],
                            "role": msg.get("role"),
                        })
                        break
                
                if len(results) >= limit:
                    break
                    
            except Exception:
                continue
        
        return results


# ═══════════════════════════════════════════════════════════════════════════════
# CLI COMMANDS (for session subcommand)
# ═══════════════════════════════════════════════════════════════════════════════

import typer

session_app = typer.Typer(help="Manage conversation sessions")

_manager: Optional[SessionManager] = None

def get_manager() -> SessionManager:
    global _manager
    if _manager is None:
        _manager = SessionManager()
    return _manager


# ═══════════════════════════════════════════════════════════════════════════════
# PER-CHAT WORKSPACE RESOLUTION
# ═══════════════════════════════════════════════════════════════════════════════

def record_created_file(session_id: Optional[str], file_path) -> None:
    """Anchor a session's workspace from a created file (single shared setter).

    Sets ``runtime_state["last_project_path"]`` and - once, never overwritten -
    ``session.project_path`` (VAF_Projects paths only), which arms the
    [SESSION WORKSPACE] context note. Historically this logic lived ONLY in the
    /api/workflow/update HTTP endpoint, which is the SUBPROCESS notification
    fallback: files written in-process (main-agent write_file, workflow engine)
    updated the UI but never anchored the session, so the workspace note never
    fired for those chats (live incident). Both notify paths
    call this now. Fail-safe: never raises.
    """
    try:
        if not session_id or not file_path:
            return
        from vaf.tools.coder import is_unsafe_project_dir
        project_dir = str(Path(file_path).parent.resolve())
        # Never record unsafe dirs (e.g. /home/<user>) as the session's
        # project - that would poison every later edit-task in this chat.
        if is_unsafe_project_dir(project_dir):
            return
        mgr = get_manager()
        loaded = mgr.load(session_id)
        if not getattr(loaded, "runtime_state", None):
            loaded.runtime_state = {}
        loaded.runtime_state["last_project_path"] = project_dir
        # Anchor session workspace on first "real" project creation
        # (VAF_Projects paths only). session.project_path is stable - set once,
        # never overwritten - giving the chat a persistent workspace root
        # independent of which sub-project was last touched.
        if not getattr(loaded, "project_path", ""):
            try:
                from vaf.core.platform import Platform
                _vaf_root = str(Platform.documents_dir())
                if "VAF_Projects" in project_dir and project_dir.startswith(_vaf_root):
                    loaded.project_path = project_dir
            except Exception:
                pass
        mgr.save(loaded, sync_state=False)
    except Exception:
        pass


def get_session_workspace_dir(session_id: Optional[str] = None, create: bool = False) -> Optional[Path]:
    """Per-chat workspace folder: VAF_Projects/<uid[:8]>/<session_id>/.

    Single source for every sub-agent that creates files (coder projects,
    documents, research reports) and for the WebUI workspace browser — the
    same convention the coder uses in _generate_project_directory.

    session_id falls back to VAF_SESSION_ID / the IPC context. Returns None
    without session context. With create=False only an EXISTING folder is
    returned (browser use); with create=True the preferred candidate is
    created (agent output use).
    """
    import os as _os
    import re as _re

    sid = (session_id or _os.environ.get("VAF_SESSION_ID", "")).strip()
    if not sid:
        try:
            from vaf.core.subagent_ipc import get_current_session_id
            sid = get_current_session_id() or ""
        except Exception:
            sid = ""
    if not sid:
        return None
    folder = _re.sub(r'[^a-zA-Z0-9_-]', '', sid)[:32]
    if not folder:
        return None

    uid = ""
    try:
        sess = get_manager().load(sid)
        uid = str((getattr(sess, "metadata", None) or {}).get("user_scope_id") or "")
    except Exception:
        uid = ""

    from vaf.core.platform import Platform
    root = Platform.documents_dir() / "VAF_Projects"
    candidates = []
    if uid:
        candidates.append(root / uid[:8] / folder)
    candidates.append(root / folder)

    if create:
        target = candidates[0]
        try:
            target.mkdir(parents=True, exist_ok=True)
            _apply_channel_workspace_label(target, sid)
            return target
        except Exception:
            return None
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _workspace_has_real_content(path: Path) -> bool:
    """True if `path` holds anything but dotfiles - checked RECURSIVELY, so a
    tree of only empty subfolders (no dotfiles) still counts as empty. Any
    walk error is treated as "has content" (fail toward keeping the folder,
    never toward deleting something we could not fully inspect).

    onerror=raise is what makes that fail-safe REAL: os.walk's default
    (onerror=None) silently SKIPS unreadable subdirectories instead of
    raising, so a permission-denied subtree full of files classified the
    whole workspace as "empty" and the except-clause below was dead code
    (audit finding, fbf9250..HEAD range)."""
    import os as _os

    def _walk_error(err):
        raise err

    try:
        for _root, dirs, files in _os.walk(path, onerror=_walk_error):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            if any(not f.startswith(".") for f in files):
                return True
        return False
    except Exception:
        return True


def _cleanup_empty_session_workspace(session_id: str) -> None:
    """Remove a session's workspace folder IF it exists and is EMPTY.

    "Empty" ignores dotfiles (e.g. the .vaf_workspace.json channel label) and
    is checked recursively, so a folder that was only ever auto-labeled or
    holds nothing but empty subfolders still counts as empty. Called from
    SessionManager.delete() before the session record is removed (the
    uid-scoped lookup inside get_session_workspace_dir needs the session to
    still be loadable). Best-effort: any failure is swallowed, deleting the
    session must never be blocked by a workspace-folder problem.

    Live sub-agent guard: while a sub-agent/workflow is still RUNNING (or
    pending) for this session it may be writing into the workspace at this
    very moment - "empty right now" says nothing, the first output file can
    land between the emptiness check and the rmtree. Skip workspace removal
    entirely then (the session record is still deleted; the folder stays and
    is at worst an orphan the central explorer can still reach). Same
    ipc.get_active_tasks(session_id=...) probe the WS stop handler uses.
    """
    try:
        try:
            from vaf.core.subagent_ipc import get_ipc as _get_ipc
            _ipc = _get_ipc()
            if _ipc.get_active_tasks(session_id=str(session_id)) or \
                    _ipc.get_pending_tasks(session_id=str(session_id)):
                return  # a live run may be writing here - never rmtree under it
        except Exception:
            pass  # IPC unavailable: fall through, emptiness check still applies
        path = get_session_workspace_dir(session_id, create=False)
        if not path or not path.is_dir():
            return
        if _workspace_has_real_content(path):
            return  # holds real content - never auto-delete
        import shutil as _shutil
        _shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def _preserve_workspace_title(manager, session_id: str) -> None:
    """When a deleted chat leaves its workspace behind (it holds real content),
    save the chat's title as the workspace display label so the orphaned folder
    keeps its human name in the Data Explorer instead of falling back to the raw
    session-id folder name (the title lives in the session record, which is about
    to be removed). A user-set label is never overwritten (rename wins). Called
    from SessionManager.delete() AFTER the empty-workspace cleanup (an empty
    folder is gone by then, nothing to label) and BEFORE the record is removed -
    both the uid-scoped workspace lookup and the title need the session to still
    be loadable. Best-effort: never blocks the deletion."""
    try:
        path = get_session_workspace_dir(session_id, create=False)
        if not path or not path.is_dir():
            return
        if read_workspace_label(path):
            return  # explicit rename wins over the chat title
        # Read the title straight from the record file instead of manager.load():
        # load() always repoints manager._current at whatever it loads, and
        # deleting a background chat must not touch the active-session pointer.
        title = ""
        for ext in (".json", ".json.gz"):
            fp = manager.storage_dir / f"{session_id}{ext}"
            if fp.exists():
                if ext.endswith(".gz"):
                    with gzip.open(fp, "rt", encoding="utf-8") as f:
                        data = json.load(f)
                else:
                    with open(fp, "r", encoding="utf-8") as f:
                        data = json.load(f)
                title = str((data or {}).get("name") or "").strip()
                break
        if title:
            write_workspace_label(path, title)
    except Exception:
        pass


def resolve_agent_output_dir(default: Path, session_id: Optional[str] = None) -> Path:
    """Output dir for file-creating sub-agents: the chat's workspace when a
    session exists (so documents/reports land next to the chat's projects and
    show up in the WebUI workspace browser), otherwise the agent's legacy
    default directory."""
    workspace = get_session_workspace_dir(session_id, create=True)
    if workspace:
        return workspace
    default.mkdir(parents=True, exist_ok=True)
    return default


WORKSPACE_LABEL_FILE = ".vaf_workspace.json"


def get_user_projects_root(user_scope_id: Optional[str]) -> Optional[Path]:
    """Per-user root holding ALL of a user's chat workspaces: VAF_Projects/<uid8>/.

    uid8 = first 8 hex chars of the user_scope_id UUID (dashes stripped, lowercased) — the SAME
    derivation get_session_workspace_dir uses (~line 860) and the /api/file isolation check
    (web_server.py ~1382). Returns None without a user_scope_id (no per-user root)."""
    uid = str(user_scope_id or "").replace("-", "").lower()
    if not uid:
        return None
    from vaf.core.platform import Platform
    return Platform.documents_dir() / "VAF_Projects" / uid[:8]


def get_session_attachments_dir(
    session_id: Optional[str],
    user_scope_id: Optional[str] = None,
    create: bool = True,
) -> Optional[Path]:
    """Per-chat folder for uploaded image attachments: VAF_Projects/<uid8>/<session_id>/attachments/.

    Images are stored here as FILES (not base64 inline in session.json) so the chat stays lean,
    the agent can reference them by path (list_files / read_file / analyze_image), and the WebUI
    can serve them via /api/file. Takes user_scope_id EXPLICITLY (unlike get_session_workspace_dir,
    which reads it from saved session metadata) so it is correct on the very FIRST turn — before
    the session has been persisted with its scope. Falls back to the un-scoped
    VAF_Projects/<session_id>/ for the local-admin / no-scope case, matching get_session_workspace_dir.
    Returns None without a usable session_id; with create=True the folder is created."""
    import re as _re
    folder = _re.sub(r'[^a-zA-Z0-9_-]', '', (session_id or "").strip())[:32]
    if not folder:
        return None
    base = get_user_projects_root(user_scope_id)
    if base is None:
        from vaf.core.platform import Platform
        base = Platform.documents_dir() / "VAF_Projects"
    target = base / folder / "attachments"
    if create:
        try:
            target.mkdir(parents=True, exist_ok=True)
            _apply_channel_workspace_label(target.parent, session_id)
        except Exception:
            return None
    return target


def read_workspace_label(folder: Path) -> Optional[str]:
    """User-set display label from <folder>/.vaf_workspace.json, or None. Fully exception-guarded."""
    try:
        p = Path(folder) / WORKSPACE_LABEL_FILE
        if not p.is_file():
            return None
        label = str((json.loads(p.read_text(encoding="utf-8")) or {}).get("label") or "").strip()
        return label or None
    except Exception:
        return None


def write_workspace_label(folder: Path, label: str) -> bool:
    """Set the workspace display label (a small dotfile INSIDE the workspace, so it survives session
    deletion -> orphans stay renamable). Never renames the folder. Atomic write. Returns success."""
    import os as _os
    try:
        folder = Path(folder)
        if not folder.is_dir():
            return False
        label = str(label or "").strip()[:200]
        p = folder / WORKSPACE_LABEL_FILE
        tmp = folder / (WORKSPACE_LABEL_FILE + ".tmp")
        tmp.write_text(json.dumps({"label": label}, ensure_ascii=False), encoding="utf-8")
        _os.replace(str(tmp), str(p))
        return True
    except Exception:
        return False


def _apply_channel_workspace_label(workspace_root: Path, session_id: Optional[str]) -> None:
    """Give a freshly-created messaging-channel workspace a friendly display label
    ('Telegram'/'WhatsApp'/'Discord') instead of the raw 'telegram_<id>' folder name.
    The on-disk folder is never renamed (rename = label only, by design). Idempotent: a
    user-set (or already-applied) label is never overwritten. Fully exception-guarded."""
    try:
        sid = str(session_id or "")
        label = None
        for prefix, name in (("telegram_", "Telegram"), ("whatsapp_", "WhatsApp"), ("discord_", "Discord")):
            if sid.startswith(prefix):
                label = name
                break
        if not label:
            return
        if not (Path(workspace_root) / WORKSPACE_LABEL_FILE).exists():
            write_workspace_label(workspace_root, label)
    except Exception:
        pass


def resolve_workspace_display_name(folder: Path, session_id: str, live_title: Optional[str]) -> str:
    """Display-name precedence (locked decision: rename = display label only):
    explicit label file -> the linked live session's title -> the folder name (== session_id).
    Works for orphans (no live session) because it never requires one."""
    label = read_workspace_label(folder)
    if label:
        return label
    if live_title and str(live_title).strip():
        return str(live_title).strip()
    return str(session_id)


@session_app.command("list")
def list_sessions(
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum sessions to show")
):
    """List all saved sessions."""
    from rich.console import Console
    from rich.table import Table
    
    console = Console()
    manager = get_manager()
    sessions = manager.list(limit=limit)
    
    if not sessions:
        console.print("[yellow]No saved sessions found.[/yellow]")
        return
    
    table = Table(title="Saved Sessions", show_header=True)
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Messages", justify="right")
    table.add_column("Updated")
    table.add_column("Summary")
    
    for s in sessions:
        updated = s["updated_at"][:10] if s["updated_at"] else "?"
        table.add_row(
            s["id"],
            s["name"][:30],
            str(s["message_count"]),
            updated,
            s["summary"][:40],
        )
    
    console.print(table)


@session_app.command("load")
def load_session(
    session_id: str = typer.Argument(..., help="Session ID to load")
):
    """Load a saved session."""
    from rich.console import Console
    
    console = Console()
    manager = get_manager()
    
    try:
        session = manager.load(session_id)
        console.print(f"[green]✓ Loaded session: {session.name} ({len(session.messages)} messages)[/green]")
    except FileNotFoundError:
        console.print(f"[red]✗ Session not found: {session_id}[/red]")
        raise typer.Exit(1)


@session_app.command("delete")
def delete_session(
    session_id: str = typer.Argument(..., help="Session ID to delete"),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation")
):
    """Delete a saved session."""
    from rich.console import Console
    
    console = Console()
    manager = get_manager()
    
    if not force:
        confirm = typer.confirm(f"Delete session {session_id}?")
        if not confirm:
            console.print("[yellow]Cancelled.[/yellow]")
            return
    
    if manager.delete(session_id):
        console.print(f"[green]✓ Deleted session: {session_id}[/green]")
    else:
        console.print(f"[red]✗ Session not found: {session_id}[/red]")


@session_app.command("export")
def export_session(
    session_id: str = typer.Argument(..., help="Session ID to export"),
    format: str = typer.Option("markdown", "--format", "-f", help="Export format (markdown, json)"),
    output: str = typer.Option(None, "--output", "-o", help="Output file path")
):
    """Export a session to file."""
    from rich.console import Console
    
    console = Console()
    manager = get_manager()
    
    try:
        session = manager.load(session_id)
        content = manager.export(session, format=format)
        
        if output:
            with open(output, 'w', encoding='utf-8') as f:
                f.write(content)
            console.print(f"[green]✓ Exported to: {output}[/green]")
        else:
            console.print(content)
            
    except FileNotFoundError:
        console.print(f"[red]✗ Session not found: {session_id}[/red]")
        raise typer.Exit(1)


@session_app.command("search")
def search_sessions(
    query: str = typer.Argument(..., help="Search query"),
    limit: int = typer.Option(10, "--limit", "-n", help="Maximum results")
):
    """Search sessions by content."""
    from rich.console import Console
    from rich.table import Table
    
    console = Console()
    manager = get_manager()
    
    results = manager.search(query, limit=limit)
    
    if not results:
        console.print(f"[yellow]No sessions found matching: {query}[/yellow]")
        return
    
    table = Table(title=f"Search Results: '{query}'")
    table.add_column("Session ID", style="cyan")
    table.add_column("Name")
    table.add_column("Role")
    table.add_column("Match")
    
    for r in results:
        table.add_row(
            r["session_id"],
            r["session_name"][:20],
            r["role"],
            r["match"][:50] + "..." if len(r["match"]) > 50 else r["match"],
        )
    
    console.print(table)

