# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One rendered look at a page: the probe behind the build-run-inspect-fix loop.

`render_page(target)` opens a URL or a workspace file in the sandbox browser,
waits for it to load, and answers with what a developer would look at first:
the final URL and title, the rendered text, every console message and page
error, every failed network request, and a screenshot. It is a PROBE, not an
agent: no clicking, no typing, one navigation - the coder calls it after
writing a page ("build, render, read, fix, repeat", the same shape as its
run_tests loop), and the main agent exposes it as the render_check tool. For
reproducible end-to-end suites, real test tooling remains the right answer;
this is the quick look between edits.

Placement and reuse: harness internals beside browser_interactive, whose
helpers carry everything browser-shaped here - endpoint resolution honours
the per-user pool, the busy answer comes from the same managers that arbitrate
the lease, and file targets ride the EXISTING workspace mirror
(/home/browser/Workspace) instead of a serving route of their own. The probe
never evicts anyone: a browser somebody is using answers busy instead.

localhost: the container's localhost is the container. Targets addressing
localhost/127.0.0.1/0.0.0.0 are rewritten to host.docker.internal (mapped by
compose/pool to the host) and the answer says so. Honest limit, documented
where the tool is: a dev server bound strictly to 127.0.0.1 is unreachable
from any container - it must listen on 0.0.0.0 (e.g. `next dev -H 0.0.0.0`).
The name adds no reachability the container did not have: the docker bridge
address of the host was always routable; host.docker.internal merely names it.
"""

from __future__ import annotations

import json
import os
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from vaf.core.log_helper import append_domain_log

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0"}
_LOAD_TIMEOUT_S = 25.0


def _endpoint_for(scope: str):
    """(cdp_base, manager) for this scope - the pool instance when one exists,
    the shared browser otherwise. Mirrors the browser tool's resolution.
    Strict mode's PoolExhausted propagates: silently rendering on the SHARED
    browser is exactly the fallback strict forbids, and the render lane must
    refuse like every other lane rather than become the quiet exception."""
    from vaf.core import browser_interactive as bi
    try:
        from vaf.core.browser_pool import PoolExhausted, get_browser_pool
        inst = get_browser_pool().resolve(scope)
    except PoolExhausted:
        raise
    except Exception:
        inst = None
    if inst is not None:
        return inst.cdp_base, bi._manager_for_instance(inst)
    return bi.get_interactive_manager().cdp_base(), bi.get_interactive_manager()


def _rewrite_local(target: str):
    """(url, rewritten) with container-unreachable local hosts renamed."""
    parts = urlsplit(target)
    host = (parts.hostname or "").lower()
    if host in _LOCAL_HOSTS:
        netloc = "host.docker.internal"
        if parts.port:
            netloc += f":{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment)), True
    return target, False


def _file_target_to_container_url(target: str, scope: str, manager) -> Optional[str]:
    """A host file under the caller's own project root, as a mirror file:// URL.

    The jail is the point: only files under get_user_projects_root(scope) may
    be rendered - the same boundary every file tool enforces - and the mirror
    is synced first so the file the coder wrote two seconds ago is the file
    the browser opens. Returns None when the path is outside the jail or the
    root is unknown."""
    from vaf.core.session import get_user_projects_root
    root = get_user_projects_root(scope)
    if root is None:
        return None
    try:
        real_root = os.path.realpath(os.fspath(root))
        real_target = os.path.realpath(os.path.expanduser(target))
        if not (real_target == real_root or real_target.startswith(real_root + os.sep)):
            return None
        if not os.path.isfile(real_target):
            return None
        manager.sync_workspace(scope)
        rel = os.path.relpath(real_target, real_root).replace(os.sep, "/")
        return f"file:///home/browser/Workspace/{rel}"
    except Exception:
        return None


def render_page(target: str, user_scope_id: Optional[str] = None,
                wait_ms: int = 1500, max_text: int = 8000) -> dict:
    """Render one target and report what a developer would look at.

    Never raises. BLOCKING (CDP, load wait): call from a worker thread.
    Answer keys: ok, busy, error, requested, url, rewritten, title, text,
    console, page_errors, failed_requests, screenshot_b64."""
    from vaf.core.browser_interactive import resolve_browser_scope

    out = {"ok": False, "busy": False, "error": "", "requested": target,
           "url": "", "rewritten": False, "title": "", "text": "",
           "console": [], "page_errors": [], "failed_requests": [],
           "screenshot_b64": ""}
    try:
        scope = resolve_browser_scope(user_scope_id)
        cdp_base, manager = _endpoint_for(scope)

        # A browser somebody is using is theirs: the probe waits for no one
        # and evicts no one - busy is an answer, not an error to retry blindly.
        if manager.has_activity():
            out["busy"] = True
            out["error"] = ("The browser is in use (an interactive session or an "
                            "agent run). Try again when it is free.")
            return out

        t = target.strip()
        if t.lower().startswith(("http://", "https://")):
            url, out["rewritten"] = _rewrite_local(t)
        else:
            url = _file_target_to_container_url(t, scope, manager)
            if url is None:
                out["error"] = (
                    "Not renderable: the target must be an http(s) URL or an "
                    "existing file inside your own project workspace.")
                return out
        result = _render_via_cdp(cdp_base, url, wait_ms=wait_ms, max_text=max_text)
        out.update(result)
        return out
    except Exception as e:
        out["error"] = f"render failed: {type(e).__name__}: {e}"
        append_domain_log("webui", f"[browser_render] {out['error']}")
        return out


def _render_via_cdp(cdp_base: str, url: str, *, wait_ms: int, max_text: int) -> dict:
    """The protocol half - navigate, observe, screenshot, park. Own seam so
    tests pin the orchestration above without a browser. BLOCKING."""
    import asyncio

    from vaf.core.browser_interactive import resolve_cdp_ws_url

    ws_url = resolve_cdp_ws_url(cdp_base)

    async def _main() -> dict:
        from cdp_use.client import CDPClient
        res = {"ok": False, "error": "", "url": url, "title": "", "text": "",
               "console": [], "page_errors": [], "failed_requests": [],
               "screenshot_b64": ""}
        client = CDPClient(ws_url)
        await client.start()
        try:
            targets = await asyncio.wait_for(client.send.Target.getTargets(), 10)
            pages = [t for t in targets.get("targetInfos", []) if t.get("type") == "page"]
            if not pages:
                res["error"] = "the browser has no page to render into"
                return res
            att = await asyncio.wait_for(
                client.send.Target.attachToTarget(
                    params={"targetId": pages[0]["targetId"], "flatten": True}), 10)
            sid = att["sessionId"]

            console: list = []
            page_errors: list = []
            failed: list = []
            req_urls: dict = {}
            loaded = asyncio.Event()

            def _on_console(ev, session_id=None):
                try:
                    # debug level dropped deliberately: the ad-block extension
                    # logs debug chatter on every page (measured: 9 of 11
                    # entries on a trivial probe), and it would bury the
                    # developer's own log/warn/error lines.
                    if ev.get("type") == "debug":
                        return
                    args = ev.get("args", [])
                    text = " ".join(str(a.get("value", a.get("description", "")))
                                    for a in args)[:500]
                    console.append(f"[{ev.get('type', 'log')}] {text}")
                except Exception:
                    pass

            def _on_request(ev, session_id=None):
                try:
                    req_urls[ev.get("requestId")] = str(
                        (ev.get("request") or {}).get("url", ""))[:200]
                except Exception:
                    pass

            def _on_exception(ev, session_id=None):
                try:
                    d = ev.get("exceptionDetails", {})
                    text = (d.get("exception", {}) or {}).get("description") or d.get("text", "")
                    page_errors.append(str(text)[:500])
                except Exception:
                    pass

            def _on_load_failed(ev, session_id=None):
                try:
                    # The event carries only a requestId; the URL a developer
                    # actually needs ("WHICH file failed") comes from the
                    # requestWillBeSent correlation above.
                    url_ = req_urls.get(ev.get("requestId"), "")
                    failed.append(f"{ev.get('errorText', 'failed')}: {url_}".rstrip(": "))
                except Exception:
                    pass

            def _on_response(ev, session_id=None):
                try:
                    r = ev.get("response", {})
                    if int(r.get("status", 0)) >= 400:
                        failed.append(f"HTTP {r.get('status')}: {str(r.get('url', ''))[:200]}")
                except Exception:
                    pass

            def _on_loaded(ev, session_id=None):
                loaded.set()

            client.register.Runtime.consoleAPICalled(_on_console)
            client.register.Runtime.exceptionThrown(_on_exception)
            client.register.Network.requestWillBeSent(_on_request)
            client.register.Network.loadingFailed(_on_load_failed)
            client.register.Network.responseReceived(_on_response)
            client.register.Page.loadEventFired(_on_loaded)

            await asyncio.wait_for(client.send.Runtime.enable(session_id=sid), 10)
            await asyncio.wait_for(client.send.Network.enable(params={}, session_id=sid), 10)
            await asyncio.wait_for(client.send.Page.enable(session_id=sid), 10)
            await asyncio.wait_for(
                client.send.Page.navigate(params={"url": url}, session_id=sid), 15)
            try:
                await asyncio.wait_for(loaded.wait(), _LOAD_TIMEOUT_S)
            except asyncio.TimeoutError:
                page_errors.append(f"load event did not fire within {int(_LOAD_TIMEOUT_S)}s "
                                   "(the page may still be usable below)")
            await asyncio.sleep(max(0.0, wait_ms / 1000.0))

            ev = await asyncio.wait_for(
                client.send.Runtime.evaluate(
                    params={"expression":
                            "JSON.stringify({t: document.title,"
                            " x: document.body ? document.body.innerText : ''})",
                            "returnByValue": True},
                    session_id=sid), 15)
            try:
                payload = json.loads((ev.get("result") or {}).get("value") or "{}")
            except Exception:
                payload = {}
            res["title"] = str(payload.get("t", ""))[:300]
            text = str(payload.get("x", "")).strip()
            if len(text) > max_text:
                text = text[:max_text] + "\n[... truncated ...]"
            res["text"] = text
            try:
                cap = await asyncio.wait_for(
                    client.send.Page.captureScreenshot(
                        params={"format": "jpeg", "quality": 70}, session_id=sid), 15)
                res["screenshot_b64"] = cap.get("data") or ""
            except Exception:
                pass
            # Park: the probe leaves the browser the way it found it - a page
            # that keeps animating with nobody watching is the 1027% incident.
            try:
                await asyncio.wait_for(
                    client.send.Page.navigate(params={"url": "about:blank"},
                                              session_id=sid), 10)
            except Exception:
                pass
            res["console"] = console[:50]
            res["page_errors"] = page_errors[:20]
            res["failed_requests"] = failed[:30]
            res["ok"] = True
            return res
        finally:
            try:
                await asyncio.wait_for(client.stop(), 5)
            except Exception:
                pass

    return asyncio.run(_main())
