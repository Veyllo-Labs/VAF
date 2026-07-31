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
              `username`, so the dispatcher's nameless-caller fallback decides the name for
              almost every session. That fallback used to be the literal "admin" and now
              resolves the CONFIGURED owner - the two differ on every installation whose
              owner did not register under that name, and the sentence here asserted the
              benign reading of it (see test_identity_kwargs_declaration.py). A guard on this
              axis would report nearly everything as red today, and that would be the honest
              answer.
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

def _module_constants(tree):
    """Module-level literal assignments, so `name = TOOL_NAME` can be resolved.

    This lookup is not a nicety. Without it `ast.literal_eval` raises on the Name node, the
    class is skipped in silence, and the tool is simply absent from the guard - which is
    what happened: `cloud_storage` declares `file_path`, its module contains no
    `is_safe_path`, and the set below reported 31 tools while there were 32. A detector
    that drops what it cannot parse reads exactly like a detector that found nothing wrong.
    """
    consts = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and node.targets
                and isinstance(node.targets[0], ast.Name)):
            try:
                consts[node.targets[0].id] = ast.literal_eval(node.value)
            except Exception:
                pass
    return consts


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
        consts = _module_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            name = params = None
            for body in node.body:
                if not (isinstance(body, ast.Assign) and body.targets
                        and isinstance(body.targets[0], ast.Name)):
                    continue
                try:
                    value = ast.literal_eval(body.value)
                except Exception:
                    # A module-level constant, the shape that used to vanish here.
                    if isinstance(body.value, ast.Name) and body.value.id in consts:
                        value = consts[body.value.id]
                    else:
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


def _tool_classes_at_runtime():
    """The same question asked of the imported classes, as a check on the parser.

    Attribution is by `__module__`, not by which file the name was found in: half a dozen
    modules re-export each other's tools, and counting a class where it was imported made
    `cloud_storage` look like it lived in `librarian.py` - which DOES contain `is_safe_path`,
    so the first version of this cross-check cleared the exact tool it was written to catch.
    """
    import importlib

    from vaf.tools.base import BaseTool

    out = {}
    for f in sorted(TOOLS_DIR.glob("*.py")):
        if f.name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"vaf.tools.{f.stem}")
        except Exception:
            continue
        for obj in vars(module).values():
            if not (isinstance(obj, type) and issubclass(obj, BaseTool) and obj is not BaseTool):
                continue
            if obj.__module__ != f"vaf.tools.{f.stem}":
                continue
            name, params = getattr(obj, "name", None), getattr(obj, "parameters", None)
            if not name or not isinstance(params, dict):
                continue
            props = params.get("properties") if params.get("type") == "object" else params
            if any(_PATH_PARAM.match(k) for k in (props or {})):
                out[name] = f.stem
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
        # The imported name must be a FUNCTION in that module whose body reaches
        # `is_safe_path`, and it must be called here. "Any name from a module that happens
        # to contain the string" cleared `cloud_storage`, which imports `LibrarianTool` for
        # the document viewer and checks no path of its own - an acquittal on a coincidence,
        # in the direction where a false clean bill costs the most.
        for alias in node.names:
            if not re.search(rf"\b{re.escape(alias.name)}\s*\(", src):
                continue
            if _is_path_resolving_function(target, alias.name):
                return True
    return False


def _is_path_resolving_function(module_path, symbol):
    """Is `symbol` a module-level function in `module_path` that reaches `is_safe_path`?"""
    try:
        tree = ast.parse(module_path.read_bytes().decode())
    except SyntaxError:                           # pragma: no cover
        return False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol:
            return "is_safe_path" in ast.dump(node) or any(
                isinstance(n, ast.Name) and n.id == "is_safe_path" for n in ast.walk(node)
            ) or "is_safe_path" in ast.unparse(node)
    return False


def _local_path_tools():
    for name, stem, params in _tool_classes():
        real = [p for p in params if (name, p) not in NOT_A_LOCAL_PATH]
        if real:
            yield name, stem, real


# ── the guard ────────────────────────────────────────────────────────────────

def test_the_detector_sees_every_tool_the_runtime_does():
    """THE GUARD ON THE GUARD, and the reason this file was quietly incomplete.

    Everything else here measures tools. Nothing measured the thing that FINDS the tools, so
    when the parser silently dropped a class whose `name`/`parameters` came from module
    constants, the set shrank by one and every assertion stayed green. A frozen set is only
    as honest as its collector, and a collector that skips what it cannot parse fails in the
    reassuring direction - the same asymmetry as a gate with no assertion on the refusing
    side.
    """
    static = {name for name, _, _ in _tool_classes()}
    runtime = _tool_classes_at_runtime()
    invisible = sorted(set(runtime) - static)
    assert not invisible, (
        f"the parser cannot see tool(s) that declare a path-shaped parameter: "
        f"{[(n, runtime[n]) for n in invisible]}. They are absent from every assertion in "
        f"this file, so their absence reads as a clean bill of health."
    )


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
