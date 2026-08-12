# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Loop protection speaks as the framework, not as the user (vaf/core/agent.py).

The hard stop (tool-turn budget) and the soft reminder used to append
`{"role": "user", "content": "[System: ..."}` into the history. Everything
downstream reads that role literally:

- `vaf/framework.py` copies every non-system message into the session store, so
  the note was PERSISTED as human input,
- session titles and `cross_chat` take the first user message,
- `session.has_user_interaction` decides GC by role == "user",
- context compression overwrites the tracked user intent from the last user
  message, and the memory lane counts user turns to place its cutoff,
- the web UI renders it as a human chat bubble on reload.

The code even compensated for its own pollution by skipping messages that
start with "[System" when it scans back for the user's goal. That workaround
stays for old sessions; new ones must not need it.

Both injections sit AFTER the tool-execution loop, so this is a role-semantics
fix and not a tool-call adjacency one (Rule 4.1 is untouched).
"""
import ast
from pathlib import Path

import pytest

SOURCE = Path("vaf/core/agent.py").read_text(encoding="utf-8")


def _appends_in_chat_step():
    """Every `self.history.append({...})` literal inside chat_step, as (role, content_src)."""
    tree = ast.parse(SOURCE)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "chat_step")
    out = []
    for call in (n for n in ast.walk(fn) if isinstance(n, ast.Call)):
        func = call.func
        if not (isinstance(func, ast.Attribute) and func.attr == "append"):
            continue
        target = ast.unparse(func.value)
        if target != "self.history" or not call.args:
            continue
        arg = call.args[0]
        if not isinstance(arg, ast.Dict):
            continue
        role = content = None
        for k, v in zip(arg.keys, arg.values):
            key = ast.unparse(k).strip("'\"") if k is not None else ""
            if key == "role":
                role = ast.unparse(v).strip("'\"")
            elif key == "content":
                content = ast.unparse(v)
        out.append((role, content or ""))
    return out


def test_the_loop_protection_notes_are_not_stored_as_user_input():
    """A framework nudge with a role of 'user' is indistinguishable from what
    the human typed, in the session store and in every heuristic that reads it."""
    offenders = [
        content for role, content in _appends_in_chat_step()
        if role == "user" and ("hard_stop" in content or "_reminder" in content)
    ]
    assert offenders == [], f"loop protection appends as the user again: {offenders}"


def test_the_remaining_synthetic_user_injections_are_the_measured_gap_list():
    """Same defect class, deliberately NOT changed in the loop-protection fix.

    The two sub-agent nudges below still append as the user. They are named
    here rather than hidden, because a gap the guard knows about is a decision
    and a gap it does not know about is an accident. Whoever converts them
    shortens this list; a THIRD entry appearing means a new one was written.
    """
    synthetic = sorted(
        content[:60] for role, content in _appends_in_chat_step()
        if role == "user" and "System:" in content
    )
    assert len(synthetic) == 2, (
        "the synthetic role:user injections changed - update this list "
        f"deliberately: {synthetic}"
    )
    assert all("sub-agent" in c.lower() or "sub_agent" in c.lower() or "Sub-Agent" in c
               for c in synthetic), f"an unexpected synthetic user message: {synthetic}"


def test_both_limits_still_inject_something():
    """The counter-proof: the roles are right because the notes exist, not
    because they were deleted."""
    appended = _appends_in_chat_step()
    systems = [c for r, c in appended if r == "system"]
    assert any("hard_stop" in c for c in systems), "the hard stop stopped injecting"
    assert any("_reminder" in c for c in systems), "the soft reminder stopped injecting"


def test_the_session_copy_would_have_persisted_the_old_shape():
    """Pins WHY this matters: framework.py skips system and keeps everything
    else, so the old role made the note durable."""
    fw = Path("vaf/framework.py").read_text(encoding="utf-8")
    assert 'if role == "system":' in fw and "continue" in fw, \
        "the session copy no longer skips system messages - re-check this fix"


@pytest.mark.parametrize("marker", ["[System: HARD STOP", "[System: You have already made"])
def test_the_notes_still_read_as_framework_text(marker):
    """The wording is what the model acts on; only the role changed."""
    assert marker in SOURCE, f"the loop-protection text changed: {marker}"
