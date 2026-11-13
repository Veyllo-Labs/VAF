# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The file lanes refuse a network share or a device path while it is still text
(`vaf.is_remote_path`, `vaf/core/path_jail.py`): asked first by `_allowed_file_path` in
`vaf/core/web_server.py`, by the file tools' screen `is_safe_path`, and by the two tools that touch
their path before that screen (`codesearch`, `download_file`). The coding agent's lanes too:
`git_add_commit`'s files, the folder of the git and project notes tools, and the loop's own
checks on a file tool's path.

On Windows, `_allowed_file_path` used to resolve the caller's path before comparing it with the
served roots, and resolving `\\\\host\\share\\x` opens that share: an SMB connection to a foreign
host carrying the Windows account's NTLM login attempt. A Markdown picture
`/api/file?path=%5C%5Chost%5Cs%5Cx.png` in a reply was enough to trigger it. On Linux and macOS
the same text is a harmless path, so no local run could fail; the Windows reading is simulated
here with `ntpath` on every host, which is how Windows itself splits these spellings.

MUTATIONS, each turns the named test red:
- `return norm.startswith("//")` only (drop the `\\??\\` case)
  -> test_every_windows_remote_spelling_is_refused;
- drop the `.replace("\\\\", "/")` in `is_remote_path`
  -> test_every_windows_remote_spelling_is_refused;
- move the `is_remote_path` check below `Platform.normalize_path` in `_allowed_file_path`
  -> test_the_file_lane_refuses_before_it_resolves, test_the_check_comes_before_any_resolve;
- a new `Platform.normalize_path(` call anywhere else in `vaf/`
  -> test_normalize_path_has_no_new_caller;
- drop the `is_remote_path` check at the top of `is_safe_path`
  -> test_the_file_tools_refuse_before_they_touch_the_disk;
- drop it from `codesearch` or `download_file`
  -> test_the_two_tools_that_touch_their_path_first_refuse_too,
     test_no_tool_touches_its_path_before_the_screen;
- take the folder alias's rest as it stands (`'/'.join(parts[1:])`, no `.lstrip('/')`)
  -> test_a_folder_alias_never_joins_a_share_path;
- drop the check at the top of `is_unsafe_project_dir`, `project_dir_refusal` or `jail_allows`
  -> test_the_project_folder_check_and_the_jail_refuse_before_they_resolve;
- `_resolve_project` probing the folder before acting on every refusal (the old order: only
  OUTSIDE_ACCOUNT before `os.path.isdir`), the coder's `_looks_like_file_path` probing a share
  path, `_project_path_refusal` or `_choose_base_dir` without their share answer,
  `run_in_workspace` or `_safe_mount_path` without the check
  -> test_the_project_lanes_refuse_a_share_before_touching_it (and the static guard for the
  ordering in `_resolve_project`);
- the Windows simulation calling `ntpath.abspath` at call time instead of the function captured
  before the patch -> test_the_windows_simulation_holds_where_os_path_is_ntpath
  (RecursionError, as on a Windows host, where os.path is ntpath); the same for `ntpath.join`
  in `_windows_paths` -> test_the_path_simulation_holds_where_os_path_is_ntpath;
- `git_add_commit` taking a file as `os.path.join(base_dir, f) if not os.path.isabs(f) else f`
  again -> test_git_add_commit_refuses_a_share_file_before_touching_it;
- `contained_path` replaced by a plain join (normalized and resolved)
  -> test_git_add_commit_adds_only_files_inside_the_project (an absolute in-project path is
  committed); dropping the `isinstance(files, str)` coercion -> the same test (`git add -- .`
  from the name's characters commits every file); no drive check in `contained_path` -> the
  same test (`sub/Z:/e.txt` is committed) and, in tests/contract/test_contract_path_jail.py,
  test_a_drive_in_a_later_part_is_refused_before_anything_resolves;
- staging the resolved path instead of the name the call gave, or the name unnormalized
  -> test_git_add_commit_stages_an_inside_link_as_the_link;
- `tool_work_folder` never refusing, or `update_codex` / `add_memory` not asking it
  -> test_the_git_tools_refuse_a_share_folder_on_the_chat_lane;
- staging the name without `:(literal)` -> test_git_add_commit_reads_a_name_literally;
- the existence check on the resolved path instead of `os.path.lexists` on the name
  -> test_git_add_commit_stages_an_inside_link_as_the_link (a dangling link is skipped);
- `edit_file` or `read_file` dropped from `_LOOP_PATH_TOOLS`, or `content` judged as well
  -> test_the_coder_loop_refusal_names_only_share_paths;
- `or _remote_path_refusal(fn_name, fn_args)` dropped from the local-tool chain, or asked below
  the edit_file block -> test_the_coder_loop_refuses_a_share_before_its_own_checks;
- `_identical_rewrite` without its `is_remote_path` answer, or `os.path.exists(full_path)` back
  in the loop's regex fallback -> test_the_duplicate_write_check_never_probes_a_share;
- the allowlist compared with `fn.name` instead of the qualified name
  -> test_no_tool_touches_its_path_before_the_screen, test_the_exemption_is_keyed_on_the_class;
  the entry keyed on the bare `run` -> test_no_tool_touches_its_path_before_the_screen,
  test_every_exemption_is_still_needed.
"""
import ast
import ntpath
import re
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]

REMOTE = [
    "\\\\evil.example\\share\\x.png",
    "//evil.example/share/x.png",
    "\\\\evil.example/share/x.png",
    "/\\evil.example\\share\\x.png",
    "\\\\?\\UNC\\evil.example\\share\\x.png",
    "\\\\?\\C:\\Users\\a\\x.png",
    "\\\\.\\pipe\\x",
    "//./pipe/x",
    "\\??\\UNC\\evil.example\\share\\x.png",
    "/??/UNC/evil.example/share/x.png",
    "  \\\\evil.example\\share\\x.png",
    "\\\\wsl$\\Ubuntu\\home\\user\\x",
    "\\\\wsl.localhost\\Ubuntu\\home\\user\\x",
]
LOCAL = [
    "C:\\Users\\a\\Documents\\x.png", "C:/Users/a/x.png", "/home/user/Documents/x.png",
    "~/Documents/x.png", "\\Users\\a\\x.png", "relative\\x.png", "", "?", "/?/x",
]


def _source(path):
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def test_every_windows_remote_spelling_is_refused():
    from vaf.core.path_jail import is_remote_path

    for spelled in REMOTE:
        assert is_remote_path(spelled), spelled
    for spelled in LOCAL:
        assert not is_remote_path(spelled), spelled
    # Windows itself reads the UNC and device spellings as a drive of their own: proof that the
    # list above is what a Windows host would reach out to, read with ntpath on any host.
    for spelled in REMOTE:
        text = spelled.strip()
        if not text.replace("\\", "/").startswith("/??/"):
            drive = ntpath.splitdrive(text)[0]
            assert drive[:2] in ("\\\\", "//", "\\/", "/\\"), (spelled, drive)


def _admin_request():
    req = Request({"type": "http", "method": "GET", "path": "/api/file", "headers": []})
    req.state.user = {"user_id": "1", "username": "admin", "role": "admin",
                      "user_scope_id": "ab12cd34-0000-0000-0000-000000000000"}
    return req


def test_the_file_lane_refuses_before_it_resolves(monkeypatch):
    """Even the most permissive caller (an admin) is refused, and the path is never resolved."""
    import vaf.core.web_server as ws
    from vaf.core.platform import Platform

    resolved = []
    monkeypatch.setattr(Platform, "normalize_path", staticmethod(lambda p: resolved.append(p) or Path(p)))
    for spelled in REMOTE:
        with pytest.raises(HTTPException) as refused:
            ws._allowed_file_path(spelled, _admin_request())
        assert refused.value.status_code == 403, spelled
    assert resolved == [], f"resolved before the refusal: {resolved}"


def _allowed_file_path_source():
    src = _source(ROOT / "vaf" / "core" / "web_server.py")
    body = src[src.index("def _allowed_file_path("):]
    return body[:re.search(r"\n(?=\S)", body[1:]).start() + 1]


def test_the_check_comes_before_any_resolve():
    body = _allowed_file_path_source()
    check = body.index("if is_remote_path(path_str):")
    assert check < body.index("Platform.normalize_path(path_str)")
    assert ".resolve(" not in body[:check] and "Path(" not in body[:check]


# Who may call Platform.normalize_path, a resolve that reaches out on Windows: the file lane,
# behind its own refusal, and the trust store, which resolves the process's own working folder.
_NORMALIZE_CALLERS = {"vaf/core/web_server.py": 1, "vaf/core/trust.py": 1}


def test_normalize_path_has_no_new_caller():
    found = {}
    for path in sorted((ROOT / "vaf").rglob("*.py")):
        n = len(re.findall(r"\bnormalize_path\(", _source(path))) - len(
            re.findall(r"def normalize_path\(", _source(path)))
        if n:
            found[path.relative_to(ROOT).as_posix()] = n
    assert found == _NORMALIZE_CALLERS, (
        "a new caller of Platform.normalize_path: a path from a request or a tool argument must "
        f"pass vaf.core.path_jail.is_remote_path before it is resolved. Found {found}")


def _touches(monkeypatch):
    """Record every call that would reach the disk (and on Windows a share) for a path."""
    import os
    import pathlib

    import vaf.core.data_stores as data_stores

    seen = []

    def record(name, real):
        def wrapper(*a, **k):
            seen.append((name, str(a[0]) if a else ""))
            return real(*a, **k)
        return wrapper

    monkeypatch.setattr(pathlib.Path, "resolve", record("resolve", pathlib.Path.resolve))
    monkeypatch.setattr(pathlib.Path, "exists", record("exists", pathlib.Path.exists))
    monkeypatch.setattr(pathlib.Path, "is_dir", record("is_dir", pathlib.Path.is_dir))
    monkeypatch.setattr(os.path, "realpath", record("realpath", os.path.realpath))
    monkeypatch.setattr(os.path, "exists", record("os.exists", os.path.exists))
    monkeypatch.setattr(os.path, "isfile", record("isfile", os.path.isfile))
    monkeypatch.setattr(os.path, "isdir", record("isdir", os.path.isdir))
    monkeypatch.setattr(os.path, "lexists", record("lexists", os.path.lexists))
    monkeypatch.setattr(pathlib.Path, "is_file", record("is_file", pathlib.Path.is_file))
    monkeypatch.setattr(data_stores, "_spellings", record("_spellings", data_stores._spellings))
    return seen


def test_the_file_tools_refuse_before_they_touch_the_disk(monkeypatch):
    from vaf.tools.filesystem import REMOTE_PATH_REFUSAL, is_safe_path

    seen = _touches(monkeypatch)
    for spelled in REMOTE:
        for read_only in (True, False):
            assert is_safe_path(spelled, read_only=read_only) == (False, REMOTE_PATH_REFUSAL), spelled
    assert seen == [], f"touched before the refusal: {seen}"


def test_the_two_tools_that_touch_their_path_first_refuse_too(monkeypatch):
    from vaf.tools.codesearch import CodeSearchTool
    from vaf.tools.download_file import DownloadFileTool
    from vaf.tools.filesystem import REMOTE_PATH_REFUSAL

    seen = _touches(monkeypatch)
    for spelled in REMOTE:
        assert CodeSearchTool().run(query="x", path=spelled) == f"[ERROR] {REMOTE_PATH_REFUSAL}", spelled
        assert DownloadFileTool().run(url="https://example.invalid/x.png", save_to=spelled) == REMOTE_PATH_REFUSAL
    assert [s for s in seen if "evil.example" in s[1] or "pipe" in s[1] or "C:" in s[1]] == []


# Calls that reach the disk for a path; on Windows each of them opens a share path.
_DISK = {"resolve", "exists", "is_file", "is_dir", "stat", "lstat", "realpath", "isfile", "isdir",
         "open", "read_text", "read_bytes", "iterdir", "glob", "samefile"}


def _name(call):
    return getattr(call.func, "attr", getattr(call.func, "id", ""))


# The screens a path from outside passes before a lane acts on it. Each refuses a share or
# device path first, before anything of its own resolves it.
_SCREENS = {"is_safe_path", "project_dir_refusal", "is_unsafe_project_dir", "jail_allows"}


def _bare_test(test, var):
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        test = test.operand
    return isinstance(test, ast.Name) and test.id == var


def _settled_line(fn, call, parents):
    """The line from which a screen's answer has been acted on as a whole: the `if` that tests
    the call itself, or the first `if refusal:` / `if not allowed:` on the name it was assigned
    to. A comparison (`if refusal == OUTSIDE_ACCOUNT:`) acts on one answer only."""
    node, parent = call, parents.get(call)
    if isinstance(parent, ast.UnaryOp) and isinstance(parent.op, ast.Not):
        node, parent = parent, parents.get(parent)
    if isinstance(parent, ast.If) and parent.test is node:
        return parent.lineno
    if isinstance(parent, ast.Assign) and len(parent.targets) == 1 and isinstance(parent.targets[0], ast.Name):
        var = parent.targets[0].id
        tests = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.If) and n.lineno > call.lineno
                 and _bare_test(n.test, var)]
        if tests:
            return min(tests)
    return call.lineno


# (file, function, variable) whose disk calls come before a LATER screen in the same function
# because an earlier one, in another function, already screened the value. The function is
# named with its class (`CodingAgentTool.run`): coder.py alone defines seven run() methods, and
# a bare `run` would exempt every one of them.
#
# NAMED BOUNDARY: `contained_path` is not one of `_SCREENS`. It refuses every rooted fragment
# (a share spelling included) before it resolves anything, so a fragment passed through it is
# screened by construction, and its first argument is the caller's own root. Adding it to
# `_SCREENS` finds nothing on the current tree.
_SCREENED_ELSEWHERE = {
    # base_dir is `_choose_base_dir`'s answer, which asked project_dir_refusal for it; the
    # is_unsafe_project_dir near the end of run() is a second question about the finished
    # project, not the first screen of base_dir.
    ("vaf/tools/coder.py", "CodingAgentTool.run", "base_dir"),
}


def _violations(rel, source, allow):
    """Every disk call on a screened path before its screen's answer is acted on, in one file.
    ``allow`` holds (file, qualified function, variable) entries that are exempt."""
    bad = []
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or fn.name in _SCREENS:
            continue
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
        screens = [c for c in calls if _name(c) in _SCREENS]
        if not screens:
            continue
        owner = parents.get(fn)
        qual = f"{owner.name}.{fn.name}" if isinstance(owner, ast.ClassDef) else fn.name
        asked = [c.lineno for c in calls if _name(c) == "is_remote_path"]
        first_ask = min(asked) if asked else None
        for screen in screens:
            settled = _settled_line(fn, screen, parents)
            names = {n.id for a in screen.args[:1] for n in ast.walk(a) if isinstance(n, ast.Name)}
            for c in calls:
                if _name(c) in _DISK and c.lineno < settled:
                    used = {n.id for n in ast.walk(c) if isinstance(n, ast.Name)} & names
                    used = {u for u in used if (rel, qual, u) not in allow}
                    if used and (first_ask is None or first_ask > c.lineno):
                        bad.append(f"{rel}:{c.lineno} {qual}: "
                                   f"{_name(c)}() on {sorted(used)} before {_name(screen)} is acted on")
    return bad


def test_no_tool_touches_its_path_before_the_screen():
    """In every function that screens a path (`is_safe_path`, `project_dir_refusal`,
    `is_unsafe_project_dir`, `jail_allows`), nothing reaches the disk with that path DIRECTLY
    before the screen's answer has been acted on as a whole, unless `is_remote_path` was asked
    even earlier. The path is followed by the variable names in the screen's argument, not
    through a value copied into another name first. What this found: `codesearch` resolving
    and `download_file` probing a folder before `is_safe_path`, and `run_project_git`'s
    `_resolve_project` probing the folder after `project_dir_refusal` had been asked but only
    its account answer acted on. All three now ask or act first."""
    bad = []
    for path in sorted((ROOT / "vaf").rglob("*.py")):
        bad += _violations(path.relative_to(ROOT).as_posix(), _source(path), _SCREENED_ELSEWHERE)
    assert not bad, ("a path from outside reaches the disk before its screen is acted on; ask "
                     "vaf.core.path_jail.is_remote_path first:\n" + "\n".join(sorted(set(bad))))


_TWO_RUNS = """
class CodingAgentTool:
    def run(self, base_dir):
        os.path.exists(base_dir)
        if is_unsafe_project_dir(base_dir):
            return
class Other:
    def run(self, base_dir):
        os.path.exists(base_dir)
        if is_unsafe_project_dir(base_dir):
            return
"""


def test_the_exemption_is_keyed_on_the_class():
    """The exemption for `CodingAgentTool.run` does not cover another class's run() in the
    same file."""
    bad = _violations("vaf/tools/coder.py", _TWO_RUNS, _SCREENED_ELSEWHERE)
    assert [b.split(" ", 2)[1] for b in bad] == ["Other.run:"], bad


def test_every_exemption_is_still_needed():
    """An entry that suppresses nothing is stale (or keyed so that it matches nothing) and
    would hide the next real finding in a function of that name."""
    for entry in sorted(_SCREENED_ELSEWHERE):
        rel, qual, var = entry
        found = _violations(rel, _source(ROOT / rel), _SCREENED_ELSEWHERE - {entry})
        assert [b for b in found if f" {qual}: " in b and f"'{var}'" in b], entry


def test_is_safe_path_asks_first():
    src = _source(ROOT / "vaf" / "tools" / "filesystem.py")
    body = src[src.index("def is_safe_path("):]
    assert body.index("if is_remote_path(str(path or \"\")):") < body.index("_resolve_folder_alias(path)")


ALIAS_SPELLINGS = [
    "DOCUMENTS///evil.example/share/x",
    "documents//evil.example/share/x",
    "Documents///evil.example/share/x",
    "DOCUMENTS\\\\\\evil.example\\share\\x",
    "DOWNLOADS/\\evil.example\\share\\x",
    "Desktop///evil.example/share/x",
]


def test_a_folder_alias_never_joins_a_share_path(tmp_path, monkeypatch):
    """`DOCUMENTS///host/share` passed the text check, and the alias joined its rooted rest,
    which replaced the folder: `//host/share` here, `\\\\host\\share` on Windows."""
    from vaf.core.path_jail import is_remote_path
    from vaf.tools.filesystem import _resolve_folder_alias, is_safe_path

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    for name in ("Documents", "Downloads", "Desktop"):
        (tmp_path / name).mkdir()
    for spelled in ALIAS_SPELLINGS:
        joined = _resolve_folder_alias(spelled)
        assert not is_remote_path(joined), (spelled, joined)
        assert Path(joined).is_relative_to(tmp_path), (spelled, joined)
    seen = _touches(monkeypatch)
    for spelled in ALIAS_SPELLINGS:
        is_safe_path(spelled, read_only=True)
        is_safe_path(spelled)
    remote = [s for s in seen if is_remote_path(s[1])]
    assert remote == [], f"a share path reached the disk: {remote}"


def test_the_project_folder_check_and_the_jail_refuse_before_they_resolve(monkeypatch):
    from vaf.core.workspace_guard import REMOTE_PROJECT_DIR, is_unsafe_project_dir, project_dir_refusal
    from vaf.tools.filesystem import jail_allows

    seen = _touches(monkeypatch)
    for spelled in REMOTE:
        assert is_unsafe_project_dir(spelled) is True, spelled
        assert project_dir_refusal(spelled, user_scope_id="ab12cd34-0000-0000-0000-000000000000",
                                   user_role="user") == REMOTE_PROJECT_DIR, spelled
        # An empty scope and an admin reach everything else; a share path not even then.
        for scope, role in (("", None), ("ab12cd34-0000-0000-0000-000000000000", "admin")):
            assert jail_allows(spelled, user_scope_id=scope, user_role=role) is False, (spelled, role)
    assert seen == [], f"touched before the refusal: {seen}"


# A project folder written as a share, in both conventions. On a Linux host os.path.abspath
# reads a backslash spelling as a relative name; Windows keeps it a UNC path, which is what
# the simulation below (ntpath.abspath for these spellings) makes the real lanes see.
PROJECT_SHARES = ["//evil.example/share/proj", "\\\\evil.example\\share\\proj",
                  "//evil.example/share/proj/index.html", "\\\\evil.example\\share\\proj\\index.html"]
SCOPE = "ab12cd34-0000-0000-0000-000000000000"


def _windows_abspath(monkeypatch):
    import os

    from vaf.core.path_jail import is_remote_path

    # Both captured before the patch: on Windows os.path IS ntpath, so a lambda that looked up
    # ntpath.abspath at call time would find itself and recurse.
    real = os.path.abspath
    real_ntabspath = ntpath.abspath
    monkeypatch.setattr(os.path, "abspath",
                        lambda p: real_ntabspath(p) if is_remote_path(str(p)) else real(p))


def test_the_windows_simulation_holds_where_os_path_is_ntpath(monkeypatch):
    """On Windows `os.path` and `ntpath` are one module, so patching one patches the other.
    Simulated here by making `os.path` the ntpath module; the patch must still answer."""
    import os

    monkeypatch.setattr(os, "path", ntpath)
    _windows_abspath(monkeypatch)
    assert ntpath.abspath("\\\\evil.example\\share\\proj") == "\\\\evil.example\\share\\proj"
    assert os.path.abspath("//evil.example/share/proj") == "\\\\evil.example\\share\\proj"


def test_the_project_lanes_refuse_a_share_before_touching_it(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from vaf.core.environments import EnvironmentManager, EnvironmentRefused
    from vaf.core.path_jail import is_remote_path
    from vaf.core.workspace_guard import REMOTE_PROJECT_DIR
    from vaf.tools import coder
    from vaf.tools.project_git import _resolve_project
    from vaf.tools.workspace_exec import run_in_workspace

    _windows_abspath(monkeypatch)
    own = tmp_path / "VAF_Projects" / "new"
    tool = SimpleNamespace(_get_session_project_path=lambda *a, **k: "",
                           _determine_base_dir=lambda *a, **k: str(own))
    seen = _touches(monkeypatch)
    for spelled in PROJECT_SHARES:
        for scope, role in (("", None), (SCOPE, "user"), (SCOPE, "admin")):
            path, error = _resolve_project(spelled, user_scope_id=scope or None, user_role=role)
            assert path == "" and REMOTE_PROJECT_DIR in error, (spelled, error)
            assert REMOTE_PROJECT_DIR in coder._project_path_refusal(spelled, scope, role), spelled
            choice = coder._choose_base_dir(tool, "build it", spelled, scope=scope, role=role,
                                            content_only=False)
            assert REMOTE_PROJECT_DIR in choice.refusal, (spelled, choice)
            # A share in the task text is never the project; the run goes to its own folder.
            task = f"build a page in directory {spelled}"
            choice = coder._choose_base_dir(tool, task, None, scope=scope, role=role, content_only=False)
            assert choice.base_dir == str(own) and not is_remote_path(choice.base_dir), (spelled, choice)
        rc, _out, err, mode = run_in_workspace(spelled, "true", user_scope_id=None, user_role=None)
        assert (rc, mode) == (-2, "refused") and REMOTE_PROJECT_DIR in err, (spelled, err)
        with pytest.raises(EnvironmentRefused, match="network share"):
            EnvironmentManager._safe_mount_path(spelled)
    touched = [s for s in seen if is_remote_path(s[1])]
    assert touched == [], f"a share path reached the disk: {touched}"


# ── the coding agent: git_add_commit's files, the git tools' folder, the loop's own checks ──

def _windows_paths(monkeypatch):
    """Windows' reading of a share spelling in `os.path.isabs` and `os.path.join` (and abspath),
    on any host: there a UNC path is absolute and joining it to a folder returns the share path
    itself, so a lane that joins before it screens reaches the share."""
    import os

    from vaf.core.path_jail import is_remote_path

    _windows_abspath(monkeypatch)
    # Captured before the patch, for the reason given in _windows_abspath.
    real_isabs, real_join = os.path.isabs, os.path.join
    nt_isabs, nt_join = ntpath.isabs, ntpath.join
    monkeypatch.setattr(os.path, "isabs",
                        lambda p: nt_isabs(p) if is_remote_path(str(p)) else real_isabs(p))
    monkeypatch.setattr(os.path, "join",
                        lambda a, *p: nt_join(a, *p) if any(is_remote_path(str(x)) for x in (a, *p))
                        else real_join(a, *p))


def test_the_path_simulation_holds_where_os_path_is_ntpath(monkeypatch):
    import os

    monkeypatch.setattr(os, "path", ntpath)
    _windows_paths(monkeypatch)
    assert os.path.isabs("//evil.example/share/x") is True
    assert os.path.join("C:\\p", "\\\\evil.example\\share\\x") == "\\\\evil.example\\share\\x"
    assert os.path.join("C:\\p", "a") == "C:\\p\\a"


# What a disk call on one of the REMOTE spellings carries, also after a join onto a folder.
_SHARE_MARKS = ("evil.example", "pipe", "C:\\Users", "Ubuntu")


def _share_touches(seen):
    return [s for s in seen if any(m in s[1] for m in _SHARE_MARKS)]


def _record_git(monkeypatch):
    import subprocess

    import vaf.core.git_runner as git_runner

    steps = []

    def fake(args, cwd, **_k):
        steps.append(list(args))
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    monkeypatch.setattr(git_runner, "run_project_git", fake)
    return steps


def test_git_add_commit_refuses_a_share_file_before_touching_it(tmp_path, monkeypatch):
    import os

    from vaf.tools.coder import GitAddCommitTool, _base_dir_tool

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _windows_paths(monkeypatch)
    steps = _record_git(monkeypatch)
    seen = _touches(monkeypatch)
    for spelled in REMOTE:
        answer = GitAddCommitTool().run(base_dir=str(repo), message="m", files=[spelled])
        assert answer.startswith("❌ Not committed:"), (spelled, answer)
    # Only the repository probe in the run folder, nothing on any file name.
    assert {s[1] for s in seen} <= {os.path.join(str(repo), ".git")}, seen
    wrapped = _base_dir_tool(GitAddCommitTool, str(repo), owner_scope=None, owner_role=None)
    for spelled in REMOTE:
        answer = wrapped.run(message="m", files=["index.html", spelled])
        assert answer.startswith("❌ Not committed:"), (spelled, answer)
    assert _share_touches(seen) == [], _share_touches(seen)
    assert steps == [], f"a git step ran: {steps}"


def _git(*args, cwd):
    import subprocess

    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def test_git_add_commit_adds_only_files_inside_the_project(tmp_path):
    import os
    import shutil

    from vaf.tools.coder import GitAddCommitTool

    if not shutil.which("git"):
        pytest.skip("git is not installed")
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    _git("init", "-q", cwd=repo)
    _git("config", "user.name", "T", cwd=repo)
    _git("config", "user.email", "t@example.invalid", cwd=repo)
    for name in ("a.txt", "sub/b.txt", "c.txt", "d.txt", "e.txt", "f.txt"):
        (repo / name).write_text(name, encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    tool = GitAddCommitTool()

    def committed():
        return _git("show", "--name-only", "--format=", "HEAD", cwd=repo).stdout.split()

    def staged():
        return _git("diff", "--cached", "--name-only", cwd=repo).stdout.split()

    assert tool.run(base_dir=str(repo), message="one", files=["a.txt"]).startswith("✅")
    assert committed() == ["a.txt"]
    assert tool.run(base_dir=str(repo), message="two", files=["sub\\b.txt"]).startswith("✅")
    assert committed() == ["sub/b.txt"]
    # One name as a string is that file, not its characters.
    assert tool.run(base_dir=str(repo), message="three", files="c.txt").startswith("✅")
    assert committed() == ["c.txt"]
    # A missing file is skipped, the rest is committed.
    assert tool.run(base_dir=str(repo), message="four", files=["nope.txt", "d.txt"]).startswith("✅")
    assert committed() == ["d.txt"]

    head = _git("rev-parse", "HEAD", cwd=repo).stdout
    refused = [str(repo / "e.txt"), "../outside.txt", str(outside), os.path.join("..", "repo", "e.txt"),
               "sub/Z:/e.txt"]
    link = repo / "link.txt"
    try:
        os.symlink(outside, link)
        refused.append("link.txt")
    except (OSError, NotImplementedError):
        pass  # Windows without the privilege to create a link
    for entry in refused:
        # A good name next to the refused one is not staged either.
        answer = tool.run(base_dir=str(repo), message="no", files=["e.txt", entry])
        assert answer.startswith("❌ Not committed:") and entry in answer, (entry, answer)
        assert staged() == [], entry
        assert _git("rev-parse", "HEAD", cwd=repo).stdout == head, entry


def test_git_add_commit_stages_an_inside_link_as_the_link(tmp_path):
    """A link inside the project that points to another file inside it is staged as the link
    the caller named, not as its target, and a name is normalized the way git expects it."""
    import os
    import shutil

    from vaf.tools.coder import GitAddCommitTool

    if not shutil.which("git"):
        pytest.skip("git is not installed")
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    _git("init", "-q", cwd=repo)
    _git("config", "user.name", "T", cwd=repo)
    _git("config", "user.email", "t@example.invalid", cwd=repo)
    (repo / "real.txt").write_text("one", encoding="utf-8")
    (repo / "sub" / "b.txt").write_text("b", encoding="utf-8")
    tool = GitAddCommitTool()
    assert tool.run(base_dir=str(repo), message="base", files=["real.txt"]).startswith("✅")
    try:
        os.symlink("real.txt", repo / "alias.txt")
    except (OSError, NotImplementedError):
        pytest.skip("links cannot be created here")
    # A change to the target that nobody asked to commit.
    (repo / "real.txt").write_text("two", encoding="utf-8")

    def committed():
        return _git("show", "--name-only", "--format=", "HEAD", cwd=repo).stdout.split()

    assert tool.run(base_dir=str(repo), message="link", files=["alias.txt"]).startswith("✅")
    assert committed() == ["alias.txt"]
    # A link whose target inside the project is missing is still the link the call named.
    os.symlink("missing.txt", repo / "dangling.txt")
    assert tool.run(base_dir=str(repo), message="dangling", files=["dangling.txt"]).startswith("✅")
    assert committed() == ["dangling.txt"]
    assert _git("diff", "--name-only", cwd=repo).stdout.split() == ["real.txt"]
    assert tool.run(base_dir=str(repo), message="sub", files=[".\\sub//./b.txt"]).startswith("✅")
    assert committed() == ["sub/b.txt"]


def test_git_add_commit_reads_a_name_literally(tmp_path):
    """A name is a file name, never git pathspec magic: a file called `:(glob)*` is that one
    file, not every file in the project."""
    import os
    import shutil

    from vaf.tools.coder import GitAddCommitTool

    if not shutil.which("git"):
        pytest.skip("git is not installed")
    if os.name == "nt":
        pytest.skip("':', '(' and '*' cannot be part of a file name on Windows")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.name", "T", cwd=repo)
    _git("config", "user.email", "t@example.invalid", cwd=repo)
    for name in (":(glob)*", "other.txt", "third.txt"):
        (repo / name).write_text(name, encoding="utf-8")
    tool = GitAddCommitTool()
    assert tool.run(base_dir=str(repo), message="magic", files=[":(glob)*"]).startswith("✅")
    assert _git("show", "--name-only", "--format=", "HEAD", cwd=repo).stdout.split() == [":(glob)*"]


def test_the_git_tools_refuse_a_share_folder_on_the_chat_lane(tmp_path, monkeypatch):
    """On the main agent the four git tools, `update_codex` and `add_memory` are registered too,
    and `base_dir` is what the model wrote. Driven through the framework's funnel, as the chat
    lane calls them."""
    import os

    from vaf.core.tool_dispatch import ToolCaller
    from vaf.tools.coder import GitAddCommitTool, GitInitTool, GitLogTool, GitStatusTool
    from vaf.tools.knowledge import AddMemoryTool, UpdateCodexTool

    tools = (GitInitTool(), GitAddCommitTool(), GitStatusTool(), GitLogTool(), UpdateCodexTool(),
             AddMemoryTool())
    caller = ToolCaller({t.name: t for t in tools}, gate_enabled=False, max_result_chars=None)
    # A backslash spelling is a relative name on POSIX: a tool that did not refuse would
    # create it here, not in the worktree.
    monkeypatch.chdir(tmp_path)
    _windows_paths(monkeypatch)
    steps = _record_git(monkeypatch)
    seen = _touches(monkeypatch)
    real_makedirs, real_open = os.makedirs, open
    monkeypatch.setattr(os, "makedirs",
                        lambda p, *a, **k: seen.append(("makedirs", str(p))) or real_makedirs(p, *a, **k))
    monkeypatch.setattr("builtins.open",
                        lambda p, *a, **k: seen.append(("open", str(p))) or real_open(p, *a, **k))
    args = {"message": "m", "title": "t", "content": "c", "note": "n"}
    for spelled in REMOTE:
        for tool in tools:
            answer = caller.execute(tool.name, {"base_dir": spelled, **args})
            assert answer.startswith("❌ Refused:") and "network share" in answer, (
                tool.name, spelled, answer)
    assert _share_touches(seen) == [], _share_touches(seen)
    assert steps == [], f"a git step ran: {steps}"


def test_the_coder_loop_refusal_names_only_share_paths():
    from vaf.tools.coder import _remote_path_refusal
    from vaf.tools.filesystem import REMOTE_PATH_REFUSAL

    for name in ("write_file", "edit_file", "read_file"):
        for spelled in REMOTE:
            assert _remote_path_refusal(name, {"path": spelled, "content": "x"}) == REMOTE_PATH_REFUSAL, (
                name, spelled)
        for spelled in LOCAL:
            assert _remote_path_refusal(name, {"path": spelled}) is None, (name, spelled)
    # The content is the file's text, not a path.
    assert _remote_path_refusal("write_file", {"path": "app.js", "content": "// comment\n"}) is None
    assert _remote_path_refusal("write_file", {"path": "app.js", "content": "//evil.example/s"}) is None
    # Other tools screen their own path.
    for name in ("render_check", "list_files", "bash"):
        assert _remote_path_refusal(name, {"path": REMOTE[0]}) is None, name
    assert _remote_path_refusal("write_file", "not a dict") is None


def _run_source():
    import inspect

    from vaf.tools.coder import CodingAgentTool

    return inspect.getsource(CodingAgentTool.run)


def test_the_coder_loop_refuses_a_share_before_its_own_checks():
    src = _run_source()
    site = src.index("elif fn_name in self.local_tools:")
    block_start = src.index("_refusal = _coder_dispatch_refusal(", site)
    ask = src.index("or _remote_path_refusal(fn_name, fn_args)", site)
    answered = src.index("\n                        continue\n", block_start)
    assert block_start < ask < answered, "the refusal is part of the local-tool chain"
    for later in ("_unread_edit_reason(", "_sibling_file_note(", "# Generate Diff or Content Preview",
                  "_coder_funnel("):
        assert ask < src.index(later, site), later


def test_the_duplicate_write_check_never_probes_a_share(tmp_path, monkeypatch):
    from vaf.tools.coder import _identical_rewrite

    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    assert _identical_rewrite("a.txt", "hello", str(tmp_path)) == str(tmp_path / "a.txt")
    assert _identical_rewrite(str(tmp_path / "a.txt"), " hello ", str(tmp_path)) == str(tmp_path / "a.txt")
    assert _identical_rewrite("a.txt", "other", str(tmp_path)) is None
    assert _identical_rewrite("b.txt", "hello", str(tmp_path)) is None

    _windows_paths(monkeypatch)
    seen = _touches(monkeypatch)
    for spelled in REMOTE:
        assert _identical_rewrite(spelled, "hello", str(tmp_path)) is None, spelled
    assert seen == [], f"touched a share path: {seen}"

    src = _run_source()
    start = src.index('if "path" in extracted_args and "content" in extracted_args:')
    block = src[start:src.index("fn_args = extracted_args", start)]
    assert "_identical_rewrite(" in block
    assert "os.path.exists(full_path)" not in block
