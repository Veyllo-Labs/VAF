"""
VAF Snapshot - Git-based code change tracking and undo
Track file changes and revert to previous states
"""
import subprocess
import shutil
import platform
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional
import json
import hashlib


def _get_subprocess_kwargs() -> dict:
    """Get platform-specific kwargs for headless subprocess execution."""
    kwargs = {}
    if platform.system() == "Windows":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs

# ═══════════════════════════════════════════════════════════════════════════════
# SNAPSHOT CLASS
# ═══════════════════════════════════════════════════════════════════════════════

class Snapshot:
    """
    Manages file snapshots for undo functionality.
    Uses Git when available, falls back to file-based snapshots.
    """
    
    def __init__(self, project_path: str = None):
        self.project_path = Path(project_path or ".").resolve()
        self.snapshot_dir = self.project_path / ".vaf" / "snapshots"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._use_git = self._check_git()
        self._current_snapshot: Optional[str] = None
        self._snapshots: List[Dict] = []
        self._load_index()
    
    def _check_git(self) -> bool:
        """Check if we can use Git for snapshots."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=self.project_path,
                capture_output=True,
                text=True,
                **_get_subprocess_kwargs()
            )
            return result.returncode == 0
        except Exception:
            return False
    
    def _load_index(self):
        """Load snapshot index."""
        index_file = self.snapshot_dir / "index.json"
        if index_file.exists():
            try:
                with open(index_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self._snapshots = data.get("snapshots", [])
            except Exception:
                self._snapshots = []
    
    def _save_index(self):
        """Save snapshot index."""
        index_file = self.snapshot_dir / "index.json"
        with open(index_file, 'w', encoding='utf-8') as f:
            json.dump({"snapshots": self._snapshots}, f, indent=2)
    
    def _file_hash(self, filepath: Path) -> str:
        """Calculate file hash."""
        if not filepath.exists():
            return ""
        with open(filepath, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    
    # ═══════════════════════════════════════════════════════════════════════════
    # GIT-BASED SNAPSHOTS
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _git_snapshot(self, message: str = None) -> Optional[str]:
        """Create a Git-based snapshot (stash or commit)."""
        try:
            sp_kwargs = _get_subprocess_kwargs()

            # Check for changes
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.project_path,
                capture_output=True,
                text=True,
                **sp_kwargs
            )

            if not status.stdout.strip():
                return None  # No changes

            # Stage all changes
            subprocess.run(
                ["git", "add", "-A"],
                cwd=self.project_path,
                capture_output=True,
                **sp_kwargs
            )

            # Create commit with VAF marker
            msg = message or f"VAF snapshot {datetime.now().isoformat()}"
            result = subprocess.run(
                ["git", "commit", "-m", f"[VAF] {msg}", "--no-verify"],
                cwd=self.project_path,
                capture_output=True,
                text=True,
                **sp_kwargs
            )

            if result.returncode == 0:
                # Get commit hash
                hash_result = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=self.project_path,
                    capture_output=True,
                    text=True,
                    **sp_kwargs
                )
                return hash_result.stdout.strip()[:8]
            
            return None
            
        except Exception:
            return None
    
    def _git_restore(self, commit_hash: str) -> bool:
        """Restore to a Git commit."""
        try:
            result = subprocess.run(
                ["git", "checkout", commit_hash, "--", "."],
                cwd=self.project_path,
                capture_output=True,
                text=True,
                **_get_subprocess_kwargs()
            )
            return result.returncode == 0
        except Exception:
            return False

    def _git_diff(self, commit_hash: str = None) -> str:
        """Get Git diff."""
        try:
            cmd = ["git", "diff"]
            if commit_hash:
                cmd.append(commit_hash)

            result = subprocess.run(
                cmd,
                cwd=self.project_path,
                capture_output=True,
                text=True,
                **_get_subprocess_kwargs()
            )
            return result.stdout
        except Exception:
            return ""
    
    # ═══════════════════════════════════════════════════════════════════════════
    # FILE-BASED SNAPSHOTS (fallback)
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _file_snapshot(self, files: List[str] = None, message: str = None) -> str:
        """Create file-based snapshot."""
        snapshot_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        snapshot_path = self.snapshot_dir / snapshot_id
        snapshot_path.mkdir(exist_ok=True)
        
        # Get files to snapshot
        if files:
            target_files = [Path(f) for f in files]
        else:
            # Snapshot all text files (simplified)
            target_files = list(self.project_path.rglob("*"))
            target_files = [f for f in target_files if f.is_file() 
                          and ".vaf" not in str(f)
                          and ".git" not in str(f)
                          and f.suffix in ('.py', '.js', '.ts', '.json', '.yaml', '.yml', '.md', '.txt', '.toml', '.cfg')]
        
        files_saved = []
        for filepath in target_files:
            if not filepath.exists() or not filepath.is_file():
                continue
            
            try:
                rel_path = filepath.relative_to(self.project_path)
                dest = snapshot_path / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(filepath, dest)
                files_saved.append(str(rel_path))
            except Exception:
                continue
        
        # Save metadata
        metadata = {
            "id": snapshot_id,
            "created": datetime.now().isoformat(),
            "message": message or "Snapshot",
            "files": files_saved,
            "type": "file"
        }
        
        with open(snapshot_path / "metadata.json", 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2)
        
        return snapshot_id
    
    def _file_restore(self, snapshot_id: str, files: List[str] = None) -> bool:
        """Restore from file-based snapshot."""
        snapshot_path = self.snapshot_dir / snapshot_id
        
        if not snapshot_path.exists():
            return False
        
        # Load metadata
        try:
            with open(snapshot_path / "metadata.json", 'r', encoding='utf-8') as f:
                metadata = json.load(f)
        except Exception:
            metadata = {"files": []}
        
        # Restore files
        files_to_restore = files or metadata.get("files", [])
        
        for rel_path in files_to_restore:
            src = snapshot_path / rel_path
            dest = self.project_path / rel_path
            
            if src.exists():
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
                except Exception:
                    continue
        
        return True
    
    # ═══════════════════════════════════════════════════════════════════════════
    # PUBLIC API
    # ═══════════════════════════════════════════════════════════════════════════
    
    def track(self, files: List[str] = None, message: str = None) -> Optional[str]:
        """
        Create a snapshot of the current state.
        Returns snapshot ID or None if no changes.
        """
        if self._use_git:
            snapshot_id = self._git_snapshot(message)
            snapshot_type = "git"
        else:
            snapshot_id = self._file_snapshot(files, message)
            snapshot_type = "file"
        
        if snapshot_id:
            self._snapshots.append({
                "id": snapshot_id,
                "type": snapshot_type,
                "created": datetime.now().isoformat(),
                "message": message or "Snapshot"
            })
            self._current_snapshot = snapshot_id
            self._save_index()
        
        return snapshot_id
    
    def restore(self, snapshot_id: str = None, files: List[str] = None) -> bool:
        """
        Restore to a previous snapshot.
        If no ID given, restores to previous snapshot.
        """
        if not snapshot_id:
            # Find last snapshot
            if not self._snapshots:
                return False
            snapshot_id = self._snapshots[-1]["id"]
        
        # Find snapshot info
        snapshot_info = None
        for s in self._snapshots:
            if s["id"] == snapshot_id:
                snapshot_info = s
                break
        
        if not snapshot_info:
            return False
        
        if snapshot_info.get("type") == "git":
            return self._git_restore(snapshot_id)
        else:
            return self._file_restore(snapshot_id, files)
    
    def diff(self, snapshot_id: str = None) -> str:
        """Get diff from snapshot to current state."""
        if self._use_git:
            return self._git_diff(snapshot_id)
        else:
            # Basic file-based diff (simplified)
            return "File-based diff not fully implemented. Use Git for full diff support."
    
    def list(self, limit: int = 20) -> List[Dict]:
        """List available snapshots."""
        return self._snapshots[-limit:][::-1]  # Most recent first
    
    def undo(self) -> bool:
        """Undo to the previous snapshot."""
        return self.restore()
    
    def patch(self, filepath: str, original: str, modified: str) -> Dict[str, Any]:
        """
        Track a specific file change.
        Creates minimal snapshot for single file changes.
        """
        result = {
            "file": filepath,
            "success": False,
            "snapshot_id": None
        }
        
        file_path = self.project_path / filepath
        
        # Store original before modification
        snapshot_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:20]
        patch_dir = self.snapshot_dir / "patches" / snapshot_id
        patch_dir.mkdir(parents=True, exist_ok=True)
        
        # Save original content
        with open(patch_dir / "original.txt", 'w', encoding='utf-8') as f:
            f.write(original)
        
        # Save file path
        with open(patch_dir / "meta.json", 'w', encoding='utf-8') as f:
            json.dump({
                "file": filepath,
                "created": datetime.now().isoformat()
            }, f)
        
        self._snapshots.append({
            "id": snapshot_id,
            "type": "patch",
            "file": filepath,
            "created": datetime.now().isoformat(),
            "message": f"Patch: {filepath}"
        })
        self._save_index()
        
        result["success"] = True
        result["snapshot_id"] = snapshot_id
        
        return result
    
    def clear_old(self, keep: int = 50) -> int:
        """Remove old snapshots, keeping the most recent ones."""
        if len(self._snapshots) <= keep:
            return 0
        
        to_remove = self._snapshots[:-keep]
        removed = 0
        
        for snapshot in to_remove:
            snapshot_id = snapshot["id"]
            snapshot_type = snapshot.get("type", "file")
            
            if snapshot_type == "file":
                path = self.snapshot_dir / snapshot_id
                if path.exists():
                    shutil.rmtree(path)
                    removed += 1
            elif snapshot_type == "patch":
                path = self.snapshot_dir / "patches" / snapshot_id
                if path.exists():
                    shutil.rmtree(path)
                    removed += 1
        
        self._snapshots = self._snapshots[-keep:]
        self._save_index()
        
        return removed


# ═══════════════════════════════════════════════════════════════════════════════
# CLI COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

import typer

snapshot_app = typer.Typer(help="Manage code snapshots and undo")

_snapshot: Optional[Snapshot] = None

def get_snapshot() -> Snapshot:
    global _snapshot
    if _snapshot is None:
        _snapshot = Snapshot()
    return _snapshot


@snapshot_app.command("create")
def create_snapshot(
    message: str = typer.Option(None, "--message", "-m", help="Snapshot message")
):
    """Create a new snapshot of current state."""
    from rich.console import Console
    
    console = Console()
    snap = get_snapshot()
    
    with console.status("Creating snapshot..."):
        snapshot_id = snap.track(message=message)
    
    if snapshot_id:
        console.print(f"[green]✓ Created snapshot: {snapshot_id}[/green]")
    else:
        console.print("[yellow]No changes to snapshot.[/yellow]")


@snapshot_app.command("list")
def list_snapshots(
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum snapshots to show")
):
    """List available snapshots."""
    from rich.console import Console
    from rich.table import Table
    
    console = Console()
    snap = get_snapshot()
    snapshots = snap.list(limit=limit)
    
    if not snapshots:
        console.print("[yellow]No snapshots available.[/yellow]")
        return
    
    table = Table(title="Code Snapshots", show_header=True)
    table.add_column("ID", style="cyan")
    table.add_column("Type")
    table.add_column("Created")
    table.add_column("Message")
    
    for s in snapshots:
        created = s["created"][:16] if s.get("created") else "?"
        table.add_row(
            s["id"],
            s.get("type", "file"),
            created,
            s.get("message", "")[:40],
        )
    
    console.print(table)


@snapshot_app.command("restore")
def restore_snapshot(
    snapshot_id: str = typer.Argument(None, help="Snapshot ID to restore (default: last)"),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation")
):
    """Restore to a previous snapshot."""
    from rich.console import Console
    
    console = Console()
    snap = get_snapshot()
    
    if not force:
        msg = f"Restore to snapshot {snapshot_id or 'last'}?" if snapshot_id else "Restore to last snapshot?"
        if not typer.confirm(msg):
            console.print("[yellow]Cancelled.[/yellow]")
            return
    
    with console.status("Restoring..."):
        success = snap.restore(snapshot_id)
    
    if success:
        console.print(f"[green]✓ Restored to snapshot: {snapshot_id or 'last'}[/green]")
    else:
        console.print("[red]✗ Failed to restore snapshot.[/red]")


@snapshot_app.command("undo")
def undo_changes():
    """Undo to the last snapshot (shortcut for restore)."""
    from rich.console import Console
    
    console = Console()
    snap = get_snapshot()
    
    with console.status("Undoing changes..."):
        success = snap.undo()
    
    if success:
        console.print("[green]✓ Undone to last snapshot.[/green]")
    else:
        console.print("[red]✗ No snapshot to undo to.[/red]")


@snapshot_app.command("diff")
def show_diff(
    snapshot_id: str = typer.Argument(None, help="Snapshot ID to diff against")
):
    """Show diff from snapshot to current state."""
    from rich.console import Console
    from rich.syntax import Syntax
    
    console = Console()
    snap = get_snapshot()
    
    diff_output = snap.diff(snapshot_id)
    
    if diff_output:
        syntax = Syntax(diff_output, "diff", theme="dracula")
        console.print(syntax)
    else:
        console.print("[yellow]No differences found.[/yellow]")


@snapshot_app.command("clean")
def clean_snapshots(
    keep: int = typer.Option(50, "--keep", "-k", help="Number of snapshots to keep")
):
    """Remove old snapshots."""
    from rich.console import Console
    
    console = Console()
    snap = get_snapshot()
    
    removed = snap.clear_old(keep=keep)
    console.print(f"[green]✓ Removed {removed} old snapshots.[/green]")

