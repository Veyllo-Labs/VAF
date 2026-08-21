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
  by the container a run pinned at its start;
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
            if op == "scrub":
                # The quick handover scrub: cookies plus every site's stored
                # state in one sweep. storageTypes "all" covers local_storage,
                # indexeddb, cache_storage, service_workers and cookies; the
                # wildcard origin is honoured by this Chromium (measured: a
                # localStorage entry set on a real origin was gone after this
                # call). The HTTP disk cache is NOT in "all" - that residue is
                # the full scrub's job, and the docs name it.
                await asyncio.wait_for(client.send.Storage.clearCookies(params={}), 10)
                await asyncio.wait_for(
                    client.send.Storage.clearDataForOrigin(
                        params={"origin": "*", "storageTypes": "all"}), 15)
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
        from vaf.core.browser_pool import _docker
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


def _sync_workspace_to_container(container_name: str, user_scope_id: str,
                                 prev_sig) -> tuple:
    """Mirror the owner's files into the browser. Returns (signature, paths).

    The reverse of the download sweep: a website's file picker (and the
    agent's upload_file action) can only see the CONTAINER filesystem, so
    uploading "my PDF" is impossible unless the file exists in there. The
    mirror is one-way (host wins, container copy is disposable), bulk (one
    docker cp of a staged tree, not one exec per file), and signature-gated:
    an unchanged workspace costs a directory walk and nothing else. `paths`
    always lists every mirrored file's container path - the agent's upload
    whitelist - whether or not a copy happened this round. BLOCKING: call off
    the event loop. Never raises."""
    paths: List[str] = []
    try:
        from vaf.core.browser_pool import _docker
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
        import tempfile
        staging = tempfile.mkdtemp(prefix="vaf-browser-ws-")
        try:
            for rel, _s, _m in files:
                dest = os.path.join(staging, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(os.path.join(os.fspath(root), rel), dest)
            _docker(["exec", "-u", "root", container_name, "sh", "-c",
                     f"mkdir -p {_WS_CONTAINER_DIR} && chown -R browser:browser {_WS_CONTAINER_DIR}"],
                    timeout=20)
            r = _docker(["cp", f"{staging}/.", f"{container_name}:{_WS_CONTAINER_DIR}/"],
                        timeout=300)
            if r.returncode != 0:
                return prev_sig, paths
            _docker(["exec", "-u", "root", container_name, "sh", "-c",
                     f"chown -R browser:browser {_WS_CONTAINER_DIR}"], timeout=20)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
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
        from vaf.core.browser_pool import _docker
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


def scrub_mode() -> str:
    """The handover scrub depth: "quick" (CDP sweep, default) or "full" (adds a
    profile wipe with a Chromium relaunch). An env knob like its siblings
    VAF_BROWSER_MAX_PARALLEL and VAF_BROWSER_CDP_URL; anything unrecognized
    means quick, so a typo degrades to the safe-and-fast default."""
    return "full" if os.environ.get("VAF_BROWSER_SCRUB", "").strip().lower() == "full" else "quick"


def request_profile_wipe(container_name: str = "vaf-browser") -> None:
    """Full-scrub half: wipe the container's Chromium profile between launches.

    A `docker restart` does NOT do this - the container filesystem survives
    restarts, so History, Login Data, autofill and downloads would all come
    back. Instead the entrypoint's supervisor owns the wipe: this drops a
    marker and kills Chromium, the supervisor relaunches it and start_chromium
    deletes the profile and the downloads first when the marker is present.
    The extension reinstalls itself into the fresh profile (external-
    extensions provider, see the Dockerfile). Callers need no explicit wait:
    every CDP consumer already polls through resolve_cdp_ws_url, which rides
    out the relaunch. Same docker access and best-effort stance as the stop
    watchdog's container restart in vaf/tools/browser_agent.py."""
    try:
        import subprocess
        from vaf.core.service_stack import resolve_docker_exe
        docker = resolve_docker_exe()
        subprocess.run(
            [docker, "exec", "-u", "root", container_name, "sh", "-c",
             "touch /home/browser/.scrub-profile"
             " && chown browser:browser /home/browser/.scrub-profile"
             " && pkill chromium"],
            timeout=20, capture_output=True,
        )
        append_domain_log("webui", "[browser_interactive] full scrub: profile wipe requested")
    except Exception as e:
        append_domain_log("webui", f"[browser_interactive] full scrub failed (quick scrub still ran): {e}")


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
    emitted only to the chat session that owns the run. The ticket dies with
    the run. Screenshots keep streaming regardless: they are the fallback for
    the lanes without a grant (workflow tile, spawned child)."""

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
        self._lease: Optional[InteractiveLease] = None
        self._agent_active = False
        self._agent_stream: Optional[AgentStream] = None
        # The lease an agent takeover evicted: (user_scope_id, session_id).
        # This is the fact "the person was driving BEFORE the agent took the
        # browser", kept server-side so it survives a page reload during the
        # run. agent_run_ended() turns it into a resumable=True stop event to
        # that session, whose window then re-enters the interactive mode
        # instead of closing; any freshly started lease clears it.
        self._pre_agent_holder: Optional[tuple] = None
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
        self._janitor_alive = False

    # -- environment -------------------------------------------------------
    def cdp_base(self) -> str:
        # Env fallback evaluated at CALL time on purpose: tests and deployments
        # repoint the shared browser mid-process via these variables.
        return self._cdp_base_url or os.environ.get("VAF_BROWSER_CDP_URL", "http://localhost:9222")

    def vnc_base(self) -> str:
        return self._vnc_base_url or os.environ.get("VAF_BROWSER_VNC_URL", "http://127.0.0.1:6901")

    def container_name(self) -> str:
        return self._container_name

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
            self._agent_active = True
            lease = self._lease
            self._pre_agent_holder = ((lease.user_scope_id, lease.session_id)
                                      if lease else None)
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

    def hand_jar_to_run(self, user_scope_id: Optional[str] = None,
                        persistent: bool = False,
                        continuing: bool = False) -> None:
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

        Named boundary: the gate is a per-process semaphore, so two VAF
        processes (a chat run and a workflow child) can still reach the same
        SHARED container at once. That race predates this lane - it is what
        VAF_BROWSER_MAX_PARALLEL has always been - and the pool is the answer
        to it: a per-user instance is only ever driven by that user's runs."""
        try:
            scope = resolve_browser_scope(user_scope_id)
            with self._lock:
                jar_scope = self._last_cookie_scope
                dedicated = self._dedicated_scope
            if dedicated is not None:
                # A per-user instance: the profile IS this user's, and nobody
                # else's state can be in it. Only the clean-start promise of a
                # non-persistent run still applies, and never a profile wipe.
                if not persistent:
                    _cookie_op(self.cdp_base(), "scrub")
            elif jar_scope != scope:
                if scrub_mode() == "full":
                    request_profile_wipe(self._container_name)
                _cookie_op(self.cdp_base(), "scrub")
                # Same rule as the interactive handover: a previous holder's
                # transfer folders never ride into another scope's session.
                _purge_container_downloads(self._container_name)
                self._ws_sig = None
            elif not persistent and not continuing:
                # Same scope, but the run promised a clean start: quick scrub
                # only - the person's own logins are already exported and come
                # back on their next interactive lease or persistent run.
                # `continuing` overrides the promise: a run that takes over the
                # person's LIVE session exists to carry it on, and scrubbing
                # would log out the very session it was handed.
                _cookie_op(self.cdp_base(), "scrub")
            _set_download_behavior(self.cdp_base(), allow=downloads_mode() != "off")
            with self._lock:
                self._last_cookie_scope = scope
        except Exception as e:
            append_domain_log("webui", f"[browser_interactive] agent jar handover failed: {e}")

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
        sig, paths = _sync_workspace_to_container(
            self._container_name, str(user_scope_id), prev)
        with self._lock:
            self._ws_sig = sig
        return paths

    def agent_stream_started(self, session_id: Optional[str]) -> None:
        """Grant the run's own chat window a watch-only live stream of the run.

        In-process lane only: in a spawned child this manager is not the one
        the web server's proxy validates tickets against, so a grant made
        there would be a link to a 403 - the child lane keeps the screenshot
        view. Emitted only to the session that owns the run; the ticket IS the
        capability, so nobody else may receive it."""
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

    def agent_run_ended(self, notify: bool = True) -> None:
        with self._lock:
            self._agent_active = False
            # The watch-only ticket dies with the run; a stream that outlives
            # it would keep showing the browser to a window with no run behind.
            self._agent_stream = None
            sid = self._last_session_id
            holder = self._pre_agent_holder if notify else None
            if notify:
                self._pre_agent_holder = None
            run_scope = self._last_cookie_scope
        # A run downloads too (the agent saves a PDF for the person): drain to
        # the run's OWN scope - the jar owner at this moment is that run's.
        self.sweep_downloads(run_scope)
        # Tell the window that owned the interactive view the browser is free
        # again. When an interactive lease was evicted FOR this run, the stop
        # event says resumable: the holder's window re-enters the interactive
        # mode instead of closing - the run only borrowed the browser.
        # Best-effort. notify=False is the spawn lane, where run() returns its
        # marker while the child is only starting - announcing a free browser
        # there would be a lie (the holder stays remembered for the run that
        # does end with a notification).
        if not notify:
            return
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
        unless the caller is an admin, who may evict.
        """
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

        # Jar handover, before the person starts browsing. A scope CHANGE gets
        # the scrub, not just a cookie clear: the previous holder's
        # localStorage/IndexedDB carry logins on token-based sites just as
        # cookies do. On the SHARED browser an unknown owner counts as a change -
        # after a server restart the tracker is empty while the container may
        # still hold anyone's state, and trusting that gap is how residue
        # crosses users. A DEDICATED instance is exempt: only its own user is
        # ever routed to it, so its profile is theirs to keep.
        try:
            with self._lock:
                jar_scope = self._last_cookie_scope
                dedicated = self._dedicated_scope
            if dedicated is None and jar_scope != user_scope_id:
                if scrub_mode() == "full":
                    request_profile_wipe(self._container_name)
                _cookie_op(self.cdp_base(), "scrub")
                # Residue in the transfer folders belongs to the PREVIOUS
                # holder; delivering it onward (or letting the next person
                # upload it) would be a cross-user hand-off. It dies here,
                # and the mirror starts over for the new holder.
                _purge_container_downloads(self._container_name)
                self._ws_sig = None
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
        # Whatever the person downloaded and the janitor has not drained yet
        # leaves WITH them - on takeover too, so the run that follows never
        # finds another scope's files in the hand-off folder.
        self.sweep_downloads(lease.user_scope_id)

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
        """The proxy's auth gate: a lease's ticket or a run's watch-only ticket."""
        with self._lock:
            return self._match_ticket(ticket)

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
                self.sweep_downloads(lease.user_scope_id)
                self.sync_workspace(lease.user_scope_id)
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
        return mgr


def _all_managers() -> List[InteractiveBrowserManager]:
    with _manager_lock:
        managers = list(_pool_managers.values())
    managers.append(get_interactive_manager())
    return managers


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
    manager, so the browser degrades to time-sharing instead of vanishing."""
    try:
        from vaf.core.browser_pool import get_browser_pool
        inst = get_browser_pool().resolve(resolve_browser_scope(user_scope_id))
        if inst is not None:
            return _manager_for_instance(inst)
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
    process. An unknown name answers with the shared manager - the same browser
    the run itself falls back to."""
    if container_name and container_name != "vaf-browser":
        mgr = peek_manager_for_container(container_name)
        if mgr is not None:
            return mgr
    return get_interactive_manager()


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
    run's own browser. No jar work - see hand_jar_to_run. Never raises."""
    try:
        _manager_for_run(container_name).stop_for_agent_run()
    except Exception:
        pass


def hand_jar_to_run(user_scope_id: Optional[str] = None,
                    persistent: bool = False,
                    container_name: Optional[str] = None,
                    continuing: bool = False) -> None:
    """Hook for BrowserAgentTool.run(), AFTER the concurrency gate: hand the
    browser's jar to this run's scope. Never raises."""
    try:
        _manager_for_run(container_name).hand_jar_to_run(
            user_scope_id=user_scope_id, persistent=persistent,
            continuing=continuing)
    except Exception:
        pass


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
        return _manager_for_run(container_name).sync_workspace(
            resolve_browser_scope(user_scope_id))
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
    """Hook for BrowserAgentTool.run()'s in-process lane: grant the run's own
    window a watch-only live stream of the run's own browser. Never raises."""
    try:
        _manager_for_run(container_name).agent_stream_started(session_id)
    except Exception:
        pass


def agent_run_ended(notify: bool = True,
                    container_name: Optional[str] = None) -> None:
    """Hook for BrowserAgentTool.run()'s finally. Never raises."""
    try:
        _manager_for_run(container_name).agent_run_ended(notify=notify)
    except Exception:
        pass
