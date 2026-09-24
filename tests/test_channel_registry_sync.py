# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The channel registry is the only list of channels (CLAUDE Rule 2: CI guard over prose).

`vaf/core/channels.py` declares each messaging channel once. This file used to hold the
copies of that list against each other, about forty-five of them across thirty files;
two had drifted before it existed (the persona route and the Front Office prompt each
missed a platform), and the copies that decided which tools a chat may run failed OPEN:
four tools blocked "on telegram, whatsapp and discord" would have run on a fourth
channel. Now the copies read the registry, and the tests below pin three things: that
they do, that a channel added to the registry is blocked by every restricted tool from
the first day, and that no hand-written channel list grows back.
"""
import ast
import importlib
import re
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from vaf.core import channels as reg
from vaf.core.channels import (
    ALL_SEND_TOOLS,
    CHANNEL_SEND_TOOLS,
    CHAT_CHANNELS,
    CHAT_SEND_TOOLS,
    FRONT_OFFICE_MESSENGERS,
    KNOWN_CHANNELS,
    MAIN_MESSENGERS,
)

REPO = Path(__file__).resolve().parent.parent


def _src(module) -> str:
    return Path(module.__file__).read_text(encoding="utf-8")


# ── the registry itself ──────────────────────────────────────────────────────

def test_the_registry_is_consistent_in_itself():
    names = [c.name for c in reg.CHANNELS]
    assert len(names) == len(set(names)), "a channel is declared twice"
    assert set(CHAT_CHANNELS) <= set(KNOWN_CHANNELS)
    assert set(FRONT_OFFICE_MESSENGERS) <= set(CHAT_CHANNELS), "a Front Office channel needs a bridge"
    assert MAIN_MESSENGERS == CHAT_CHANNELS, "a main messenger is a channel VAF can deliver to"
    for c in reg.CHANNELS:
        assert c.send_tool == f"send_{c.name}"
    assert set(CHAT_SEND_TOOLS) <= set(ALL_SEND_TOOLS) == set(CHANNEL_SEND_TOOLS.values())


def test_the_registry_imports_nothing_from_vaf():
    """Tool classes read it at class-definition time and the dependency-free ingress policy
    reads it too; one VAF import here and the first import cycle is a matter of time."""
    tree = ast.parse(_src(reg))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("vaf"), node.module
        if isinstance(node, ast.Import):
            assert not any(a.name.startswith("vaf") for a in node.names)


def test_the_old_names_are_the_registrys_own_objects():
    import vaf.core.messaging_connections as mc
    assert mc.KNOWN_CHANNELS is KNOWN_CHANNELS
    assert mc.ROUTABLE_CHANNELS is CHAT_CHANNELS
    assert mc.CHANNEL_SEND_TOOLS is CHANNEL_SEND_TOOLS


def test_router_dispatches_every_chat_channel():
    import vaf.core.messaging_connections as mc
    src = _src(mc)
    for ch in CHAT_CHANNELS:
        assert f'main == "{ch}"' in src, f"send_to_main_messenger lost the dispatch branch for {ch}"


# ── who may be a main messenger ──────────────────────────────────────────────

def test_every_main_messenger_check_reads_the_registry():
    import vaf.api.user_persona_routes as pr
    import vaf.auth.user_workspace as uw
    from vaf.tools.user_identity import UpdateUserIdentityTool
    assert "MAIN_MESSENGERS as VALID_MAIN_MESSENGERS" in _src(uw)
    assert "MAIN_MESSENGERS as valid_main_messengers" in _src(pr)
    enum = UpdateUserIdentityTool.parameters["properties"]["main_messenger"]["enum"]
    assert tuple(enum) == MAIN_MESSENGERS


def test_a_channel_without_a_bridge_is_not_a_main_messenger():
    """Slack is known and has no bridge: picking it used to be accepted and then delivered
    to the web UI while the profile said Slack."""
    assert "slack" in KNOWN_CHANNELS and "slack" not in MAIN_MESSENGERS


@pytest.mark.parametrize("stored,read", [("slack", None), ("signal", None), ("email", None),
                                         ("WhatsApp", "WhatsApp"), ("telegram", "telegram")])
def test_a_stored_main_messenger_outside_the_registry_reads_as_not_set(tmp_path, monkeypatch, stored, read):
    """A profile saved while Slack was offered keeps saying Slack on disk; on read it heals
    to "not set", so the agent asks once instead of the setting naming one channel while
    delivery goes to another."""
    import json
    from vaf.auth.user_workspace import UserWorkspace
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)
    ws = UserWorkspace("alice")
    ws.ensure_exists()
    data = json.loads(ws.user_identity_file.read_text(encoding="utf-8"))
    data["main_messenger"] = stored
    ws.user_identity_file.write_text(json.dumps(data), encoding="utf-8")
    assert ws.get_user_identity()["main_messenger"] == read


def test_the_settings_picker_offers_exactly_the_main_messengers():
    src = (REPO / "web" / "components" / "SettingsModal.tsx").read_text(encoding="utf-8")
    block = src.split("const MAIN_MESSENGERS: { id: string; label: string }[] = [", 1)[1].split("];", 1)[0]
    assert tuple(re.findall(r"id: '([a-z]+)'", block)) == MAIN_MESSENGERS
    picker = src.split("tModals('userIdentity.notSet')", 1)[1].split("</select>", 1)[0]
    assert "MAIN_MESSENGERS.map(" in picker and '<option value="' not in picker, \
        "the picker renders the list, it does not repeat it"


# ── send tools ───────────────────────────────────────────────────────────────

def test_the_send_tool_sets_hold_every_channel_send_tool():
    from vaf.core.automation import _SEND_STEP_TOOLS
    from vaf.core.front_office_tools import FRONT_OFFICE_ALLOWED_TOOLS
    from vaf.core.thinking_mode import _SENT_TOOLS
    import vaf.core.agent as agent_mod
    every = set(ALL_SEND_TOOLS) | {"send_to_user", "send_mail"}
    assert every <= _SENT_TOOLS, "an unstripped send tool is an untracked outbound channel in background runs"
    assert every <= _SEND_STEP_TOOLS, "automation double-delivery dedup"
    # Per-platform send tools are the contact-to-OWNER back-channel; the channel-agnostic
    # send_to_user stays out by design (default deny).
    assert set(ALL_SEND_TOOLS) <= FRONT_OFFICE_ALLOWED_TOOLS and "send_to_user" not in FRONT_OFFICE_ALLOWED_TOOLS
    assert agent_mod._OWNER_SEND_TOOLS == CHAT_SEND_TOOLS + ("send_to_user",), \
        "the owner back-channel is every send tool that can reach somebody"


def test_the_agent_and_the_engine_inject_scope_into_every_send_tool():
    import vaf.core.agent as agent_mod
    import vaf.workflows.engine as engine_mod
    assert "CHANNEL_SEND_TOOLS.get(ch)" in _src(agent_mod), "the channel-to-tool map is the registry's"
    assert "if name in ALL_SEND_TOOLS or name == \"send_to_user\":" in _src(agent_mod)
    assert "elif tool_name in ALL_SEND_TOOLS or tool_name == \"send_to_user\":" in _src(engine_mod), \
        "workflow engine scope injection (cross-user leak risk)"


# ── ingress and chat sources (these fail OPEN for a channel they do not know) ──

def test_ingress_and_chat_sources_are_the_registrys():
    import vaf.core.channel_ingress_policy as ip
    import vaf.core.tool_dispatch as td
    from vaf.core.config import Config
    assert ip._SUPPORTED_CHANNELS == CHAT_CHANNELS
    assert ip.MESSENGER_FRONT_OFFICE_CHANNELS == FRONT_OFFICE_MESSENGERS
    assert td.CHANNEL_SOURCES == frozenset(CHAT_CHANNELS)
    assert td.CHANNEL_SESSION_PREFIXES == tuple(f"{c}_" for c in CHAT_CHANNELS)
    closed = {"mode": "inherit", "open_to_new_senders": False}
    for policy in (ip._default_policy(), Config.DEFAULTS["channel_ingress_policy"]):
        for ch in CHAT_CHANNELS:
            assert policy[ch] == closed, f"{ch} starts with a closed door"


_RESTRICTED_TOOLS = (
    ("vaf.tools.agent_tool_builder", "AgentToolBuilderTool"),
    ("vaf.tools.agent_workflow_builder", "AgentWorkflowBuilderTool"),
    ("vaf.tools.browser_agent", "BrowserAgentTool"),
    ("vaf.tools.render_check", "RenderCheckTool"),
    ("vaf.tools.timer", "SetTimerTool"),
    ("vaf.tools.host_bash", "HostBashTool"),
    ("vaf.tools.python_exec", "PythonExecTool"),
)


@pytest.mark.parametrize("module,cls_name", _RESTRICTED_TOOLS)
def test_a_channel_added_to_the_registry_is_blocked_from_the_first_day(module, cls_name):
    """The measurement this rule was written on: with the tool names listed by hand, four
    of these seven ran on a fourth channel. The `"channel"` sentinel blocks whatever the
    registry calls a chat channel, so adding a row is enough. `channel_tools_unrestricted`
    is switched off here because it lifts the policy block for every channel alike."""
    import vaf.core.tool_dispatch as td
    from vaf.core.config import Config
    from vaf.core.tool_contract import evaluate_tool_policy
    cls = getattr(importlib.import_module(module), cls_name)
    real_get = Config.get
    with mock.patch.object(Config, "get", side_effect=lambda k, d=None: False if k == "channel_tools_unrestricted" else real_get(k, d)), \
            mock.patch.object(td, "CHANNEL_SOURCES", frozenset(CHAT_CHANNELS) | {"newchat"}):
        for src in CHAT_CHANNELS + ("newchat",):
            decision = evaluate_tool_policy(cls.name, cls(), src, td.is_channel_session(src, None))
            assert decision.blocked, f"{cls.name} runs on {src}"
        if not getattr(cls, "admin_only", False):
            assert not evaluate_tool_policy(cls.name, cls(), "web", td.is_channel_session("web", None)).blocked


def test_no_tool_blocks_chat_channels_by_name():
    """A tool that lists chat channels by name is open on the next one; ("channel",) is the
    spelling that covers them all (docs/EMBEDDING.md)."""
    offenders = []
    for path in sorted((REPO / "vaf" / "tools").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                target = node.targets[0] if isinstance(node, ast.Assign) else node.target
                if getattr(target, "id", None) != "channel_restrictions" or node.value is None:
                    continue
                if not isinstance(node.value, (ast.Tuple, ast.List)):
                    continue
                names = {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
                if names & set(KNOWN_CHANNELS):
                    offenders.append(f"{path.relative_to(REPO).as_posix()}:{node.lineno} {sorted(names)}")
    assert not offenders, "write channel_restrictions = (\"channel\",):\n  " + "\n  ".join(offenders)


# ── no hand-written channel list grows back ──────────────────────────────────

# The lists that name channels on purpose, each with the reason it is not the registry's.
_NAMED_EXCEPTIONS = {
    ("vaf/core/contacts_store.py", "CHANNEL_TYPES"):
        "contact-book address types: a phone and an email are addresses without a channel, "
        "and each messenger's address has a format of its own to parse",
    ("vaf/core/contacts_store.py", "labels"):
        "the contact block reads a WhatsApp value as the person's phone number, a wording "
        "no channel label carries",
    ("vaf/core/contacts_store.py", "out"):
        "contact_endpoints builds each channel's store keys with that channel's own parser "
        "(a WhatsApp number plus its @lid jids, a numeric Telegram id, a Discord user id)",
    ("vaf/core/inbox.py", "_GROUP_LIKE"):
        "each messenger's group id shape as a LIKE pattern, pinned against is_group's code",
    ("vaf/core/tool_contract.py", "TOOL_CATEGORIES"):
        "the vocabulary of tool bundles, an open field: a channel's tools are a bundle "
        "once they exist, whether or not the channel has a bridge",
    ("vaf/core/tool_contract.py", "CATEGORY_LABELS"): "the display names of that vocabulary",
    ("vaf/whare_wananga/preconditions.py", "_CONNECTION_CHECKS"):
        "one configured-check FUNCTION per channel, which is code rather than a list",
    ("vaf/api/security_routes.py", "collect_channels_status()"):
        "the security perimeter counts each channel's paired endpoints from that channel's own "
        "config shape (two Telegram whitelists, WhatsApp phone numbers, a verified Discord "
        "admin); a new channel adds its own count here (CONNECTIONS.md, Channel model)",
}
_CHANNEL_WORDS = set(KNOWN_CHANNELS)
_TOOL_WORDS = set(ALL_SEND_TOOLS) | {f"read_{c}_chat" for c in KNOWN_CHANNELS}
# A chat session id starts with its channel's name and "_": a list of those prefixes is the
# same copy in another spelling (a review found four, the web routing guard among them).
_PREFIX_WORDS = {f"{c}_" for c in KNOWN_CHANNELS}


def _owner(stack):
    for node in reversed(stack):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if isinstance(target, ast.Name):
                return target.id
            if isinstance(target, ast.Attribute):
                return target.attr
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return node.name + "()"
    return "<module>"


def _hand_written_channel_lists():
    out = subprocess.run(["git", "ls-files", "-z", "vaf/"], cwd=REPO, capture_output=True,
                         check=True).stdout.decode("utf-8", "ignore")
    found = []
    for rel in out.split("\0"):
        if not rel.endswith(".py") or "node_modules" in rel or not (REPO / rel).is_file():
            continue
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        stack = []

        def visit(node):
            stack.append(node)
            if isinstance(node, (ast.Tuple, ast.List, ast.Set, ast.Dict)):
                elts = node.keys if isinstance(node, ast.Dict) else node.elts
                # One level down as well: a list of pairs, (("telegram_", "Telegram"), ...),
                # names one channel per pair and is still a list of every channel.
                flat = []
                for e in elts:
                    flat.extend(e.elts if isinstance(e, (ast.Tuple, ast.List)) else [e])
                words = {e.value for e in flat if isinstance(e, ast.Constant) and isinstance(e.value, str)}
                if (len(words & _CHANNEL_WORDS) >= 2 or len(words & _TOOL_WORDS) >= 2
                        or len(words & _PREFIX_WORDS) >= 2):
                    found.append((rel, _owner(stack[:-1]), node.lineno))
            for child in ast.iter_child_nodes(node):
                visit(child)
            stack.pop()

        visit(tree)
    return found


def test_no_hand_written_channel_list_outside_the_registry():
    """Two or more channel names (or their send and read tools, or their session-id
    prefixes, also as pairs) in one literal is a copy of the registry. Read vaf/core/channels.py instead; a list that has to name channels on
    purpose goes into _NAMED_EXCEPTIONS with its reason."""
    offenders = [f"{rel}:{line} ({owner})" for rel, owner, line in _hand_written_channel_lists()
                 if (rel, owner) not in _NAMED_EXCEPTIONS]
    assert not offenders, "hand-written channel lists:\n  " + "\n  ".join(offenders)


def test_every_named_exception_still_exists():
    """An exception whose list is gone is a blanket waiting for the next copy."""
    live = {(rel, owner) for rel, owner, _ in _hand_written_channel_lists()}
    stale = [k for k in _NAMED_EXCEPTIONS if k not in live]
    assert not stale, f"drop these exceptions, their lists are gone: {stale}"
