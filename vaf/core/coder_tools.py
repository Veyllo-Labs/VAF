# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The coding agent's tool allow-list: WHICH tools the coder is offered.

This is a WHITELIST, and it exists so the coder is handed the tools it can
actually build with, and nothing else. Read it as the answer to "what is a
coding agent allowed to reach for": files, code, the project's git history,
shell, tests, and looking things up. Everything that acts on the user's behalf
in the outside world stays out, with one named exception: `browser_agent`, kept
for interactively verifying built pages (see its entry below).

WHY A WHITELIST AND NOT A BLACKLIST. The coder used to discover its tools by
walking `vaf/tools/` and instantiating every `BaseTool` subclass it found,
excluding three by name. Every tool added anywhere in the product therefore
landed in the coder's request automatically, and the list grew to 130: 11 mail
tools, 20 messenger tools, 9 calendar and contact tools. A blacklist has to be
updated for every tool the product ever gains, which is the same as saying it
is never up to date; a whitelist has to be updated only when the CODER gains
something, which is a decision somebody is making anyway.

It was also a live failure, not just untidy. OpenAI refuses more than 128
functions per request, so the 130-tool array came back as
`Invalid 'tools': array too long. Expected an array with maximum length 128,
but got an array with length 130 instead.` - a 400 on the very first loop, on
every OpenAI coder run. Veyllo, DeepSeek and the local server enforce no such
limit, which is why it stayed invisible until the provider changed.

CONFIGURABLE. `coder_tool_allowlist` REPLACES this set (empty = use it as
written); `coder_tool_allowlist_extra` is always ADDED to whichever set is in
force. Both take a comma-separated list of tool names, and both are admin-only:
which tools a coding agent may call is a capability decision, not a per-user
preference. See `docs/setup/CONFIG_SCHEMA.md`.

The names here are the ones a tool reports as `BaseTool.name`. A name that no
longer exists is harmless: the caller intersects with the tools it actually
built, exactly as the front-office list does.
"""
from typing import Iterable, Optional, Set

# ── The coder's own control loop ──────────────────────────────────────────────
# Without these the run cannot plan, finish or write anything. They are the
# reason a caller must never let a config override empty the list out.
_LOOP = {
    "set_todos",        # the plan; the run forces this call before anything else
    "task_done",        # marks one task finished and advances the loop
    "ask_user",         # a blocking question when the task is ambiguous
    "request_clarification",
}

# ── Files and code: the actual work ───────────────────────────────────────────
_FILES = {
    "write_file",       # create, or rewrite an existing file whole
    "edit_file",        # surgical search/replace in an existing file
    "read_file",
    "list_files",
    "find_files",
    "codesearch",       # search the project's code
    "tree",
    "move_file",
    "folder_size",
    "report_filename",  # name the deliverable back to the caller
}

# ── Running and checking what was written ─────────────────────────────────────
_EXECUTE = {
    "bash",             # workspace shell
    "host_bash",
    "python_sandbox",
    "python_exec",
    "linter",
    "run_tests",
    "render_check",     # ONE look at the rendered page: errors, console, text
    # The interactive half of that verify loop: click, fill forms, walk a
    # multi-page flow on the page the coder just built. render_check's own
    # description defers anything beyond a single look to browser_agent, so
    # keeping one without the other leaves built pages tested for "does it
    # render" but never for "does it work". It CAN also drive arbitrary
    # websites - accepted deliberately for the verify capability; an instance
    # that wants it out sets `coder_tool_allowlist` without it.
    "browser_agent",
    "repair_report",
}

# ── The project's own history ─────────────────────────────────────────────────
# Workspace-scoped: these act on the project the coder was pointed at.
_PROJECT = {
    "git_init",
    "git_add_commit",
    "git_status",
    "git_log",
    "set_git_coauthor",
    "project_history",
    "project_rollback",
}

# ── Looking things up ─────────────────────────────────────────────────────────
# Reading, never writing. GitHub's WRITING tools (`github_create_issue`,
# `github_update_file`) are deliberately absent: they change somebody else's
# repository, which is an outward-facing act and not something a build step
# should be able to do on its own.
_RESEARCH = {
    "web_search",
    "web_fetch",
    "webfetch",
    "web_deep_search",
    "analyze_image",    # a design mockup handed in as an image
    "memory_search",
    "list_tools",
    "search_tools",
    "mcp_call",         # one name, however many MCP servers are connected
    "use_skill",
    "list_skills",
    "read_skill",
    "github_get_file",
    "github_get_file_structure",
    "github_get_tree",
    "github_list_directory",
    "github_search_files",
    "github_list_repos",
    "github_list_issues",
    "github_list_pulls",
}

# ── What the run learned, written back ────────────────────────────────────────
_LEARN = {
    "add_memory",       # workspace-scoped by a wrapper the coder installs
    "update_codex",
}

CODER_ALLOWED_TOOLS = frozenset(_LOOP | _FILES | _EXECUTE | _PROJECT | _RESEARCH | _LEARN)

# The subset a config override must never be able to remove. Dropping any of
# these does not restrict the coder, it breaks it: a run that cannot call
# set_todos never starts, and one that cannot write_file produces nothing.
CODER_REQUIRED_TOOLS = frozenset(_LOOP | {"write_file", "read_file", "edit_file", "list_files"})


def _split(raw) -> Set[str]:
    """Tool names from a config value: comma or whitespace separated, or a list."""
    if not raw:
        return set()
    if isinstance(raw, (list, tuple, set, frozenset)):
        items: Iterable = raw
    else:
        items = str(raw).replace("\n", ",").replace(" ", ",").split(",")
    return {str(x).strip() for x in items if str(x).strip()}


def resolve_coder_tools(config: Optional[dict] = None) -> frozenset:
    """The allow-list actually in force, after the two config keys.

    `coder_tool_allowlist` replaces the built-in set; `coder_tool_allowlist_extra`
    is added on top of whichever set is in force. The required names are unioned
    back in unconditionally, so a typo in an override degrades to "fewer optional
    tools" rather than to a coder that cannot write a file.
    """
    cfg = config or {}

    def _get(key: str):
        if key in cfg:
            return cfg.get(key)
        from vaf.core.config import Config
        return Config.get(key, "")

    base = _split(_get("coder_tool_allowlist")) or set(CODER_ALLOWED_TOOLS)
    return frozenset(base | _split(_get("coder_tool_allowlist_extra")) | CODER_REQUIRED_TOOLS)
