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
from dataclasses import dataclass, field, asdict

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
    
    def to_dict(self) -> Dict:
        return {k: v for k, v in asdict(self).items() if v is not None}
    
    @classmethod
    def from_dict(cls, data: Dict) -> "Message":
        return cls(**data)


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
        """Get message history for API calls."""
        messages = self.messages[-limit:] if limit else self.messages
        return [{"role": m.role, "content": m.content} for m in messages]
    
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
    
    def new(self, name: str = None, model: str = "", project_path: str = "") -> Session:
        """Create a new session."""
        session = Session(
            name=name or f"Session {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            model=model,
            project_path=project_path,
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
                session.runtime_state = snapshot.to_dict()
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
        
        if compress:
            with gzip.open(filepath, 'wt', encoding='utf-8') as f:
                f.write(data)
        else:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(data)
        
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
    
    def list(self, limit: int = 50) -> List[Dict]:
        """List all sessions."""
        sessions = []
        
        for filepath in sorted(self.storage_dir.glob("*.json*"), 
                               key=lambda p: p.stat().st_mtime, 
                               reverse=True)[:limit]:
            try:
                if filepath.suffix == '.gz':
                    with gzip.open(filepath, 'rt', encoding='utf-8') as f:
                        data = json.load(f)
                else:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                
                sessions.append({
                    "id": data.get("id"),
                    "name": data.get("name"),
                    "created_at": data.get("created_at"),
                    "updated_at": data.get("updated_at"),
                    "model": data.get("model"),
                    "message_count": len(data.get("messages", [])),
                    "summary": Session.from_dict(data).summary(),
                })
            except Exception:
                continue
        
        return sessions
    
    def delete(self, session_id: str) -> bool:
        """Delete a session."""
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

