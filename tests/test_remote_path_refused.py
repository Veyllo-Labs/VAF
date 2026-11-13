# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The file lane refuses a network share or a device path while it is still text
(`vaf.core.path_jail.is_remote_path`, asked first by `_allowed_file_path` in
`vaf/core/web_server.py`).

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
  -> test_normalize_path_has_no_new_caller.
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
