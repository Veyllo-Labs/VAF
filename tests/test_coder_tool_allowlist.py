# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The coding agent's tool allow-list, and the place it is applied.

The coder used to discover its tools by walking vaf/tools/ and instantiating every
BaseTool subclass, minus three excluded by name. A captured live request carried 130
tools: 11 for mail, 20 for messengers, 9 for calendars and contacts. OpenAI refuses
more than 128 functions per request, so that request came back as

    Invalid 'tools': array too long. Expected an array with maximum length 128,
    but got an array with length 130 instead.

on the first loop of every OpenAI coder run. These tests pin the whitelist that
replaced the blacklist, and, just as importantly, WHERE it is applied: the first
attempt filtered the context schema only, which the ~24 hand-appended tools and the
127 auto-discovered ones simply bypassed, and the captured request was still 130.
"""
import ast
import pathlib

import pytest

from vaf.core.coder_tools import (
    CODER_ALLOWED_TOOLS,
    CODER_REQUIRED_TOOLS,
    resolve_coder_tools,
)

CODER_PY = pathlib.Path(__file__).resolve().parents[1] / "vaf" / "tools" / "coder.py"
_SOURCE = CODER_PY.read_bytes().decode("utf-8")

# OpenAI's hard ceiling, measured: 128 -> 200, 129 -> 400 "array too long".
OPENAI_MAX_FUNCTIONS = 128


def _tool_names_the_coder_builds() -> set:
    """Every name the coder writes into a tool schema literal of its own."""
    names = set()
    for node in ast.walk(ast.parse(_SOURCE)):
        if not isinstance(node, ast.Dict):
            continue
        keys = [k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        if keys[:1] != ["type"] or "function" not in keys:
            continue
        for k, v in zip(node.keys, node.values):
            if not (isinstance(k, ast.Constant) and k.value == "function"):
                continue
            if not isinstance(v, ast.Dict):
                continue
            for kk, vv in zip(v.keys, v.values):
                if (isinstance(kk, ast.Constant) and kk.value == "name"
                        and isinstance(vv, ast.Constant)):
                    names.add(vv.value)
    return names


# ── The list must not starve the coder ────────────────────────────────────────

def test_every_tool_the_coder_builds_is_allowed():
    """A tool the coder advertises but the list drops is a tool it can never call."""
    built = _tool_names_the_coder_builds()
    assert built, "no tool schemas found in coder.py - the AST scan needs updating"
    missing = sorted(built - CODER_ALLOWED_TOOLS)
    assert not missing, (
        f"the coder builds these into its schema but the allow-list drops them: {missing}. "
        "Add them to vaf/core/coder_tools.py or stop advertising them."
    )


def test_the_required_tools_are_in_the_list():
    assert CODER_REQUIRED_TOOLS <= CODER_ALLOWED_TOOLS


@pytest.mark.parametrize("tool", ["set_todos", "write_file", "edit_file", "read_file",
                                  "bash", "python_sandbox", "linter", "codesearch",
                                  "run_tests", "git_add_commit"])
def test_the_tools_a_build_actually_needs_are_present(tool):
    assert tool in CODER_ALLOWED_TOOLS


# ── The list must not hand a build step the outside world ─────────────────────

@pytest.mark.parametrize("marker", ["mail", "whatsapp", "telegram", "discord", "slack",
                                    "calendar", "contact", "timer", "automation"])
def test_outward_facing_families_stay_out(marker):
    """These are what made the list 130 long, and none of them builds anything."""
    leaked = sorted(t for t in CODER_ALLOWED_TOOLS if marker in t)
    assert not leaked, f"outward-facing tools in the coder allow-list: {leaked}"


def test_github_writing_tools_stay_out_while_reading_is_allowed():
    # Reading a repository is research; writing to one is an outward-facing act a
    # build step must not perform on its own.
    assert "github_get_file" in CODER_ALLOWED_TOOLS
    assert "github_update_file" not in CODER_ALLOWED_TOOLS
    assert "github_create_issue" not in CODER_ALLOWED_TOOLS


def test_the_list_leaves_room_under_the_provider_ceiling():
    assert len(CODER_ALLOWED_TOOLS) < OPENAI_MAX_FUNCTIONS


# ── Configurable, without being able to break the coder ───────────────────────

def test_default_is_the_builtin_list():
    assert resolve_coder_tools({}) == CODER_ALLOWED_TOOLS


def test_allowlist_replaces_and_extra_adds():
    replaced = resolve_coder_tools({"coder_tool_allowlist": "read_file, bash"})
    assert "bash" in replaced
    assert "git_log" not in replaced, "a non-empty allowlist must REPLACE, not extend"

    extended = resolve_coder_tools({"coder_tool_allowlist_extra": "send_mail"})
    assert "send_mail" in extended
    assert CODER_ALLOWED_TOOLS <= extended


def test_an_override_can_never_remove_what_the_run_needs():
    """A typo must degrade to fewer optional tools, not to a coder that cannot write."""
    crippled = resolve_coder_tools({"coder_tool_allowlist": "read_file"})
    assert CODER_REQUIRED_TOOLS <= crippled


def test_config_value_accepts_commas_whitespace_and_lists():
    for raw in ("a,b", "a b", "a,\nb", ["a", "b"]):
        got = resolve_coder_tools({"coder_tool_allowlist": raw})
        assert {"a", "b"} <= got


# ── Registered as real, admin-only config keys ────────────────────────────────

def test_both_keys_are_registered_and_admin_only():
    from vaf.core.config import Config
    for key in ("coder_tool_allowlist", "coder_tool_allowlist_extra"):
        assert key in Config.DEFAULTS, f"{key} missing from Config.DEFAULTS"
        assert key in Config.GLOBAL_CONFIG_KEYS, (
            f"{key} must be admin-only: it decides which tools a build step may reach for"
        )


def test_both_keys_are_documented():
    schema = (pathlib.Path(__file__).resolve().parents[1]
              / "docs" / "setup" / "CONFIG_SCHEMA.md").read_bytes().decode("utf-8")
    for key in ("coder_tool_allowlist", "coder_tool_allowlist_extra"):
        assert f"`{key}`" in schema, f"{key} has no row in CONFIG_SCHEMA.md"


# ── WHERE it is applied: after the appends, not before them ───────────────────
# The stage above is worthless if the wiring filters the wrong list. The first
# attempt at this change filtered only the context schema and the captured request
# was unchanged at 130 tools, because ~24 hand-appended and 127 auto-discovered
# tools are added afterwards. This pins the order.

def test_the_allowlist_is_applied_after_the_schema_is_complete():
    extend_at = _SOURCE.index("tools_schema.extend(plug_and_play_tools)")
    filter_at = _SOURCE.index("_apply_tool_allowlists(_dedupe_tools_schema(")
    assert extend_at < filter_at, (
        "the allow-list must be applied AFTER the auto-discovered tools are added, "
        "or they bypass it entirely"
    )


def test_the_coder_resolves_the_allowlist_once_per_run():
    assert "from vaf.core.coder_tools import resolve_coder_tools" in _SOURCE
    assert "_coder_allowed = resolve_coder_tools()" in _SOURCE


def test_browser_agent_is_allowed_for_interactive_page_verification():
    """render_check is one look with no clicking; its own description defers
    multi-step flows to browser_agent. Without it, a built page is verified for
    "does it render" but never for "does it work" (forms, navigation, games)."""
    assert "browser_agent" in CODER_ALLOWED_TOOLS


def test_browser_agent_is_advertised_in_task_contexts():
    """The plug-and-play copy is main-context-only, but pages are written (and must
    be tested) in TASK contexts, so the task branch carries its own schema entry."""
    task_branch = _SOURCE.index('"name": "render_check"')
    next_main_marker = _SOURCE.index("if is_main_context:", task_branch)
    assert '"name": "browser_agent"' in _SOURCE[task_branch:next_main_marker], (
        "browser_agent's schema entry must sit in the task-context branch beside "
        "render_check, or interactive verification is unavailable while building"
    )
