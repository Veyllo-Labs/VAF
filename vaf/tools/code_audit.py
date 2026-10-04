# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""code_audit: the main agent reviews a project's code change and reports what it found.

It only reads. The findings go to the person with the question whether the coding agent
should fix them, and the code changes only after a yes - then through `coding_agent`, with
the audit's fix prompt as the task. Inside the coding agent the same review runs with the
coder's own model, after every commit of its loop (vaf/tools/coder.py); this module is the
main agent's door to it. The engine is `vaf.code_audit` (docs/agents/CODE_AUDIT.md).
"""
from typing import Any

from vaf.tools.base import BaseTool

# What the person is asked before anything is changed: the model is told in the result,
# where it reads it, rather than in a prompt section every turn pays for.
_ASK_FIRST = (
    "\n\nNEXT STEP: show the person these findings in your own words (severity, file, what "
    "is wrong) and ASK whether the coding agent should fix them. Change nothing before they "
    "say yes. On a yes, call coding_agent with project_path={path!r} and the fix prompt "
    "below as the task, unchanged.\n\n--- fix prompt ---\n{prompt}"
)

# The result is handed over whole (result_is_deliverable), so it bounds itself: the fix
# prompt carries findings up to this size, the rest are named by title - the coder's own
# audit after its fix finds them again.
_PROMPT_CHARS = 20_000


class CodeAuditTool(BaseTool):
    name = "code_audit"
    category = "code"
    permission_level = "read"
    side_effect_class = "none"
    # session_id: Stop in this chat ends the review before its next model call.
    identity_kwargs = ("user_scope_id", "user_role", "session_id")
    # What the engine reads stays inside this account's folders: the project path is
    # checked against the jail before the engine starts, and the engine itself only reads
    # inside the repository (no symlink out, no file git does not track).
    file_access = "read"
    # The fix prompt is handed to coding_agent "unchanged": the chat's 2,000-character result
    # cap would cut it in the middle, so the whole result is returned, bounded by render().
    result_is_deliverable = True
    # A review of a large change is a handful of model calls of up to a few minutes each
    # (a reasoning model thinks for about two minutes over one batch).
    timeout_seconds = 1200
    description = (
        "Review a project's code change like a code reviewer: finds real bugs, security "
        "problems and risky changes in what was changed (against the last pushed state, or "
        "only committed / only uncommitted work, or whole files), proves each finding "
        "against the code and gives a fix prompt. Read-only: use it when the person asks to "
        "review, audit or check code (\"prüf den Code\", \"review my changes\"); to then "
        "change code, ask them first and use coding_agent."
    )
    input_examples = [
        {},
        {"project_path": "/home/user/Documents/VAF_Projects/shop", "scope": "uncommitted"},
        {"scope": "files", "paths": ["src/payment.py"], "profile": "assertive"},
    ]
    parameters = {
        "type": "object",
        "properties": {
            "project_path": {
                "type": "string",
                "description": "Absolute path to the project (a git repository). Defaults to "
                               "this chat's project.",
            },
            "scope": {
                "type": "string",
                "enum": ["changes", "committed", "uncommitted", "files"],
                "description": "changes (default): everything since the last pushed state or "
                               "the previous commit; committed / uncommitted: only that part; "
                               "files: whole files instead of a change.",
            },
            "base": {
                "type": "string",
                "description": "Optional: compare against this branch or commit.",
            },
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional: only these files or folders (relative to the project).",
            },
            "profile": {
                "type": "string",
                "enum": ["chill", "assertive"],
                "description": "chill (default): bugs, security, what matters; assertive: "
                               "also style and small things.",
            },
        },
        "required": [],
    }

    def run(self, **kwargs: Any) -> str:
        from vaf.core.code_audit import (PROFILES, SCOPES, ask_via_complete, code_audit,
                                         parallel_for)
        from vaf.tools.filesystem import jail_allows
        from vaf.tools.project_git import _resolve_project

        path, err = _resolve_project(kwargs.get("project_path") or kwargs.get("base_dir", ""))
        if err:
            return f"Error: {err}"
        if not jail_allows(path, user_scope_id=kwargs.get("user_scope_id"),
                           user_role=kwargs.get("user_role"), mode="read"):
            return f"Error: {path} is outside the folders this account may read."
        scope = kwargs.get("scope") or "changes"
        if scope not in SCOPES:
            return f"Error: scope is one of {', '.join(SCOPES)}."
        profile = kwargs.get("profile") or "chill"
        if profile not in PROFILES:
            profile = "chill"
        paths = kwargs.get("paths") or None
        if isinstance(paths, str):
            paths = [paths]

        session_id = kwargs.get("session_id") or ""

        def _stopped() -> bool:
            if not session_id:
                return False
            from vaf.core.task_queue import TaskQueue
            return TaskQueue().should_stop(session_id)

        report = code_audit(path, scope=scope, base=(kwargs.get("base") or None), paths=paths,
                            profile=profile, ask=ask_via_complete(caller="tool:code_audit"),
                            max_files=40, parallel=parallel_for(), should_stop=_stopped)
        return self.render(report, path)

    @staticmethod
    def render(report, path: str) -> str:
        """The report for the main agent, compact and bounded: status, walkthrough, one line
        per finding, then - only when there is something to fix - the question to ask and
        the fix prompt to hand over."""
        head = f"Code audit of {path}: {report.status.upper()}"
        if report.status_reason:
            head += f" - {report.status_reason}"
        lines = [head + f" ({len(report.files_reviewed)} file(s) reviewed, scope {report.scope})."]
        if report.summary:
            lines.append("Summary: " + report.summary.strip())
        counts = f"{len(report.findings)} verified finding(s)"
        if report.unverified:
            counts += f", {len(report.unverified)} unverified (not proven, not to be fixed)"
        lines.append(counts + ":" if report.findings else counts + ".")
        for n, f in enumerate(report.findings, 1):
            lines.append(f"{n}. [{f.severity.upper()}] {f.where()} - {f.title}")
        if report.files_skipped:
            lines.append(f"Not reviewed: {len(report.files_skipped)} file(s), e.g. "
                         + ", ".join(f"{p} ({r})" for p, r in report.files_skipped[:5]))
        prompt = report.fix_prompt(max_chars=_PROMPT_CHARS)
        if not prompt:
            return "\n".join(lines)
        return "\n".join(lines) + _ASK_FIRST.format(path=path, prompt=prompt)
