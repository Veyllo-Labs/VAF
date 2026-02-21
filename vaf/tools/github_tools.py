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
    """Return first GitHub account for this user from config (account_id, login, allow_write)."""
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
    """Get the content of a file from a GitHub repository."""
    name = "github_get_file"
    category = "github"
    description = (
        "Get the content of a file from a GitHub repository. Use when the user wants to read code or a file from their GitHub repo. "
        "Requires GitHub connected. Parameters: owner (repo owner), repo (repo name), path (file path in repo), ref (branch/tag/commit, default: default branch)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "owner": {"type": "string", "description": "Repository owner (username or org)."},
            "repo": {"type": "string", "description": "Repository name."},
            "path": {"type": "string", "description": "Path to the file in the repo (e.g. README.md, src/main.py)."},
            "ref": {"type": "string", "description": "Branch, tag, or commit SHA. Omit for default branch."},
        },
        "required": ["owner", "repo", "path"],
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
        
        if not owner or not repo_name or not path:
            return "Missing required parameters: owner, repo, path."
        
        try:
            repo = g.get_repo(f"{owner}/{repo_name}")
            # ref can be passed to get_contents
            content_file = repo.get_contents(path, ref=ref)
            
            if isinstance(content_file, list):
                msg = f"Path '{path}' is a directory containing {len(content_file)} items."
                log_github_activity(username, "get_file", f"Attempted to read directory: {owner}/{repo_name}/{path}", account_id=account_id)
                return f"{msg} Please specify a file path."
            
            content = content_file.decoded_content.decode("utf-8", errors="replace")
            log_github_activity(
                username, 
                "get_file", 
                f"Read file: {owner}/{repo_name}/{path} (ref={ref or 'default'})",
                account_id=account_id
            )
            return content
        except GithubException as e:
            if e.status == 404:
                err_msg = f"File or repository not found: {owner}/{repo_name}/{path}"
                log_github_activity(username, "get_file", f"File not found: {owner}/{repo_name}/{path}", account_id=account_id, success=False, error=err_msg)
                return err_msg
            err_msg = f"GitHub API error: {e.status} {e.data.get('message', str(e))}"
            log_github_activity(username, "get_file", f"Failed to read file: {owner}/{repo_name}/{path}", account_id=account_id, success=False, error=err_msg)
            return err_msg
        except Exception as e:
            logger.exception("github_get_file failed")
            log_github_activity(username, "get_file", f"Failed to read file: {owner}/{repo_name}/{path}", account_id=account_id, success=False, error=str(e))
            return f"Error reading file: {e}"


class GitHubListIssuesTool(BaseTool):
    """List issues for a GitHub repository."""
    name = "github_list_issues"
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


# Module load confirmation (visible in server logs on startup)
logger.info("GitHub tools module loaded (PyGithub available: %s)", _PYGITHUB_AVAILABLE)
