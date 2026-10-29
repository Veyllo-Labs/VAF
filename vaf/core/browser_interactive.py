# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Interactive browser lane: who holds the shared sandbox browser, and their logins.

The vaf-browser container's KasmVNC streams the display and carries mouse and
keyboard natively, so nothing in here relays frames or input. This module owns
the two things the stream cannot decide for itself:

- the LEASE: one person drives one Chromium at a time, an agent run always
  wins over a person, and a foreign user scope is refused rather than
  silently joined to someone else's cookie jar. There is one manager PER
  BROWSER: the shared container's (the default and fallback), plus one for
  each per-user instance the pool hands out (vaf/core/browser_pool.py) - the
  registry at the bottom routes by scope, by stream ticket, by session, and
  by the container a run pinned at its start. The shared container is also
  driven from other processes (a coder child's render_check, a spawned run,
  the terminal app), so its jar owner and its current holders are kept in a
  record every process reads (vaf/core/browser_jar.py), never in one
  manager's memory alone;
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
which is why the path is auth-middleware-exempt. A socket outlives its request,
so validating the ticket at connect is not enough: the relay keeps the grant
object the ticket resolved to and asks `grant_current` on every message it
passes, in both directions, with a watchdog for a socket that carries none. When
the lease or the run's watch grant ends (a stop, a takeover, a newer window, an
admin, the run's end, a revoked account), its sockets close with 4403 and no
frame or keystroke crosses after the end - otherwise an open socket went on
showing, and driving, the browser of whoever held it next.

Placement: vaf/core beside web_interface/web_server (harness internals per
docs/ARCHITECTURE.md). vaf/tools/browser_agent.py imports THIS module (tools ->
core is the established direction) both for the shared helpers below and to
evict the interactive lease when a run starts.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

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


def resolve_browser_scope(user_scope_id: Optional[str] = None) -> str:
    """The one answer to "whose browser use is this": explicit arg, the
    child-process env, the local admin scope (single-user/local mode), then
    "default". Storage paths and the shared-jar arbitration must resolve the
    SAME way, or a run and its own login store disagree about the owner."""
    _scope = user_scope_id or os.environ.get("VAF_USER_SCOPE_ID")
    if not _scope:
        try:
            from vaf.core.config import get_local_admin_scope_id
            _scope = get_local_admin_scope_id()
        except Exception:
            _scope = "default"
    return str(_scope)


def browser_storage_state_path(user_scope_id: Optional[str] = None,
                               session_name: str = "default") -> str:
    """Path of the per-scope persistent browser login store, creating its dir.

    USER ISOLATION: the cookie/login store is keyed by user_scope_id so one
    user's persistent browser logins are never shared with (or readable by)
    another. Scope resolution: resolve_browser_scope above.
    """
    _scope = resolve_browser_scope(user_scope_id)
    scope_seg = "".join(c if c.isalnum() or c in "-_" else "_" for c in _scope) or "default"
    sessions_dir = os.path.join(os.path.expanduser("~"), ".vaf", "browser_sessions", scope_seg)
    os.makedirs(sessions_dir, exist_ok=True)
    try:
        from vaf.core.secure_store import harden_dir
        harden_dir(sessions_dir)
    except Exception:
        pass
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_name) or "default"
    return os.path.join(sessions_dir, f"{safe_name}.json")


# ---------------------------------------------------------------------------
# The stream port's credential
# ---------------------------------------------------------------------------

_VNC_SECRET_CACHE = ""


def browser_vnc_secret() -> str:
    """The KasmVNC stream port's password, from the data keyring, minted on first use.

    Why the port needs one at all: CDP and the stream are both published on
    loopback, but they are NOT equally defended. CDP answers 403 to any request
    carrying an Origin header, so a web page cannot drive it. KasmVNC only
    requires Origin to be PRESENT and accepts any value, and a page's WebSocket
    always sends one and cannot suppress it - measured live: a page on an
    unrelated origin opened ws://127.0.0.1:6901/websockify and got a
    bidirectional RFB channel, framebuffer out and key/pointer events in. Basic
    auth closes exactly that, because the WebSocket constructor has no way to
    set an Authorization header (also measured: with auth on, the same page is
    refused, and credentials in the URL are not converted into a header).

    Modelled on `redis_password()` (vaf/memory/cache.py): a secret belongs in
    the keyring, never in config.json, and it is assembled where it is used.

    STABLE BY DESIGN, not by accident: BrowserPool ADOPTS containers created by
    an earlier VAF process, and a container keeps the environment it was
    CREATED with for life - a rotating value would answer 401 on every adopted
    instance and present to the person as a blank window.

    Answers "" when the keyring cannot be read. That is deliberately not a
    fallback to no-auth: the container has no unauthenticated mode any more, so
    an empty secret makes the stream fail loudly instead of quietly opening it.
    """
    global _VNC_SECRET_CACHE
    if _VNC_SECRET_CACHE:
        return _VNC_SECRET_CACHE
    try:
        from vaf.core.data_keyring import get_data_secret
        # Memoized because the asset lane asks once per fetched file and every
        # call decrypts the keyring from disk. Only a NON-empty answer is
        # cached, so a briefly unreadable keyring is retried rather than frozen.
        secret = get_data_secret("browser_vnc_secret", min_length=16)
        if secret:
            _VNC_SECRET_CACHE = secret
        return secret
    except Exception as e:
        append_domain_log("webui", f"[browser_interactive] stream credential unavailable: {e}")
        return ""


def vnc_auth_headers() -> dict:
    """Authorization header for every VAF lane that reaches the stream port.

    One home for the three call sites (the asset proxy, the websocket relay and
    the pool's health probe), so a lane cannot be forgotten silently - a
    forgotten one does not fail in tests, it shows up as a blank panel.

    The credential stays SERVER-SIDE, and that is load-bearing rather than
    tidy: if the person's own browser ever authenticated to the raw port, it
    caches the credentials for that origin and would attach them to a later
    cross-origin WebSocket handshake, which hands the page vector straight back
    (measured). VAF's ticketed proxy is what keeps the person's browser away
    from the port, so it must stay the only door.
    """
    import base64

    secret = browser_vnc_secret()
    if not secret:
        return {}
    token = base64.b64encode(f"vaf:{secret}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


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
            if op == "clear_cookies":
                # The belt after a verified profile wipe on a scope change:
                # the wipe already deleted every site's stored state with the
                # profile, so cookies are all that could have appeared since.
                await asyncio.wait_for(client.send.Storage.clearCookies(params={}), 10)
                return None
            if op == "scrub":
                # The quick SAME-SCOPE scrub: cookies plus stored state of the
                # origins the cookies name. The wildcard origin is gone on
                # purpose: Chromium 151 answers clearDataForOrigin("*") with
                # -32603 (measured live 2026-08-23), so the sweep enumerates
                # origins from the jar instead. Known residual, same-user
                # only: an origin that stored localStorage without ever
                # setting a cookie is not in the list - acceptable here
                # because this op never runs on a scope change (that path is
                # the verified profile wipe), only on a user's own clean
                # start. The HTTP disk cache is NOT reachable either way -
                # that residue is the profile wipe's job, and the docs name it.
                res = await asyncio.wait_for(client.send.Storage.getCookies(params={}), 10)
                domains = {str(c.get("domain", "")).lstrip(".").strip()
                           for c in (res.get("cookies") or [])}
                await asyncio.wait_for(client.send.Storage.clearCookies(params={}), 10)
                for domain in sorted(d for d in domains if d):
                    try:
                        await asyncio.wait_for(
                            client.send.Storage.clearDataForOrigin(
                                params={"origin": f"https://{domain}",
                                        "storageTypes": "all"}), 15)
                    except Exception:
                        # One stubborn origin must not stop the sweep of the
                        # rest; the same-user residual is documented above.
                        pass
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
    Read through data_files: the store is encrypted at rest, and a plaintext
    file from before the change still loads (the reader keys on the magic).
    """
    try:
        from vaf.core import data_files
        state = data_files.read_json(path)
        if not isinstance(state, dict):
            return []
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
    Written through data_files: live site cookies are auth tokens, encrypted
    at rest like the chat sessions (ENCRYPTION_AT_REST.md).
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
    from vaf.core import data_files
    data_files.write_json_atomic(path, state)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def _current_page(cdp_base: str) -> Optional[dict]:
    """URL and title of the page a person is most likely looking at.

    The HTTP target list on purpose (no websocket): one bounded GET, usable
    from the blocking takeover hook. Same tab heuristic as snapshot_context:
    prefer a real page over parked about: blanks. CDP does not say which tab
    has focus, so "first real page" is the best available answer - and for the
    handover it only has to name where the person was, not win a tie between
    multiple open pages."""
    import urllib.request as _req
    try:
        with _req.urlopen(cdp_base.rstrip("/") + "/json/list", timeout=3) as resp:
            targets = json.loads(resp.read())
        pages = [t for t in targets if t.get("type") == "page"]
        if not pages:
            return None
        page = next((t for t in pages
                     if not str(t.get("url", "")).startswith("about:")), None)
        if page is None:
            return None
        return {"url": str(page.get("url", "")), "title": str(page.get("title", ""))[:200]}
    except Exception:
        return None


def downloads_mode() -> str:
    """What happens to files downloaded inside the sandbox browser.

    "workspace" (default): they are swept out of the container, pass the same
    threat funnel every other ingress lane asks (vaf/core/threat_db.py), and
    land in the owner's own file area (VAF_Projects/<uid8>/Downloads) - the
    container is a sandbox, not a place a person can reach into.
    "off": downloading is denied outright at the browser level.
    An env knob like its siblings; anything unrecognized means workspace."""
    return "off" if os.environ.get("VAF_BROWSER_DOWNLOADS", "").strip().lower() == "off" else "workspace"


def _set_download_behavior(cdp_base: str, allow: bool) -> None:
    """Tell the browser whether downloads may happen at all.

    Browser-level CDP, one short-lived client (the _cookie_op pattern), and
    the setting OUTLIVES the connection - which is the point: set once at
    every handover, it holds for the person's whole session or the agent's
    whole run. 'allow' pins the container path the sweep drains; 'deny' is
    the off switch, enforced in the browser rather than in our chrome.
    BLOCKING: call from a worker thread. Best-effort."""
    import asyncio

    ws_url = resolve_cdp_ws_url(cdp_base)

    async def _main():
        from cdp_use.client import CDPClient
        client = CDPClient(ws_url)
        await client.start()
        try:
            params = ({"behavior": "allow", "downloadPath": "/home/browser/Downloads"}
                      if allow else {"behavior": "deny"})
            await asyncio.wait_for(
                client.send.Browser.setDownloadBehavior(params=params), 10)
        finally:
            try:
                await asyncio.wait_for(client.stop(), 5)
            except Exception:
                pass

    try:
        asyncio.run(_main())
    except Exception as e:
        append_domain_log("webui", f"[browser_interactive] download policy not applied: {e}")


def _purge_container_downloads(container_name: str) -> None:
    """Delete both transfer folders in the container, unread.

    Called on a scope CHANGE: download residue a previous (or unknown) holder
    left behind must never be delivered into the NEXT person's workspace, and
    the previous holder's synced WORKSPACE copy must never be readable (or
    uploadable to a website) by the next person. Best-effort; the full profile
    scrub wipes the same folders as part of its job."""
    try:
        from vaf.core.containers import docker as _docker
        _docker(["exec", container_name, "sh", "-c",
                 "rm -rf /home/browser/Downloads/* /home/browser/Workspace/* 2>/dev/null || true"],
                timeout=30)
    except Exception:
        pass


def workspace_sync_mode() -> str:
    """Whether the owner's files are mirrored INTO the browser for uploads.

    "on" (default): the holder's VAF_Projects tree appears (size-capped) at
    /home/browser/Workspace - the folder the file picker is anchored to, and
    the whitelist agent runs may upload from. "off": nothing is mirrored.
    An env knob like its siblings; anything unrecognized means on."""
    return "off" if os.environ.get("VAF_BROWSER_WORKSPACE_SYNC", "").strip().lower() == "off" else "on"


_WS_SYNC_FILE_MAX = 64 * 1024 * 1024
_WS_SYNC_TOTAL_MAX = 512 * 1024 * 1024
_WS_CONTAINER_DIR = "/home/browser/Workspace"


def _eligible_workspace_files(root) -> List[tuple]:
    """(relative_path, size, mtime_ns) of every file the mirror carries.

    Hidden entries are skipped (dotfiles are tool state, not upload material),
    single files over the per-file cap are skipped, and the walk stops adding
    once the total cap is reached - a mirror is a convenience, not a backup."""
    out: List[tuple] = []
    total = 0
    try:
        base = os.fspath(root)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in sorted(dirnames) if not d.startswith(".")]
            for fn in sorted(filenames):
                if fn.startswith("."):
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if st.st_size > _WS_SYNC_FILE_MAX:
                    continue
                if total + st.st_size > _WS_SYNC_TOTAL_MAX:
                    return out
                total += st.st_size
                # POSIX separators at the source: these relatives become
                # CONTAINER paths (the upload whitelist), and os.path.relpath
                # answers with backslashes on a Windows host - the pinned
                # str(PurePath) serialization class (Windows CI red; Linux can
                # never reproduce it because os.sep is already "/"). The
                # host-side staging join below accepts the mixed form.
                rel = os.path.relpath(full, base).replace(os.sep, "/")
                out.append((rel, st.st_size, st.st_mtime_ns))
    except Exception:
        pass
    return out


def _tar_stream_to_container(container_name: str, tar_path: str) -> int:
    """Extract a host tar INSIDE the container as its own user; returncode.
    The seam the mirror tests stub. Replaces `docker cp` + root chown: under
    cap_drop ALL even a root exec has no CAP_CHOWN, and cp would strand the
    files under the host uid - extraction as the browser user owns them."""
    try:
        import subprocess
        from vaf.core.service_stack import resolve_docker_exe
        with open(tar_path, "rb") as tar_in:
            r = subprocess.run(
                [resolve_docker_exe(), "exec", "-i", container_name,
                 "tar", "-xf", "-", "-C", _WS_CONTAINER_DIR],
                stdin=tar_in, capture_output=True, timeout=300)
        return int(r.returncode)
    except Exception:
        return 1


def _sync_workspace_to_container(container_name: str, user_scope_id: str,
                                 prev_sig) -> tuple:
    """Mirror the owner's files into the browser. Returns (signature, paths).

    The reverse of the download sweep: a website's file picker (and the
    agent's upload_file action) can only see the CONTAINER filesystem, so
    uploading "my PDF" is impossible unless the file exists in there. The
    mirror is one-way (host wins, container copy is disposable), bulk (one
    tar stream of a staged tree, not one exec per file), and signature-gated:
    an unchanged workspace costs a directory walk and nothing else. The tar
    is EXTRACTED INSIDE the container as the browser user, deliberately not
    `docker cp` + root chown: under cap_drop ALL even a root exec has no
    CAP_CHOWN, and cp would strand the files under the host uid. `paths`
    always lists every mirrored file's container path - the agent's upload
    whitelist - whether or not a copy happened this round. BLOCKING: call off
    the event loop. Never raises."""
    paths: List[str] = []
    try:
        from vaf.core.containers import docker as _docker
        from vaf.core.session import get_user_projects_root

        root = get_user_projects_root(user_scope_id)
        if root is None or not os.path.isdir(root):
            return prev_sig, paths
        files = _eligible_workspace_files(root)
        paths = [f"{_WS_CONTAINER_DIR}/{rel}" for rel, _s, _m in files]
        sig = (len(files), sum(s for _r, s, _m in files),
               max((m for _r, _s, m in files), default=0))
        if sig == prev_sig:
            return sig, paths

        import shutil
        import tarfile
        import tempfile
        staging = tempfile.mkdtemp(prefix="vaf-browser-ws-")
        try:
            for rel, _s, _m in files:
                dest = os.path.join(staging, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(os.path.join(os.fspath(root), rel), dest)
            tar_path = staging + ".tar"
            with tarfile.open(tar_path, "w") as tf:
                for rel, _s, _m in files:
                    tf.add(os.path.join(staging, rel), arcname=rel)
            _docker(["exec", container_name, "mkdir", "-p", _WS_CONTAINER_DIR], timeout=20)
            if _tar_stream_to_container(container_name, tar_path) != 0:
                return prev_sig, paths
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            try:
                os.unlink(staging + ".tar")
            except Exception:
                pass
        append_domain_log("webui",
                          f"[browser_interactive] workspace mirrored into browser: "
                          f"{len(files)} file(s)")
        return sig, paths
    except Exception as e:
        append_domain_log("webui", f"[browser_interactive] workspace sync failed: {e}")
        return prev_sig, paths


_DOWNLOAD_MAX_BYTES = 512 * 1024 * 1024
_DOWNLOAD_NAME_OK = re.compile(r"[^A-Za-z0-9._ ()\[\]-]")


def _sweep_container_downloads(container_name: str, user_scope_id: str) -> List[str]:
    """Move finished downloads out of the container into their owner's files.

    The sandbox browser downloads into its own container filesystem, where no
    person can reach ("it downloaded, but where?" - measured live with a PDF
    that existed only inside the container). This drains that folder: every
    completed file (Chromium keeps in-progress ones as *.crdownload) is copied
    out, asked past the SAME threat funnel every ingress lane asks
    (inspect_upload_file, origin browser_download - a downloaded file is
    foreign bytes like any upload), and placed under the owner's
    VAF_Projects/<uid8>/Downloads with a sanitized, collision-suffixed name.
    Delivered or blocked, the container copy is deleted - the folder is a
    hand-off point, not storage. Returns the delivered filenames. BLOCKING
    (docker + hashing): call off the event loop. Never raises."""
    delivered: List[str] = []
    try:
        from vaf.core.containers import docker as _docker
        from vaf.core.session import get_user_projects_root

        root = get_user_projects_root(user_scope_id)
        if root is None:
            return delivered
        r = _docker(["exec", container_name, "sh", "-c",
                     "find /home/browser/Downloads -maxdepth 1 -type f "
                     "! -name '*.crdownload' -printf '%s\\t%p\\n' 2>/dev/null || true"],
                    timeout=30)
        if r.returncode != 0:
            return delivered
        entries = []
        for line in (r.stdout or "").splitlines():
            if "\t" not in line:
                continue
            size_s, path = line.split("\t", 1)
            try:
                entries.append((int(size_s), path.strip()))
            except ValueError:
                continue
        if not entries:
            return delivered

        import shutil
        import tempfile
        from vaf.core.config import resolve_caller_username
        from vaf.core.threat_db import inspect_upload_file

        target_dir = root / "Downloads"
        for size, cpath in entries:
            raw_name = os.path.basename(cpath)
            if size > _DOWNLOAD_MAX_BYTES:
                append_domain_log("webui",
                                  f"[browser_interactive] download skipped (over size cap): "
                                  f"{raw_name} ({size} bytes)")
                _docker(["exec", container_name, "rm", "-f", cpath], timeout=20)
                continue
            staged = None
            try:
                fd, staged = tempfile.mkstemp(prefix="vaf-browser-dl-")
                os.close(fd)
                c = _docker(["cp", f"{container_name}:{cpath}", staged], timeout=120)
                if c.returncode != 0:
                    continue
                name = _DOWNLOAD_NAME_OK.sub("_", raw_name).strip("._ ") or "download"
                name = name[:180]
                verdict = inspect_upload_file(
                    staged, filename=name, origin="browser_download",
                    username=resolve_caller_username(None, user_scope_id))
                if verdict.blocked:
                    # The funnel has logged and surfaced it; the bytes go nowhere.
                    append_domain_log("webui",
                                      f"[browser_interactive] download blocked by threat "
                                      f"funnel: {name}")
                else:
                    target_dir.mkdir(parents=True, exist_ok=True)
                    dest = target_dir / name
                    stem, dot, ext = name.rpartition(".")
                    n = 1
                    while dest.exists():
                        dest = target_dir / (f"{stem}-{n}.{ext}" if dot else f"{name}-{n}")
                        n += 1
                    shutil.move(staged, dest)
                    staged = None
                    try:
                        os.chmod(dest, 0o600)
                    except Exception:
                        pass
                    delivered.append(dest.name)
                    append_domain_log("webui",
                                      f"[browser_interactive] download delivered to "
                                      f"workspace: {dest.name}")
                _docker(["exec", container_name, "rm", "-f", cpath], timeout=20)
            finally:
                if staged:
                    try:
                        os.unlink(staged)
                    except Exception:
                        pass
    except Exception as e:
        append_domain_log("webui", f"[browser_interactive] download sweep failed: {e}")
    return delivered


class BrowserHandoverError(RuntimeError):
    """A cross-scope handover could not be made safe, so it was refused.

    Raised by the fail-closed handover paths when the verified profile wipe
    (or the cookie clear behind it) did not succeed: proceeding would let the
    next holder browse over the previous holder's state. RuntimeError on
    purpose - the agent lane's generic `except Exception` must catch it (a
    BaseException would ride past the run's error handling)."""


class BrowserHeldElsewhere(BrowserHandoverError):
    """The shared browser is held by another VAF process (a lease, a run, or a
    probe that outlasted the wait), so this run was refused before it took the
    jar. A BrowserHandoverError, so every lane that refuses on a failed handover
    refuses here too."""


def scrub_mode() -> str:
    """The quick-scrub depth for SAME-SCOPE clean starts: "quick" (CDP sweep,
    default) or "full" (adds a profile wipe with a Chromium relaunch). An env
    knob (VAF_BROWSER_SCRUB) like its siblings VAF_BROWSER_MAX_PARALLEL and
    VAF_BROWSER_CDP_URL; anything unrecognized means quick, so a typo degrades
    to the fast default. A scope CHANGE no longer consults this: the cross-user
    handover is ALWAYS a verified profile wipe, not configurable - a switch
    that turns isolation off would not be a setting but a defect."""
    return "full" if os.environ.get("VAF_BROWSER_SCRUB", "").strip().lower() == "full" else "quick"


def _wipe_exec(container_name: str, shell_cmd: str, *, timeout: float = 20) -> int:
    """One `docker exec` for the verified wipe, answering the returncode.
    Non-zero on any failure including a missing docker. The single seam the
    verified-wipe unit tests stub. Runs as the container's OWN user on
    purpose: under cap_drop ALL a root exec has no CAP_DAC_OVERRIDE/CAP_CHOWN
    and cannot even touch a file in browser's home (measured live), while the
    browser user owns everything the wipe protocol needs."""
    try:
        import subprocess
        from vaf.core.service_stack import resolve_docker_exe
        docker = resolve_docker_exe()
        r = subprocess.run([docker, "exec", container_name, "sh", "-c", shell_cmd],
                           timeout=timeout, capture_output=True)
        return int(r.returncode)
    except Exception:
        return 1


def verified_profile_wipe(container_name: str = "vaf-browser",
                          cdp_base: str = "", *,
                          deadline_s: float = 40.0) -> bool:
    """Wipe the container's Chromium profile and PROVE that it happened.

    The cross-scope handover stands on this proof, so every step is checked,
    and the ORDER is the security argument. A same-scope clean start in full
    scrub mode uses it too, for a different reason: the wipe deletes the
    mirrored Workspace folder at the relaunch, seconds after the marker is
    dropped, and only the confirmation says when the mirror may copy again. pkill sends SIGTERM, so for a moment the OLD Chromium
    still answers CDP - which is why CDP reachability proves nothing here. The
    marker does: the supervisor consumes it strictly AFTER `rm -rf` of the
    profile (entrypoint.sh start_chromium), and its loop-top `pkill -9` has
    killed the old Chromium before that. Marker gone = profile gone.

    1. Drop the marker, rc-checked, SEPARATE from the kill: the old single
       `touch && chown && pkill` chain reported pkill's rc, and pkill answers 1
       when Chromium happened to be down (a crash window) although the marker
       WAS dropped - a false refusal with a live marker left behind, which
       would wipe the profile at some unrelated future relaunch. As the
       browser user, never root: under cap_drop ALL a root exec cannot write
       into browser's home (no CAP_DAC_OVERRIDE), and browser owns the home,
       so no chown is needed either.
    2. Kill Chromium, best-effort (`|| true`).
    3. Poll for the marker's ABSENCE (the wipe confirmation; relaunch is
       measured at 3-5s, the deadline is generous).
    4. Only then wait for CDP as plain readiness.
    On failure, remove the marker best-effort so no delayed wipe lies in wait.
    Module-level so tests can stub it as bi.<name>.
    """
    marker = "/home/browser/.scrub-profile"
    if _wipe_exec(container_name, f"touch {marker}") != 0:
        append_domain_log("webui", "[browser_interactive] verified wipe: marker could not be set")
        return False
    _wipe_exec(container_name, "pkill chromium || true")
    deadline = time.time() + deadline_s
    confirmed = False
    while time.time() < deadline:
        if _wipe_exec(container_name, f"test ! -f {marker}", timeout=10) == 0:
            confirmed = True
            break
        time.sleep(0.5)
    if not confirmed:
        _wipe_exec(container_name, f"rm -f {marker}")
        append_domain_log("webui", "[browser_interactive] verified wipe: marker not consumed in time")
        return False
    if cdp_base:
        try:
            resolve_cdp_ws_url(cdp_base)
        except Exception as e:
            append_domain_log("webui", f"[browser_interactive] verified wipe: relaunch not reachable: {e}")
            return False
    append_domain_log("webui", "[browser_interactive] verified wipe: profile wipe confirmed")
    return True


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
    # Bytes received FROM the display server on this lease. An open socket is not a
    # picture: the viewer opens its socket immediately and shows its own splash for
    # the whole handshake that follows, so "connected" has to mean pixels, not TCP.
    pixels_seen: int = 0


@dataclass
class AgentStream:
    """A WATCH-ONLY stream grant for the duration of one browser_agent run.

    The agent view in the window shows the same streamed Chromium the person
    would drive, instead of a rebuilt chrome bar over 1.5s screenshots - but
    the person must not be able to type into a browser the agent is driving,
    so the stream URL carries the viewer's view_only setting and the grant is
    emitted only to the chat session that owns the run. Minted only once the run
    holds the browser (after its handover), and the ticket dies with the run or
    with the next grant, closing every socket opened on it. Screenshots keep
    streaming regardless: they are the fallback for the lanes without a grant
    (workflow tile, spawned child)."""

    session_id: str
    ticket: str
    started_at: float
    stream_connections: int = 0
    pixels_seen: int = 0


class InteractiveBrowserManager:
    """Singleton arbiter for the interactive use of the shared browser."""

    # A closed viewer gets this long to come back (tab reload, brief network
    # loss) before the lease is released and cookies are exported. Deliberately
    # generous: reading without touching anything is normal browsing, so
    # "activity" is an open stream connection, not input.
    GRACE_S = 120.0
    # How long a holder's release waits for its file transfers before it leaves
    # the release to the last of them. Above the mirror's tar extraction (300 s)
    # and one download copy (120 s), each of which its subprocess timeout ends.
    TRANSFER_DRAIN_S = 330.0
    # How often an open stream socket that carries no traffic asks whether its
    # grant still stands. Only the close of a QUIET socket waits for this: the
    # relay checks the grant on every message it passes, so no byte crosses after
    # the grant's end whatever the interval is. Measured: the check over 100
    # sockets costs 0.29 % of one core at 0.25 s and 0.05 % at 1 s.
    STREAM_RECHECK_S = 0.5
    # What separates the protocol handshake from the first real picture. Measured
    # against this container: the ENTIRE handshake is 45 bytes (greeting 12,
    # security types 2, result 4, ServerInit 27) and the first framebuffer update is
    # 49132. 4 KB sits 91x above the one and 12x below the other, so it cannot be
    # reached by handshake traffic and cannot be missed by a real frame. Counted
    # cumulatively rather than per frame, so an update that arrives split into
    # several smaller frames still counts.
    PIXELS_THRESHOLD = 4096
    _LIVENESS_EVERY_TICKS = 3   # janitor ticks (5s each) between container probes

    def __init__(self, cdp_base_url: Optional[str] = None,
                 vnc_base_url: Optional[str] = None,
                 container_name: str = "vaf-browser",
                 dedicated_scope: Optional[str] = None) -> None:
        # Which browser this manager arbitrates. The default (None, None,
        # "vaf-browser") is the SHARED container with its env-overridable
        # endpoints - the only browser that exists unless the pool is on.
        # Pool-created managers get their instance's endpoints instead; the
        # whole lease/scrub/grant machinery below is per-browser by
        # construction, so the pool multiplies managers rather than teaching
        # this class about instances.
        self._cdp_base_url = cdp_base_url
        self._vnc_base_url = vnc_base_url
        self._container_name = container_name
        self._lock = threading.RLock()
        # Serializes the WHOLE handover sections of start() and
        # hand_jar_to_run(), which are deliberately not under the state _lock
        # (they block on docker and CDP for seconds). Without it two
        # interleaving handovers can each restore their own cookies into the
        # shared jar and the install-time busy answer cannot undo that: the
        # winner would browse over the loser's restored cookies. One mutex
        # makes the busy check, the wipe and the cookie restore one atomic
        # story; worst-case wait is one profile wipe (~5s), fallback path only,
        # or one render probe, which holds it from its handover to its parking
        # (hold_for_probe; a few seconds for a page that loads, bounded by the
        # probe's own CDP timeouts for one that does not).
        self._handover_mutex = threading.Lock()
        self._lease: Optional[InteractiveLease] = None
        # Runs of THIS browser that are queued at its concurrency gate or driving
        # it: stop_for_agent_run adds one, agent_run_ended (or agent_run_abandoned,
        # for a run that leaves before it drives) takes it back. A count, not a
        # flag: with one slot a second run evicts and waits at the gate, and the
        # first run's end cleared a single flag although the second was about to
        # drive - measured, another user's interactive start then got the browser
        # under that run, and a probe of this process rendered under it.
        self._agent_runs = 0
        self._agent_stream: Optional[AgentStream] = None
        # The lease an agent takeover evicted: (user_scope_id, session_id).
        # This is the fact "the person was driving BEFORE the agent took the
        # browser", kept server-side so it survives a page reload during the
        # run. agent_run_ended() turns it into a resumable=True stop event to
        # that session, whose window then re-enters the interactive mode
        # instead of closing; any freshly started lease clears it.
        self._pre_agent_holder: Optional[tuple] = None
        # A run ended while another was still counted: its give-back waits for the last
        # one, and a queued run that leaves without driving settles it
        # (agent_run_abandoned).
        self._giveback_deferred = False
        # The page the evicted person was on when an agent run took the
        # browser: {scope, url, title, at}. This is what turns a takeover into
        # a HANDOVER - the run is told to carry on there instead of opening
        # the site fresh in a new tab (measured live: asked to take over an
        # open marketplace session, the run navigated a new tab and left the
        # person's tab standing). Consumed once by the run of the same scope,
        # expires quickly, and a person re-opening the browser themselves
        # clears it - their return outdates the handover.
        self._handover: Optional[dict] = None
        self._last_session_id: str = ""
        # Whose cookies currently sit in the shared container jar. Survives the
        # lease so a scope handover can clear the jar even after a stop.
        # On a DEDICATED (pooled) browser the answer is known from the start
        # and cannot change: only that scope is ever routed here. Without this
        # the manager would read its own user's profile as a stranger's jar on
        # the first use after every process start and scrub it - in full mode
        # deleting the very history, passwords and downloads the per-user
        # profile volume exists to keep.
        self._dedicated_scope = dedicated_scope
        self._last_cookie_scope: Optional[str] = dedicated_scope
        # Workspace-mirror freshness: what was last synced into the container,
        # for whom. Reset on a scope change so the next holder gets a full
        # mirror of THEIR files rather than a stale signature match.
        self._ws_sig = None
        # Counts the wipes of the Workspace folder (`_workspace_wiped`). A sync
        # copies outside the lock, so one that read its signature before a wipe
        # may finish after it; it stores its signature only if no wipe came in
        # between, or the files it copied (and the wipe deleted) count as there.
        self._ws_wipe_gen = 0
        # File transfers between the container and a holder's files (the
        # workspace mirror in, the download sweep out) run outside every lock and
        # can take minutes. `_xfer_scope` names the scope whose files may move
        # right now: set when a holder holds this browser (a lease installed, a
        # run's handover done), cleared when it lets go. A transfer registers
        # under the lock only for that scope (`_held_transfer`), and a holder's
        # release waits for the registered ones before it gives up its entry in
        # the cross-process record (`_release_after_transfers`). That entry is
        # what keeps every other holder, in any process, out of the shared
        # browser, so no other holder's handover can purge, wipe and browse
        # while a previous holder's files are still moving. A dedicated
        # instance has one scope for life, so its transfers are always its own.
        self._xfer_scope: Optional[str] = dedicated_scope
        self._xfers = 0
        self._xfers_idle = threading.Condition(self._lock)
        # Entries whose release outlived TRANSFER_DRAIN_S: the last transfer
        # releases them when it ends, never earlier.
        self._release_when_drained: List[str] = []
        self._janitor_alive = False
        # This process's entries in the shared browser's cross-process holder
        # record (vaf.core.browser_jar): the lease's and the in-process run's.
        self._lease_token: Optional[str] = None
        self._run_token: Optional[str] = None
        self._run_generation = 0
        # The lease a run on ANOTHER browser evicted here: (scope, session), for
        # that run's give-back. Kept apart from _pre_agent_holder, which belongs
        # to a run of this browser.
        self._elsewhere_holder: Optional[tuple] = None
        # A browser that can hold a lease is one a revoked account must lose. Here,
        # where every manager comes into being (the shared one and each pooled
        # one), so no creation path can leave a lease without the listener.
        _ensure_revocation_listener()

    # -- environment -------------------------------------------------------
    def cdp_base(self) -> str:
        # Env fallback evaluated at CALL time on purpose: tests and deployments
        # repoint the shared browser mid-process via these variables.
        return self._cdp_base_url or os.environ.get("VAF_BROWSER_CDP_URL", "http://localhost:9222")

    def vnc_base(self) -> str:
        return self._vnc_base_url or os.environ.get("VAF_BROWSER_VNC_URL", "http://127.0.0.1:6901")

    def container_name(self) -> str:
        return self._container_name

    @property
    def _agent_active(self) -> bool:
        """A run of this browser is queued or driving (see `_agent_runs`)."""
        return self._agent_runs > 0

    def has_activity(self) -> bool:
        """Anything alive on this browser: a lease, a run, or a watch grant.
        The pool's reaper asks this before stopping an idle instance."""
        with self._lock:
            return (self._lease is not None or self._agent_active
                    or self._agent_stream is not None)

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

    def stop_for_agent_run(self, user_scope_id: Optional[str] = None,
                           persistent: bool = False) -> None:
        """The agent is about to use the browser: evict any interactive lease.

        Whoever held the lease is remembered: the agent may take the browser,
        but a window the person opened themselves gets it back when the run
        ends (agent_run_ended announces that as resumable) instead of being
        closed over their head. No lease means nothing to give back.

        Deliberately NO jar work here. This hook fires at the very top of
        run(), before the spawn branch and before the concurrency gate, so a
        scrub in here lands on a browser another run may still be driving -
        it would log that run out of every site mid-task, and in full mode
        kill its Chromium outright. hand_jar_to_run() does that half, after
        the gate."""
        with self._lock:
            self._agent_runs += 1
            lease = self._lease
            # Only an eviction names a holder. A run queued behind one that is still
            # driving finds no lease, and clearing the holder there took the give-back
            # from the person the first run evicted (their window closed at the end).
            if lease is not None:
                self._pre_agent_holder = (lease.user_scope_id, lease.session_id)
        if lease is not None:
            # Capture WHERE the person is before the eviction, so the run can
            # continue there. Best-effort and bounded; a takeover of an idle
            # browser (no lease) has nobody to hand anything over.
            page = _current_page(self.cdp_base())
            if page and page.get("url"):
                with self._lock:
                    self._handover = {"scope": lease.user_scope_id,
                                      "at": time.time(), **page}
        try:
            self.stop("agent_takeover", force=True)
        except Exception:
            # An interactive-manager failure must never fail an agent run.
            pass

    def evict_for_run_elsewhere(self) -> None:
        """A run pinned to ANOTHER browser takes this one's lease (see the module
        stop_for_agent_run): evict it and remember the holder for the give-back,
        and nothing else. No agent drives this browser, so it is not marked as
        driven, and this manager's own run state (`_agent_active`, its holder
        entry in the cross-process record, its run generation) is left alone: it
        may belong to a run of its own that is still driving."""
        with self._lock:
            lease = self._lease
            if lease is None:
                return
            self._elsewhere_holder = (lease.user_scope_id, lease.session_id)
        try:
            self.stop("agent_takeover", force=True)
        except Exception:
            pass

    def give_back_elsewhere(self) -> None:
        """The other half: the run elsewhere has ended, so the evicted holder may
        resume here. Emits the give-back only."""
        with self._lock:
            holder, self._elsewhere_holder = self._elsewhere_holder, None
        if holder is None:
            return
        self._emit({"status": "stopped", "reason": "agent_done",
                    "saving": False, "streamPath": "", "resumable": True}, holder[1])
        self._arm_unclaimed_parker()

    def give_back_pending(self) -> bool:
        """Is an evicted person waiting to get this browser back?

        The run's end-of-task parking asks before it blanks the tabs: a
        browser about to be handed back must keep its state - the person
        continues exactly where the agent stopped, finished or was stopped."""
        with self._lock:
            return self._pre_agent_holder is not None

    UNCLAIMED_PARK_S = 20.0

    def _park_if_unclaimed(self) -> bool:
        """Park the browser unless somebody claimed it meanwhile.

        The give-back skips the run's own parking so the person can continue
        where the agent stopped - but if nobody takes the browser back (the
        window was closed mid-run, or the resume was refused), the animating
        page the run left behind burns CPU with nobody watching (the 1027%
        incident). This is the fallback the skip relies on."""
        with self._lock:
            if self._lease is not None or self._agent_active:
                return False
        # A handover or a probe of this process holds the mutex, and a holder of
        # another process is in the record: parking would blank the page either
        # of them is about to read.
        if self._handover_mutex.locked():
            return False
        if self._dedicated_scope is None:
            try:
                from vaf.core import browser_jar
                if browser_jar.record_for(self._container_name).foreign_holders():
                    return False
            except Exception:
                return False
        try:
            park_browser_idle(self.cdp_base())
        except Exception:
            pass
        return True

    def _arm_unclaimed_parker(self) -> None:
        def _later():
            time.sleep(self.UNCLAIMED_PARK_S)
            self._park_if_unclaimed()
        threading.Thread(target=_later, daemon=True,
                         name="browser-giveback-parker").start()

    HANDOVER_TTL_S = 180.0

    def take_agent_handover(self, user_scope_id: Optional[str] = None) -> Optional[dict]:
        """The page the evicted person was on, for the run that takes over.

        Consume-once and scope-gated: only the run of the SAME scope may learn
        where somebody was browsing, and only the first one - a later,
        unrelated run must not inherit a stale 'continue here'. The TTL covers
        the remaining staleness (a run that spent minutes in the queue)."""
        scope = resolve_browser_scope(user_scope_id)
        with self._lock:
            h = self._handover
            if h is None:
                return None
            if time.time() - h.get("at", 0) > self.HANDOVER_TTL_S:
                self._handover = None
                return None
            if h.get("scope") != scope:
                return None
            self._handover = None
            return dict(h)

    def run_generation(self) -> int:
        """Which run of this browser is current: agent_run_ended moves it on. A run
        reads it once it has passed its concurrency gate, right before it hands it
        to hand_jar_to_run - not earlier: the end of the run ahead of a run still
        queued at the gate moves it on, and the queued run's handover would then
        be refused as if it had been stopped."""
        with self._lock:
            return self._run_generation

    def hand_jar_to_run(self, user_scope_id: Optional[str] = None,
                        persistent: bool = False,
                        continuing: bool = False,
                        run_generation: Optional[int] = None) -> None:
        """Hand the browser's cookie jar over to the run that is about to use it.

        Called AFTER the concurrency gate, by the process that actually drives
        the browser (a spawned child does its own, once it holds its own gate) -
        never before, see stop_for_agent_run above.

        The jar half exists because the agent lane shares one cookie jar with
        everyone: browser_use works in the default browser context and calls
        browser-level Storage.setCookies (measured - no createBrowserContext
        anywhere in it), so without a handover a run browses with whatever the
        previous holder left behind. The eviction in stop_for_agent_run has
        already EXPORTED that holder's cookies to their per-scope file, so the
        scrub wipes nothing that is not saved. A non-persistent run is scrubbed
        even for the same scope: "starts with a clean browser" is the tool's
        documented promise.

        Across processes the gate does not reach, so the SHARED container's run
        is claimed in the holder record every VAF process keeps
        (vaf.core.browser_jar): a probe of another process is waited out, a
        lease or a run there refuses this one with BrowserHeldElsewhere, and the
        owner the handover compares with is the record's, never this process's
        memory alone. Named boundary: within one process the gate admits
        VAF_BROWSER_MAX_PARALLEL runs at once, and above 1 they share one jar,
        as they always have, and one holder entry: the first run to end releases
        it, so other processes see the browser free while the second run still
        drives it (and that end also retires a second run still waiting for a
        probe). The pool is the answer to that, since a per-user instance is only
        ever driven by that user's runs.

        `run_generation` is what run_generation() answered after the run passed
        its concurrency gate (BrowserAgentTool._run_browser reads it there, and
        run() ends a run before it opens the gate for the next one, so the end of
        the run ahead of a queued run neither refuses its handover nor releases
        its holder entry; and the queued run's own place in `_agent_runs`, taken
        at its eviction, keeps the browser marked as driven in between). The wait for a
        probe of another process runs in the run's executor thread, and
        a Stop cancels the run without waiting for that thread: agent_run_ended
        had already run when the late claim landed, so the run's entry stayed in
        the record for the life of the process and every other process saw the
        browser busy (measured: a coder's render_check busy until a restart). A
        run that is no longer current when its claim lands releases it and
        raises BrowserHandoverError before it touches the jar. Without the
        argument the generation is read here, on entry.

        A SCOPE CHANGE is fail-closed: the verified profile wipe either
        confirms, or this raises BrowserHandoverError and the run must not
        browse - proceeding would hand it the previous holder's live logins.
        Same-scope paths stay lenient (a failed clean-start scrub costs the
        promise, not isolation) and are only logged."""
        scope = resolve_browser_scope(user_scope_id)
        with self._lock:
            generation = self._run_generation if run_generation is None else run_generation
        with self._handover_mutex:
            with self._lock:
                claimed = self._run_token is not None
                current = self._run_generation == generation
            if not current:
                raise BrowserHandoverError("the run ended before it held the browser")
            if self._dedicated_scope is None and not claimed:
                # The run is claimed in the cross-process record before it takes
                # the jar: a probe of another process is waited out, a lease or a
                # run there refuses this one (BrowserHeldElsewhere). Released by
                # agent_run_ended, like the in-process flag.
                from vaf.core.browser_jar import KIND_RUN
                token, other = self._claim_shared(KIND_RUN, scope, wait_s=self.PROBE_WAIT_S)
                if token is None:
                    append_domain_log("webui", "[browser_interactive] shared browser held by "
                                               f"another VAF process ({(other or {}).get('kind')}); "
                                               "run refused")
                    raise BrowserHeldElsewhere(
                        f"shared browser in use by another VAF process ({(other or {}).get('kind')})")
                with self._lock:
                    current = self._run_generation == generation
                    if current:
                        self._run_token = token
                if not current:
                    self._release_shared(token)
                    append_domain_log("webui", "[browser_interactive] the run ended while it "
                                               "waited for the shared browser; claim released")
                    raise BrowserHandoverError("the run ended while it waited for the browser")
            self._hand_jar_locked(scope, persistent=persistent, continuing=continuing)
            with self._lock:
                if self._run_generation == generation:
                    self._xfer_scope = scope

    @contextlib.contextmanager
    def hold_for_probe(self, user_scope_id: Optional[str] = None):
        """Hold this browser for one render probe: yields True to render, False for busy.

        The probe (vaf.core.browser_render) navigates the browser's first page and
        reads its text, so it is a holder of the jar like an agent run, and it gets
        the same handover: hand_jar_to_run's rules with persistent=True. On the
        SHARED browser an owner the cross-process record does not vouch for is
        the purge plus the verified profile wipe, fail-closed: BrowserHandoverError,
        raised before anything is navigated. Afterwards the jar is the probe's
        scope, for every process, so the files its file target mirrors into the
        Workspace are purged by the next holder's handover instead of showing up
        in that person's file picker. A dedicated instance gets no wipe (only its
        own user is routed to it) and no scrub (persistent).

        The handover mutex is held for the WHOLE probe, not just the handover: a
        lease or a run that started between the wipe and the navigation would get
        its own jar restored under the probe, and the probe would read that
        holder's page. Taken without waiting, with the busy check repeated inside
        it - the probe waits for no one and evicts no one, so a held mutex or any
        activity answers busy. The probe is most often a coder child's, in a
        process of its own, where this manager's lease and memory are empty: on
        the shared browser the probe is therefore also claimed in the holder
        record (any live holder of another process is busy, and a lease or a run
        there waits for the probe), and the queue scan counts a browser agent run
        spawned but not yet holding the browser. On a dedicated instance both
        would count other users' runs on their own instances, so there it stays
        local."""
        scope = resolve_browser_scope(user_scope_id)
        if not self._handover_mutex.acquire(blocking=False):
            yield False
            return
        try:
            if self.has_activity() or (self._dedicated_scope is None
                                       and self.is_agent_active()):
                yield False
                return
            token = None
            if self._dedicated_scope is None:
                # Any live holder of another process (a person's lease in the web
                # process while this probe runs in a coder child, a spawned run,
                # another probe) is busy too: this process's state cannot see it.
                from vaf.core.browser_jar import KIND_PROBE
                token, _other = self._claim_shared(KIND_PROBE, scope, wait_s=0.0)
                if token is None:
                    yield False
                    return
            try:
                self._hand_jar_locked(scope, persistent=True, continuing=False, lane="render")
                yield True
            finally:
                self._release_shared(token)
        finally:
            self._handover_mutex.release()

    def _hand_jar_locked(self, scope: str, *, persistent: bool, continuing: bool,
                         lane: str = "agent") -> None:
        """hand_jar_to_run's body, for a caller that already holds _handover_mutex
        (hand_jar_to_run itself, and hold_for_probe, which keeps the mutex for the
        whole probe). One implementation, so the probe's handover cannot drift from
        the agent run's. Raises BrowserHandoverError like hand_jar_to_run; `lane`
        only labels the refusal in the log."""
        with self._lock:
            dedicated = self._dedicated_scope
        if dedicated is not None:
            # A per-user instance: the profile IS this user's, and nobody
            # else's state can be in it. Only the clean-start promise of a
            # non-persistent run still applies, and never a profile wipe.
            if not persistent:
                try:
                    _cookie_op(self.cdp_base(), "scrub")
                except Exception as e:
                    append_domain_log("webui", f"[browser_interactive] clean-start scrub failed: {e}")
        else:
            holds, instance = self._jar_holds(scope)
            if not holds:
                self._take_jar(scope, lane, instance)
            elif not persistent and not continuing:
                # Same scope, but the run promised a clean start: quick scrub -
                # the person's own logins are already exported and come back on
                # their next interactive lease or persistent run. In full mode
                # the clean start deepens to a profile wipe (the mode's one
                # remaining meaning; a scope change wipes regardless).
                # `continuing` overrides the promise: a run that takes over the
                # person's LIVE session exists to carry it on, and scrubbing
                # would log out the very session it was handed.
                try:
                    if scrub_mode() == "full":
                        # Verified, although a clean start stays best-effort: the
                        # wipe deletes the mirrored Workspace folder at Chromium's
                        # relaunch, seconds after the request, so the mirror's
                        # signature may only be dropped once the deletion is
                        # confirmed - a sync in between would copy the files,
                        # store their signature, and lose them to the wipe. An
                        # unconfirmed wipe may still have deleted them, so the
                        # signature is dropped either way (one extra copy at most).
                        try:
                            if not verified_profile_wipe(self._container_name, self.cdp_base()):
                                append_domain_log("webui", "[browser_interactive] full scrub: "
                                                           "profile wipe not confirmed; quick scrub only")
                        finally:
                            self._workspace_wiped()
                    _cookie_op(self.cdp_base(), "scrub")
                except Exception as e:
                    append_domain_log("webui", f"[browser_interactive] clean-start scrub failed: {e}")
        try:
            _set_download_behavior(self.cdp_base(), allow=downloads_mode() != "off")
        except Exception as e:
            append_domain_log("webui", f"[browser_interactive] download policy not applied: {e}")
        with self._lock:
            self._last_cookie_scope = scope

    # -- the shared browser across processes ----------------------------------
    # Everything below is the SHARED container's: a dedicated instance has one
    # user, so its owner is known from the start and nobody else holds it.
    # vaf.core.browser_jar holds the record and says why it exists.

    def _jar_holds(self, scope: str) -> tuple:
        """(does the shared profile hold this scope's state, the container instance
        that answer is about). Asked of the record every VAF process keeps, never of
        this process's memory: a coder child or a spawned run in another process
        changes hands without this manager seeing it. Anything the record cannot
        vouch for, a record that cannot be read included, is an unknown owner: the
        caller wipes."""
        from vaf.core import browser_jar
        instance = browser_jar.container_instance(self._container_name)
        try:
            return browser_jar.record_for(self._container_name).owner_is(scope, instance), instance
        except Exception:
            return False, instance

    def _take_jar(self, scope: str, lane: str, instance: str) -> None:
        """The cross-scope handover of the shared browser: the record's owner is
        cleared first, then the transfer folders are purged (a previous holder's
        downloads and mirrored Workspace files never ride into another scope's
        session), the profile wipe is VERIFIED, the cookies are cleared, and only
        then is the new owner recorded - a crash in between leaves "unknown",
        never a wrong name. Fail-closed: anything short of that raises
        BrowserHandoverError, a record that cannot be written included, because
        the other processes would keep trusting the old owner."""
        from vaf.core import browser_jar
        jar = browser_jar.record_for(self._container_name)
        try:
            # A holder of THIS process is not kept out by the record (it only
            # refuses other processes), so the handover itself waits for the
            # previous holder's file transfers: purging and wiping under one
            # would let its files land in the new holder's browser afterwards.
            if not self._transfers_drained(self.TRANSFER_DRAIN_S):
                raise BrowserHandoverError("the previous holder's file transfer is still running")
            jar.set_owner(None, instance)
            _purge_container_downloads(self._container_name)
            if not verified_profile_wipe(self._container_name, self.cdp_base()):
                raise BrowserHandoverError("profile wipe not confirmed")
            _cookie_op(self.cdp_base(), "clear_cookies")
            jar.set_owner(scope, instance)
            self._workspace_wiped()
            with self._lock:
                self._last_cookie_scope = scope
        except Exception as e:
            # The purge above may already have emptied the folder.
            self._workspace_wiped()
            append_domain_log(
                "webui", f"[browser_interactive] {lane} cross-scope handover refused: {e}")
            try:
                from vaf.core.security_events import log_security_event
                log_security_event("browser_handover_failed",
                                   username=str(scope or "")[:32],
                                   detail=str(e)[:200], channel="browser")
            except Exception:
                pass
            if isinstance(e, BrowserHandoverError):
                raise
            raise BrowserHandoverError(str(e)) from e

    # A probe of another process is short and bounded, so a lease or a run waits
    # it out like it waits for a probe of its own process (the handover mutex);
    # past this budget it answers busy rather than hang the window or the run.
    PROBE_WAIT_S = 60.0

    def _claim_shared(self, kind: str, scope: str, *, wait_s: float) -> tuple:
        """Record this process as a holder of the shared browser: (token, None),
        or (None, the live holder of another process that is in the way). A probe
        of another process is waited out for up to wait_s; any other holder, or a
        record that cannot be read ({"kind": "unknown"}), is the answer at once."""
        from vaf.core import browser_jar
        jar = browser_jar.record_for(self._container_name)
        deadline = time.time() + max(0.0, wait_s)
        while True:
            try:
                token, other = jar.claim(kind, scope)
            except browser_jar.JarRecordUnavailable as e:
                append_domain_log("webui", f"[browser_interactive] holder record unavailable: {e}")
                return None, {"kind": "unknown"}
            if token is not None:
                return token, None
            if (other or {}).get("kind") != browser_jar.KIND_PROBE or time.time() >= deadline:
                return None, other or {"kind": "unknown"}
            time.sleep(0.25)

    def _release_shared(self, token: Optional[str]) -> None:
        if token:
            from vaf.core import browser_jar
            browser_jar.record_for(self._container_name).release(token)

    def sweep_downloads(self, user_scope_id: Optional[str]) -> List[str]:
        """Drain finished downloads to their owner's workspace. Mode-gated,
        BLOCKING (docker + hashing), never raises - see the module helper."""
        if downloads_mode() == "off" or not user_scope_id:
            return []
        return _sweep_container_downloads(self._container_name, str(user_scope_id))

    def sync_workspace(self, user_scope_id: Optional[str]) -> List[str]:
        """Mirror the holder's files into the browser for uploads; returns the
        mirrored container paths (the agent's upload whitelist). Mode-gated,
        signature-cheap when nothing changed, BLOCKING, never raises."""
        if workspace_sync_mode() == "off" or not user_scope_id:
            return []
        with self._lock:
            prev = self._ws_sig
            gen = self._ws_wipe_gen
        sig, paths = _sync_workspace_to_container(
            self._container_name, str(user_scope_id), prev)
        with self._lock:
            if self._ws_wipe_gen == gen:
                self._ws_sig = sig
        return paths

    def _workspace_wiped(self) -> None:
        """The mirrored Workspace folder was deleted, or may have been: the next
        sync copies everything again, and a sync still in flight from before
        this point stores nothing (`_ws_wipe_gen`). Called AFTER the deletion
        is confirmed (or known to have failed), never at the request."""
        with self._lock:
            self._ws_wipe_gen += 1
            self._ws_sig = None

    def _held_transfer(self, scope: Optional[str], transfer, refused):
        """Run one file transfer for `scope` only while that scope holds this
        browser; answer `refused` without running it otherwise. Registered under
        the lock in the same step as the check, so a holder's release either sees
        it (and waits for it) or it sees the release (and does not start)."""
        with self._lock:
            if not scope or self._xfer_scope != scope:
                return refused
            self._xfers += 1
        try:
            return transfer()
        finally:
            pending: List[str] = []
            with self._lock:
                self._xfers -= 1
                if self._xfers == 0:
                    self._xfers_idle.notify_all()
                    pending, self._release_when_drained = self._release_when_drained, []
            for token in pending:
                self._release_unless_reclaimed(token)

    def _transfers_drained(self, timeout: float) -> bool:
        """Wait until no file transfer is in flight; False when `timeout` ran out
        first. The caller holds no lock (the wait releases this manager's)."""
        with self._lock:
            deadline = time.monotonic() + timeout
            while self._xfers > 0:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._xfers_idle.wait(left)
            return True

    def _release_after_transfers(self, token: Optional[str]) -> None:
        """Give up a holder entry once no file transfer is in flight: the entry is
        what keeps a holder of ANOTHER process out. The caller has already cleared
        `_xfer_scope`, so no new transfer can start. A transfer that outlives
        TRANSFER_DRAIN_S keeps the entry: the last transfer releases it when it
        ends (`_held_transfer`), so the browser stays held, never handed over,
        while files are still moving."""
        if not token:
            return
        if not self._transfers_drained(self.TRANSFER_DRAIN_S):
            with self._lock:
                if self._xfers > 0:
                    self._release_when_drained.append(token)
                    append_domain_log("webui", "[browser_interactive] a file transfer is still "
                                               "running; the browser is released when it ends")
                    return
        self._release_unless_reclaimed(token)

    def _release_unless_reclaimed(self, token: str) -> None:
        """Release an entry unless a holder of this process has claimed it again in
        the meantime: a lease claim of the same process answers with the existing
        entry's token, so a new lease that started while the old one was still
        releasing holds that very token, and releasing it would leave the new
        lease without its entry - open to a holder of another process."""
        with self._lock:
            if token in (self._lease_token, self._run_token):
                return
        self._release_shared(token)

    def agent_stream_started(self, session_id: Optional[str]) -> None:
        """Grant the run's own chat window a watch-only live stream of the run.

        Called once the run HOLDS the browser, right after its handover
        (BrowserAgentTool._run_browser): before it, the browser still shows the
        previous holder's state, or a probe of another process the handover waits
        out. A refused handover raises first and grants nothing.

        In-process lane only: in a spawned child this manager is not the one
        the web server's proxy validates tickets against, so a grant made
        there would be a link to a 403 - the child lane keeps the screenshot
        view. Emitted only to the session that owns the run; the ticket IS the
        capability, so nobody else may receive it. A grant replaced here ends,
        and so do the sockets opened on it (`grant_current`)."""
        if not session_id:
            return
        if os.environ.get("VAF_IN_SUBAGENT_TERMINAL", "").strip().lower() in ("1", "true", "yes"):
            return
        with self._lock:
            stream = AgentStream(session_id=str(session_id),
                                 ticket=secrets.token_urlsafe(24),
                                 started_at=time.time())
            self._agent_stream = stream
            payload = self._agent_payload(stream)
        self._emit(payload, str(session_id))

    def agent_run_abandoned(self) -> None:
        """A run that evicted (stop_for_agent_run) and then left before it drove -
        the concurrency gate timed out on it: it gives its place in `_agent_runs`
        back, and nothing else. The run ahead of it is still driving, so the run
        generation, the holder entry and the give-back are that run's to settle."""
        with self._lock:
            self._agent_runs = max(0, self._agent_runs - 1)
            # Unless the run ahead already ended and left its give-back to the last
            # counted run, which this one now is.
            settle = self._agent_runs == 0 and self._giveback_deferred
            holder = sid = None
            if settle:
                self._giveback_deferred = False
                holder, self._pre_agent_holder = self._pre_agent_holder, None
                sid = self._last_session_id
        if settle:
            self._announce_run_end(holder, sid)

    def agent_run_ended(self, notify: bool = True) -> None:
        with self._lock:
            # This run's place in the count, in both modes; a run queued behind it
            # keeps the browser marked as driven.
            self._agent_runs = max(0, self._agent_runs - 1)
            # The browser is handed back, and its window told it is free, only when
            # the LAST run ends: a run queued behind this one drives next.
            last_run = self._agent_runs == 0
            # The spawn lane (notify=False) never claimed: its child does, in its
            # own process, and releases there.
            run_token = None
            if notify:
                run_token, self._run_token = self._run_token, None
                if self._dedicated_scope is None:
                    self._xfer_scope = None
                # The run is over: a handover of it still under way in an executor
                # thread (stopped while it waited) must not take the browser now.
                self._run_generation += 1
            # The watch-only ticket dies with the run; a stream that outlives
            # it would keep showing the browser to a window with no run behind.
            self._agent_stream = None
            sid = self._last_session_id
            # Who settles the give-back: the last counted run's end. A spawned run's
            # end (notify=False) settles one only when a run ahead of it already ended
            # and left it deferred; otherwise the holder stays remembered for a
            # notifying end, as before. A notifying end with a run still counted
            # defers it, and the flag is left alone by every other end.
            settle = last_run and (notify or self._giveback_deferred)
            holder = None
            if settle:
                holder, self._pre_agent_holder = self._pre_agent_holder, None
                self._giveback_deferred = False
            elif notify:
                self._giveback_deferred = True
            run_scope = self._last_cookie_scope
        # A run downloads too (the agent saves a PDF for the person): drain to
        # the run's OWN scope - the jar owner at this moment is that run's.
        self.sweep_downloads(run_scope)
        # After the run's own mirror, which a stopped run may have left running in
        # an executor thread.
        self._release_after_transfers(run_token)
        # Tell the window that owned the interactive view the browser is free
        # again. When an interactive lease was evicted FOR this run, the stop
        # event says resumable: the holder's window re-enters the interactive
        # mode instead of closing - the run only borrowed the browser.
        # Best-effort. notify=False is the spawn lane, where run() returns its
        # marker while the child is only starting - announcing a free browser
        # there would be a lie (the holder stays remembered for the run that
        # does end with a notification). A run still queued keeps both for its own end.
        # A spawned run that settles a deferred give-back hands the browser back to
        # the evicted person only: its child drives now, so "free" would be a lie.
        if not settle:
            return
        self._announce_run_end(holder, sid if notify else None)

    def _announce_run_end(self, holder: Optional[tuple], sid: Optional[str]) -> None:
        """The last counted run is over: the evicted holder's window gets the browser back
        (resumable), or the run's window is told it is free."""
        if holder is not None:
            self._emit({"status": "stopped", "reason": "agent_done",
                        "saving": False, "streamPath": "", "resumable": True},
                       holder[1])
            # The run's parking stood down for this give-back; make sure an
            # unclaimed browser still gets parked (window closed mid-run).
            self._arm_unclaimed_parker()
        elif sid:
            self._emit({"status": "stopped", "reason": "agent_done",
                        "saving": False, "streamPath": "", "resumable": False}, sid)

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
        unless the caller is an admin, who may evict. A revoked account gets
        nothing (`_scope_revoked`; checked again where the lease is installed).
        """
        if _scope_revoked(user_scope_id):
            return dict(_REVOKED_REFUSAL)
        if self.is_agent_active():
            # The run's own window may WATCH: hand it the view-only stream
            # grant when one exists for exactly this session. Any other
            # session gets the bare refusal - the ticket is the capability,
            # and a foreign user must not see what someone's agent browses.
            with self._lock:
                stream = self._agent_stream
                if stream is not None and stream.session_id == session_id:
                    return self._agent_payload(stream)
            return self._payload("agent_active", reason="agent_run")

        # One handover at a time: the busy check, the wipe and the cookie
        # restore below must read as one atomic story, or two interleaving
        # start() calls can each restore their own cookies into the shared jar
        # and the later lease install cannot undo the earlier restore.
        with self._handover_mutex:
            return self._start_locked(user_scope_id, session_id, save=save,
                                      session_name=session_name, is_admin=is_admin)

    def _start_locked(self, user_scope_id: str, session_id: str, *, save: bool,
                      session_name: str, is_admin: bool) -> dict:
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
            # If the wipe below then fails, the evicted lease is already gone
            # and the admin gets the refusal: nobody holds the lease, the
            # evicted user's cookies are saved, and the fail-closed gate holds.
            self.stop("superseded", force=True)

        # The stream is about to be promised to a window: make sure the
        # container actually answers first.
        try:
            resolve_cdp_ws_url(self.cdp_base())
        except Exception:
            return self._payload("error", reason="browser_unavailable")

        with self._lock:
            dedicated = self._dedicated_scope
        if dedicated is None:
            # The SHARED browser is used from other processes too (a coder
            # child's render_check, a spawned run, the terminal app), where this
            # manager's lease and memory do not reach. The lease is claimed in
            # the record they all keep: a probe there is waited out, as one of
            # this process is (the handover mutex); a run or a lease there
            # refuses, as one here does.
            from vaf.core.browser_jar import KIND_LEASE, KIND_RUN
            token, other = self._claim_shared(KIND_LEASE, user_scope_id, wait_s=self.PROBE_WAIT_S)
            if token is None:
                kind = (other or {}).get("kind")
                if kind == KIND_RUN:
                    return self._payload("agent_active", reason="agent_run")
                if kind == "unknown":
                    return self._payload("error", reason="handover_failed")
                return {"status": "busy", "reason": "in_use",
                        "saving": False, "streamPath": ""}
            with self._lock:
                self._lease_token = token

            # Jar handover, before the person starts browsing. A scope CHANGE is
            # a VERIFIED profile wipe, fail-closed: the previous holder's
            # cookies, localStorage/IndexedDB, history, saved passwords and
            # autofill all die with the profile, or nobody gets the browser. The
            # owner is the record's, not this process's memory: whatever the
            # record cannot vouch for (no record, another container or an
            # earlier start of this one, a handover cut short) counts as a
            # change. A DEDICATED instance is exempt: only its own user is ever
            # routed to it, so its profile is theirs to keep.
            holds, instance = self._jar_holds(user_scope_id)
            if not holds:
                try:
                    self._take_jar(user_scope_id, "interactive", instance)
                except BrowserHandoverError:
                    # Released unless a lease of this process is still installed,
                    # decided now under the lock (as at the install below): a lease
                    # seen before the claim proves nothing, since a run's eviction
                    # in between leaves a fresh entry with no lease behind it.
                    with self._lock:
                        stale = self._lease is None
                        if stale:
                            self._lease_token = None
                    if stale:
                        self._release_shared(token)
                    return self._payload("error", reason="handover_failed")

        # The lenient half: the jar is verifiably the new holder's now, so a
        # failure here (their own cookie restore, the mirror) costs comfort,
        # not isolation - grant the lease and say so in the log.
        try:
            _set_download_behavior(self.cdp_base(), allow=downloads_mode() != "off")
            # The person's files appear in the browser's file picker from the
            # first moment of their session.
            self.sync_workspace(user_scope_id)
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
            # The run check again, in the same lock step that installs the lease.
            # start() checked at its top, and a run that began while this start
            # was in its handover (seconds: the verified wipe) found no lease to
            # evict - stop_for_agent_run reads the lease and counts itself under
            # this same lock - and then drove under the lease installed here.
            # Measured: the person kept a live ticket on the run's browser. Checked
            # here, the run's count and this install cannot interleave.
            refused_for_run = self._agent_runs > 0
            # A revoked account gets no lease either. The revocation listener stopped
            # the leases that stood when the account was revoked; one installed after
            # that is never stopped (measured: a revocation during this start's wipe
            # left the account a live ticket). The mark is set before any listener
            # runs, and the listener reads the lease under this same lock, so either
            # it sees this install or this install sees the mark.
            refused_revoked = not refused_for_run and _scope_revoked(user_scope_id)
            stale_token = None
            if refused_for_run or refused_revoked:
                if self._lease is None:
                    # No lease of this process is installed, so the record entry
                    # is this start's own claim and goes back with it. Decided
                    # here, not from a lease seen before the claim: a run's
                    # eviction between that look and the claim released the old
                    # entry, the claim made a new one, and that one stayed in the
                    # record with no lease behind it (measured: other processes saw
                    # the browser held until this process's next lease cycle). A
                    # lease still installed here is evicted by the run's
                    # stop_for_agent_run, which releases its entry.
                    stale_token, self._lease_token = self._lease_token, None
            else:
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
                # A fresh (or refreshed) lease supersedes any pending give-back
                # from an earlier agent takeover: the browser has an owner again.
                # The person returning also outdates any captured handover page.
                self._pre_agent_holder = None
                self._handover = None
                # A stream is being handed out, so a viewer is about to draw its picture
                # from scratch and will show its own splash while doing so. The count of
                # what the PREVIOUS viewer received says nothing about that, and leaving
                # it standing reported "picture is up" before a single pixel had arrived -
                # which is how the foreign splash came back on every open after the first.
                # Resetting here rather than only on the last disconnect makes it hold no
                # matter how the window was closed.
                lease.pixels_seen = 0
                self._xfer_scope = user_scope_id
                self._ensure_janitor()
                payload = self._payload("active", lease=lease)
        if refused_for_run or refused_revoked:
            # The record entry this start claimed goes back with it; the run's
            # own handover finds this start's scope as the owner and wipes.
            self._release_shared(stale_token)
            if refused_revoked:
                return dict(_REVOKED_REFUSAL)
            return self._payload("agent_active", reason="agent_run")
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
            if self._dedicated_scope is None:
                self._xfer_scope = None
            sid = lease.session_id
            lease_token, self._lease_token = self._lease_token, None

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
        # Whatever the person downloaded and the janitor has not drained yet
        # leaves WITH them - on takeover too, so the run that follows never
        # finds another scope's files in the hand-off folder.
        self.sweep_downloads(lease.user_scope_id)
        # Released last: the export above still reads the person's own jar, and a
        # transfer the janitor started for this person must end first.
        self._release_after_transfers(lease_token)

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
    def _match_ticket(self, ticket: str):
        """The lease or agent stream this ticket belongs to. Caller holds the lock."""
        lease = self._lease
        if lease is not None and secrets.compare_digest(lease.ticket, ticket or ""):
            return lease
        stream = self._agent_stream
        if stream is not None and secrets.compare_digest(stream.ticket, ticket or ""):
            return stream
        return None

    def validate_ticket(self, ticket: str):
        """The proxy's auth gate: a lease's ticket or a run's watch-only ticket.
        Returns the grant itself, which the stream relay keeps for `grant_current`."""
        with self._lock:
            return self._match_ticket(ticket)

    def grant_current(self, grant) -> bool:
        """Is this grant (what validate_ticket returned) still this browser's lease
        or its run's watch grant? The stream relay asks on every message it passes
        and a watchdog asks for a quiet socket; False ends the socket.

        Derived from the two fields every grant change writes, so no end of a grant
        can be missed by it, present or future: a stop, a takeover, a newer window,
        an admin, the run's end, a new watch grant all replace or clear one of them.
        IDENTITY, not ticket equality: the same window's refresh keeps its lease
        object and so keeps its socket, and a grant of another kind can never stand
        in for the one a socket was opened on (a lease socket is never turned into a
        watch socket).

        Deliberately without the lock. It runs per message on the server's event
        loop, which must never wait for a worker thread inside a handover, and the
        writes it reads are single reference assignments under the lock: a reader
        sees the old or the new object, never a torn one."""
        return grant is not None and (grant is self._lease or grant is self._agent_stream)

    def stream_connected(self, ticket: str) -> bool:
        with self._lock:
            grant = self._match_ticket(ticket)
            if grant is None:
                return False
            grant.stream_connections += 1
        # Deliberately NO emit here. This is the socket opening, and the viewer's own
        # splash covers the handshake that follows - announcing it as "connected"
        # lifted our cover at the exact moment that splash appeared. stream_bytes()
        # makes that call instead. This path stays what it always was: the auth gate
        # and the single-viewer count.
        return True

    def stream_bytes(self, ticket: str, count: int) -> bool:
        """Report bytes relayed from the display server. Returns True once the
        picture is up, so the caller can stop reporting.

        This - not the socket accept - is what the window's connecting cover waits
        for. Emitting only on the crossing keeps it to one message per grant.
        """
        with self._lock:
            grant = self._match_ticket(ticket)
            if grant is None:
                return True    # no grant of ours: nothing to report, stop asking
            if grant.pixels_seen >= self.PIXELS_THRESHOLD:
                return True
            grant.pixels_seen += max(0, count)
            crossed = grant.pixels_seen >= self.PIXELS_THRESHOLD
            if not crossed:
                return False
            payload = (self._payload("active", lease=grant)
                       if isinstance(grant, InteractiveLease)
                       else self._agent_payload(grant))
            sid = grant.session_id
        self._emit(payload, sid)
        return True

    def stream_disconnected(self, ticket: str) -> None:
        with self._lock:
            grant = self._match_ticket(ticket)
            if grant is None:
                return
            grant.stream_connections = max(0, grant.stream_connections - 1)
            gone = grant.stream_connections == 0
            if gone:
                # The next viewer draws its own picture from scratch and shows its
                # own splash while doing so, so it must wait for its own frames.
                grant.pixels_seen = 0
                if isinstance(grant, InteractiveLease):
                    grant.last_disconnect = time.time()
            payload = (self._payload("active", lease=grant)
                       if isinstance(grant, InteractiveLease)
                       else self._agent_payload(grant))
            sid = grant.session_id
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
                # Deliver finished downloads promptly (one janitor tick, ~5s,
                # after the browser completes them), not only at session end:
                # "it downloaded, but where?" is answered by the file simply
                # appearing in the person's own Downloads folder. The reverse
                # mirror stays fresh the same way - a file that just landed in
                # the workspace becomes uploadable within a tick, and an
                # unchanged workspace costs a directory walk, nothing more.
                # Both only while this person still holds the browser: the lease
                # was read above, and it may have ended since.
                scope = lease.user_scope_id
                self._held_transfer(scope, lambda: self.sweep_downloads(scope), [])
                self._held_transfer(scope, lambda: self.sync_workspace(scope), [])
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
    @staticmethod
    def _stream_doc_path(ticket: str, *, view_only: bool = False) -> str:
        """The viewer document URL for one ticket.

        The ?path= is not decoration, it is the whole stream. The KasmVNC client
        builds its socket URL from SETTINGS, not relative to its own directory:
            ws://<host>:<port>/ + getSetting("path")   // default "websockify"
        so without this it dials ws://<backend>/websockify, a route that does not
        exist here, and the viewer never connects. The client reads settings from
        the URL (hash first, then query), so handing it the ticketed path makes it
        dial our proxy route instead. No leading slash: the client adds "/" itself.
        host/port/encrypt stay default and therefore point at the iframe's own
        origin, which IS the backend serving this route.

        RELATIVE on purpose. The document is loaded same-origin by the window, so
        it travels whichever front door the person is on (the dev server, or the
        HTTPS proxy when LAN hosting is on) and no scheme has to be guessed here -
        guessing it wrong is exactly how this lane produced an empty response: the
        backend port speaks HTTPS while TLS is on, and a plain http:// iframe URL
        got nothing back. The socket's host and port are appended by the frontend
        from its own backend-socket helper, which is the app's single answer to
        "where is the backend".
        resize=remote: the server only ALLOWS resizing, the viewer has to ask
        for it. Without this the display stays at its start geometry and the
        window shows black bars above and below the page.
        view_only=1 (the agent's watch grant): the viewer setting is read as a
        truthy STRING, so it is either present ("1") or absent - never "0".
        """
        ws_path = f"api/browser-vnc/t/{ticket}/websockify"
        doc = (f"/api/browser-vnc/t/{ticket}/index.html"
               f"?path={ws_path}&resize=remote")
        if view_only:
            doc += "&view_only=1"
        return doc

    def _agent_payload(self, stream: Optional[AgentStream] = None) -> dict:
        """The agent-run status for the run's own window: watchable, not drivable.

        Same event shape as the lease payloads, same status name the start()
        refusal always used ("agent_active") - the difference is that the run's
        own session now gets a streamPath to watch the run through."""
        if stream is None:
            with self._lock:
                stream = self._agent_stream
        return {
            "status": "agent_active",
            "reason": "agent_run",
            "saving": False,
            "streamPath": (self._stream_doc_path(stream.ticket, view_only=True)
                           if stream else ""),
            "viewerConnected": bool(stream and stream.pixels_seen >= self.PIXELS_THRESHOLD),
        }

    def _payload(self, status: str, *, reason: str = "",
                 lease: Optional[InteractiveLease] = None) -> dict:
        if lease is None:
            with self._lock:
                lease = self._lease
        active = status == "active" and lease is not None
        stream = self._stream_doc_path(lease.ticket) if active else ""
        return {
            "status": status,
            "reason": reason,
            "saving": bool(lease.save_enabled) if lease else False,
            "streamPath": stream,
            # Is a viewer socket actually attached right now? The window shows its own
            # connecting state until this turns true, which keeps the stream viewer's
            # own branded loading screen out of sight without touching its files.
            "viewerConnected": bool(active and lease.pixels_seen >= self.PIXELS_THRESHOLD),
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
# Managers for the pool's per-user instances, keyed by container name. The
# shared manager stays its own global (tests pin and monkeypatch it), the
# registry only ever grows pool entries; scans walk both.
_pool_managers: Dict[str, InteractiveBrowserManager] = {}


def get_interactive_manager() -> InteractiveBrowserManager:
    """The SHARED browser's manager - the fallback every lane can rely on."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = InteractiveBrowserManager()
        return _manager


def _manager_for_instance(inst) -> InteractiveBrowserManager:
    """The manager of one pooled instance, created once per container name.

    The manager outlives the instance's start: the idle reaper stops the
    container and the next use starts it again, and Docker publishes it on NEW
    host ports at every start (the pool publishes without a fixed host port).
    The endpoints are therefore taken from the instance on EVERY call, which is
    what each caller has just resolved from the running container; everything
    else the manager carries (lease, grants, runs, the dedicated scope) belongs
    to the container and stays. A manager that kept its first endpoints answered
    `browser_unavailable` for that person until VAF restarted (measured)."""
    with _manager_lock:
        mgr = _pool_managers.get(inst.container_name)
        if mgr is None:
            mgr = InteractiveBrowserManager(
                cdp_base_url=inst.cdp_base,
                vnc_base_url=inst.vnc_base,
                container_name=inst.container_name,
                dedicated_scope=inst.user_scope_id,
            )
            _pool_managers[inst.container_name] = mgr
        else:
            mgr._cdp_base_url = inst.cdp_base
            mgr._vnc_base_url = inst.vnc_base
        return mgr


def _all_managers() -> List[InteractiveBrowserManager]:
    with _manager_lock:
        managers = list(_pool_managers.values())
    managers.append(get_interactive_manager())
    return managers


_REVOKED_REFUSAL = {"status": "stopped", "reason": "account_revoked",
                    "saving": False, "streamPath": ""}


def _scope_revoked(user_scope_id) -> bool:
    """Whether the account was deactivated or deleted (`vaf.core.revocation`). Takes
    only the revocation module's own lock, which is never held while a manager's lock
    is taken, so it may be asked under a manager's lock."""
    try:
        from vaf.core.revocation import is_revoked
        key = str(user_scope_id or "").strip()
        return bool(key) and is_revoked(key)
    except Exception:
        return False


def _on_account_revoked(user_scope_id: str) -> None:
    """Revocation listener (vaf.core.revocation): a revoked account loses the browser.

    Every lease of that scope, on every browser, is stopped like any other end of a
    lease: its cookies are exported, its downloads drained, its holder entry released,
    and the sockets opened on it close (`grant_current`). Leases of other scopes are
    untouched: the stop names the scope as its requester, so a lease that changed
    hands between the look and the stop is refused, not ended. A start that was under
    way refuses at its install (`_scope_revoked`). The account's watch grant ends with
    its run, which the same revocation stops (cooperatively, within seconds).

    Only a REVOCATION (deactivation, deletion). The listeners also hear a demotion and
    a narrowed allowlist (`stop_account_work`), and those leave the browser alone: a
    lease carries neither the role nor the allowlist - start() reads neither, beyond
    an admin's eviction at the moment of the start - so ending it would take the
    browser from a person who may open it again at once.

    Runs on the revoking thread, where the blocking stop is allowed. Never raises."""
    try:
        from vaf.core.revocation import is_revoked
        key = str(user_scope_id or "").strip()
        if not key or not is_revoked(key):
            return
        for mgr in _all_managers():
            with mgr._lock:
                lease = mgr._lease
                scope = lease.user_scope_id if lease is not None else None
            if scope is None or str(scope).strip() != key:
                continue
            try:
                mgr.stop("account_revoked", requester_scope=scope)
            except Exception:
                pass
    except Exception:
        pass


def _ensure_revocation_listener() -> None:
    """Register `_on_account_revoked` once; `add_revocation_listener` ignores a
    listener it already holds. Called by every manager's constructor."""
    try:
        from vaf.core.revocation import add_revocation_listener
        add_revocation_listener(_on_account_revoked)
    except Exception:
        pass


def peek_manager_for_container(container_name: str) -> Optional[InteractiveBrowserManager]:
    """Registry lookup without side effects - the pool's reaper asks here."""
    if container_name == "vaf-browser":
        return get_interactive_manager()
    with _manager_lock:
        return _pool_managers.get(container_name)


def get_manager_for_scope(user_scope_id: Optional[str]) -> InteractiveBrowserManager:
    """The manager of the browser THIS scope should use.

    Resolves through the pool, which may START the scope's own instance -
    BLOCKING, run in an executor. Every pool failure answers with the shared
    manager, so the browser degrades to time-sharing instead of vanishing -
    EXCEPT under strict mode: PoolExhausted propagates, because handing the
    shared manager to a strict-pool user is the exact fallback strict
    forbids; callers surface it as a busy/refusal."""
    try:
        from vaf.core.browser_pool import PoolExhausted, get_browser_pool
        inst = get_browser_pool().resolve(resolve_browser_scope(user_scope_id))
        if inst is not None:
            return _manager_for_instance(inst)
    except PoolExhausted:
        raise
    except Exception:
        pass
    return get_interactive_manager()


def _manager_for_run(container_name: Optional[str] = None) -> InteractiveBrowserManager:
    """The manager of the browser a run PINNED at its start.

    Addressed by container name, never re-resolved by scope: the pool's
    scope-to-instance map is shared and mutable, so a scope lookup can answer
    differently at the end of a run than it did at the start (the instance was
    idle-stopped, or the capacity gate sent the run to the shared browser and
    an instance appeared later). That mismatch would clear `_agent_active` on a
    manager that never set it and leave the real one flagged forever, so every
    interactive start on that browser would be refused for the life of the
    process. A pooled browser's manager exists because the run registered it
    at its start (register_run_browser), before its first hook; an unknown name
    therefore means the shared browser, which is also where a run without an
    instance falls back to. Before that registration, a run on a pooled browser
    nobody had opened since the start counted, evicted, wiped and mirrored on the
    SHARED manager while it drove its own instance (measured: the shared run
    count stayed at 1 for the life of the process once the pooled manager
    appeared mid-run, and a spawned child was refused for a lease it never
    touched)."""
    if container_name and container_name != "vaf-browser":
        mgr = peek_manager_for_container(container_name)
        if mgr is not None:
            return mgr
    return get_interactive_manager()


def register_run_browser(instance) -> None:
    """Hook for BrowserAgentTool.run(), right after the pool resolve and before
    stop_for_agent_run: the manager of the pooled browser this run drives exists
    from here on (`_manager_for_instance`), so every later hook addressed by its
    container name lands on it, and the pool's reaper sees the run as activity.
    No instance (the shared browser) is nothing to do. Never raises."""
    if instance is None:
        return
    try:
        _manager_for_instance(instance)
    except Exception:
        pass


def get_manager_by_ticket(ticket: str) -> Optional[InteractiveBrowserManager]:
    """The manager whose lease or watch grant owns this stream ticket. The VNC
    proxy routes resolve the instance THROUGH the ticket, nothing else."""
    for mgr in _all_managers():
        if mgr.validate_ticket(ticket) is not None:
            return mgr
    return None


def manager_for_session(session_id: str) -> Optional[InteractiveBrowserManager]:
    """The manager whose interactive lease this chat session holds, if any."""
    for mgr in _all_managers():
        if mgr.interactive_session_id() == session_id:
            return mgr
    return None


def get_manager_for_stop(user_scope_id: Optional[str],
                         session_id: Optional[str] = None) -> InteractiveBrowserManager:
    """The manager a stop request should land on: the one actually holding a
    lease for this session or scope. Deliberately no pool RESOLVE here - a
    stop must never start a container to have something to stop."""
    if session_id:
        mgr = manager_for_session(str(session_id))
        if mgr is not None:
            return mgr
    scope = resolve_browser_scope(user_scope_id) if user_scope_id else None
    if scope:
        for mgr in _all_managers():
            with mgr._lock:
                if mgr._lease is not None and mgr._lease.user_scope_id == scope:
                    return mgr
    return get_interactive_manager()


def stop_for_agent_run(container_name: Optional[str] = None) -> None:
    """Hook for BrowserAgentTool.run(): evict the interactive lease on the
    run's own browser. No jar work - see hand_jar_to_run. Never raises.

    The lease can sit on a DIFFERENT manager than the one this run pinned. The
    pool resolves a scope to an instance twice, at different moments: once when
    the person opens the browser (`get_manager_for_scope`) and again when a run
    starts (`get_browser_pool().resolve`). If the answer changed in between (the
    instance was idle-stopped or reaped, or the capacity gate sent the run to the
    shared browser), the run pins one manager while the person's lease lives on
    another. Evicting only the run's manager left that lease in place and, worse,
    unremembered: no `_pre_agent_holder` anywhere, so the give-back at the end of
    the run had nobody to hand back to. The person's window then waited for an
    event that was never sent and stayed in the agent view for good.

    "One person drives at a time" is a property of the browser, not of a single
    manager instance, so the lease is followed wherever it lives. Only managers
    that actually hold one are touched.
    """
    try:
        run_mgr = _manager_for_run(container_name)
        run_mgr.stop_for_agent_run()
        for mgr in _all_managers():
            if mgr is run_mgr:
                continue
            with mgr._lock:
                has_lease = mgr._lease is not None
            if has_lease:
                # Eviction and give-back only: that browser is not the one this
                # run drives, and its own run state may belong to a run of its
                # own (agent_run_ended below says why).
                mgr.evict_for_run_elsewhere()
    except Exception:
        pass


def run_generation(container_name: Optional[str] = None) -> int:
    """Hook for BrowserAgentTool._run_browser, after the concurrency gate and right
    before hand_jar_to_run: which run of the run's browser is current. Never raises."""
    try:
        return _manager_for_run(container_name).run_generation()
    except Exception:
        return 0


def hand_jar_to_run(user_scope_id: Optional[str] = None,
                    persistent: bool = False,
                    container_name: Optional[str] = None,
                    continuing: bool = False,
                    run_generation: Optional[int] = None) -> None:
    """Hook for BrowserAgentTool.run(), AFTER the concurrency gate: hand the
    browser's jar to this run's scope.

    Raises BrowserHandoverError when a CROSS-SCOPE handover could not be made
    safe - the run must refuse instead of browsing over the previous holder's
    logins. The old "Never raises" contract swallowed exactly that case."""
    _manager_for_run(container_name).hand_jar_to_run(
        user_scope_id=user_scope_id, persistent=persistent,
        continuing=continuing, run_generation=run_generation)


def take_agent_handover(user_scope_id: Optional[str] = None,
                        container_name: Optional[str] = None) -> Optional[dict]:
    """Hook for BrowserAgentTool: the page the evicted person was on, once,
    for the run of the same scope on the same browser. Never raises."""
    try:
        return _manager_for_run(container_name).take_agent_handover(user_scope_id)
    except Exception:
        return None


def sync_workspace_for_run(user_scope_id: Optional[str] = None,
                           container_name: Optional[str] = None) -> List[str]:
    """Hook for BrowserAgentTool: mirror the run owner's files into the run's
    browser and return their container paths - the upload whitelist the agent
    hands to browser-use. Never raises."""
    try:
        mgr = _manager_for_run(container_name)
        scope = resolve_browser_scope(user_scope_id)
        # Only while the run holds the browser: its executor thread can outlive
        # a stopped run, and the run's end waits for it before letting go.
        return mgr._held_transfer(scope, lambda: mgr.sync_workspace(scope), [])
    except Exception:
        return []


def give_back_pending(container_name: Optional[str] = None) -> bool:
    """Hook for BrowserAgentTool's end-of-run parking: keep the browser's
    state when an evicted person is about to take it back. Never raises."""
    try:
        return _manager_for_run(container_name).give_back_pending()
    except Exception:
        return False


def agent_stream_started(session_id: Optional[str],
                         container_name: Optional[str] = None) -> None:
    """Hook for BrowserAgentTool._run_browser, right after the run's handover (the
    in-process lane): grant the run's own window a watch-only live stream of the
    run's own browser. Never raises."""
    try:
        _manager_for_run(container_name).agent_stream_started(session_id)
    except Exception:
        pass


def agent_run_abandoned(container_name: Optional[str] = None) -> None:
    """Hook for BrowserAgentTool.run(): the run evicted and then left before it
    drove (its concurrency gate timed out). Gives back its count only. Never
    raises."""
    try:
        _manager_for_run(container_name).agent_run_abandoned()
    except Exception:
        pass


def agent_run_ended(notify: bool = True,
                    container_name: Optional[str] = None) -> None:
    """Hook for BrowserAgentTool.run()'s finally. Never raises.

    Mirrors stop_for_agent_run: the run's own manager ends the run, and every
    other manager whose lease this run evicted is told to hand back - the
    give-back only (give_back_elsewhere). Another manager's `_agent_active`, its
    holder entry in the cross-process record and its run generation belong to
    that browser's own run, which may still be driving: ending them here was the
    asymmetry `_manager_for_run` exists to prevent, one level up. notify=False
    (the spawn lane) leaves the remembered holders for the run that ends with a
    notification.
    """
    try:
        run_mgr = _manager_for_run(container_name)
        run_mgr.agent_run_ended(notify=notify)
        if not notify:
            return
        for mgr in _all_managers():
            if mgr is run_mgr:
                continue
            # The give-back only. The full agent_run_ended on another manager
            # ended THAT browser's run too: measured, a run ending on a dedicated
            # instance released the shared browser's holder entry and moved its
            # generation on while another run still drove it there, so a coder
            # child's render would wipe under that run.
            mgr.give_back_elsewhere()
    except Exception:
        pass
