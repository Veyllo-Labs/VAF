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
from typing import Dict, Any, Iterator, List, Optional, Tuple
from dataclasses import dataclass, field, asdict, fields

# ═══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

def default_sessions_dir() -> Path:
    """THE session directory. One function, because there were three copies.

    Also the seam the test suite redirects: the store hangs off `~/.vaf`, which
    no HOME- or XDG-redirection covered, so suite runs wrote synthetic chats
    straight into the developer's real installation for as long as the default
    was spelled out at each site.
    """
    from vaf.core.platform import Platform
    return Path(Platform.vaf_dir()) / "sessions"


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
    sessions_dir = default_sessions_dir()

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
            self.storage_dir = default_sessions_dir()
        
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

        if compress:
            # Legacy shape, kept only because a pre-existing .gz keeps its extension
            # on rewrite (claim_unscoped). Nothing produces new ones.
            tmp_filepath = self.storage_dir / f".{session.id}.tmp"
            try:
                with gzip.open(tmp_filepath, 'wt', encoding='utf-8') as f:
                    f.write(data)
                tmp_filepath.replace(filepath)  # atomic rename on POSIX
            except Exception:
                try:
                    tmp_filepath.unlink()
                except Exception:
                    pass
                raise
            return filepath

        # A chat is the most personal thing VAF stores. Encrypted at rest (unless
        # file_encryption_enabled is off), written atomically, owner-only.
        from vaf.core import data_files
        data_files.write_bytes_atomic(filepath, data.encode("utf-8"))
        return filepath

    def _read_session_file(self, filepath: Path) -> Dict[str, Any]:
        """Parse one session file, encrypted or not, gz or not.

        THE read seam. Every caller that used to open these files itself goes
        through here, so "is it encrypted" is answered in exactly one place and
        a plaintext file from before the change still loads.
        """
        if filepath.suffix == '.gz':
            with gzip.open(filepath, 'rt', encoding='utf-8') as f:
                return json.load(f)
        from vaf.core import data_files
        return json.loads(data_files.read_bytes(filepath).decode("utf-8"))
    
    def load(self, session_id: str, restore_state: bool = True,
             repoint: bool = True) -> Session:
        """
        Load a session by ID.

        Args:
            session_id: ID of session to load
            restore_state: Whether to restore state to registry after loading
            repoint: Whether the loaded session becomes `_current`. Pass False
                     to READ another session (rename, an exit-time name check)
                     without changing what "current" means.

        Returns:
            Loaded Session instance
        """
        # Try both compressed and uncompressed
        for ext in [".json", ".json.gz"]:
            filepath = self.storage_dir / f"{session_id}{ext}"
            if filepath.exists():
                data = self._read_session_file(filepath)

                session = Session.from_dict(data)
                if repoint:
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
                data = self._read_session_file(filepath)

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

    def archive_dir(self, user_scope_id: str = None) -> Path:
        """Where one account's archived chats live: ``<sessions>/archive/<scope>``.

        Keyed by scope AND holding files that carry their own ``user_scope_id``,
        so isolation does not depend on the directory alone - a file moved by
        hand into the wrong folder still names its owner, the way every other
        per-user store here works.
        """
        from vaf.core.cost import _scope_key  # canonical scope -> folder name

        return self.storage_dir / "archive" / _scope_key(user_scope_id)

    def archive(self, session_id: str, user_scope_id: str = None) -> bool:
        """Keep a chat out of the sidebar but available to the agent's memory.

        Deliberately a MOVE of the session file, not a new export format: the
        file is already the whole conversation, already encrypted at rest, and
        already carries the owner in its metadata. Anything that can read a
        session can read an archived one - which is the point, since the memory
        lane is meant to retrieve from these later. A second format would have
        to be taught to every reader and would drift from the first one.

        The caller checks ownership (the web layer does this before delete
        too); the scope here decides which account's archive it lands in.
        """
        target_dir = self.archive_dir(user_scope_id)
        moved = False
        for ext in [".json", ".json.gz"]:
            src = self.storage_dir / f"{session_id}{ext}"
            if not src.exists():
                continue
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                try:
                    target_dir.chmod(0o700)
                except Exception:
                    pass  # best effort; Windows has no meaningful mode here
                src.replace(target_dir / f"{session_id}{ext}")
                moved = True
            except Exception:
                import logging
                logging.getLogger(__name__).warning(
                    "Could not archive session %s", session_id, exc_info=True)
                return False
        return moved

    def list_archived(self, user_scope_id: str = None, limit: int = 200) -> List[Dict]:
        """This account's archived chats, newest first. Never another's.

        Reads only inside that account's archive directory and re-checks the
        owner recorded IN each file, so a stray file cannot leak by sitting in
        the wrong folder.
        """
        from vaf.core.cost import _scope_key

        rows: List[Dict] = []
        base = self.archive_dir(user_scope_id)
        if not base.is_dir():
            return rows
        want = _scope_key(user_scope_id)
        for path in base.glob("*.json*"):
            try:
                data = self._read_session_file(path)
            except Exception:
                continue
            meta = data.get("metadata") or {}
            if _scope_key(meta.get("user_scope_id")) != want:
                continue
            msgs = data.get("messages") or []
            rows.append({
                "id": data.get("id") or path.stem,
                "name": data.get("name") or path.stem,
                "updated_at": data.get("updated_at") or "",
                "message_count": len(msgs),
            })
        rows.sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
        return rows[:limit]

    def search_archived(self, query: str, user_scope_id: str = None,
                        limit: int = 100) -> List[Dict]:
        """Find a phrase across ALL of this account's archived chats.

        Server-side because the browser only ever holds the chat it has open: a
        search that needed the reader to open the right chat first is not a
        search, it is a confirmation.

        MATCHING IS THE CROSS CHAT HINT LANE'S, not a second one - the same
        `query_terms` / `_match_text` / `_excerpt` the agent uses when it looks
        into other chats. So this finds `Reisekosten` inside
        `Reisekostenabrechnung`, and `Pruefung` where the text says `Prüfung`,
        exactly as the agent does; two matchers would have meant a phrase the
        agent can find and the user cannot, in the same archive.

        What is deliberately NOT taken from that lane is its selection rules
        (`cross_chat_hint_min_terms`, `min_score`, the corpus filler filter).
        Those decide which chats are worth spending prompt space on; a search
        box has to show what it found, including a single common word.

        Each hit names its chat, the message INDEX inside it and the matching
        line, so the viewer can open that chat at that message.
        """
        from vaf.core.cost import _scope_key
        from vaf.core.cross_chat import _excerpt, _match_text, query_terms

        terms = query_terms(str(query or ""))
        hits: List[Dict] = []
        if not terms:
            return hits
        base = self.archive_dir(user_scope_id)
        if not base.is_dir():
            return hits
        want = _scope_key(user_scope_id)
        for path in sorted(base.glob("*.json*")):
            try:
                data = self._read_session_file(path)
            except Exception:
                continue
            meta = data.get("metadata") or {}
            if _scope_key(meta.get("user_scope_id")) != want:
                continue
            name = str(data.get("name") or path.stem)
            chat_id = str(data.get("id") or path.stem)
            if _match_text(terms, name):
                hits.append({"chat_id": chat_id, "name": name, "index": -1, "line": name})
            for i, msg in enumerate(data.get("messages") or []):
                if msg.get("role") not in ("user", "assistant"):
                    continue
                content = str(msg.get("content") or "")
                matched = _match_text(terms, content)
                if not matched:
                    continue
                hits.append({
                    "chat_id": chat_id,
                    "name": name,
                    "index": i,
                    "line": _excerpt(content, sorted(matched)) or content[:160],
                    # The words AS THEY APPEAR in the text, not the folded query
                    # terms. The viewer highlights these literally, and it has to:
                    # a reader who typed "Pruefung" gets a hit on "Prüfung", and
                    # highlighting what they typed would mark nothing at all -
                    # leaving them to guess what the search actually matched.
                    "words": _matched_words(terms, content),
                    # The exact character range the agent would be handed for
                    # this hit. The viewer marks THIS, so what is highlighted in
                    # the chat is the passage the model sees - not a guess at it.
                    "span": _passage_span(terms, content),
                })
                if len(hits) >= limit:
                    return hits
        return hits

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
        """Rename a session ON DISK, and nothing else.

        Deliberately free of load()'s side effects: `restore_state=False` /
        `sync_state=False` keep the renamed session's runtime_state out of
        the live registry (and the live snapshot out of a foreign session
        file), and the manager's `_current` pointer is preserved - renaming
        a session from a list must not change what "current" means. A
        manager without a registry was safe here by accident; one WITH a
        registry (the terminal app binds it) was not.

        The caller that renames its OWN live session updates its in-memory
        object itself - this method's contract is the file.
        """
        try:
            session = self.load(session_id, restore_state=False, repoint=False)
            session.name = new_name
            self.save(session, sync_state=False)
            return True
        except FileNotFoundError:
            return False

    def list_ui(self, limit: int = 50, user_scope_id: str = None) -> List[Dict]:
        """The session list a CHAT SURFACE shows - one rule for every surface.

        `list()` minus channel chats (their dashboards own them) and
        thinking sessions (internal runs). The web sidebar and the terminal
        app's panel both consume THIS, so what a session list shows cannot
        diverge between surfaces again. The channel prefixes come from the
        dispatch module's registry, not a local copy.
        """
        from vaf.core.tool_dispatch import CHANNEL_SESSION_PREFIXES
        out = []
        for s in self.list(limit=limit, user_scope_id=user_scope_id):
            sid = str(s.get("id") or "")
            if sid.startswith(CHANNEL_SESSION_PREFIXES):
                continue
            meta = s.get("metadata") or {}
            if meta.get("source") == "thinking" or sid.startswith("thinking_"):
                continue
            out.append(s)
        # Agent rooms come FIRST, and the rule lives here rather than in each surface,
        # because this function's whole reason for existing is that a session list must
        # not diverge between the sidebar and the terminal panel again.
        #
        # A room is NOT a session and must never be loaded as one: Session.save rewrites
        # the entire message list, which with N writers reproduces exactly the lost
        # update the room store is built to avoid. That is why the rows carry
        # kind="room" and no session id a loader would accept - a surface that treats
        # one as a session is a bug, and tests/test_room_rows_are_not_sessions.py says
        # so out loud.
        return _room_rows(user_scope_id) + out

    def claim_unscoped(self, user_scope_id: str) -> int:
        """Stamp every session WITHOUT an owner scope as `user_scope_id`'s.

        One-time repair for sessions from before scope stamping existed: the
        list's legacy rule shows no-scope sessions to EVERY user, so their
        names leaked into other users' sidebars, while the ownership gate
        already treated them as admin-only for actions. A pre-scoping session
        can only belong to the machine owner - multi-user arrived WITH
        scoping - so the local admin scope is the truth, not a guess.
        Idempotent, cheap when there is nothing to claim; the launchers call
        it at boot. Reads and writes without touching `_current` or the
        state registry (`repoint=False` / `sync_state=False`).
        """
        scope = str(user_scope_id or "").strip()
        if not scope:
            return 0
        claimed = 0
        seen = set()
        for p in sorted(self.storage_dir.glob("*.json*")):
            sid = p.name.split(".")[0]
            if not sid or sid in seen:
                continue
            seen.add(sid)
            try:
                session = self.load(sid, restore_state=False, repoint=False)
            except Exception:
                continue
            meta = session.metadata or {}
            if str(meta.get("user_scope_id") or "").strip():
                continue
            session.metadata = {**meta, "user_scope_id": scope}
            try:
                self.save(session, sync_state=False,
                          compress=p.suffix == ".gz")
                claimed += 1
            except Exception:
                continue
        return claimed

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
    
    def iter_owned_sessions(
        self,
        user_scope_id: Optional[str],
        *,
        exclude_session_id: Optional[str] = None,
        max_age_days: Optional[int] = None,
        max_files: int = 200,
        max_candidates: int = 50,
        max_bytes: int = 2_000_000,
        include_channel: bool = True,
        include_thinking: bool = False,
    ) -> "Iterator[Tuple[Path, Dict[str, Any]]]":
        """Yield `(path, data)` for the sessions this scope OWNS, newest first.

        THE STRICT OWNERSHIP RULE, and why it is not `list()`'s.
        `list()` filters leniently on purpose: a session with NO `user_scope_id`
        is shown to every scope, because a pre-scoping session can only belong to
        the machine owner and its NAME in a sidebar is harmless. Reading a
        session's CONTENT is a different question, so this iterator answers the
        strict one: the caller's scope and the session's scope must both be
        non-empty and equal. An unowned session belongs to nobody here.

        Scopes are compared as STRINGS, never parsed. `get_local_admin_scope_id()`
        returns whatever the config holds, the CLI binds it unparsed on purpose,
        and real stores carry non-UUID spellings; a parse gate would quietly
        return nothing at all for those installations.

        Being an admin does not widen this. The ownership gate in the web server
        answers "may this identity act on that session" (for an admin: on all of
        them), which is not the same question as "is that session this scope's".

        Two independent bounds, because they fail differently: `max_files` caps
        how many files are EXAMINED (so a big multi-tenant store cannot turn a
        per-turn call into a full scan) and `max_candidates` caps how many are
        YIELDED. Capping only the first would return nothing to a user whose
        newest files all belong to someone else.

        Oversized sessions are skipped rather than parsed (`max_bytes`), and every
        per-file failure is skipped rather than raised: a chat deleted between the
        listing and the read is the normal case this is used in, not an error.
        """
        # Local import: the channel registry imports back into the core (Rule 2 says
        # the prefixes have exactly one home, and this is not it).
        from vaf.core.tool_dispatch import CHANNEL_SESSION_PREFIXES

        target_scope = str(user_scope_id).strip() if user_scope_id else ""
        if not target_scope:
            return

        # stat() inside the try as well: a file can vanish between glob and sort,
        # and that is precisely the "chat deleted mid-scan" case this serves.
        candidates: List[Tuple[float, Path]] = []
        # Archived chats are searched too. That is the whole point of keeping
        # them: the user chose "delete but keep so the agent can still remember
        # this", and a memory that ignored them would have made that promise
        # false. Isolation is unaffected - the strict owner check below reads
        # the scope out of every file, exactly as it does for a live session.
        _sources = list(self.storage_dir.glob("*.json*"))
        try:
            _archive = self.archive_dir(user_scope_id)
            if _archive.is_dir():
                _sources.extend(_archive.glob("*.json*"))
        except Exception:
            pass
        for filepath in _sources:
            try:
                candidates.append((filepath.stat().st_mtime, filepath))
            except OSError:
                continue
        candidates.sort(key=lambda item: item[0], reverse=True)

        cutoff = None
        if max_age_days:
            cutoff = datetime.now().timestamp() - (int(max_age_days) * 86400)

        examined = 0
        yielded = 0
        for mtime, filepath in candidates:
            if examined >= max_files or yielded >= max_candidates:
                return
            if cutoff is not None and mtime < cutoff:
                return  # sorted newest-first, so everything after this is older too
            sid = filepath.name.split(".json")[0]
            if exclude_session_id and sid == str(exclude_session_id):
                continue
            if not include_channel and sid.startswith(CHANNEL_SESSION_PREFIXES):
                continue
            if not include_thinking and sid.startswith("thinking_"):
                continue
            examined += 1
            try:
                if filepath.stat().st_size > max_bytes:
                    continue
                data = self._read_session_file(filepath)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            meta = data.get("metadata") or {}
            if meta.get("hidden_from_list"):
                continue
            if not include_thinking and meta.get("source") == "thinking":
                continue
            session_scope = str(meta.get("user_scope_id") or "").strip()
            if not session_scope or session_scope != target_scope:
                continue
            yielded += 1
            yield filepath, data

    def list_owned(
        self,
        limit: int = 50,
        user_scope_id: str = None,
        *,
        include_channel: bool = True,
        include_thinking: bool = False,
    ) -> List[Dict]:
        """`list()`'s rows, but under the STRICT ownership rule of iter_owned_sessions.

        Use this wherever a caller must answer "which sessions are THIS scope's",
        rather than "which sessions may this scope see listed".
        """
        rows = []
        for _path, data in self.iter_owned_sessions(
            user_scope_id,
            max_candidates=limit,
            max_files=max(limit * 10, 200),
            include_channel=include_channel,
            include_thinking=include_thinking,
        ):
            rows.append({
                "id": data.get("id"),
                "name": data.get("name"),
                "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at"),
                "model": data.get("model"),
                "message_count": len(data.get("messages", [])),
                "summary": Session.from_dict(data).summary(),
                "metadata": data.get("metadata") or {},
            })
        return rows

    def search(self, query: str, limit: int = 10, *, user_scope_id: str) -> List[Dict]:
        """Search the caller's OWN sessions by content, one hit per session.

        `user_scope_id` is mandatory: this reads message text, and the previous
        version globbed the whole store with no scope at all, so on a multi-user
        installation it answered with other people's chats.
        """
        query_lower = query.lower()
        results = []

        for _path, data in self.iter_owned_sessions(user_scope_id, max_candidates=max(limit, 1)):
            for msg in data.get("messages", []):
                if query_lower in (msg.get("content") or "").lower():
                    results.append({
                        "session_id": data.get("id"),
                        "session_name": data.get("name"),
                        "match": (msg.get("content") or "")[:100],
                        "role": msg.get("role"),
                    })
                    break
            if len(results) >= limit:
                break

        return results


def session_access_allowed(
    manager,
    session_id: str,
    *,
    user_scope_id: Optional[str],
    is_admin: bool,
    allow_missing: bool = False,
) -> Tuple[bool, Optional["Session"]]:
    """May this caller act on this session? The one ownership rule, for every transport.

    The storage layer is scope-agnostic - only `list()` filters - so a caller that names
    a session id reaches it unless something says no. This is that something, and it lives
    here rather than inside one transport because the answer must not depend on whether
    the request arrived over a WebSocket or over HTTP. It grew up as the WebSocket gate;
    the Context Window's HTTP endpoint needed the same rule and would otherwise have been
    the second hand-rolled copy of it.

    `manager` is anything with `load()` and `storage_dir` - a plain parameter rather than
    a method on SessionManager so the gate's tests can hand it a store that raises on
    demand, which is how the unreadable-file branch below is exercised at all.

    Returns `(allowed, loaded_session_or_None)` so a caller that is about to read the
    session does not load it twice.

    Policy, strict:
    - an admin passes (the machine owner must never be locked out of their own store);
    - otherwise the session's recorded `user_scope_id` must be non-empty and equal to the
      caller's - a session with NO scope predates isolation and therefore belongs to the
      local admin alone, not to everyone;
    - `allow_missing` passes an id that does not exist yet, for the flow where the first
      message creates the session. It does NOT pass a session that exists but cannot be
      read: those two used to arrive here as one, and answering them alike let a caller
      claim a stranger's id whose file was corrupt or empty. Any doubt about whether the
      file is there resolves to "it is", i.e. to deny.
    """
    try:
        loaded = manager.load(session_id)
    except Exception:
        exists = True
        try:
            storage_dir = Path(manager.storage_dir)
            exists = any((storage_dir / f"{session_id}{ext}").exists()
                         for ext in (".json", ".json.gz"))
        except Exception:
            exists = True           # cannot tell -> treat as existing -> restrictive
        if exists:
            return (is_admin, None)
        return (bool(allow_missing) or is_admin, None)

    if is_admin:
        return (True, loaded)
    session_scope = (getattr(loaded, "metadata", None) or {}).get("user_scope_id")
    allowed = (
        session_scope is not None
        and str(user_scope_id or "") != ""
        and str(session_scope) == str(user_scope_id)
    )
    return (allowed, loaded)


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
    import re as _re

    # One resolver, and it answers per CONTEXT before it looks at the process boundary.
    # Reading the environment first was the pivot of a cross-tenant defect: a scheduled
    # automation, which declares that it belongs to no web session, would still find a live
    # chat turn's id here and build its output directory inside that tenant's workspace -
    # with a raw open(), so the per-user file jail never saw it.
    if not (session_id or "").strip():
        try:
            from vaf.core.subagent_ipc import get_current_session_id
            session_id = get_current_session_id() or ""
        except Exception:
            session_id = ""
    sid = (session_id or "").strip()
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
                data = manager._read_session_file(fp)
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
    """Search your own sessions by content."""
    from rich.console import Console
    from rich.table import Table
    from vaf.core.identity_binding import resolve_owner_identity

    console = Console()
    manager = get_manager()

    # The CLI has no authentication: the local user is the machine owner, so the
    # search runs under the owner's scope rather than over the whole store.
    results = manager.search(query, limit=limit, user_scope_id=resolve_owner_identity().scope)

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




def _room_rows(user_scope_id: Optional[str] = None) -> List[Dict]:
    """The agent rooms a chat surface shows above the conversations.

    LIVE rooms only: a closed one is over and drops out, which is the only way a person
    has to clear this list. Its transcript stays readable through `vaf a2a log` and
    `vaf a2a export`.

    Both local lanes are looked up, because "my rooms" means the ones my agent joined
    AND the ones I joined from a terminal - they are different participants by design,
    and a person does not think of them as two lists.

    Never raises: a chat sidebar that cannot draw because a room directory is damaged
    would be a worse failure than a missing row.
    """
    try:
        from vaf.core.a2a.room import joined_rooms, participant_key
    except Exception:
        return []

    rows: List[Dict] = []
    seen = set()

    def _human_unread(room) -> int:
        """What the PERSON has not looked at yet, from their own reading position.

        The sidebar is the person's surface, so its badge must not count the
        AGENT'S backlog: that number stayed red after the person had read
        everything, because the agent's cursor only moves when its turn runs.
        The person's cursor is the cli lane's - the browser and the terminal
        share it on purpose - and it needs no membership: a reading position is
        the reader's own file. Bookkeeping frames and the person's own words are
        not news.
        """
        try:
            from vaf.core.a2a.room import BOOKKEEPING_KINDS, derive_peer_id
            human = derive_peer_id(participant_key("cli", user_scope_id), room.room_id)
            position = room.store.cursor(human)
            return len([f for f in room.store.read_since(position)
                        if f.kind not in BOOKKEEPING_KINDS and f.sender != human])
        except Exception:
            return 0

    for lane in ("agent", "cli"):
        try:
            key = participant_key(lane, user_scope_id)
            for room, identity in joined_rooms(key):
                if room.room_id in seen:
                    continue
                seen.add(room.room_id)
                # A CLOSED room leaves this list. It is a live list of conversations a
                # person can still take part in, and a closed one is over: nothing more
                # can be written to it by anybody, including its host.
                #
                # The transcript is not lost and the promise it was closed under still
                # holds - `vaf a2a log <id>` and `vaf a2a export <id>` read it forever.
                # What was missing was any way to CLEAR the list at all: closing left
                # the row standing, so the bin promised removal and delivered a label.
                if room.closed:
                    continue
                rows.append({
                    "id": f"room:{room.room_id}",
                    "kind": "room",
                    "room_id": room.room_id,
                    "name": room.manifest.get("topic") or room.room_id,
                    "room_kind": room.kind,
                    "role": identity.role,
                    "peer": identity.peer_id,
                    "unread": _human_unread(room),
                    "members": len(room.members()),
                    "closed": bool(room.closed),
                    "updated_at": "",
                    "message_count": len(room.store.frames()),
                })
        except Exception:
            continue
    rows.sort(key=lambda row: (-row["unread"], row["room_id"]))
    return rows


def _matched_words(terms, text) -> List[str]:
    """The surface forms in `text` that the folded query terms hit.

    Lives next to the search because it answers the same question one step
    further on: not "does this match" but "which words did", which is what a
    highlight needs to be able to draw.
    """
    import re

    from vaf.core.cross_chat import _term_hits_word, fold

    out: List[str] = []
    # Over the ORIGINAL text, folding only for the comparison: `tokenize` returns
    # folded words, and handing "pruefung" to a highlight that has to find
    # "Prüfung" in the rendered message would mark nothing.
    for surface in re.findall(r"\w+", text or "", flags=re.UNICODE):
        folded = fold(surface).lower()
        if not folded:
            continue
        if any(_term_hits_word(term, folded) for term in terms):
            if surface not in out:
                out.append(surface)
    return out[:12]


def _passage_span(terms, content: str) -> List[int]:
    """`[start, end)` of the window `cross_chat._excerpt` would quote, on the RAW text.

    `_excerpt` builds its window on a whitespace-collapsed, folded copy, so its
    offsets do not address the original message. The viewer needs offsets that
    do, because it highlights the passage inside the message as rendered. Same
    window rule (`_SNIPPET_CHARS`, starting a third of it before the first
    match), applied to the text the reader is actually looking at.
    """
    import re

    from vaf.core.cross_chat import _SNIPPET_CHARS, _term_hits_word, fold

    position = -1
    for m in re.finditer(r"\w+", content or "", flags=re.UNICODE):
        folded = fold(m.group(0)).lower()
        if folded and any(_term_hits_word(term, folded) for term in terms):
            position = m.start()
            break
    if position < 0:
        return [0, min(len(content or ""), _SNIPPET_CHARS)]
    start = max(0, position - _SNIPPET_CHARS // 3)
    start = min(start, max(0, len(content) - _SNIPPET_CHARS))
    return [start, min(len(content), start + _SNIPPET_CHARS)]
