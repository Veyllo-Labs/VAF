# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Interactive browser lane: who holds the shared sandbox browser, and their logins.

The vaf-browser container's KasmVNC streams the display and carries mouse and
keyboard natively, so nothing in here relays frames or input. This module owns
the two things the stream cannot decide for itself:

- the LEASE: one person drives the shared Chromium at a time, an agent run
  always wins over a person, and a foreign user scope is refused rather than
  silently joined to someone else's cookie jar;
- the LOGINS: cookies are loaded and exported per user scope through the same
  storage-state files the browser agent's persistent sessions use, so a login
  performed by hand is a login the agent has on its next run, and vice versa.

Access to the stream is a TICKET in the URL path (`/api/browser-vnc/t/<ticket>/`),
validated against the lease on every request. It reaches the two halves of the
stream by different routes, and the difference is load-bearing: the KasmVNC
client resolves ASSETS relative to its own directory, so the ticket rides along
on those by itself, but it builds its SOCKET url from settings
(`ws://<host>:<port>/` + the `path` setting) and would dial a ticketless
`/websockify` that does not exist here. The settings are overridable per URL, so
`_payload` hands the ticketed socket path over in `?path=` - see the comment
there. The ticket is the credential (the same pattern as the A2A room seat lane),
which is why the path is auth-middleware-exempt.

Placement: vaf/core beside web_interface/web_server (harness internals per
docs/ARCHITECTURE.md). vaf/tools/browser_agent.py imports THIS module (tools ->
core is the established direction) both for the shared helpers below and to
evict the interactive lease when a run starts.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import List, Optional

from vaf.core.log_helper import append_domain_log


# ---------------------------------------------------------------------------
# Shared helpers (moved here from vaf/tools/browser_agent.py, which delegates)
# ---------------------------------------------------------------------------

def resolve_cdp_ws_url(base: str) -> str:
    """Fetch /json/version and return the full webSocketDebuggerUrl.

    Accepts both http:// and ws:// base URLs. Polls with bounded backoff instead
    of a single probe: a connect that lands inside the container's startup
    window (compose healthcheck start_period ~20s) or hits a still-booting /
    just-restarted vaf-browser must not fail on the first try. Deadline is
    VAF_BROWSER_CDP_WAIT_S (default 30s, > start_period + one healthcheck
    interval). BLOCKING (sleeps): call from a worker thread, never on an event
    loop.
    """
    import urllib.request as _urlreq

    http_base = base.replace("ws://", "http://").replace("wss://", "https://")
    url = http_base.rstrip("/") + "/json/version"
    try:
        _deadline_s = float(os.environ.get("VAF_BROWSER_CDP_WAIT_S", "30") or 30)
    except Exception:
        _deadline_s = 30.0
    _deadline = time.monotonic() + max(0.0, _deadline_s)
    last_err: Exception | None = None
    attempt = 0
    while True:
        attempt += 1
        try:
            with _urlreq.urlopen(url, timeout=3) as resp:
                data = json.loads(resp.read())
            ws_url = data["webSocketDebuggerUrl"]
            # Ensure the hostname matches what the host can reach
            # (Chromium may report its internal container hostname)
            ws_url = re.sub(r"ws://[^/]+", f"ws://{http_base.split('//')[1].split('/')[0]}", ws_url)
            return ws_url
        except Exception as e:
            last_err = e
            if time.monotonic() >= _deadline:
                break
            time.sleep(min(1.5, max(0.5, 0.5 * attempt)))
    raise RuntimeError(
        f"Browser service not ready: Chrome DevTools at {http_base} did not respond within "
        f"{int(_deadline_s)}s. Is `vaf-browser` running/healthy? "
        f"Check: docker ps | grep vaf-browser ; docker logs vaf-browser\n"
        f"Details: {last_err}"
    ) from last_err


def browser_storage_state_path(user_scope_id: Optional[str] = None,
                               session_name: str = "default") -> str:
    """Path of the per-scope persistent browser login store, creating its dir.

    USER ISOLATION: the cookie/login store is keyed by user_scope_id so one
    user's persistent browser logins are never shared with (or readable by)
    another. Scope resolution: explicit arg, the child-process env, the local
    admin scope (single-user/local mode), then "default".
    """
    _scope = user_scope_id or os.environ.get("VAF_USER_SCOPE_ID")
    if not _scope:
        try:
            from vaf.core.config import get_local_admin_scope_id
            _scope = get_local_admin_scope_id()
        except Exception:
            _scope = "default"
    scope_seg = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(_scope)) or "default"
    sessions_dir = os.path.join(os.path.expanduser("~"), ".vaf", "browser_sessions", scope_seg)
    os.makedirs(sessions_dir, exist_ok=True)
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_name) or "default"
    return os.path.join(sessions_dir, f"{safe_name}.json")


def park_browser_idle(cdp_base: str) -> None:
    """Leave the shared browser on one blank tab when a run or session ends.

    Closing a CDP session only drops the connection; the container's Chromium
    keeps every tab exactly as it was left, and a page that animates keeps
    rendering forever. Measured live: one visit to an animated site left
    `vaf-browser` at 1027% CPU (ten cores) minutes after the agent had finished
    and reported - the machine stayed loaded with nobody watching. A parked
    browser costs about 5%.

    HTTP CDP endpoints on purpose: they are the one interface that works
    whether or not the session ended cleanly, and this must also be reachable
    after a crashed or cancelled run. Best-effort throughout - a browser that
    cannot be parked is not a reason to fail a finished task.
    """
    import urllib.request as _req

    def _call(path: str, method: str = "GET") -> str:
        r = _req.Request(cdp_base.rstrip("/") + path, method=method)
        with _req.urlopen(r, timeout=5) as resp:
            return resp.read().decode()

    try:
        pages = [t for t in json.loads(_call("/json/list")) if t.get("type") == "page"]
        if not pages:
            # Nothing to keep. Only here is a new tab created, and it is the rare
            # case: the browser has no page at all and the next run needs one.
            try:
                _call("/json/new?about:blank", "PUT")
            except Exception:
                _call("/json/new?about:blank")
            return

        # KEEP the oldest page and empty it by NAVIGATING; do not create a tab and
        # close the rest. Chromium's window is launched in app mode (no tab strip,
        # no toolbar) and only that FIRST window is an app window: a tab created
        # through CDP comes up as an ordinary browser window with all its chrome.
        # The previous version created one and closed the app window along with it
        # (its data: URL is not "about:blank"), so re-opening the browser showed a
        # whole browser UI inside the window - a browser in the browser.
        keep, rest = pages[0], pages[1:]
        for target in rest:
            try:
                _call("/json/close/" + target["id"])
            except Exception:
                pass

        if not str(keep.get("url", "")).startswith("about:blank"):
            _navigate_blank(keep.get("webSocketDebuggerUrl") or "")
    except Exception:
        pass


def _navigate_blank(target_ws_url: str) -> None:
    """Point one page at about:blank over its own CDP socket. Best-effort.

    A page is emptied rather than closed so the window it lives in survives; the
    HTTP CDP API can list and close targets but cannot navigate one, which is why
    this reaches for the socket.
    """
    if not target_ws_url:
        return
    import asyncio

    async def _go():
        from websockets.asyncio.client import connect
        async with connect(target_ws_url, open_timeout=5, ping_interval=None) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Page.navigate",
                                      "params": {"url": "about:blank"}}))
            await asyncio.wait_for(ws.recv(), 5)

    try:
        asyncio.run(_go())
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Cookie transfer between the shared jar and the per-scope storage-state files
# ---------------------------------------------------------------------------

def _cookie_op(cdp_base: str, op: str, cookies: Optional[List[dict]] = None):
    """One short-lived CDP connection for a Storage-domain cookie operation.

    Browser-level calls, no target session needed (the same three calls
    browser_use itself makes). BLOCKING (opens its own event loop): call from a
    worker thread. A dedicated CDPClient per operation on purpose - cdp_use's
    event registry allows one handler per event, so sharing a client with any
    other lane would be a conflict waiting to happen.
    """
    import asyncio

    ws_url = resolve_cdp_ws_url(cdp_base)

    async def _main():
        from cdp_use.client import CDPClient
        client = CDPClient(ws_url)
        await client.start()
        try:
            if op == "get":
                res = await asyncio.wait_for(client.send.Storage.getCookies(params={}), 10)
                return res.get("cookies", [])
            if op == "set":
                if cookies:
                    await asyncio.wait_for(
                        client.send.Storage.setCookies(params={"cookies": cookies}), 10)
                return None
            if op == "clear":
                await asyncio.wait_for(client.send.Storage.clearCookies(params={}), 10)
                return None
            raise ValueError(f"unknown cookie op: {op}")
        finally:
            try:
                await asyncio.wait_for(client.stop(), 5)
            except Exception:
                pass

    return asyncio.run(_main())


def _load_storage_cookies(path: str) -> List[dict]:
    """Read a Playwright-shaped storage_state file into CDP CookieParam dicts.

    Mirrors browser_use's own restore normalisation: session cookies are stored
    with expires 0/-1, and CDP rejects those values, so the key is omitted.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        return []
    out: List[dict] = []
    for c in state.get("cookies", []) or []:
        if not c.get("name") or c.get("domain") is None:
            continue
        param = {
            "name": c["name"],
            "value": c.get("value", ""),
            "domain": c["domain"],
            "path": c.get("path", "/"),
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", False)),
        }
        if c.get("sameSite") in ("Strict", "Lax", "None"):
            param["sameSite"] = c["sameSite"]
        exp = c.get("expires")
        if isinstance(exp, (int, float)) and exp not in (0, -1):
            param["expires"] = exp
        out.append(param)
    return out


def _export_storage_cookies(path: str, cookies: List[dict]) -> None:
    """Write CDP cookies as a Playwright-shaped storage_state file, atomically.

    The exact 8-field mapping browser_use's export_storage_state uses, so the
    file stays readable by the agent's persistent-session lane and vice versa.
    origins stays empty: nothing on either lane round-trips localStorage.
    """
    state = {
        "cookies": [
            {
                "name": c.get("name", ""),
                "value": c.get("value", ""),
                "domain": c.get("domain", ""),
                "path": c.get("path", "/"),
                "expires": c.get("expires", -1),
                "httpOnly": bool(c.get("httpOnly", False)),
                "secure": bool(c.get("secure", False)),
                "sameSite": c.get("sameSite", "Lax"),
            }
            for c in cookies
        ],
        "origins": [],
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# The lease
# ---------------------------------------------------------------------------

@dataclass
class InteractiveLease:
    user_scope_id: str
    session_id: str          # chat session whose window shows the stream
    save_enabled: bool
    session_name: str        # storage-state file name shared with the agent
    ticket: str              # path credential for the stream proxy
    started_at: float
    stream_connections: int = 0
    last_disconnect: float = 0.0


class InteractiveBrowserManager:
    """Singleton arbiter for the interactive use of the shared browser."""

    # A closed viewer gets this long to come back (tab reload, brief network
    # loss) before the lease is released and cookies are exported. Deliberately
    # generous: reading without touching anything is normal browsing, so
    # "activity" is an open stream connection, not input.
    GRACE_S = 120.0
    _LIVENESS_EVERY_TICKS = 3   # janitor ticks (5s each) between container probes

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._lease: Optional[InteractiveLease] = None
        self._agent_active = False
        self._last_session_id: str = ""
        # Whose cookies currently sit in the shared container jar. Survives the
        # lease so a scope handover can clear the jar even after a stop.
        self._last_cookie_scope: Optional[str] = None
        self._janitor_alive = False

    # -- environment -------------------------------------------------------
    @staticmethod
    def cdp_base() -> str:
        return os.environ.get("VAF_BROWSER_CDP_URL", "http://localhost:9222")

    @staticmethod
    def vnc_base() -> str:
        return os.environ.get("VAF_BROWSER_VNC_URL", "http://127.0.0.1:6901")

    # -- agent coordination ------------------------------------------------
    def is_agent_active(self) -> bool:
        """Is a browser_agent run underway, in-process or as a spawned child?

        The flag covers the in-process lane (set at run() start, cleared in its
        finally). The IPC scan is the durable truth for the spawned-child lane,
        where the parent's run() returns a marker immediately.
        """
        with self._lock:
            if self._agent_active:
                return True
        try:
            from vaf.core.subagent_ipc import get_ipc
            ipc = get_ipc()
            for t in list(ipc.get_active_tasks(None)) + list(ipc.get_pending_tasks(None)):
                if getattr(t, "agent_type", "") == "browser_agent":
                    return True
        except Exception:
            pass
        return False

    def stop_for_agent_run(self) -> None:
        """The agent is about to use the browser: evict any interactive lease."""
        with self._lock:
            self._agent_active = True
        try:
            self.stop("agent_takeover", force=True)
        except Exception:
            # An interactive-manager failure must never fail an agent run.
            pass

    def agent_run_ended(self, notify: bool = True) -> None:
        with self._lock:
            self._agent_active = False
            sid = self._last_session_id
        # Tell the window that owned the interactive view the browser is free
        # again; the frontend may offer a restart. Best-effort. notify=False is
        # the spawn lane, where run() returns its marker while the child is
        # only starting - announcing a free browser there would be a lie.
        if notify and sid:
            self._emit({"status": "stopped", "reason": "agent_done",
                        "saving": False, "streamPath": ""}, sid)

    # -- lease lifecycle ---------------------------------------------------
    def start(self, user_scope_id: str, session_id: str, *, save: bool = True,
              session_name: str = "default", is_admin: bool = False) -> dict:
        """Take (or refresh) the interactive lease. BLOCKING: run in an executor.

        Interactive use is always the PERSISTENT mode (save=True): whether a
        login gets remembered is the person's own decision inside the browser,
        which asks - a second switch in our chrome would be the same question
        asked twice. The parameter exists because the storage lane underneath
        is shared with the agent's persistent/non-persistent runs and the tests
        pin both directions of it. Same scope + same chat session -> refresh,
        the ticket and stream survive. Same scope, different chat session ->
        the newer window wins with a fresh ticket. Foreign scope -> busy,
        unless the caller is an admin, who may evict.
        """
        if self.is_agent_active():
            return self._payload("agent_active", reason="agent_run")

        with self._lock:
            prev = self._lease
            if prev and prev.user_scope_id != user_scope_id and not is_admin:
                # Deliberately no owner details in the refusal: one user must
                # not learn who else is on the machine from a busy signal.
                return {"status": "busy", "reason": "other_user",
                        "saving": False, "streamPath": ""}

        if prev and prev.user_scope_id != user_scope_id:
            # Admin eviction: end the foreign lease properly (its cookies are
            # exported if it asked for that) before the jar changes hands.
            self.stop("superseded", force=True)

        # The stream is about to be promised to a window: make sure the
        # container actually answers first.
        try:
            resolve_cdp_ws_url(self.cdp_base())
        except Exception:
            return self._payload("error", reason="browser_unavailable")

        # Cookie jar handover, before the person starts browsing.
        try:
            with self._lock:
                jar_scope = self._last_cookie_scope
            if jar_scope is not None and jar_scope != user_scope_id:
                _cookie_op(self.cdp_base(), "clear")
            if save:
                cookies = _load_storage_cookies(
                    browser_storage_state_path(user_scope_id, session_name))
                if cookies:
                    _cookie_op(self.cdp_base(), "set", cookies)
            with self._lock:
                self._last_cookie_scope = user_scope_id
        except Exception as e:
            append_domain_log("webui", f"[browser_interactive] cookie handover failed: {e}")

        with self._lock:
            prev = self._lease
            same_window = (prev is not None
                           and prev.user_scope_id == user_scope_id
                           and prev.session_id == session_id)
            if same_window:
                prev.save_enabled = save
                prev.session_name = session_name
                lease = prev
            else:
                if prev is not None and prev.session_id != session_id:
                    # The old window's iframe dies with its ticket; tell it why.
                    self._emit({"status": "stopped", "reason": "superseded",
                                "saving": False, "streamPath": ""}, prev.session_id)
                lease = InteractiveLease(
                    user_scope_id=user_scope_id,
                    session_id=session_id,
                    save_enabled=save,
                    session_name=session_name,
                    ticket=secrets.token_urlsafe(24),
                    started_at=time.time(),
                    last_disconnect=time.time(),
                )
                self._lease = lease
            self._last_session_id = session_id
            self._ensure_janitor()
            payload = self._payload("active", lease=lease)
        self._emit(payload, session_id)
        return payload

    def stop(self, reason: str, *, requester_scope: Optional[str] = None,
             force: bool = False) -> dict:
        """Release the lease. BLOCKING when cookies are exported: use an executor."""
        with self._lock:
            lease = self._lease
            if lease is None:
                return {"status": "stopped", "reason": reason,
                        "saving": False, "streamPath": ""}
            if not force and requester_scope is not None \
                    and requester_scope != lease.user_scope_id:
                return {"status": "busy", "reason": "not_owner",
                        "saving": False, "streamPath": ""}
            self._lease = None
            sid = lease.session_id

        if lease.save_enabled:
            try:
                cookies = _cookie_op(self.cdp_base(), "get")
                if cookies is not None:
                    _export_storage_cookies(
                        browser_storage_state_path(lease.user_scope_id, lease.session_name),
                        cookies)
            except Exception as e:
                append_domain_log("webui", f"[browser_interactive] cookie export failed: {e}")
        # Same idle rule as after an agent run: never leave an animating page
        # burning CPU with nobody watching. Skipped on agent takeover - the
        # agent is about to drive the very tabs parking would close.
        if reason != "agent_takeover":
            park_browser_idle(self.cdp_base())

        payload = {"status": "stopped", "reason": reason,
                   "saving": lease.save_enabled, "streamPath": ""}
        self._emit(payload, sid)
        return payload

    def interactive_session_id(self) -> Optional[str]:
        """The chat session currently driving the browser, or None. Cheap."""
        with self._lock:
            return self._lease.session_id if self._lease else None

    def snapshot_context(self, session_id: str) -> Optional[dict]:
        """What the person in the interactive browser is looking at, right now.

        Rides along with a chat message as turn context (the code-viewer
        pattern): current page URL, selected text, and a screenshot. Only for
        the chat session that HOLDS the lease; anything else gets None.
        BLOCKING (own CDP connection): call from an executor.
        """
        with self._lock:
            lease = self._lease
            if lease is None or lease.session_id != session_id:
                return None
        import asyncio

        try:
            ws_url = resolve_cdp_ws_url(self.cdp_base())
        except Exception:
            return None

        async def _main():
            from cdp_use.client import CDPClient
            client = CDPClient(ws_url)
            await client.start()
            try:
                targets = await asyncio.wait_for(client.send.Target.getTargets(), 5)
                pages = [t for t in targets.get("targetInfos", []) if t.get("type") == "page"]
                if not pages:
                    return None
                # The tab the person is on: prefer a real page over parked blanks.
                page = next((t for t in pages if not str(t.get("url", "")).startswith("about:")), pages[0])
                att = await asyncio.wait_for(
                    client.send.Target.attachToTarget(
                        params={"targetId": page["targetId"], "flatten": True}), 5)
                sid = att["sessionId"]
                selection = ""
                try:
                    ev = await asyncio.wait_for(
                        client.send.Runtime.evaluate(
                            params={"expression": "window.getSelection().toString()",
                                    "returnByValue": True},
                            session_id=sid), 5)
                    selection = str((ev.get("result") or {}).get("value") or "")
                except Exception:
                    pass
                shot = ""
                try:
                    cap = await asyncio.wait_for(
                        client.send.Page.captureScreenshot(
                            params={"format": "jpeg", "quality": 55}, session_id=sid), 10)
                    shot = cap.get("data") or ""
                except Exception:
                    pass
                return {"url": str(page.get("url", "")),
                        "selection": selection.strip()[:4000],
                        "screenshot_b64": shot}
            finally:
                try:
                    await asyncio.wait_for(client.stop(), 5)
                except Exception:
                    pass

        try:
            return asyncio.run(_main())
        except Exception:
            return None

    # -- stream proxy hooks ------------------------------------------------
    def validate_ticket(self, ticket: str) -> Optional[InteractiveLease]:
        with self._lock:
            lease = self._lease
            if lease is not None and secrets.compare_digest(lease.ticket, ticket or ""):
                return lease
            return None

    def stream_connected(self, ticket: str) -> bool:
        with self._lock:
            lease = self._lease
            if lease is None or not secrets.compare_digest(lease.ticket, ticket or ""):
                return False
            lease.stream_connections += 1
            first = lease.stream_connections == 1
            payload = self._payload("active", lease=lease)
            sid = lease.session_id
        # The window covers the viewer's own loading screen until pixels are really
        # flowing; this is the moment it may lift, and only the server can know it.
        if first:
            self._emit(payload, sid)
        return True

    def stream_disconnected(self, ticket: str) -> None:
        with self._lock:
            lease = self._lease
            if lease is None or not secrets.compare_digest(lease.ticket, ticket or ""):
                return
            lease.stream_connections = max(0, lease.stream_connections - 1)
            gone = lease.stream_connections == 0
            if gone:
                lease.last_disconnect = time.time()
            payload = self._payload("active", lease=lease)
            sid = lease.session_id
        if gone:
            self._emit(payload, sid)

    # -- janitor -----------------------------------------------------------
    def _ensure_janitor(self) -> None:
        # Caller holds the lock.
        if self._janitor_alive:
            return
        self._janitor_alive = True
        threading.Thread(target=self._janitor_loop, daemon=True,
                         name="browser-interactive-janitor").start()

    def _janitor_loop(self) -> None:
        """Ends leases nobody is watching and detects a dead/restarted container.

        The stop watchdog in browser_agent may `docker restart vaf-browser` at
        any time; the probe converts that into an explicit stopped status within
        ~15s instead of a silently frozen iframe.
        """
        fails = 0
        tick = 0
        try:
            while True:
                time.sleep(5)
                tick += 1
                with self._lock:
                    lease = self._lease
                    if lease is None:
                        return
                    idle_gone = (lease.stream_connections <= 0
                                 and time.time() - lease.last_disconnect > self.GRACE_S
                                 and time.time() - lease.started_at > self.GRACE_S)
                if idle_gone:
                    self.stop("viewer_gone", force=True)
                    return
                if tick % self._LIVENESS_EVERY_TICKS == 0:
                    try:
                        import urllib.request as _req
                        with _req.urlopen(self.cdp_base().rstrip("/") + "/json/version",
                                          timeout=3):
                            pass
                        fails = 0
                    except Exception:
                        fails += 1
                        if fails >= 2:
                            self.stop("browser_restarted", force=True)
                            return
        finally:
            with self._lock:
                self._janitor_alive = False
                # A lease created between our exit decision and this finally
                # would be orphaned without a janitor; re-arm for it.
                if self._lease is not None:
                    self._ensure_janitor()

    # -- plumbing ----------------------------------------------------------
    def _payload(self, status: str, *, reason: str = "",
                 lease: Optional[InteractiveLease] = None) -> dict:
        if lease is None:
            with self._lock:
                lease = self._lease
        active = status == "active" and lease is not None
        # The ?path= is not decoration, it is the whole stream. The KasmVNC client
        # builds its socket URL from SETTINGS, not relative to its own directory:
        #     ws://<host>:<port>/ + getSetting("path")   // default "websockify"
        # so without this it dials ws://<backend>/websockify, a route that does not
        # exist here, and the viewer never connects. The client reads settings from
        # the URL (hash first, then query), so handing it the ticketed path makes it
        # dial our proxy route instead. No leading slash: the client adds "/" itself.
        # host/port/encrypt stay default and therefore point at the iframe's own
        # origin, which IS the backend serving this route.
        stream = ""
        if active:
            ws_path = f"api/browser-vnc/t/{lease.ticket}/websockify"
            # RELATIVE on purpose. The document is loaded same-origin by the window, so
            # it travels whichever front door the person is on (the dev server, or the
            # HTTPS proxy when LAN hosting is on) and no scheme has to be guessed here -
            # guessing it wrong is exactly how this lane produced an empty response: the
            # backend port speaks HTTPS while TLS is on, and a plain http:// iframe URL
            # got nothing back. The socket's host and port are appended by the frontend
            # from its own backend-socket helper, which is the app's single answer to
            # "where is the backend".
            # resize=remote: the server only ALLOWS resizing, the viewer has to ask
            # for it. Without this the display stays at its start geometry and the
            # window shows black bars above and below the page.
            stream = (f"/api/browser-vnc/t/{lease.ticket}/index.html"
                      f"?path={ws_path}&resize=remote")
        return {
            "status": status,
            "reason": reason,
            "saving": bool(lease.save_enabled) if lease else False,
            "streamPath": stream,
            # Is a viewer socket actually attached right now? The window shows its own
            # connecting state until this turns true, which keeps the stream viewer's
            # own branded loading screen out of sight without touching its files.
            "viewerConnected": bool(active and lease.stream_connections > 0),
        }

    def _emit(self, payload: dict, session_id: str) -> None:
        try:
            from vaf.core.web_interface import get_web_interface
            wi = get_web_interface()
            if wi is not None:
                wi.emit_browser_interactive_state(dict(payload), session_id=session_id)
        except Exception:
            pass


_manager: Optional[InteractiveBrowserManager] = None
_manager_lock = threading.Lock()


def get_interactive_manager() -> InteractiveBrowserManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = InteractiveBrowserManager()
        return _manager


def stop_for_agent_run() -> None:
    """Hook for BrowserAgentTool.run(): evict the interactive lease. Never raises."""
    try:
        get_interactive_manager().stop_for_agent_run()
    except Exception:
        pass


def agent_run_ended(notify: bool = True) -> None:
    """Hook for BrowserAgentTool.run()'s finally. Never raises."""
    try:
        get_interactive_manager().agent_run_ended(notify=notify)
    except Exception:
        pass
