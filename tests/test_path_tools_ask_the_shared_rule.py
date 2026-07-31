# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Every tool that takes a local path asks the shared rule - or is named here with a reason.

THIS GUARD COVERS ONE OF THREE AXES. Read that before concluding anything from a green run.
The round that produced it found problems on three separate axes, and only the first is
mechanically countable:

  1. PATH     does a tool that takes a filesystem path ask `is_safe_path`?   <- this file
  2. IDENTITY on whose behalf is it acting?  No guard exists. The source itself is empty:
              of 3178 stored sessions, 3172 carry a `user_scope_id` and 24 carry a
              `username`, so the dispatcher's `username or "admin"` fallback resolves to the
              machine owner for almost every session. A guard here would report nearly
              everything as red today, and that would be the honest answer.
  3. OWNERSHIP whose session, whose resource?  No guard exists. Three WebSocket commands take
              a client-supplied `sessionId` without an ownership check, and so does the
              endpoint behind the workflow report viewer.

So a green run here means "no tool reads a path without asking", not "path handling is
safe" and certainly not "isolation is done". The unconditional exposures found in this round
sit on axes 2 and 3.

WHY A FROZEN SET RATHER THAN A LIST IN A PLANNING DOCUMENT. The same lane was counted three
times from a viewpoint and was wrong three times, always in the flattering direction: "three
messengers" (there are four senders), "the librarian" (one of its two dispatch paths), "the
unjailed half of the write surface" (it was a third). A list in a document is read when
somebody goes looking. A red test is read when somebody breaks something, which is the only
moment that finds the case a year from now.

WHAT THIS FILE CAN SEE, measured rather than asserted. It reads tool classes under
`vaf/tools/`, and that boundary falls differently on the two halves of the question:

  ENTRY POINTS   31 of 31. Every tool class in the tree that declares a path-shaped parameter
                 lives in `vaf/tools/`; there are zero elsewhere. Nothing enters unseen.
  DECISIONS      1 of 4. Where containment is actually decided is mostly NOT in reach:
                   `is_safe_path`        vaf/tools/filesystem.py          <- seen
                   `_safe_join`          vaf/core/thinking_workspace.py   <- not seen
                   automation runner     vaf/core/automation.py           <- not seen (no check)
                   `/api/file` allowlist vaf/core/web_server.py           <- not seen

That asymmetry cuts BOTH ways, and it is why `CONTAINED_ELSEWHERE` exists as its own set. The
guard cannot see a hole that opens after the path leaves a tool - `create_automation` hands a
model-written `output_path` to a runner that `mkdir`s it with no check at all - and it cannot
see a containment that lives outside either, which is why the two thinking-workspace tools look
unguarded here while `_safe_join` demonstrably contains them. Both entries had to be decided by
measuring the receiving end, not by reading this file.

HOW IT WORKS. Every tool class declaring a path-shaped parameter must reach `is_safe_path`,
either in its own module or through a resolver it imports (`send_discord` and `send_to_user`
import `send_telegram._resolve_path`, so counting occurrences per file reports 0 for them and
is simply wrong). Anything that does not is listed in `UNGUARDED` with a reason. That set may
only SHRINK: a tool that starts asking must be removed from it, and a new tool that does not
ask fails immediately.
"""
import ast
import pathlib
import re

import pytest

TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1] / "vaf" / "tools"

# Parameter names that look like a filesystem path.
_PATH_PARAM = re.compile(
    r"^(path|file_path|filepath|src|dst|dest|directory|folder|dir|filename|source|target|"
    r"output_path|input_path|local_path)s?$", re.I)

# Parameters that LOOK like a path and are not one. Named individually, because "it is
# probably a remote path" is exactly the kind of assumption this file exists to replace.
NOT_A_LOCAL_PATH = {
    ("find_mail", "folder"): "an IMAP mailbox name, not a directory",
    ("label_mail", "folder"): "an IMAP mailbox name",
    ("mail_inbox", "folder"): "an IMAP mailbox name",
    ("mark_mail_answered", "folder"): "an IMAP mailbox name",
    ("read_mail", "folder"): "an IMAP mailbox name",
    ("github_get_file", "path"): "a path inside a GitHub repository, resolved by the API",
    ("github_get_file_structure", "path"): "a path inside a GitHub repository",
    ("github_list_directory", "path"): "a path inside a GitHub repository",
    ("github_update_file", "path"): "a path inside a GitHub repository",
}

# Tools that take a local path and never reach a containment decision. Every entry is a known
# GAP with a reason, not a permission. THIS SET MAY ONLY SHRINK.
UNGUARDED = {
    "codesearch":
        "resolves a model-supplied path and walks it with rglob('*'). No jail, no declared "
        "identity. Found by the census on 2026-07-30, ranked, not yet fixed.",
    "linter":
        "resolves a model-supplied path and runs an external linter over it via subprocess. "
        "Found by the census on 2026-07-30, ranked, not yet fixed.",
    "create_automation":
        "MEASURED 2026-07-30, and worse than 'hands it on': the tool itself never opens the "
        "path, but the runner does - vaf/core/automation.py does expanduser() and then "
        "mkdir(parents=True) on it, and that whole file contains is_safe_path, user_jail and "
        "compute_user_jail zero times. The value is model-written ('e.g. Documents, Desktop'). "
        "What is NOT yet measured is what gets written into the directory afterwards, which is "
        "what decides the severity.",
    "update_automation":
        "Same path, same runner, same absence of any check - it edits the value that "
        "create_automation stored, so both feed one unguarded write site.",
}

# Tools that do NOT ask `is_safe_path` and are nevertheless contained, by a different mechanism
# that was MEASURED rather than assumed. Separate from UNGUARDED on purpose: a list that mixes
# "known hole" with "safe by another route" makes its own count meaningless, and the count is
# the only thing that turns this lane from a search space into a finite set.
CONTAINED_ELSEWHERE = {
    "document_writer":
        "contained by TWO mechanisms, neither of them is_safe_path, and both are needed. "
        "Its `filename` is a NAME, so a path-shaped value is refused outright (an absolute "
        "one used to swallow the base directory in the join); and `file_access = 'write'` "
        "gives it the per-user boundary, which is what stops a well-formed name from "
        "landing in another tenant's tree. Closed 2026-07-31 by DECLARING rather than "
        "hand-building - the first consumer of that primitive.",
    "thinking_workspace_read":
        "contained by `_safe_join` (vaf/core/thinking_workspace.py), verified by running it: "
        "'/etc/passwd' and '../../../.ssh/id_rsa' raise ValueError('Path escapes workspace "
        "boundary'), 'sub/ok.txt' resolves inside. The base is keyed per scope AND per task.",
    "thinking_workspace_write":
        "same `_safe_join`, same measurement. It writes rather than reads, which is why it was "
        "ranked above its sibling while both were still unmeasured - the measurement cleared "
        "both.",
}

def _tool_classes():
    """(tool_name, module_stem, [path-shaped params]) for every tool in vaf/tools."""
    out = []
    for f in sorted(TOOLS_DIR.glob("*.py")):
        if f.name.startswith("_"):
            continue
        try:
            tree = ast.parse(f.read_bytes().decode())
        except SyntaxError:                       # pragma: no cover - not our concern here
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            name = params = None
            for body in node.body:
                if (isinstance(body, ast.Assign) and body.targets
                        and isinstance(body.targets[0], ast.Name)):
                    try:
                        value = ast.literal_eval(body.value)
                    except Exception:
                        continue
                    if body.targets[0].id == "name":
                        name = value
                    elif body.targets[0].id == "parameters":
                        params = value
            if not name or not isinstance(params, dict):
                continue
            props = params.get("properties") if params.get("type") == "object" else params
            hits = [k for k in (props or {}) if _PATH_PARAM.match(k)]
            if hits:
                out.append((name, f.stem, hits))
    return out


def _asks_the_shared_rule(stem: str) -> bool:
    """Own check, or through a resolver imported from another vaf module that has one.

    The delegation half is not politeness: `send_discord` imports the resolver from
    `send_telegram`, so `grep -c is_safe_path send_discord.py` returns 0 while the check runs.
    A guard that counted per file would report a fixed tool as broken and, worse, would teach
    the next reader that per-file counting is how you measure this.
    """
    src = (TOOLS_DIR / f"{stem}.py").read_bytes().decode()
    if "is_safe_path" in src:
        return True
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if not node.module.startswith("vaf."):
            continue
        target = pathlib.Path(*node.module.split(".")).with_suffix(".py")
        if not target.exists() or "is_safe_path" not in target.read_bytes().decode():
            continue
        # Only counts if a name imported from it is actually CALLED here.
        if any(re.search(rf"\b{re.escape(a.name)}\s*\(", src) for a in node.names):
            return True
    return False


def _local_path_tools():
    for name, stem, params in _tool_classes():
        real = [p for p in params if (name, p) not in NOT_A_LOCAL_PATH]
        if real:
            yield name, stem, real


# ── the guard ────────────────────────────────────────────────────────────────

def test_every_local_path_tool_asks_or_is_named_here():
    """A new tool that takes a path and never asks is a red test on the day it is written,
    instead of an audit finding a year later."""
    missing = {name: params for name, stem, params in _local_path_tools()
               if not _asks_the_shared_rule(stem) and name not in UNGUARDED and name not in CONTAINED_ELSEWHERE}
    assert not missing, (
        f"tool(s) take a local path without asking is_safe_path: {missing}. Either route the "
        "path through the shared rule, or add an entry to UNGUARDED explaining why not - and "
        "if the parameter is not a local path at all, name it in NOT_A_LOCAL_PATH instead."
    )


def test_the_unguarded_set_only_shrinks():
    """A name that stays in the list after being fixed is how a frozen set rots into a stale
    document. This is the half that makes it a ratchet rather than a snapshot."""
    stems = {name: stem for name, stem, _ in _local_path_tools()}
    listed = {**UNGUARDED, **CONTAINED_ELSEWHERE}
    fixed = [n for n in listed if n in stems and _asks_the_shared_rule(stems[n])]
    assert not fixed, (
        f"{fixed} now ask(s) is_safe_path - remove the entry from UNGUARDED so the set keeps "
        "measuring what is actually left"
    )
    gone = [n for n in listed if n not in stems]
    assert not gone, (
        f"{gone} no longer exist(s) or no longer declare(s) a path parameter; remove the "
        "stale entry rather than leaving the count wrong"
    )


@pytest.mark.parametrize("entry", sorted({**UNGUARDED, **CONTAINED_ELSEWHERE}))
def test_each_exception_carries_a_reason(entry):
    """A bare name would turn this into a permission list. The reason is what a later reader
    needs in order to decide whether the exception still holds."""
    reasons = {**UNGUARDED, **CONTAINED_ELSEWHERE}
    assert len(reasons[entry]) > 40, f"{entry} is listed without a usable reason"


def test_the_exclusions_are_justified_individually():
    """Same rule for the other direction: "probably remote" is the assumption this file
    replaces."""
    for key, reason in NOT_A_LOCAL_PATH.items():
        assert len(reason) > 15, f"{key} is excluded without a reason"


# ── what the numbers were when this was frozen ───────────────────────────────

def test_the_shape_of_the_lane_is_still_what_was_measured():
    """Not a count for its own sake: if these move a lot, the census that bounded this lane
    was taken against a different codebase and its conclusions need re-reading.

    Measured 2026-07-30: 31 tools declare a path-shaped parameter, 9 of those parameters are
    not local paths, leaving 22 - of which 15 ask (13 directly, 2 through the shared
    resolver) and 7 do not.
    """
    all_params = _tool_classes()
    local = list(_local_path_tools())
    asks = [n for n, stem, _ in local if _asks_the_shared_rule(stem)]

    assert len(all_params) >= 25, "far fewer path-shaped tools than when this was measured"
    assert len(local) - len(asks) == len(UNGUARDED) + len(CONTAINED_ELSEWHERE), (
        f"{len(local) - len(asks)} tools do not ask, but {len(UNGUARDED)} gaps plus "
        f"{len(CONTAINED_ELSEWHERE)} contained-elsewhere are listed - the sets and the code "
        "disagree"
    )
