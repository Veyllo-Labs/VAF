# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The file lanes refuse a network share or a device path while it is still text
(`vaf.is_remote_path`, `vaf/core/path_jail.py`): asked first by `_allowed_file_path` in
`vaf/core/web_server.py`, by the file tools' screen `is_safe_path`, and by the two tools that touch
their path before that screen (`codesearch`, `download_file`).

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
  -> test_the_project_folder_check_and_the_jail_refuse_before_they_resolve.
"""
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


def test_no_tool_touches_its_path_before_the_screen():
    """In every function that screens a path with `is_safe_path`, nothing reaches the disk with
    that path DIRECTLY before the screen, unless `is_remote_path` was asked even earlier. The
    path is followed by the variable names in the screen's argument, not through a value
    copied into another name first. The two places this found (`codesearch` resolving,
    `download_file` probing a folder) ask it now."""
    import ast

    bad = []
    for path in sorted((ROOT / "vaf").rglob("*.py")):
        tree = ast.parse(_source(path))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or fn.name == "is_safe_path":
                continue
            calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
            screens = [c for c in calls if _name(c) == "is_safe_path"]
            if not screens:
                continue
            first_screen = min(c.lineno for c in screens)
            asked = [c.lineno for c in calls if _name(c) == "is_remote_path"]
            first_ask = min(asked) if asked else None
            names = {n.id for c in screens for a in c.args[:1] for n in ast.walk(a) if isinstance(n, ast.Name)}
            for c in calls:
                if _name(c) in _DISK and c.lineno < first_screen:
                    used = {n.id for n in ast.walk(c) if isinstance(n, ast.Name)} & names
                    if used and (first_ask is None or first_ask > c.lineno):
                        bad.append(f"{path.relative_to(ROOT).as_posix()}:{c.lineno} {fn.name}: "
                                   f"{_name(c)}() on {sorted(used)} before is_safe_path")
    assert not bad, ("a path from a tool argument reaches the disk before the screen; ask "
                     "vaf.core.path_jail.is_remote_path first:\n" + "\n".join(bad))


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
