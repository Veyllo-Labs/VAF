# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""CI guard: no blocking HTTP may be reachable from an `async def`.

A synchronous `httpx`/`requests` call inside an async route - directly, or via a sync
helper the route awaits - blocks the ENTIRE uvicorn event loop. Every router mounts on the
same app, so it stalls every HTTP request AND the `/ws` WebSocket, for every user, for as
long as the vendor takes to answer.

Incident 2026-07-20: `/api/voice/elevenlabs/{models,voices}` did exactly that (15 s timeout,
up to 3 sequential pages), which froze the UI while the owner switched the speech provider.
The same pattern was live in `email_routes` (OAuth verify) and `telegram_routes` (dashboard).

CLAUDE.md prefers a CI guard over a prose rule, so this is the guard. To fix an offender,
use the conventions already in this codebase:
  - an async client: `async with httpx.AsyncClient(timeout=...) as c: await c.get(...)`
    (see `vaf/api/tts_routes.py`), or
  - keep the sync helper and await it off the loop: `await asyncio.to_thread(helper, ...)`
    (see `vaf/api/cloud_routes.py`, `vaf/api/email_routes.py`).
"""
import ast
from pathlib import Path

_ROOTS = ("vaf/api", "vaf/core")
_BLOCKING_MODULES = {"httpx", "requests"}
_BLOCKING_ATTRS = {"get", "post", "put", "patch", "delete", "head", "request"}

# Modules whose async code runs on its OWN event loop in its OWN thread rather than the
# shared uvicorn loop would belong here: blocking there cannot freeze the web UI or the
# WebSocket, only that component's own message handling. Kept empty on purpose - the one
# candidate (telegram_bridge, a python-telegram-bot Application in a daemon thread) had its
# six blocking file up/downloads converted too, so the guard now covers every module.
# Add an entry ONLY with a written reason; an exclusion must never be a quiet workaround.
_OWN_EVENT_LOOP: set[str] = set()


def _is_blocking_call(node: ast.AST) -> bool:
    """True for `httpx.get(...)` / `requests.post(...)` and friends (module-level API only:
    an AsyncClient is used as `client.get`, which is not a Name-qualified module call)."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    target = node.func.value
    return (
        isinstance(target, ast.Name)
        and target.id in _BLOCKING_MODULES
        and node.func.attr in _BLOCKING_ATTRS
    )


def _offenders_in(path: Path):
    """(async_fn, how, lineno) for every blocking call reachable from an async def."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return []

    sync_blocking: dict[str, int] = {}   # sync helper name -> line of its blocking call
    async_funcs: list[ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for sub in ast.walk(node):
                if _is_blocking_call(sub):
                    sync_blocking.setdefault(node.name, sub.lineno)
        elif isinstance(node, ast.AsyncFunctionDef):
            async_funcs.append(node)

    found = []
    for fn in async_funcs:
        for sub in ast.walk(fn):
            # (a) blocking call written directly inside the async def
            if _is_blocking_call(sub):
                found.append((fn.name, "directly", sub.lineno))
            # (b) the async def CALLS a sync helper that blocks. `asyncio.to_thread(helper)`
            #     passes the helper as an ARGUMENT, not as the callee, so it is not flagged.
            elif isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                line = sync_blocking.get(sub.func.id)
                if line is not None:
                    found.append((fn.name, f"via {sub.func.id}() (blocks at line {line})", sub.lineno))
    return [(path, *f) for f in found]


def test_no_blocking_http_reachable_from_async_def():
    offenders = []
    for root in _ROOTS:
        base = Path(root)
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if path.as_posix() in _OWN_EVENT_LOOP:
                continue  # runs on its own loop/thread - see _OWN_EVENT_LOOP above
            offenders.extend(_offenders_in(path))

    assert not offenders, "Blocking HTTP on the event loop:\n" + "\n".join(
        f"  {p}:{line} - async def {fn}() blocks {how}" for p, fn, how, line in offenders
    ) + "\n\nUse httpx.AsyncClient (see tts_routes.py) or await asyncio.to_thread(helper, ...)."


def test_the_guard_actually_detects_both_shapes(tmp_path):
    """Pin the detector itself, so a future refactor cannot silently make it blind."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import httpx\n"
        "import asyncio\n"
        "def helper():\n"
        "    return httpx.get('https://x')\n"
        "async def route_via_helper():\n"
        "    return helper()\n"
        "async def route_direct():\n"
        "    return requests.post('https://x')\n"
        "async def route_ok():\n"
        "    return await asyncio.to_thread(helper)\n",
        encoding="utf-8",
    )
    hits = _offenders_in(sample)
    flagged = {fn for _p, fn, _how, _line in hits}
    assert "route_via_helper" in flagged      # sync helper awaited from async
    assert "route_direct" in flagged          # blocking call written inline
    assert "route_ok" not in flagged          # to_thread passes it as an argument
