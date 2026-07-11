# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Trust & Capability Gating

Minimal "trusted folders" + user decisions for risky actions:
- once
- always
- cancel

Design goals:
- OS-independent (Platform.config_dir)
- No hardcoded paths
- Safe defaults (ask)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from vaf.core.platform import Platform

Decision = Literal["allow_once", "allow_always", "cancel"]


RISKY_TOOLS = {
    # Filesystem moves (write_file is deliberately NOT here: main-agent writes are
    # workspace-anchored + per-user jailed and gate via the plan gate instead,
    # consistent with document_writer which writes the same workspace unprompted)
    "move_file",
    # Shell execution tools (if present)
    "bash",
    "run_command",
    # Host Python execution (outside sandbox)
    "python_exec",
}


@dataclass
class TrustState:
    trusted_dirs: set[str]
    tool_policies: dict[str, str]  # tool_name -> "allow" | "ask"


def _trust_file() -> Path:
    return Platform.config_dir() / "trust.json"


def load_trust_state() -> TrustState:
    path = _trust_file()
    if not path.exists():
        return TrustState(trusted_dirs=set(), tool_policies={})
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        trusted_dirs = set(data.get("trusted_dirs", []))
        tool_policies = dict(data.get("tool_policies", {}))
        return TrustState(trusted_dirs=trusted_dirs, tool_policies=tool_policies)
    except Exception:
        return TrustState(trusted_dirs=set(), tool_policies={})


def save_trust_state(state: TrustState) -> None:
    path = _trust_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        # str() defensively: trusted_dirs must be JSON-serializable strings. A Path here
        # (e.g. from a helper that returns Path) would make json.dumps raise and silently
        # break "allow always" for every dangerous tool.
        "trusted_dirs": sorted(str(d) for d in state.trusted_dirs),
        "tool_policies": state.tool_policies,
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _norm_dir(p: Path) -> str:
    # Normalize via Platform helper if available. Platform.normalize_path returns a Path,
    # so str() the result - trusted_dirs must hold strings (see save_trust_state).
    try:
        return str(Platform.normalize_path(str(p.resolve())))
    except Exception:
        return str(p.resolve())


def is_trusted_dir(cwd: Path) -> bool:
    state = load_trust_state()
    cur = cwd.resolve()
    while True:
        if _norm_dir(cur) in state.trusted_dirs:
            return True
        if cur.parent == cur:
            return False
        cur = cur.parent


def mark_trusted_dir(cwd: Path) -> None:
    state = load_trust_state()
    state.trusted_dirs.add(_norm_dir(cwd))
    save_trust_state(state)


def set_tool_policy(tool_name: str, policy: Literal["allow", "deny", "ask"]) -> None:
    state = load_trust_state()
    # We intentionally do NOT persist "deny" (use cancel instead)
    if policy == "deny":
        policy = "ask"
    state.tool_policies[tool_name] = policy
    save_trust_state(state)


def get_tool_policy(tool_name: str) -> str:
    state = load_trust_state()
    return state.tool_policies.get(tool_name, "ask")


def should_gate_tool(tool_name: str) -> bool:
    return tool_name in RISKY_TOOLS


def explain_gate(tool_name: str) -> str:
    if tool_name in {"move_file"}:
        return "This action modifies files on disk."
    if tool_name in {"bash", "run_command"}:
        return "This action runs shell commands on your machine."
    return "This action is considered risky."


