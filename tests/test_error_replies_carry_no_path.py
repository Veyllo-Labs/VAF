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
  the exception's text (``str``, ``repr``, an f-string, ``%`` and ``.format`` with a literal
  or a template in a variable, ``getattr`` or an attribute read of ``args``, ``filename``,
  ``strerror``; through any plain ``x = e`` alias) is read only as an argument of a log call
  (``log``, ``log_*``, ``*_log``, ``logger.<level>``, ``print``), of ``describe_error`` or in a
  comparison, judged by the NEAREST call it is an argument of; it is never an argument of an
  exception that is raised (``raise Wrapper(e)``); traceback text and ``sys.exc_info()`` only
  in a log call or ``describe_error``;
- the office conversion routes and both upload lanes answer a Word or PowerPoint file that is
  not a package, or a package of the wrong type, with words of their own: the libraries'
  errors are not OSErrors and quote the path they were handed, so the package is opened from
  its bytes (vaf/core/office_package.py).

The guard fails closed: an exception name it cannot resolve to a class counts as one that
can catch an OSError. It has no exemptions.

NAMED BOUNDARIES of the guard:

- It reads the handler's own body, by name, and an exception built around the caught one
  (``raise Wrapper(e)``). Shapes it does not follow, none of which occurs in scope: an
  exception handed whole to a helper that builds the reply (measured: two in scope,
  ``_stt_busy_payload`` for ``QueueBusy``, not an OSError, and ``_is_db_not_ready_error``, a
  classification); an exception saved in the handler and read after it; an alias made by a
  walrus, by unpacking (``err, = (e,)``), by a conditional (``e if e else None``) or through
  a container (``errs = [e]; str(errs[0])``); ``str(e or "")``, ``str(*[e])``,
  ``operator.attrgetter("filename")(e)``; ``e.__cause__`` and ``e.__context__``;
  ``sys.exception()``; ``print(e, file=buf)``, ``print_exc(file=...)`` or
  ``traceback.TracebackException`` into a buffer read later; ``json.dumps(...,
  default=str)``; a comprehension or ``map(str, ...)`` over the exception; and a call named
  like a log that also replies (``log_and_reply(str(e))``), which the ``log_*`` rule passes.
- Text that is not an OSError's passes unchanged (rule (a) of ``describe_error``): it is the
  text its raiser wrote, and some raisers write a path. Measured: ``Path.relative_to``'s
  ``ValueError`` (its two calls in route modules run on paths already contained: an ``rglob``
  under the room workspace and a ``contained_path`` result); python-docx's and python-pptx's
  ``PackageNotFoundError`` and ``ValueError`` (the package is opened from its bytes at every
  call site, so they name a ``BytesIO``, and the opener answers them with words of its own);
  VAF's own ``PathEscape`` from ``open_folder``,
  ``open_for_read`` and ``vaf/core/persistence.py``, which no route module calls; and VAF's
  own exceptions that wrap a caught error's text (``UserAdminError`` and ``BackupError``, the
  two that reach a reply, build it with ``describe_error`` now).
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
import os
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
_TEXT_ATTRS = {"args", "filename", "filename2", "strerror", "__str__", "__repr__"}
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


def _is_log_name(name):
    """A name that says "log" as a word of its own: ``log``, ``log_*``, ``*_log``, ``logger``,
    ``logging``. Not a substring: ``_login_failure``, ``price_catalog`` and
    ``get_app_log_dir`` are no log calls."""
    plain = str(name).lstrip("_").lower()
    return (plain in ("log", "logger", "logging") or plain.startswith(("log_", "logger_"))
            or plain.endswith(("_log", "_logger")))


def _is_logger(node):
    """The receiver of ``<receiver>.warning(...)``: a logger by its name, or a
    ``getLogger(...)`` call."""
    if isinstance(node, ast.Name):
        return _is_log_name(node.id)
    if isinstance(node, ast.Attribute):
        return _is_log_name(node.attr)
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        return name == "getLogger"
    return False


def _allowed_sink(call):
    """A call whose arguments may carry an exception's text: a log call or describe_error."""
    func = call.func
    if isinstance(func, ast.Name):
        return _is_log_name(func.id) or func.id in ("print", "describe_error")
    if isinstance(func, ast.Attribute):
        return (func.attr == "describe_error" or _is_log_name(func.attr)
                or (func.attr in _LOG_ATTRS and _is_logger(func.value)))
    return False


def _reads_text(node, names):
    """Is ``node`` a read of the text of one of ``names`` (the handler's name and its
    aliases)?"""
    def is_var(n):
        return isinstance(n, ast.Name) and n.id in names

    def holds_var(n):
        return (is_var(n) or (isinstance(n, (ast.Tuple, ast.List)) and any(is_var(e) for e in n.elts))
                or (isinstance(n, ast.Dict) and any(is_var(v) for v in n.values)))

    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id in ("str", "repr", "format") \
                and node.args and is_var(node.args[0]):
            return True
        if isinstance(func, ast.Name) and func.id == "getattr" and len(node.args) >= 2 \
                and is_var(node.args[0]):
            attr = node.args[1]
            # A name that is not a constant could be any attribute: judged as text.
            return not (isinstance(attr, ast.Constant) and attr.value not in _TEXT_ATTRS)
        if isinstance(func, ast.Attribute) and func.attr in ("format", "format_map") and (
                any(holds_var(a) for a in node.args)
                or any(holds_var(k.value) for k in node.keywords)):
            return True
    if isinstance(node, ast.FormattedValue) and is_var(node.value):
        return True
    if isinstance(node, ast.Attribute) and node.attr in _TEXT_ATTRS and is_var(node.value):
        return True
    # `"%s" % e`, and a format string held in a variable: `tmpl % e`.
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and holds_var(node.right):
        return True
    return False


def _is_exception_text(node):
    """Traceback text, or the exception currently being handled (``sys.exc_info()``, the
    only way a bare ``except:`` reaches it), read wherever it is called."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return name in _TRACEBACK_TEXT or name == "exc_info"


def _in_allowed_sink(node, parents):
    """Judged by the NEAREST call the text is an argument of, or a comparison before it: a
    log call further up does not excuse a reply built inside it. A call on the text itself
    (``str(e).lower()``) passes it on and is looked through."""
    child = node
    while child in parents:
        parent = parents[child]
        if isinstance(parent, ast.Compare):
            return True
        if isinstance(parent, ast.Call):
            if child is parent.func:
                child = parent
                continue
            return _allowed_sink(parent)
        if isinstance(parent, (ast.stmt, ast.ExceptHandler)):
            return False
        child = parent
    return False


def _aliases(handler):
    """The handler's name and every name a plain ``x = <one of them>`` binds in its body."""
    names = {handler.name}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(handler):
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Name)
                    and node.value.id in names):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id not in names:
                        names.add(target.id)
                        changed = True
    return names


def scan_source(source, filename, module_name):
    """``file:line`` of every read of an exception's text, or of traceback text, that is not
    an argument of a log call or ``describe_error``, or part of a comparison."""
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
        aliases = _aliases(handler)
        stack = [(node, aliases) for node in handler.body]
        while stack:
            node, names = stack.pop()
            if isinstance(node, ast.ExceptHandler) and node.name in names:
                # Rebinds that one name, which is judged as a handler of its own; the other
                # names still hold the outer exception in its body.
                names = names - {node.name}
            if _reads_text(node, names) and not _in_allowed_sink(node, parents):
                findings.append((node.lineno, node.col_offset))
            if _wraps_into_a_raise(node, names):
                findings.append((node.lineno, node.col_offset))
            # A recognised read is no reason to stop: getattr(e, "filename", send(str(e)))
            # runs its default eagerly.
            stack.extend((child, names) for child in ast.iter_child_nodes(node))
    return sorted(f"{filename}:{line}" for line, _col in set(findings))


def _wraps_into_a_raise(node, names):
    """``raise Wrapper(e)``: an exception built with the caught one as an argument carries its
    text (rule (a) of describe_error passes a wrapper's text unchanged, so the global handler
    answered ``raise RuntimeError(e)`` with the path). ``raise X(...) from e`` alone is fine."""
    if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
        return False
    values = list(node.exc.args) + [k.value for k in node.exc.keywords]
    for value in values:
        if isinstance(value, ast.Starred):
            value = value.value
        if isinstance(value, ast.Name) and value.id in names:
            return True
        if isinstance(value, (ast.Tuple, ast.List)) and any(
                isinstance(e, ast.Name) and e.id in names for e in value.elts):
            return True
    return False


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
    # a name that merely contains "log" is no log call
    "login_name": 'try:\n    x()\nexcept Exception as e:\n    _login_reply(str(e))\n',
    "catalog_name": 'try:\n    x()\nexcept Exception as e:\n    price_catalog(str(e))\n',
    "log_dir_name": 'try:\n    x()\nexcept Exception as e:\n    get_app_log_dir(str(e))\n',
    "level_on_a_reply": 'try:\n    x()\nexcept Exception as e:\n    reply.error(str(e))\n',
    # the nearest call decides, not a log call further up
    "reply_inside_log": 'try:\n    x()\nexcept Exception as e:\n    log("x", reply(str(e)))\n',
    "reply_inside_compare": ('try:\n    x()\nexcept Exception as e:\n'
                             '    if send(str(e)) == 1:\n        pass\n'),
    # aliases
    "alias": 'try:\n    x()\nexcept Exception as e:\n    err = e\n    y(str(err))\n',
    "alias_chain": ('try:\n    x()\nexcept Exception as e:\n    a = e\n    b = a\n'
                    '    y(f"{b}")\n'),
    # text attributes, read by name
    "getattr_filename": 'try:\n    x()\nexcept Exception as e:\n    y(getattr(e, "filename"))\n',
    "getattr_args": 'try:\n    x()\nexcept Exception as e:\n    y(getattr(e, "args")[0])\n',
    "getattr_dynamic": 'try:\n    x()\nexcept Exception as e:\n    y(getattr(e, name))\n',
    "strerror": 'try:\n    x()\nexcept Exception as e:\n    y(e.strerror)\n',
    # a format string held in a variable
    "template_percent": ('try:\n    x()\nexcept Exception as e:\n    tmpl = "x %s"\n'
                         '    y(tmpl % e)\n'),
    "template_format": ('try:\n    x()\nexcept Exception as e:\n    tmpl = "x {}"\n'
                        '    y(tmpl.format(e))\n'),
    "format_map": 'try:\n    x()\nexcept Exception as e:\n    y("{x}".format_map({"x": e}))\n',
    # an exception built around the caught one carries its text
    "raise_wrapper": 'try:\n    x()\nexcept Exception as e:\n    raise RuntimeError(e)\n',
    "raise_wrapper_from": 'try:\n    x()\nexcept Exception as e:\n    raise ValueError(e) from e\n',
    "raise_keyword": ('try:\n    x()\nexcept Exception as e:\n'
                      '    raise HTTPException(500, detail=e)\n'),
    "raise_alias": 'try:\n    x()\nexcept Exception as e:\n    err = e\n    raise ValueError("x", err)\n',
    # a nested handler rebinds one name; the outer one still holds the OSError
    "nested_rebinding": ('try:\n    x()\nexcept Exception as e:\n    err = e\n    try:\n'
                         '        z()\n    except ValueError as err:\n'
                         '        return {"error": str(e)}\n'),
    # a recognised read is no end: its default runs eagerly
    "getattr_default": ('try:\n    x()\nexcept Exception as e:\n'
                        '    log(getattr(e, "filename", send(str(e))))\n'),
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
    "tuple_without_oserror": 'try:\n    x()\nexcept (ValueError, KeyError) as e:\n    y(str(e))\n',
    "method_on_the_text": ('try:\n    x()\nexcept Exception as e:\n'
                           '    if "denied" in str(e).lower():\n        y()\n'),
    "logger_attribute": 'try:\n    x()\nexcept Exception as e:\n    self.logger.warning("%s", str(e))\n',
    "get_logger": ('import logging\ntry:\n    x()\nexcept Exception as e:\n'
                   '    logging.getLogger(__name__).error(f"x {e}")\n'),
    "log_prefix": 'try:\n    x()\nexcept Exception as e:\n    log_attachment("X", error=str(e))\n',
    "getattr_status": ('try:\n    x()\nexcept Exception as e:\n'
                       '    y(getattr(e, "status_code", None))\n'),
    "raise_from_only": ('try:\n    x()\nexcept Exception as e:\n'
                        '    raise HTTPException(500, detail="failed") from e\n'),
    "raise_described": ('try:\n    x()\nexcept Exception as e:\n'
                        '    raise HTTPException(500, detail=describe_error(e)) from e\n'),
    "reraise": 'try:\n    x()\nexcept Exception as e:\n    log("x", f"{e}")\n    raise\n',
    "nested_inner_text": ('try:\n    x()\nexcept Exception as e:\n    err = e\n    try:\n'
                          '        z()\n    except ValueError as err:\n        y(str(err))\n'),
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
    reason = os.strerror(errno.ENAMETOOLONG)
    assert json.loads(body) == {"ok": False, "error": reason, "detail": reason}
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
    assert detail == f"Write failed: {os.strerror(errno.ENAMETOOLONG)}"

    with monkeypatch.context() as patch:
        patch.setattr(web_server.os, "mkdir", _too_long)
        detail = _detail_of(web_server.create_session_workspace_folder(
            web_server.WorkspaceMkdirRequest(sessionId="s1", name="new"), _request()))
    assert detail == f"Create failed: {os.strerror(errno.ENAMETOOLONG)}"

    (workspace / "old.txt").write_text("x", encoding="utf-8")
    with monkeypatch.context() as patch:
        patch.setattr(web_server.os, "remove", _too_long)
        detail = _detail_of(web_server.delete_session_workspace_entry(
            web_server.WorkspaceDeleteRequest(sessionId="s1", name="old.txt"), _request()))
    assert detail == f"Delete failed: {os.strerror(errno.ENAMETOOLONG)}"
    assert (workspace / "old.txt").exists()


# ---- the office conversion routes and the upload lanes --------------------------------------

def _office_package(tmp_path, kind):
    """The bytes of a real package of ``kind``: "docx", "pptx", "xlsx", or one whose main
    content type was swapped: "template" (.dotx), "macro" (.docm), "deck_template" (.potx)."""
    import zipfile

    docx = pytest.importorskip("docx")
    pptx = pytest.importorskip("pptx")
    openpyxl = pytest.importorskip("openpyxl")
    folder = tmp_path / "packages"
    folder.mkdir(exist_ok=True)
    made = {"docx": folder / "real.docx", "pptx": folder / "real.pptx",
            "xlsx": folder / "real.xlsx"}
    if not made["docx"].exists():
        docx.Document().save(made["docx"])
        pptx.Presentation().save(made["pptx"])
        openpyxl.Workbook().save(made["xlsx"])
    if kind in made:
        return made[kind].read_bytes()
    source, old, new = {
        "template": ("docx", b"wordprocessingml.document.main+xml",
                     b"wordprocessingml.template.main+xml"),
        "macro": ("docx",
                  b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
                  b"application/vnd.ms-word.document.macroEnabled.main+xml"),
        "deck_template": ("pptx", b"presentationml.presentation.main+xml",
                          b"presentationml.template.main+xml"),
    }[kind]
    out = folder / f"{kind}.zip"
    with zipfile.ZipFile(made[source]) as zin, zipfile.ZipFile(out, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "[Content_Types].xml":
                assert old in data
                data = data.replace(old, new)
            zout.writestr(item, data)
    return out.read_bytes()


_WORD_ROUTES = ("/api/file/as-html", "/api/file/docx-model")


@pytest.mark.parametrize("name, content, routes, label", [
    # not a package at all
    ("empty.docx", b"", _WORD_ROUTES, "Word"),
    ("notes.docx", b"plain text, not a zip", _WORD_ROUTES, "Word"),
    ("slides.pptx", b"plain text, not a zip", ("/api/file/as-html",), "PowerPoint"),
    ("empty.pptx", b"", ("/api/file/as-html",), "PowerPoint"),
    # a valid package of the wrong type: the library's ValueError quotes the path it was given
    ("sheet.docx", "xlsx", _WORD_ROUTES, "Word"),
    ("deck.docx", "pptx", _WORD_ROUTES, "Word"),
    ("template.docx", "template", _WORD_ROUTES, "Word"),
    ("macro.docx", "macro", _WORD_ROUTES, "Word"),
    ("letter.pptx", "docx", ("/api/file/as-html",), "PowerPoint"),
    ("sheet.pptx", "xlsx", ("/api/file/as-html",), "PowerPoint"),
    ("template.pptx", "deck_template", ("/api/file/as-html",), "PowerPoint"),
])
def test_an_office_file_that_is_no_package_answers_without_the_path(tmp_path, monkeypatch, name,
                                                                     content, routes, label):
    """python-docx and python-pptx quote the path they were handed: PackageNotFoundError for a
    file that is not a package, a ValueError for a package of the wrong type, with a "~"
    spelling expanded to the home folder. MUTATION: open from the path again in
    vaf/core/office_package.py, or answer the library's own error."""
    from fastapi.testclient import TestClient

    from vaf.core.platform import Platform
    from vaf.core.web_server import app

    data = content if isinstance(content, bytes) else _office_package(tmp_path, content)
    pytest.importorskip("docx")
    pytest.importorskip("pptx")
    home = tmp_path / "home-ab12cd34"
    docs = home / "Documents"
    docs.mkdir(parents=True)
    (docs / name).write_bytes(data)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Platform, "served_file_roots", staticmethod(lambda: [docs.resolve()]))
    monkeypatch.setattr("vaf.api.user_routes.caller_is_admin", lambda request: True)
    client = TestClient(app, raise_server_exceptions=False, client=("127.0.0.1", 50000))
    for route in routes:
        for spelled in (str(docs / name), "~/Documents/" + name, f"{docs}/./{name}"):
            answer = client.get(route, params={"path": spelled})
            assert answer.status_code == 500, (route, spelled, answer.status_code, answer.text[:200])
            assert answer.json()["detail"].endswith(f": not a valid {label} file"), answer.text
            assert str(home) not in answer.text and str(home.resolve()) not in answer.text


@pytest.mark.parametrize("name, kind, label", [
    ("deck.docx", "pptx", "Word"),
    ("template.docx", "template", "Word"),
    ("letter.pptx", "docx", "PowerPoint"),
])
def test_an_uploaded_office_file_of_the_wrong_type_names_no_temporary_path(tmp_path, monkeypatch,
                                                                           name, kind, label):
    """Both upload lanes write the file to the temporary folder (on Windows under
    C:\\Users\\<name>) and let the librarian read it. MUTATION: open the package from its
    path in vaf/tools/librarian.py again."""
    import base64
    import tempfile

    from vaf.core import web_server
    from vaf.core.web_server import process_files_to_sidebar_list, process_uploaded_files

    # The sidebar lane also asks for a preview (the Gotenberg service, then LibreOffice as a
    # subprocess); neither belongs in this test.
    monkeypatch.setattr(web_server, "_office_to_pdf_via_gotenberg", lambda *a, **k: None)
    monkeypatch.setattr(web_server, "_docx_to_pdf_via_libreoffice", lambda *a, **k: None)
    data = base64.b64encode(_office_package(tmp_path, kind)).decode("ascii")
    upload = [{"name": name, "data": data, "mimeType": "application/octet-stream"}]
    temporary = tempfile.gettempdir()
    (entry,) = asyncio.run(process_files_to_sidebar_list(upload))
    assert f"not a valid {label} file" in entry["content"], entry["content"][:300]
    assert temporary not in entry["content"]
    chat_text = asyncio.run(process_uploaded_files(upload))
    assert f"not a valid {label} file" in chat_text and temporary not in chat_text, chat_text[:300]


@pytest.mark.parametrize("name, kind, label", [
    ("deck.docx", "pptx", "Word"),
    ("letter.pptx", "docx", "PowerPoint"),
])
def test_the_read_file_tool_opens_an_office_file_from_its_bytes(tmp_path, monkeypatch, name,
                                                                kind, label):
    """The model's file reader answers a package of the wrong type in the opener's words.
    MUTATION: open from the path again in vaf/tools/filesystem.py - red."""
    from vaf.tools import filesystem

    target = tmp_path / "home-ab12cd34" / name
    target.parent.mkdir()
    target.write_bytes(_office_package(tmp_path, kind))
    monkeypatch.setattr(filesystem, "is_safe_path", lambda path, read_only=True: (True, str(target)))
    out = filesystem.ReadFileTool()._read(path=str(target))
    assert f"not a valid {label} file" in out and str(target.parent) not in out, out[:300]


def test_the_office_opener_hands_the_libraries_bytes_never_a_path(tmp_path, monkeypatch):
    """Mapping the open's errors covers the shapes measured; the bytes cover every other error
    the libraries raise later, which then names a BytesIO. An open file would not do: its
    repr carries the name. MUTATION: hand the library the path - red."""
    import io

    docx = pytest.importorskip("docx")
    pptx = pytest.importorskip("pptx")
    from vaf.core.office_package import open_powerpoint, open_word

    target = tmp_path / "home-ab12cd34" / "x.docx"
    target.parent.mkdir()
    target.write_bytes(b"anything")
    handed = []
    monkeypatch.setattr(docx, "Document", lambda arg: handed.append(arg) or "doc")
    monkeypatch.setattr(pptx, "Presentation", lambda arg: handed.append(arg) or "deck")
    assert open_word(target) == "doc" and open_powerpoint(target) == "deck"
    assert [type(arg) for arg in handed] == [io.BytesIO, io.BytesIO]
    assert all(arg.getvalue() == b"anything" for arg in handed)


def test_the_office_opener_reads_a_bounded_size(tmp_path):
    """Opened from its bytes, a package is held whole in memory: more than the bound is
    refused before the library sees it, with no path. MUTATION: drop the bound."""
    from vaf.core.office_package import OfficeFileUnreadable, open_powerpoint, open_word

    target = tmp_path / "home-ab12cd34" / "big.docx"
    target.parent.mkdir()
    target.write_bytes(_office_package(tmp_path, "docx"))
    size = target.stat().st_size
    assert open_word(target, max_bytes=size) is not None
    for opener in (open_word, open_powerpoint):
        with pytest.raises(OfficeFileUnreadable) as refused:
            opener(target, max_bytes=size - 1)
        assert "too large to open here" in str(refused.value)
        assert str(target.parent) not in str(refused.value)


def test_a_services_http_error_keeps_its_status_and_drops_its_url():
    """The cloud routes and the model download answer a requests or huggingface_hub HTTP
    error, an OSError from another module: withheld, but a refused login and a missing file
    stay told apart. MUTATION: drop the status from `_withheld` - red."""
    import requests

    from vaf.core.path_jail import describe_error

    response = requests.Response()
    response.status_code = 401
    error = requests.HTTPError(
        "401 Client Error: Unauthorized for url: https://h.example/x?token=t0k3n", response=response)
    assert describe_error(error, log=False) == "HTTPError (HTTP 401)"
    assert describe_error(requests.ConnectionError("HTTPSConnectionPool(host='h.example')"),
                          log=False) == "ConnectionError (details in the server log)"
