# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
GitHub tools: list repos, get file content, list issues/PRs (optional commit gated by allow_write).

Requires GitHub to be connected in Settings → Connections → GitHub.
Uses user_scope_id and username from agent context to resolve the linked account and token.
"""

import logging
from typing import Any, Dict, List, Optional

try:
    from github import Github, Auth, GithubException
    _PYGITHUB_AVAILABLE = True
except ImportError:
    Github = None  # type: ignore[assignment,misc]
    Auth = None  # type: ignore[assignment]
    GithubException = Exception  # type: ignore[assignment,misc]
    _PYGITHUB_AVAILABLE = False

from vaf.core.config import Config
from vaf.github.credential_github import get_github_oauth_token
from vaf.github.activity import log_github_activity
from vaf.tools.base import BaseTool
from vaf.tools.mail_utils import cred_scope_from_kwargs, cred_username_from_kwargs

logger = logging.getLogger("vaf.tools.github")


def _get_github_account_for_user(
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return first GitHub account for this user from config (strict per-user isolation)."""
    local_admin = (Config.get("local_admin_username") or "admin").strip().lower()
    if not username or username.strip().lower() == local_admin:
        gc = Config.get("github_config") or {}
    else:
        by_user = Config.get("github_config_by_user") or {}
        gc = by_user.get(username.strip(), {}) if isinstance(by_user, dict) else {}
    accounts = gc.get("accounts") or []
    for acc in accounts:
        if acc.get("enabled", True) and acc.get("account_id"):
            return acc
    return None


def _get_github_client(kwargs: dict) -> tuple:
    """Return (Github_client, account_dict) or (None, None)."""
    if not _PYGITHUB_AVAILABLE:
        logger.warning("GitHub: PyGithub not installed. Run: pip install 'PyGithub>=2.1.1'")
        return None, None
    user_scope_id = cred_scope_from_kwargs(kwargs)
    username = cred_username_from_kwargs(kwargs)
    logger.info("GitHub client request: username=%s, scope=%s, _PYGITHUB=%s", username, user_scope_id, _PYGITHUB_AVAILABLE)
    account = _get_github_account_for_user(username=username, user_scope_id=user_scope_id)
    if not account:
        logger.warning("GitHub: No account found (username=%s, scope=%s). Check Settings → Connections → GitHub.", username, user_scope_id)
        return None, None
    account_id = account.get("account_id")
    token = get_github_oauth_token(account_id, username=username, user_scope_id=user_scope_id)
    if not token:
        logger.warning("GitHub: No token for account_id=%s (username=%s). Re-connect in Settings → Connections → GitHub.", account_id, username)
        return None, None

    logger.info("GitHub client created for account=%s", account_id)
    auth = Auth.Token(token)
    return Github(auth=auth), account


def _no_github_message() -> str:
    return (
        "GitHub is not connected. Connect your GitHub account in Settings → Connections → Developer → GitHub. "
        "Then the agent can list your repos, read files, and list issues/PRs."
    )


class GitHubListReposTool(BaseTool):
    """List the user's GitHub repositories."""
    name = "github_list_repos"
    permission_level = "read"
    side_effect_class = "none"
    category = "github"
    description = (
        "List the user's GitHub repositories. Use when the user asks about their repos, projects on GitHub, or which repos they have. "
        "Requires GitHub to be connected in Settings → Connections → GitHub. "
        "Optional: visibility (all|public|private), sort (created|updated|pushed|full_name), per_page (default 30)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "visibility": {"type": "string", "description": "Filter: all, public, or private. Default: all."},
            "sort": {"type": "string", "description": "Sort by: created, updated, pushed, full_name. Default: full_name."},
            "per_page": {"type": "integer", "description": "Max repos to return (default 30, max 100)."},
        },
        "required": [],
    }

    def run(self, **kwargs) -> str:
        username = cred_username_from_kwargs(kwargs)
        g, account = _get_github_client(kwargs)
        if not g:
            return _no_github_message()
        
        account_id = account.get("account_id") if account else None
        visibility = (kwargs.get("visibility") or "all").strip() or "all"
        sort = (kwargs.get("sort") or "full_name").strip() or "full_name"
        per_page = min(100, max(1, int(kwargs.get("per_page") or 30)))
        
        try:
            repos = g.get_user().get_repos(type=visibility, sort=sort)
            
            lines = []
            count = 0
            for repo in repos:
                if count >= per_page:
                    break
                full_name = repo.full_name
                private = repo.private
                desc = (repo.description or "").strip() or "(no description)"
                lines.append(f"- {full_name} (private={private}): {desc[:80]}")
                count += 1
            
            log_github_activity(
                username, 
                "list_repos", 
                f"Listed {len(lines)} repositories (visibility={visibility})",
                account_id=account_id
            )
            return "\n".join(lines) if lines else "No repositories found."
        except GithubException as e:
            err_msg = f"GitHub API error: {e.status} {e.data.get('message', str(e))}"
            log_github_activity(username, "list_repos", "Failed to list repositories", account_id=account_id, success=False, error=err_msg)
            return err_msg
        except Exception as e:
            logger.exception("github_list_repos failed")
            log_github_activity(username, "list_repos", "Failed to list repositories", account_id=account_id, success=False, error=str(e))
            return f"Error listing repos: {e}"


class GitHubGetFileTool(BaseTool):
    """Get the content of a file from a GitHub repository, with optional line-range support for large files."""
    name = "github_get_file"
    permission_level = "read"
    side_effect_class = "none"
    category = "github"
    description = (
        "Get the content of a file from a GitHub repository. "
        "For large files use start_line/end_line to read specific sections (e.g. start_line=500, end_line=1000). "
        "Without a range, files >300 lines are capped at 200 lines with a size warning and navigation hint. "
        "Requires GitHub connected."
    )
    parameters = {
        "type": "object",
        "properties": {
            "owner":      {"type": "string",  "description": "Repository owner (username or org)."},
            "repo":       {"type": "string",  "description": "Repository name."},
            "path":       {"type": "string",  "description": "Path to the file in the repo (e.g. README.md, src/main.py)."},
            "ref":        {"type": "string",  "description": "Branch, tag, or commit SHA. Omit for default branch."},
            "start_line": {"type": "integer", "description": "First line to return (1-based, inclusive). Use with end_line to read a section of a large file."},
            "end_line":   {"type": "integer", "description": "Last line to return (1-based, inclusive). Use with start_line to read a section of a large file."},
        },
        "required": ["owner", "repo", "path"],
    }

    # Files larger than this (lines) trigger the cap + warning when no range given
    _LARGE_FILE_LINES = 300
    # How many lines to return as preview when no range is given for a large file
    _PREVIEW_LINES = 200

    def run(self, **kwargs) -> str:
        username   = cred_username_from_kwargs(kwargs)
        g, account = _get_github_client(kwargs)
        if not g:
            return _no_github_message()

        account_id = account.get("account_id") if account else None
        owner      = (kwargs.get("owner") or "").strip()
        repo_name  = (kwargs.get("repo") or "").strip()
        path       = (kwargs.get("path") or "").strip()
        ref        = (kwargs.get("ref") or "").strip() or None
        start_line = kwargs.get("start_line")  # 1-based, inclusive
        end_line   = kwargs.get("end_line")    # 1-based, inclusive

        if not owner or not repo_name or not path:
            return "Missing required parameters: owner, repo, path."

        try:
            repo         = g.get_repo(f"{owner}/{repo_name}")
            content_file = repo.get_contents(path) if not ref else repo.get_contents(path, ref=ref)

            if isinstance(content_file, list):
                items = [f"{'📁' if item.type == 'dir' else '📄'} {item.path}" for item in content_file]
                log_github_activity(username, "get_file", f"Attempted to read directory: {owner}/{repo_name}/{path}", account_id=account_id)
                return f"'{path}' is a directory. Contents:\n" + "\n".join(items)

            raw = content_file.decoded_content
            if raw is None:
                # Files >1 MB: decoded_content is None — fetch via download_url
                download_url = getattr(content_file, "download_url", None)
                if download_url:
                    import urllib.request
                    with urllib.request.urlopen(download_url, timeout=15) as resp:  # noqa: S310
                        raw = resp.read()
                else:
                    return f"File '{path}' is too large to read via API (>1 MB) and no download URL available."

            content    = raw.decode("utf-8", errors="replace")
            all_lines  = content.splitlines()
            total      = len(all_lines)
            size_kb    = len(raw) / 1024

            log_github_activity(
                username, "get_file",
                f"Read file: {owner}/{repo_name}/{path} (ref={ref or 'default'}, lines={total})",
                account_id=account_id,
            )

            # ── Range requested ──────────────────────────────────────────────
            if start_line is not None or end_line is not None:
                s = max(1, int(start_line or 1))
                e = min(total, int(end_line or total))
                if s > total:
                    return (
                        f"⚠️  start_line={s} is beyond the end of the file "
                        f"({total} lines total). Use a value ≤ {total}."
                    )
                selected = all_lines[s - 1:e]
                header = (
                    f"📄 {path}  |  {total} lines total  |  {size_kb:.1f} KB\n"
                    f"   Showing lines {s}–{e} ({len(selected)} lines)\n"
                )
                if e < total:
                    header += f"   Next section: start_line={e + 1}, end_line={min(total, e + (e - s + 1))}\n"
                header += "─" * 60 + "\n"
                numbered = "\n".join(f"{s + i:>6}  {line}" for i, line in enumerate(selected))
                return header + numbered

            # ── No range: small file → return as-is ─────────────────────────
            if total <= self._LARGE_FILE_LINES:
                return content

            # ── No range: large file → cap + warn ───────────────────────────
            preview   = all_lines[:self._PREVIEW_LINES]
            numbered  = "\n".join(f"{i + 1:>6}  {line}" for i, line in enumerate(preview))
            remaining = total - self._PREVIEW_LINES
            warning = (
                f"⚠️  Large file: {path}\n"
                f"   {total} lines  |  {size_kb:.1f} KB\n"
                f"   Showing lines 1–{self._PREVIEW_LINES} (first {self._PREVIEW_LINES} of {total}).\n"
                f"   {remaining} more lines not shown.\n"
                f"\n"
                f"   To read a specific section use:\n"
                f"   github_get_file(owner=\"{owner}\", repo=\"{repo_name}\", path=\"{path}\",\n"
                f"                   start_line={self._PREVIEW_LINES + 1}, end_line={min(total, self._PREVIEW_LINES * 2)})\n"
                f"─" * 60 + "\n"
            )
            return warning + numbered

        except GithubException as e:
            if e.status == 404:
                err_msg = f"File or repository not found: {owner}/{repo_name}/{path}"
                log_github_activity(username, "get_file", f"File not found: {owner}/{repo_name}/{path}", account_id=account_id, success=False, error=err_msg)
                return err_msg
            data    = getattr(e, "data", {}) or {}
            api_msg = data.get("message") or str(e)
            err_msg = f"GitHub API error {e.status}: {api_msg}"
            log_github_activity(username, "get_file", f"Failed to read file: {owner}/{repo_name}/{path}", account_id=account_id, success=False, error=err_msg)
            return err_msg
        except Exception as e:
            logger.exception("github_get_file failed")
            log_github_activity(username, "get_file", f"Failed to read file: {owner}/{repo_name}/{path}", account_id=account_id, success=False, error=str(e))
            return f"Error reading file: {e}"


class GitHubListDirectoryTool(BaseTool):
    """List the contents of a directory in a GitHub repository."""
    name = "github_list_directory"
    permission_level = "read"
    side_effect_class = "none"
    category = "github"
    description = (
        "List the contents of a directory in a GitHub repository. Use to explore the project structure. "
        "Parameters: owner, repo, path (folder path, default root ''), ref (branch/tag/SHA). "
        "Returns a list of files and subdirectories."
    )
    parameters = {
        "type": "object",
        "properties": {
            "owner": {"type": "string", "description": "Repository owner."},
            "repo": {"type": "string", "description": "Repository name."},
            "path": {"type": "string", "description": "Directory path (e.g. 'src', 'docs'). Default is root."},
            "ref": {"type": "string", "description": "Branch, tag, or commit SHA. Omit for default branch."},
        },
        "required": ["owner", "repo"],
    }

    def run(self, **kwargs) -> str:
        username = cred_username_from_kwargs(kwargs)
        g, account = _get_github_client(kwargs)
        if not g:
            return _no_github_message()
        
        account_id = account.get("account_id") if account else None
        owner = (kwargs.get("owner") or "").strip()
        repo_name = (kwargs.get("repo") or "").strip()
        path = (kwargs.get("path") or "").strip()
        ref = (kwargs.get("ref") or "").strip() or None
        
        if not owner or not repo_name:
            return "Missing required parameters: owner, repo."
        
        try:
            repo = g.get_repo(f"{owner}/{repo_name}")
            contents = repo.get_contents(path) if not ref else repo.get_contents(path, ref=ref)
            
            if not isinstance(contents, list):
                return f"'{path}' is a file, not a directory. Use github_get_file to read it."
            
            lines = []
            for item in contents:
                icon = "📁" if item.type == "dir" else "📄"
                lines.append(f"{icon} {item.path}")
            
            log_github_activity(
                username, 
                "list_directory", 
                f"Listed directory: {owner}/{repo_name}/{path} ({len(lines)} items)",
                account_id=account_id
            )
            return "\n".join(lines) if lines else f"Directory '{path}' is empty."
        except GithubException as e:
            if e.status == 404:
                return f"Directory or repository not found: {owner}/{repo_name}/{path}"
            err_msg = f"GitHub API error: {e.status} {e.data.get('message', str(e))}"
            log_github_activity(username, "list_directory", f"Failed to list directory: {owner}/{repo_name}/{path}", account_id=account_id, success=False, error=err_msg)
            return err_msg
        except Exception as e:
            logger.exception("github_list_directory failed")
            log_github_activity(username, "list_directory", f"Failed to list directory: {owner}/{repo_name}/{path}", account_id=account_id, success=False, error=str(e))
            return f"Error listing directory: {e}"


class GitHubSearchFilesTool(BaseTool):
    """Search for files in a GitHub repository."""
    name = "github_search_files"
    permission_level = "read"
    side_effect_class = "none"
    category = "github"
    description = (
        "Search for files in a GitHub repository by name or extension. "
        "Parameters: owner, repo, query (e.g. 'filename:README', 'extension:py'). "
        "Returns a list of matching file paths."
    )
    parameters = {
        "type": "object",
        "properties": {
            "owner": {"type": "string", "description": "Repository owner."},
            "repo": {"type": "string", "description": "Repository name."},
            "query": {"type": "string", "description": "Search query. Supports GitHub search qualifiers like 'extension:py' or 'filename:config'."},
        },
        "required": ["owner", "repo", "query"],
    }

    def run(self, **kwargs) -> str:
        username = cred_username_from_kwargs(kwargs)
        g, account = _get_github_client(kwargs)
        if not g:
            return _no_github_message()
        
        account_id = account.get("account_id") if account else None
        owner = (kwargs.get("owner") or "").strip()
        repo_name = (kwargs.get("repo") or "").strip()
        query = (kwargs.get("query") or "").strip()
        
        if not owner or not repo_name or not query:
            return "Missing required parameters: owner, repo, query."
        
        try:
            # Construct search query restricted to this repo
            full_query = f"{query} repo:{owner}/{repo_name}"
            files = g.search_code(query=full_query)
            
            lines = []
            # Code search is limited to 1000 results, we'll take top 30
            for i, f in enumerate(files):
                if i >= 30:
                    break
                lines.append(f"📄 {f.path}")
            
            log_github_activity(
                username, 
                "search_files", 
                f"Searched files in {owner}/{repo_name} with query: {query} (found {len(lines)})",
                account_id=account_id
            )
            return "\n".join(lines) if lines else f"No files found for query: {query}"
        except GithubException as e:
            err_msg = f"GitHub API error: {e.status} {e.data.get('message', str(e))}"
            log_github_activity(username, "search_files", f"Failed to search files in {owner}/{repo_name}", account_id=account_id, success=False, error=err_msg)
            return err_msg
        except Exception as e:
            logger.exception("github_search_files failed")
            log_github_activity(username, "search_files", f"Failed to search files in {owner}/{repo_name}", account_id=account_id, success=False, error=str(e))
            return f"Error searching files: {e}"


class GitHubGetTreeTool(BaseTool):
    """Get the full file tree of a GitHub repository (recursive)."""
    name = "github_get_tree"
    permission_level = "read"
    side_effect_class = "none"
    category = "github"
    description = (
        "Get a recursive list of all files in a GitHub repository. Use to get a bird's eye view of the project. "
        "Parameters: owner, repo, ref (branch/SHA), recursive (bool, default true). "
        "Returns a tree structure of paths."
    )
    parameters = {
        "type": "object",
        "properties": {
            "owner": {"type": "string", "description": "Repository owner."},
            "repo": {"type": "string", "description": "Repository name."},
            "ref": {"type": "string", "description": "Branch or commit SHA. Default is the main branch."},
            "recursive": {"type": "boolean", "description": "Whether to fetch the tree recursively. Default is true."},
        },
        "required": ["owner", "repo"],
    }

    def run(self, **kwargs) -> str:
        username = cred_username_from_kwargs(kwargs)
        g, account = _get_github_client(kwargs)
        if not g:
            return _no_github_message()
        
        account_id = account.get("account_id") if account else None
        owner = (kwargs.get("owner") or "").strip()
        repo_name = (kwargs.get("repo") or "").strip()
        ref = (kwargs.get("ref") or "").strip() or None
        recursive = kwargs.get("recursive", True)
        
        if not owner or not repo_name:
            return "Missing required parameters: owner, repo."
        
        try:
            repo = g.get_repo(f"{owner}/{repo_name}")
            
            # If ref is not provided, we need to get the default branch's tree
            if not ref:
                ref = repo.default_branch
            
            # get_git_tree needs a SHA, but we can pass a branch name
            tree = repo.get_git_tree(ref, recursive=recursive)
            
            lines = []
            for item in tree.tree:
                icon = "📁" if item.type == "tree" else "📄"
                lines.append(f"{icon} {item.path}")
                if len(lines) >= 300: # Safety limit for large repos
                    lines.append("... (limit reached)")
                    break
            
            log_github_activity(
                username, 
                "get_tree", 
                f"Fetched tree for {owner}/{repo_name} (ref={ref}, recursive={recursive})",
                account_id=account_id
            )
            return "\n".join(lines) if lines else "Tree is empty."
        except GithubException as e:
            err_msg = f"GitHub API error: {e.status} {e.data.get('message', str(e))}"
            log_github_activity(username, "get_tree", f"Failed to get tree for {owner}/{repo_name}", account_id=account_id, success=False, error=err_msg)
            return err_msg
        except Exception as e:
            logger.exception("github_get_tree failed")
            log_github_activity(username, "get_tree", f"Failed to get tree for {owner}/{repo_name}", account_id=account_id, success=False, error=str(e))
            return f"Error getting tree: {e}"


class GitHubListIssuesTool(BaseTool):
    """List issues for a GitHub repository."""
    name = "github_list_issues"
    permission_level = "read"
    side_effect_class = "none"
    category = "github"
    description = (
        "List issues for a GitHub repository. Use when the user asks about issues, bugs, or open items. "
        "Parameters: owner, repo, state (open|closed|all), per_page. Requires GitHub connected."
    )
    parameters = {
        "type": "object",
        "properties": {
            "owner": {"type": "string", "description": "Repository owner."},
            "repo": {"type": "string", "description": "Repository name."},
            "state": {"type": "string", "description": "open, closed, or all. Default: open."},
            "per_page": {"type": "integer", "description": "Max issues (default 20)."},
        },
        "required": ["owner", "repo"],
    }

    def run(self, **kwargs) -> str:
        username = cred_username_from_kwargs(kwargs)
        g, account = _get_github_client(kwargs)
        if not g:
            return _no_github_message()
        
        account_id = account.get("account_id") if account else None
        owner = (kwargs.get("owner") or "").strip()
        repo_name = (kwargs.get("repo") or "").strip()
        state = (kwargs.get("state") or "open").strip() or "open"
        per_page = min(100, max(1, int(kwargs.get("per_page") or 20)))
        
        if not owner or not repo_name:
            return "Missing required parameters: owner, repo."
        
        try:
            repo = g.get_repo(f"{owner}/{repo_name}")
            issues = repo.get_issues(state=state)
            
            lines = []
            count = 0
            for i in issues:
                if count >= per_page:
                    break
                if i.pull_request: # PyGithub returns PRs as issues too
                    continue
                lines.append(f"#{i.number} {i.title[:60]}")
                count += 1
            
            log_github_activity(
                username, 
                "list_issues", 
                f"Listed {len(lines)} issues for {owner}/{repo_name} (state={state})",
                account_id=account_id
            )
            return "\n".join(lines) if lines else f"No {state} issues found."
        except GithubException as e:
            err_msg = f"GitHub API error: {e.status} {e.data.get('message', str(e))}"
            log_github_activity(username, "list_issues", f"Failed to list issues for {owner}/{repo_name}", account_id=account_id, success=False, error=err_msg)
            return err_msg
        except Exception as e:
            logger.exception("github_list_issues failed")
            log_github_activity(username, "list_issues", f"Failed to list issues for {owner}/{repo_name}", account_id=account_id, success=False, error=str(e))
            return f"Error listing issues: {e}"


class GitHubListPullsTool(BaseTool):
    """List pull requests for a GitHub repository."""
    name = "github_list_pulls"
    permission_level = "read"
    side_effect_class = "none"
    category = "github"
    description = (
        "List pull requests for a GitHub repository. Use when the user asks about PRs, pull requests, or reviews. "
        "Parameters: owner, repo, state (open|closed|all), per_page. Requires GitHub connected."
    )
    parameters = {
        "type": "object",
        "properties": {
            "owner": {"type": "string", "description": "Repository owner."},
            "repo": {"type": "string", "description": "Repository name."},
            "state": {"type": "string", "description": "open, closed, or all. Default: open."},
            "per_page": {"type": "integer", "description": "Max PRs (default 20)."},
        },
        "required": ["owner", "repo"],
    }

    def run(self, **kwargs) -> str:
        username = cred_username_from_kwargs(kwargs)
        g, account = _get_github_client(kwargs)
        if not g:
            return _no_github_message()
        
        account_id = account.get("account_id") if account else None
        owner = (kwargs.get("owner") or "").strip()
        repo_name = (kwargs.get("repo") or "").strip()
        state = (kwargs.get("state") or "open").strip() or "open"
        per_page = min(100, max(1, int(kwargs.get("per_page") or 20)))
        
        if not owner or not repo_name:
            return "Missing required parameters: owner, repo."
        
        try:
            repo = g.get_repo(f"{owner}/{repo_name}")
            pulls = repo.get_pulls(state=state)
            
            lines = []
            count = 0
            for p in pulls:
                if count >= per_page:
                    break
                lines.append(f"#{p.number} {p.title[:60]} (from {p.head.ref})")
                count += 1
            
            log_github_activity(
                username, 
                "list_pulls", 
                f"Listed {len(lines)} pull requests for {owner}/{repo_name} (state={state})",
                account_id=account_id
            )
            return "\n".join(lines) if lines else f"No {state} pull requests found."
        except GithubException as e:
            err_msg = f"GitHub API error: {e.status} {e.data.get('message', str(e))}"
            log_github_activity(username, "list_pulls", f"Failed to list pulls for {owner}/{repo_name}", account_id=account_id, success=False, error=err_msg)
            return err_msg
        except Exception as e:
            logger.exception("github_list_pulls failed")
            log_github_activity(username, "list_pulls", f"Failed to list pulls for {owner}/{repo_name}", account_id=account_id, success=False, error=str(e))
            return f"Error listing pull requests: {e}"


class GitHubCreateIssueTool(BaseTool):
    """Create a new issue in a GitHub repository."""
    name = "github_create_issue"
    permission_level = "write"
    side_effect_class = "irreversible"
    category = "github"
    description = (
        "Create a new issue in a GitHub repository. Parameters: owner, repo, title, body (optional). "
        "Requires GitHub connected and WRITE permissions enabled in Settings."
    )
    parameters = {
        "type": "object",
        "properties": {
            "owner": {"type": "string", "description": "Repository owner."},
            "repo": {"type": "string", "description": "Repository name."},
            "title": {"type": "string", "description": "Issue title."},
            "body": {"type": "string", "description": "Issue description (markdown supported)."},
        },
        "required": ["owner", "repo", "title"],
    }

    def run(self, **kwargs) -> str:
        username = cred_username_from_kwargs(kwargs)
        g, account = _get_github_client(kwargs)
        if not g:
            return _no_github_message()
        
        account_id = account.get("account_id") if account else None
        if not account or not account.get("allow_write"):
            msg = "Action denied: This GitHub account is in READ-ONLY mode. Enable 'Write Access' in GitHub Dashboard to allow issue creation."
            log_github_activity(username, "create_issue", "Denied: Read-only mode", account_id=account_id, success=False, error=msg)
            return msg

        owner = (kwargs.get("owner") or "").strip()
        repo_name = (kwargs.get("repo") or "").strip()
        title = (kwargs.get("title") or "").strip()
        body = (kwargs.get("body") or "").strip() or None
        
        try:
            repo = g.get_repo(f"{owner}/{repo_name}")
            issue = repo.create_issue(title=title, body=body)
            log_github_activity(
                username, 
                "create_issue", 
                f"Created issue #{issue.number} in {owner}/{repo_name}: {title}",
                account_id=account_id
            )
            return f"Successfully created issue #{issue.number}: {issue.html_url}"
        except GithubException as e:
            err_msg = f"GitHub API error: {e.status} {e.data.get('message', str(e))}"
            log_github_activity(username, "create_issue", f"Failed to create issue in {owner}/{repo_name}", account_id=account_id, success=False, error=err_msg)
            return err_msg
        except Exception as e:
            logger.exception("github_create_issue failed")
            log_github_activity(username, "create_issue", f"Failed to create issue in {owner}/{repo_name}", account_id=account_id, success=False, error=str(e))
            return f"Error creating issue: {e}"


class GitHubUpdateFileTool(BaseTool):
    """Create or update a file in a GitHub repository."""
    name = "github_update_file"
    permission_level = "write"
    side_effect_class = "irreversible"
    category = "github"
    description = (
        "Create or update a file in a GitHub repository (commit). Parameters: owner, repo, path, content, message (commit message), branch (optional). "
        "Requires GitHub connected and WRITE permissions enabled in Settings."
    )
    parameters = {
        "type": "object",
        "properties": {
            "owner": {"type": "string", "description": "Repository owner."},
            "repo": {"type": "string", "description": "Repository name."},
            "path": {"type": "string", "description": "File path in repo."},
            "content": {"type": "string", "description": "New file content."},
            "message": {"type": "string", "description": "Commit message."},
            "branch": {"type": "string", "description": "Branch to commit to. Omit for default branch."},
        },
        "required": ["owner", "repo", "path", "content", "message"],
    }

    def run(self, **kwargs) -> str:
        username = cred_username_from_kwargs(kwargs)
        g, account = _get_github_client(kwargs)
        if not g:
            return _no_github_message()
        
        account_id = account.get("account_id") if account else None
        if not account or not account.get("allow_write"):
            msg = "Action denied: This GitHub account is in READ-ONLY mode. Enable 'Write Access' in GitHub Dashboard to allow file updates."
            log_github_activity(username, "update_file", "Denied: Read-only mode", account_id=account_id, success=False, error=msg)
            return msg

        owner = (kwargs.get("owner") or "").strip()
        repo_name = (kwargs.get("repo") or "").strip()
        path = (kwargs.get("path") or "").strip()
        content = kwargs.get("content")
        message = (kwargs.get("message") or "Update from VAF agent").strip()
        from vaf.tools.project_git import apply_coauthor_trailer
        message = apply_coauthor_trailer(message)
        branch = (kwargs.get("branch") or "").strip() or None
        
        try:
            repo = g.get_repo(f"{owner}/{repo_name}")
            sha = None
            try:
                # Try to get existing file to get its SHA for update
                contents = repo.get_contents(path, ref=branch)
                if isinstance(contents, list):
                    return f"Error: '{path}' is a directory."
                sha = contents.sha
            except GithubException as e:
                if e.status != 404:
                    raise

            if sha:
                # Update existing file
                res = repo.update_file(path, message, content, sha, branch=branch)
                log_github_activity(
                    username, 
                    "update_file", 
                    f"Updated file {path} in {owner}/{repo_name} (branch={branch or 'default'})",
                    account_id=account_id
                )
                return f"Successfully updated {path}. Commit: {res['commit'].sha[:7]}"
            else:
                # Create new file
                res = repo.create_file(path, message, content, branch=branch)
                log_github_activity(
                    username, 
                    "update_file", 
                    f"Created file {path} in {owner}/{repo_name} (branch={branch or 'default'})",
                    account_id=account_id
                )
                return f"Successfully created {path}. Commit: {res['commit'].sha[:7]}"
                
        except GithubException as e:
            err_msg = f"GitHub API error: {e.status} {e.data.get('message', str(e))}"
            log_github_activity(username, "update_file", f"Failed to update {path} in {owner}/{repo_name}", account_id=account_id, success=False, error=err_msg)
            return err_msg
        except Exception as e:
            logger.exception("github_update_file failed")
            log_github_activity(username, "update_file", f"Failed to update {path} in {owner}/{repo_name}", account_id=account_id, success=False, error=str(e))
            return f"Error updating file: {e}"


class GitHubGetFileStructureTool(BaseTool):
    """Analyze the top-level structure of a code file (classes, functions, methods with line numbers)."""
    name = "github_get_file_structure"
    permission_level = "read"
    side_effect_class = "none"
    category = "github"
    description = (
        "Analyze the structure of a code file and return its classes, functions, and methods with line numbers. "
        "Use BEFORE reading a large file to know which section to request with start_line/end_line. "
        "Supports Python (AST-based, precise) and JS/TS/other (regex). "
        "Parameters: owner, repo, path, ref (optional)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "owner": {"type": "string", "description": "Repository owner."},
            "repo":  {"type": "string", "description": "Repository name."},
            "path":  {"type": "string", "description": "Path to the file (e.g. src/main.py)."},
            "ref":   {"type": "string", "description": "Branch, tag, or commit SHA. Omit for default branch."},
        },
        "required": ["owner", "repo", "path"],
    }

    def _parse_python(self, content: str) -> list:
        import ast as _ast
        try:
            tree = _ast.parse(content)
        except SyntaxError as exc:
            return [{"type": "error", "message": f"Python parse error: {exc}"}]
        items = []
        for node in tree.body:
            if isinstance(node, _ast.ClassDef):
                methods = []
                for child in node.body:
                    if isinstance(child, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                        methods.append({
                            "name": child.name,
                            "start": child.lineno,
                            "end": getattr(child, "end_lineno", "?"),
                            "async": isinstance(child, _ast.AsyncFunctionDef),
                        })
                items.append({
                    "type": "class",
                    "name": node.name,
                    "start": node.lineno,
                    "end": getattr(node, "end_lineno", "?"),
                    "methods": methods,
                })
            elif isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                items.append({
                    "type": "function",
                    "name": node.name,
                    "start": node.lineno,
                    "end": getattr(node, "end_lineno", "?"),
                    "async": isinstance(node, _ast.AsyncFunctionDef),
                })
        return items

    def _parse_generic(self, content: str) -> list:
        import re
        lines = content.splitlines()
        items: list = []
        CLASS_RE = re.compile(r"^(?:export\s+)?(?:abstract\s+)?class\s+(\w+)")
        FUNC_RE  = re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)")
        ARROW_RE = re.compile(r"^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(")
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            m = CLASS_RE.match(stripped)
            if m:
                items.append({"type": "class", "name": m.group(1), "start": i, "methods": []})
                continue
            m = FUNC_RE.match(stripped) or ARROW_RE.match(stripped)
            if m:
                items.append({"type": "function", "name": m.group(1), "start": i})
        return items

    def run(self, **kwargs) -> str:
        username   = cred_username_from_kwargs(kwargs)
        g, account = _get_github_client(kwargs)
        if not g:
            return _no_github_message()

        account_id = account.get("account_id") if account else None
        owner      = (kwargs.get("owner") or "").strip()
        repo_name  = (kwargs.get("repo") or "").strip()
        path       = (kwargs.get("path") or "").strip()
        ref        = (kwargs.get("ref") or "").strip() or None

        if not owner or not repo_name or not path:
            return "Missing required parameters: owner, repo, path."

        try:
            repo         = g.get_repo(f"{owner}/{repo_name}")
            content_file = repo.get_contents(path) if not ref else repo.get_contents(path, ref=ref)

            if isinstance(content_file, list):
                return f"'{path}' is a directory. Use github_list_directory instead."

            raw = content_file.decoded_content
            if raw is None:
                download_url = getattr(content_file, "download_url", None)
                if download_url:
                    import urllib.request
                    with urllib.request.urlopen(download_url, timeout=15) as resp:  # noqa: S310
                        raw = resp.read()
                else:
                    return f"File '{path}' is too large to analyze (>1 MB) and no download URL available."

            content     = raw.decode("utf-8", errors="replace")
            total_lines = len(content.splitlines())
            size_kb     = len(raw) / 1024

            ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
            if ext == "py":
                items       = self._parse_python(content)
                parser_used = "Python AST"
            else:
                items       = self._parse_generic(content)
                parser_used = "regex"

            log_github_activity(
                username, "get_file_structure",
                f"Analyzed structure: {owner}/{repo_name}/{path} ({total_lines} lines, {len(items)} items)",
                account_id=account_id,
            )

            out = [
                f"📄 {path}  |  {total_lines} lines  |  {size_kb:.1f} KB  |  parser: {parser_used}",
                "─" * 60,
            ]

            if not items:
                out.append("No top-level classes or functions found.")
            elif items and items[0].get("type") == "error":
                out.append(f"⚠️  {items[0]['message']}")
            else:
                for item in items:
                    kind  = item["type"]
                    name  = item["name"]
                    start = item.get("start", "?")
                    end   = item.get("end", "?")
                    if kind == "class":
                        out.append(f"🔷 class {name}  (lines {start}–{end})")
                        for m in item.get("methods", []):
                            prefix = "async def" if m.get("async") else "def"
                            out.append(f"   ├─ {prefix} {m['name']}  (lines {m['start']}–{m['end']})")
                    else:
                        prefix = "async def" if item.get("async") else "def"
                        out.append(f"🔹 {prefix} {name}  (lines {start}–{end})")

            out.extend([
                "─" * 60,
                f'💡 To read a section: github_get_file(owner="{owner}", repo="{repo_name}", path="{path}", start_line=X, end_line=Y)',
            ])
            return "\n".join(out)

        except GithubException as e:
            if e.status == 404:
                return f"File or repository not found: {owner}/{repo_name}/{path}"
            data    = getattr(e, "data", {}) or {}
            api_msg = data.get("message") or str(e)
            return f"GitHub API error {e.status}: {api_msg}"
        except Exception as e:
            logger.exception("github_get_file_structure failed")
            log_github_activity(username, "get_file_structure", f"Failed: {owner}/{repo_name}/{path}", account_id=account_id, success=False, error=str(e))
            return f"Error analyzing file structure: {e}"


# Module load confirmation (visible in server logs on startup)
logger.info("GitHub tools module loaded (PyGithub available: %s)", _PYGITHUB_AVAILABLE)
