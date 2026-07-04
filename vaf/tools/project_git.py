# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Project history and rollback — owned by the Coding Agent.

Every coder run ends with a final commit, so each project folder carries a
usable version history. The coder exposes it in two ways:

1. Delegation fast path: the Main Agent calls coding_agent with a task like
   "zeig die History" or "rollback auf <id>" and the coder answers directly
   (deterministic, no agentic loop) — see _detect_history_rollback_intent and
   the fast path in CodingAgentTool.run().
2. Own discretion: inside the agentic loop the coder has project_history and
   project_rollback as local tools (base_dir-wrapped), e.g. to restore a
   known-good state after breaking the project.

These tools are coder_only — the Main Agent never gets them directly; it talks
to the coder instead.

Rollback design: nothing is ever deleted. Uncommitted changes are committed as
a backup first, then the target state is restored via `git revert` as a NEW
commit — so a rollback can itself be rolled back.
"""
import os
import platform
import re
import subprocess
from typing import List, Optional

from vaf.tools.base import BaseTool


_HISTORY_INTENT = re.compile(
    r'\b(history|historie|verlauf|versionen|versionshistorie|git[\s-]?log)\b', re.IGNORECASE
)
_ROLLBACK_INTENT = re.compile(
    r'\b(rollback|roll[\s-]?back|zur[üu]cksetzen|zur[üu]ck\s+(?:auf|zu)|wiederherstellen|'
    r'stelle?\s+.{0,40}\s+wieder\s*her|restore\s+(?:the\s+)?(?:version|state|commit))\b',
    re.IGNORECASE,
)
_CREATE_INTENT = re.compile(
    r'\b(erstelle|erstell|schreibe?|baue?|implementiere?|f[üu]ge?|create|build|write|implement|add|generate|generiere?)\b',
    re.IGNORECASE,
)
_COMMIT_REF = re.compile(r'\b[0-9a-f]{7,40}\b')


def _detect_history_rollback_intent(task: str) -> "tuple[str, str]":
    """Classify a coding_agent task as a history/rollback delegation.

    Returns (kind, commit) with kind in {"", "history", "rollback"}.
    Creation verbs always win ("Erstelle eine Seite über die History von Rom"
    runs the normal agentic loop). A rollback request that names a concrete
    commit id matches regardless of task length — the main agent often wraps
    the delegation in long explanatory text, and sending it into the agentic
    loop made a small model plan "check git status" tasks instead of simply
    rolling back. Without a commit id the conservative 200-char limit applies.
    """
    t = (task or "").strip()
    if not t or _CREATE_INTENT.search(t):
        return "", ""
    if _ROLLBACK_INTENT.search(t):
        m = _COMMIT_REF.search(t.lower())
        if m:
            return "rollback", m.group(0)
        if len(t) <= 200:
            return "rollback", ""
        return "", ""
    if len(t) > 200:
        return "", ""
    if _HISTORY_INTENT.search(t):
        return "history", ""
    return "", ""


def _run_git(args: List[str], cwd: str) -> subprocess.CompletedProcess:
    kwargs = {"cwd": cwd, "capture_output": True, "text": True}
    if platform.system() == "Windows":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(["git", *args], **kwargs)


def apply_coauthor_trailer(message: str) -> str:
    """Append the configured Co-authored-by trailer to a VAF-authored commit message.

    Applied only where VAF itself authored the committed changes — project
    versioning commits here, the coder's final commit, and GitHub file commits.
    Never applied to user-initiated commits (`vaf git commit`) or to the
    internal `[VAF]` snapshot bookkeeping commits, whose content may be the
    user's own uncommitted work.

    The trailer block is separated by two blank lines; commit with
    `--cleanup=verbatim`, the default cleanup collapses consecutive blank
    lines. Idempotent. Disabled via git_coauthor_enabled=False or an empty
    git_coauthor_identity (the set_git_coauthor tool flips this from chat).
    """
    try:
        from vaf.core.config import Config
        if not Config.get("git_coauthor_enabled"):
            return message
        identity = (Config.get("git_coauthor_identity") or "").strip()
    except Exception:
        return message
    if not identity:
        return message
    trailer = f"Co-authored-by: {identity}"
    if trailer.lower() in (message or "").lower():
        return message
    return f"{(message or '').rstrip()}\n\n\n{trailer}"


def _session_project_path() -> str:
    """Project path of the current chat (workspace first, then last project)."""
    try:
        from vaf.core.subagent_ipc import get_current_session_id
        sid = get_current_session_id()
        if not sid:
            return ""
        from vaf.core.session import SessionManager
        sess = SessionManager().load(sid)
        workspace = getattr(sess, "project_path", "") or ""
        last = (getattr(sess, "runtime_state", None) or {}).get("last_project_path", "")
        for candidate in (last, workspace):
            if candidate and os.path.isdir(candidate):
                return candidate
    except Exception:
        pass
    return ""


def _resolve_project(provided: str) -> "tuple[str, str]":
    """Resolve and validate the project directory. Returns (path, error)."""
    path = os.path.abspath(os.path.expanduser(provided)) if provided else _session_project_path()
    if not path:
        return "", (
            "No project found. This chat has no session workspace yet — "
            "pass project_path explicitly."
        )
    if not os.path.isdir(path):
        return "", f"Project directory does not exist: {path}"
    from vaf.tools.coder import is_unsafe_project_dir
    if is_unsafe_project_dir(path):
        return "", f"Refused: {path} is not a valid project directory."
    if not os.path.isdir(os.path.join(path, ".git")):
        return "", (
            f"No version history: {path} is not a git repository. "
            "Projects created by the coding agent get one automatically."
        )
    return path, ""


class ProjectHistoryTool(BaseTool):
    """Show the version history of a project folder (coder-owned)."""

    name = "project_history"
    coder_only = True  # Main Agent delegates via coding_agent task instead
    description = (
        "Show the version history of the project (one entry per saved state, "
        "newest first, with commit id, date, description and changed files). "
        "Use when the task asks for the history or before a project_rollback."
    )
    permission_level = "read"
    side_effect_class = "none"
    input_examples = [
        {},
        {"limit": 10},
    ]
    parameters = {
        "type": "object",
        "properties": {
            "project_path": {
                "type": "string",
                "description": "Optional: absolute path to the project. Defaults to the current project directory.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of versions to show (default 15).",
            },
        },
        "required": [],
    }

    def run(self, **kwargs) -> str:
        path, err = _resolve_project(kwargs.get("project_path") or kwargs.get("base_dir", ""))
        if err:
            return f"Error: {err}"
        limit = max(1, min(int(kwargs.get("limit") or 15), 50))

        log = _run_git(
            ["log", f"-n{limit}", "--date=format:%Y-%m-%d %H:%M",
             "--pretty=format:@@%h|%ad|%s", "--name-only"],
            cwd=path,
        )
        if log.returncode != 0:
            return f"Error reading history: {(log.stderr or '').strip()[:200]}"
        if not (log.stdout or "").strip():
            return f"No versions yet in {path}."

        lines = [f"Version history of {os.path.basename(path)} ({path}):", ""]
        for block in log.stdout.split("@@"):
            block = block.strip()
            if not block:
                continue
            head, *files = [ln for ln in block.splitlines() if ln.strip()]
            sha, date, subject = (head.split("|", 2) + ["", ""])[:3]
            files = [f for f in files if not f.startswith(".vaf/")]
            files_note = ", ".join(files[:6]) + (" ..." if len(files) > 6 else "")
            lines.append(f"- {sha}  {date}  {subject}")
            if files_note:
                lines.append(f"    files: {files_note}")
        lines.append("")
        lines.append('To restore an earlier version, request a rollback with its id, e.g. "rollback auf <id>".')
        return "\n".join(lines)


class ProjectRollbackTool(BaseTool):
    """Restore a project folder to an earlier version (coder-owned, undoable)."""

    name = "project_rollback"
    coder_only = True  # Main Agent delegates via coding_agent task instead
    description = (
        "Restore the project to an earlier version from project_history. "
        "Safe by design: uncommitted work is saved as a backup first and the "
        "rollback is recorded as a NEW history entry, so it can itself be undone "
        "by rolling back again. Use ONLY when the task explicitly asks for a "
        "rollback, or when you broke the project and need to return to a "
        "known-good state. Never use it as a shortcut to skip work."
    )
    permission_level = "write"
    side_effect_class = "reversible"
    input_examples = [
        {"commit": "a2200c1"},
    ]
    parameters = {
        "type": "object",
        "properties": {
            "commit": {
                "type": "string",
                "description": "Commit id of the version to restore (from project_history).",
            },
            "project_path": {
                "type": "string",
                "description": "Optional: absolute path to the project. Defaults to the current project directory.",
            },
        },
        "required": ["commit"],
    }

    def run(self, **kwargs) -> str:
        commit = (kwargs.get("commit") or "").strip()
        if not commit:
            return "Error: commit is required (get it from project_history)."
        path, err = _resolve_project(kwargs.get("project_path") or kwargs.get("base_dir", ""))
        if err:
            return f"Error: {err}"

        rev = _run_git(["rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"], cwd=path)
        if rev.returncode != 0:
            return f"Error: version '{commit}' not found in {path}. Use project_history to list valid ids."
        target = rev.stdout.strip()

        head = _run_git(["rev-parse", "HEAD"], cwd=path)
        previous_head = (head.stdout or "").strip()[:7]

        # 1) Never lose uncommitted work: back it up as its own history entry.
        backup_note = ""
        status = _run_git(["status", "--porcelain"], cwd=path)
        if (status.stdout or "").strip():
            _run_git(["add", "-A"], cwd=path)
            backup = self._commit(path, "Backup before rollback (VAF)")
            if backup.returncode != 0:
                return f"Error: could not back up current state: {(backup.stderr or '').strip()[:200]}"
            backup_note = "Uncommitted changes were saved as 'Backup before rollback (VAF)'.\n"
            previous_head = _run_git(["rev-parse", "HEAD"], cwd=path).stdout.strip()[:7]

        if target.startswith(previous_head) or previous_head.startswith(target[:7]):
            return f"{backup_note}Project is already at version {commit}. Nothing to do."

        # 2) Restore the target state as a NEW commit (history preserved).
        revert = _run_git(["revert", "--no-commit", f"{target}..HEAD"], cwd=path)
        if revert.returncode != 0:
            _run_git(["revert", "--abort"], cwd=path)
            return (
                f"Error: rollback to {commit} failed (history is not linear or has conflicts): "
                f"{(revert.stderr or '').strip()[:200]}. The project was left unchanged."
            )

        staged = _run_git(["diff", "--cached", "--quiet"], cwd=path)
        if staged.returncode == 0:
            _run_git(["revert", "--quit"], cwd=path)
            return f"{backup_note}Project content is already identical to version {commit}. Nothing to do."

        subject = _run_git(["log", "-1", "--pretty=%s", target], cwd=path).stdout.strip()
        result = self._commit(path, f"Rollback to {target[:7]}: {subject}"[:120])
        if result.returncode != 0:
            _run_git(["revert", "--quit"], cwd=path)
            _run_git(["reset", "--hard", "HEAD"], cwd=path)
            return f"Error: rollback commit failed: {(result.stderr or '').strip()[:200]}. The project was left unchanged."

        return (
            f"{backup_note}"
            f"Project restored to version {target[:7]} ('{subject}').\n"
            f"The rollback was recorded as a new history entry — to undo it, "
            f'run project_rollback(commit="{previous_head}").'
        )

    @staticmethod
    def _commit(path: str, message: str) -> subprocess.CompletedProcess:
        # verbatim keeps the two blank lines before the co-author trailer block;
        # the message is fully programmatic, so there is nothing to clean up.
        message = apply_coauthor_trailer(message)
        result = _run_git(["commit", "--cleanup=verbatim", "-m", message], cwd=path)
        if result.returncode != 0:
            err = (result.stderr or "") + (result.stdout or "")
            if any(s in err for s in ("user.name", "user.email", "identity")):
                result = _run_git(
                    ["-c", "user.name=VAF Coder", "-c", "user.email=coder@vaf.local",
                     "commit", "--cleanup=verbatim", "-m", message],
                    cwd=path,
                )
        return result
