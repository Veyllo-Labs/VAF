# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""An error reply never carries a host path.

An operating-system error quotes the absolute path it failed on (``[Errno 36] File name too
long: '/home/user/.../workspace/bbb...'``), so a reply built from ``str(e)`` hands whoever
asked the server's home folder, its account name and the layout of its data folder. A name
over 255 characters is enough to make ``is_file()``, ``mkdir`` and ``write_bytes`` raise
exactly that, and a remote room member reaches the shared-folder routes. Pinned here:

- the global exception handler answers the OS reason, and logs the traceback with the
  request's path and never its query (a room seat rides there);
- the file route decision answers 404 for a path the OS cannot look at;
- the workspace upload, mkdir and delete answer "<Prefix> failed: <reason>";
- a static guard over every route module: inside an ``except`` that can catch an OSError,
  the exception's text is read only in a log call, a comparison or ``describe_error``, and
  traceback text and ``sys.exc_info()`` only in a log call or ``describe_error``.

The guard fails closed: an exception name it cannot resolve to a class counts as one that
can catch an OSError. It has no exemptions.

NAMED BOUNDARIES of the guard:

- It reads the handler's own body. An exception handed whole to a helper that builds the
  reply is not followed; measured: two helpers in scope receive one (``_stt_busy_payload``
  for ``QueueBusy``, not an OSError, and ``_is_db_not_ready_error``, a classification).
- Non-OSError text passes unchanged (rule (a) of ``describe_error``): it is the text its
  raiser wrote. The one stdlib non-OSError measured to quote a path is ``Path.relative_to``'s
  ``ValueError``, and its two calls in route modules run on paths already contained (an
  ``rglob`` under the room workspace and a ``contained_path`` result).
- ``vaf/api/*_bridge.py`` and ``vaf/tools/*`` are outside its scope: they hand their text
  to the account's own agent as a tool result or keep it as internal status, not as an HTTP
  or socket reply.
"""
import ast
import asyncio
import builtins
import errno
import glob
import importlib
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCOPE = (["vaf/core/web_server.py", "vaf/memory/routes.py"]
         + sorted(Path(p).relative_to(ROOT).as_posix()
                  for p in glob.glob(str(ROOT / "vaf" / "api" / "*_routes.py"))))

# ---- the guard ------------------------------------------------------------------------------

_LOG_ATTRS = {"debug", "info", "warning", "warn", "error", "exception", "critical"}
# Attributes whose value is the exception's text or the path it quotes.
_TEXT_ATTRS = {"args", "filename", "filename2", "__str__", "__repr__"}
# Traceback text names the source files of the install and the frames' arguments.
_TRACEBACK_TEXT = {"format_exc", "format_exception", "format_exception_only", "format_tb",
                   "format_stack"}


def _imports(tree, package):
    """Every name an import binds anywhere in the file, to each (module, attribute) it is
    bound to: one name imported differently in two functions keeps both targets."""
    names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    names.setdefault(alias.asname, set()).add((alias.name, None))
                else:
                    top = alias.name.split(".")[0]
                    names.setdefault(top, set()).add((top, None))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                base = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                module = ".".join(base + ([module] if module else []))
            for alias in node.names:
                names.setdefault(alias.asname or alias.name, set()).add((module, alias.name))
    return names


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return [node.id] + list(reversed(parts))


def _follow(obj, prefix, rest):
    """getattr along ``rest``; a missing attribute of a module is tried as its submodule."""
    for name in rest:
        try:
            obj = getattr(obj, name)
        except AttributeError:
            if prefix is None:
                raise
            obj = importlib.import_module(f"{prefix}.{name}")
        prefix = f"{prefix}.{name}" if prefix is not None else None
    return obj


def _targets(parts, imports, module_name):
    """Every object the dotted name may denote. Raises when one cannot be resolved."""
    head, rest = parts[0], parts[1:]
    if head in imports:
        out = []
        for module, attr in imports[head]:
            if attr is None:
                out.append(_follow(importlib.import_module(module), module, rest))
                continue
            try:
                obj, prefix = getattr(importlib.import_module(module), attr), None
            except AttributeError:
                obj = importlib.import_module(f"{module}.{attr}")
                prefix = f"{module}.{attr}"
            out.append(_follow(obj, prefix, rest))
        return out
    if hasattr(builtins, head):
        return [_follow(getattr(builtins, head), None, rest)]
    return [_follow(getattr(importlib.import_module(module_name), head), None, rest)]


def _class_catches(obj):
    if isinstance(obj, tuple):
        return any(_class_catches(o) for o in obj)
    if not (isinstance(obj, type) and issubclass(obj, BaseException)):
        return True
    return issubclass(OSError, obj) or issubclass(obj, OSError)


def catches_oserror(type_node, imports, module_name):
    """Can this ``except`` clause catch an OSError? Fails closed: True for a name that does
    not resolve, and for any expression that is not a plain (dotted) name. (A bare
    ``except:`` binds no name; what it caught is reached through ``sys.exc_info()``, which
    `_is_exception_text` judges wherever it is called.)"""
    if isinstance(type_node, ast.Tuple):
        return any(catches_oserror(e, imports, module_name) for e in type_node.elts)
    parts = _dotted(type_node)
    if parts is None:
        return True
    try:
        targets = _targets(parts, imports, module_name)
    except Exception:
        return True
    return any(_class_catches(t) for t in targets)


def _allowed_sink(node):
    if isinstance(node, ast.Compare):
        return True
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return "log" in func.id.lower() or func.id in ("print", "describe_error")
    if isinstance(func, ast.Attribute):
        return (func.attr in _LOG_ATTRS or func.attr.startswith("log")
                or func.attr == "describe_error")
    return False


def _reads_text(node, var):
    def is_var(n):
        return isinstance(n, ast.Name) and n.id == var
    if isinstance(node, ast.Call):
        func = node.func
        if (isinstance(func, ast.Name) and func.id in ("str", "repr", "format")
                and node.args and is_var(node.args[0])):
            return True
        if isinstance(func, ast.Attribute) and func.attr == "format" and (
                any(is_var(a) for a in node.args)
                or any(is_var(k.value) for k in node.keywords)):
            return True
    if isinstance(node, ast.FormattedValue) and is_var(node.value):
        return True
    if isinstance(node, ast.Attribute) and node.attr in _TEXT_ATTRS and is_var(node.value):
        return True
    if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod)
            and isinstance(node.left, (ast.Constant, ast.JoinedStr))):
        right = node.right
        return (is_var(right)
                or (isinstance(right, ast.Tuple) and any(is_var(e) for e in right.elts))
                or (isinstance(right, ast.Dict) and any(is_var(v) for v in right.values)))
    return False


def _is_exception_text(node):
    """Traceback text, or the exception currently being handled (``sys.exc_info()``, the
    only way a bare ``except:`` reaches it), read wherever it is called."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return name in _TRACEBACK_TEXT or name == "exc_info"


def _in_allowed_sink(node, parents, stop=None):
    while node in parents and node is not stop:
        node = parents[node]
        if _allowed_sink(node):
            return True
    return False


def scan_source(source, filename, module_name):
    """``file:line`` of every read of an exception's text, or of traceback text, that is not
    a log call, a comparison or ``describe_error``."""
    tree = ast.parse(source, filename)
    imports = _imports(tree, module_name.rpartition(".")[0])
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    findings = []
    for node in ast.walk(tree):
        if _is_exception_text(node) and not _in_allowed_sink(node, parents):
            findings.append((node.lineno, node.col_offset))
    for handler in ast.walk(tree):
        if not isinstance(handler, ast.ExceptHandler) or not handler.name:
            continue
        if not catches_oserror(handler.type, imports, module_name):
            continue
        stack = list(handler.body)
        while stack:
            node = stack.pop()
            if isinstance(node, ast.ExceptHandler) and node.name == handler.name:
                continue  # rebinds the name: judged as a handler of its own
            if _reads_text(node, handler.name):
                if not _in_allowed_sink(node, parents, stop=handler):
                    findings.append((node.lineno, node.col_offset))
                continue
            stack.extend(ast.iter_child_nodes(node))
    return sorted(f"{filename}:{line}" for line, _col in set(findings))


def _scan_tree():
    findings = []
    for rel in SCOPE:
        source = (ROOT / rel).read_text(encoding="utf-8")
        findings += scan_source(source, rel, rel[:-3].replace("/", "."))
    return findings


def test_no_route_module_replies_with_an_exceptions_text():
    """MUTATION: put `str(e)` back at any converted site; the guard names its file:line."""
    assert len(SCOPE) > 10 and "vaf/api/whatsapp_routes.py" in SCOPE
    findings = _scan_tree()
    assert findings == [], (
        "a route reads an exception's text outside a log call: answer with "
        "vaf.describe_error(e), which keeps an OS error's reason and drops the path it "
        "quotes:\n" + "\n".join(findings))


_FLAGGED = {
    "return": 'try:\n    x()\nexcept Exception as e:\n    return {"error": str(e)}\n',
    "http": 'try:\n    x()\nexcept Exception as e:\n    raise HTTPException(500, detail=f"x {e}")\n',
    "conv_s": 'try:\n    x()\nexcept Exception as e:\n    y(f"{e!s}")\n',
    "ws_repr": ('async def f():\n    try:\n        x()\n    except Exception as e:\n'
                '        await ws.send_json({"error": repr(e)})\n'),
    "close": ('async def f():\n    try:\n        x()\n    except Exception as e:\n'
              '        await ws.close(reason=str(e))\n'),
    "yield": 'def f():\n    try:\n        x()\n    except Exception as e:\n        yield f"{e}"\n',
    "state": 'try:\n    x()\nexcept Exception as e:\n    state["error"] = str(e)[:300]\n',
    "append": 'try:\n    x()\nexcept Exception as e:\n    results.append(f"{e}")\n',
    "assign": 'try:\n    x()\nexcept Exception as e:\n    msg = str(e)\n',
    "args": 'try:\n    x()\nexcept Exception as e:\n    y(e.args)\n',
    "filename": 'try:\n    x()\nexcept Exception as e:\n    y(e.filename)\n',
    "percent": 'try:\n    x()\nexcept Exception as e:\n    y("%s" % e)\n',
    "format": 'try:\n    x()\nexcept Exception as e:\n    y("{}".format(e))\n',
    "bare": 'import sys\ntry:\n    x()\nexcept:\n    y(sys.exc_info()[1])\n',
    "base_exception": 'try:\n    x()\nexcept BaseException as e:\n    y(str(e))\n',
    "permission": 'try:\n    x()\nexcept PermissionError as e:\n    y(str(e))\n',
    "imported": ('from vaf.network.egress import EgressRefused\n'
                 'try:\n    x()\nexcept EgressRefused as e:\n    y(str(e))\n'),
    "unresolvable": 'try:\n    x()\nexcept NoSuchThing as e:\n    y(str(e))\n',
    "dotted_unresolvable": ('import json\ntry:\n    x()\nexcept json.NoSuchError as e:\n'
                            '    y(str(e))\n'),
    "call_type": 'try:\n    x()\nexcept pick() as e:\n    y(str(e))\n',
    "tuple": 'try:\n    x()\nexcept (ValueError, OSError) as e:\n    y(str(e))\n',
    "traceback": 'import traceback\ndef f():\n    state["error"] = traceback.format_exc()\n',
}

_ALLOWED = {
    "log": 'try:\n    x()\nexcept Exception as e:\n    log("X", f"{e}")\n',
    "logger": 'try:\n    x()\nexcept Exception as e:\n    logger.warning("%s", e)\n',
    "domain_log": 'try:\n    x()\nexcept Exception as e:\n    append_domain_log("webui", f"{e}")\n',
    "compare": 'try:\n    x()\nexcept Exception as e:\n    if "not found" in str(e):\n        y()\n',
    "describe": 'try:\n    x()\nexcept Exception as e:\n    y(describe_error(e))\n',
    "value_error": 'try:\n    x()\nexcept ValueError as e:\n    y(str(e))\n',
    "httpx": 'import httpx\ntry:\n    x()\nexcept httpx.RequestError as e:\n    y(str(e))\n',
    "traceback_logged": ('import traceback\ndef f():\n'
                         '    logger.error("boom %s", traceback.format_exc())\n'),
    "exc_info_described": ('import sys\ntry:\n    x()\nexcept:\n'
                           '    y(describe_error(sys.exc_info()[1]))\n'),
    "rebound": ('try:\n    x()\nexcept Exception as e:\n    try:\n        z()\n'
                '    except ValueError as e:\n        y(str(e))\n'),
}


@pytest.mark.parametrize("case", sorted(_FLAGGED))
def test_the_guard_flags_every_reply_shape(case):
    """MUTATION: drop a reply shape or the fail-closed resolution from the guard; the
    matching case goes red."""
    assert scan_source(_FLAGGED[case], f"{case}.py", f"synthetic.{case}"), case


@pytest.mark.parametrize("case", sorted(_ALLOWED))
def test_the_guard_lets_logging_and_describe_error_pass(case):
    assert scan_source(_ALLOWED[case], f"{case}.py", f"synthetic.{case}") == [], case


# ---- the global handler ---------------------------------------------------------------------

def _request(path="/api/a2a/rooms/room-x/file", query=b"seat=s-secret&path=bbb"):
    from starlette.requests import Request
    return Request({"type": "http", "method": "GET", "path": path, "query_string": query,
                    "headers": []})


def _run(coro):
    return asyncio.run(coro)


def test_the_global_handler_answers_the_os_reason_and_logs_the_traceback(tmp_path, caplog):
    """MUTATIONS: answer `str(exc)` (the path is in the body); drop `exc_info` (no traceback
    in the log); log `str(request.url)` (the seat is in the log)."""
    from vaf.core import web_server

    assert web_server.app.exception_handlers[Exception] is web_server.json_exception_handler
    target = str(tmp_path / ("b" * 256))
    try:
        raise OSError(errno.ENAMETOOLONG, "File name too long", target)
    except OSError as raised:
        exc = raised
    web_server.log_uvicorn.addHandler(caplog.handler)
    try:
        caplog.set_level(logging.ERROR, logger="uvicorn")
        response = _run(web_server.json_exception_handler(_request(), exc))
    finally:
        web_server.log_uvicorn.removeHandler(caplog.handler)
    body = response.body.decode("utf-8")
    assert response.status_code == 500
    assert str(tmp_path) not in body
    assert json.loads(body) == {"ok": False, "error": "File name too long",
                                "detail": "File name too long"}
    # The handler is attached to "uvicorn" itself, because uvicorn's own logging config turns
    # its propagation off; while propagation is on, the root sees the same record again.
    records = list({id(r): r for r in caplog.records if r.name == "uvicorn"}.values())
    assert len(records) == 1 and records[0].exc_info and records[0].exc_info[1] is exc
    line = records[0].getMessage()
    assert "/api/a2a/rooms/room-x/file" in line and "s-secret" not in line

    response = _run(web_server.json_exception_handler(_request(), ValueError("bad")))
    assert response.status_code == 500 and json.loads(response.body)["detail"] == "bad"


def test_the_global_handler_keeps_an_http_exceptions_answer():
    from fastapi import HTTPException

    from vaf.core import web_server

    response = _run(web_server.json_exception_handler(
        _request(), HTTPException(status_code=404, detail="no such file in this workspace")))
    assert response.status_code == 404
    assert b"no such file in this workspace" in response.body


# ---- the file route decision ----------------------------------------------------------------

def test_a_path_the_os_cannot_look_at_is_not_found(tmp_path, monkeypatch):
    """MUTATION: drop the `try` around `is_file`; the OSError escapes with the path in it."""
    from fastapi import HTTPException

    from vaf.core import web_server
    from vaf.core.platform import Platform

    root = (tmp_path / "docs").resolve()
    root.mkdir()
    monkeypatch.setattr(Platform, "served_file_roots", staticmethod(lambda: [root]))
    monkeypatch.setattr("vaf.api.user_routes.caller_is_admin", lambda request: True)
    if sys.platform.startswith("linux") or sys.platform == "darwin":
        # The real failure, where the OS says so: a name longer than NAME_MAX.
        with pytest.raises(OSError):
            (root / ("b" * 256)).is_file()
    raised = []

    def refusing_is_file(self):
        raised.append(self)
        raise PermissionError(errno.EACCES, "Permission denied", str(self))

    for reason in ("real", "patched"):
        if reason == "patched":
            monkeypatch.setattr(Path, "is_file", refusing_is_file)
        with pytest.raises(HTTPException) as info:
            web_server._allowed_file_path(str(root / ("b" * 256)), _request("/api/file"),
                                          must_exist=True)
        assert info.value.status_code == 404 and info.value.detail == "File not found"
    assert raised, "the patched is_file was never asked"


# ---- the workspace routes -------------------------------------------------------------------

@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    from vaf.core import web_server

    root = tmp_path / "ws-ab12cd34"
    root.mkdir()

    async def own_root(session_id, request, create=False):
        return str(root)

    monkeypatch.setattr(web_server, "_workspace_root", own_root)
    monkeypatch.setattr(web_server, "_inspect_attachment",
                        lambda *a, **k: SimpleNamespace(blocked=False))
    monkeypatch.setattr(web_server, "_requester_name", lambda request: "alice")
    monkeypatch.setattr(web_server, "connection_client_ip", lambda request: "127.0.0.1")
    return root


def _too_long(path, *a, **k):
    raise OSError(errno.ENAMETOOLONG, "File name too long", str(path))


def _detail_of(coro):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as info:
        _run(coro)
    assert info.value.status_code == 500
    return info.value.detail


def test_the_workspace_routes_answer_the_reason_without_the_path(workspace, monkeypatch):
    """MUTATION: put `{e}` back into the Write, Create or Delete answer; its assert goes red."""
    from vaf.core import web_server

    with monkeypatch.context() as patch:
        patch.setattr(web_server, "open", _too_long, raising=False)
        detail = _detail_of(web_server.upload_session_workspace_file(
            web_server.WorkspaceUploadRequest(sessionId="s1", filename="a.txt",
                                              content_base64="aGk="), _request()))
    assert detail == "Write failed: File name too long"

    with monkeypatch.context() as patch:
        patch.setattr(web_server.os, "mkdir", _too_long)
        detail = _detail_of(web_server.create_session_workspace_folder(
            web_server.WorkspaceMkdirRequest(sessionId="s1", name="new"), _request()))
    assert detail == "Create failed: File name too long"

    (workspace / "old.txt").write_text("x", encoding="utf-8")
    with monkeypatch.context() as patch:
        patch.setattr(web_server.os, "remove", _too_long)
        detail = _detail_of(web_server.delete_session_workspace_entry(
            web_server.WorkspaceDeleteRequest(sessionId="s1", name="old.txt"), _request()))
    assert detail == "Delete failed: File name too long"
    assert (workspace / "old.txt").exists()
