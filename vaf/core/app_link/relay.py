# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The connector to the Veyllo relay: one OUTBOUND connection, opened only while it is needed.

It runs on its own thread with its own asyncio loop in the web server's process (where the
pairing state is) and is idle, with no connection at all, unless the relay is switched on
AND the installation holds its relay secret S AND either a relay QR was claimed or a device
uses the relay. Nobody reaches VAF through it except the paired devices' link protocol: the
relay hands TLS bytes behind a 16-byte cid to a link session (session.py), which decides by
the device certificate who is talking. The cid is only an address; it never decides
identity. The web UI and the API are never reachable through the relay.

The relay contract (veyllo-web docs/relay.md) in what VAF uses of it:
- ``POST /v1/relay/claim {pair: P}`` (Bearer S once S exists): 202 not scanned yet; 200
  ``{instanceId, member, cid, secret?}`` (S only the first time); 409 pairing used; 410
  expired; 401 wrong S; 429 and 503 with ``retry-after``;
- ``POST /v1/relay/token`` (Bearer S) ``{token, url, expiresAt}``; the token goes ONLY into
  the upgrade's Authorization header, renewed in-band with ``{"t":"auth"}`` before expiry;
- binary frames ``[0x01][cid]`` + TLS bytes both ways (VAF never sends the all-zero cid, the
  relay's broadcast); text frames are control (``ping``/``pong``, ``apps``, ``app``,
  ``error``); close codes 4001 replaced, 4002 token expired, 4003 revoked, 4008 too fast;
- ``DELETE /v1/relay/members?member=`` per member, ``POST /v1/relay/instance
  {action:"rotate"}``, ``DELETE /v1/relay/instance``, ``POST /v1/relay/notify {member}``
  (a push without content; 401 means S is not valid, 404 the device is not reachable).

``member`` is a UUID as text and ``cid`` 32 lowercase hex characters (the 16 bytes behind
0x01): both are kept verbatim, in one place each (``_member``, ``_cid_hex``).
"""
from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit

from vaf.core.app_link import CHANNEL, CONFIG_KEY
from vaf.core.app_link import protocol as pr

logger = logging.getLogger(__name__)

CLAIM_EVERY_SECONDS = 3
CLAIMS_PER_MINUTE = 30
PING_EVERY_SECONDS = 25
PONG_WITHIN_SECONDS = 20
RENEW_BEFORE_SECONDS = 60
NOTIFY_AFTER_SECONDS = 30
NOTIFY_EVERY_SECONDS = 300
NOTIFY_UNREACHABLE_SECONDS = 3600
REPLACED_WAIT_SECONDS = 300
TOO_FAST_WAIT_SECONDS = 60
BACKOFF_MAX_SECONDS = 300
_ZERO_CID = bytes(pr.CID_BYTES)

STATES = ("off", "idle", "connecting", "connected", "backoff", "replaced", "revoked",
          "secret_rejected", "unavailable")


def _member(value: Any) -> Optional[str]:
    """A relay member as the relay writes it: a UUID as lowercase text."""
    try:
        return str(uuid.UUID(str(value))) if value else None
    except ValueError:
        return None


def _cid_hex(value: Any) -> Optional[str]:
    """A cid as 32 lowercase hex characters (bytes or text)."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex() if len(value) == pr.CID_BYTES else None
    text = str(value or "").strip().lower()
    try:
        return text if len(bytes.fromhex(text)) == pr.CID_BYTES else None
    except ValueError:
        return None


def _secret() -> str:
    from vaf.core.channel_secrets import channel_secret
    return channel_secret(CHANNEL, "relay_secret")


def _event(detail: str, username: str = "") -> None:
    try:
        from vaf.core.security_events import log_security_event
        log_security_event("app_relay_changed", channel=CHANNEL, username=username, detail=detail)
    except Exception:
        pass


def _set_instance(instance_id: str) -> None:
    from vaf.core.config import Config
    with Config._locked():
        cfg = Config.load()
        block = cfg.get(CONFIG_KEY) if isinstance(cfg.get(CONFIG_KEY), dict) else {}
        cfg[CONFIG_KEY] = {**dict(Config.DEFAULTS.get(CONFIG_KEY) or {}), **block,
                           "relay_instance_id": str(instance_id or "")}
        Config.save(cfg)


def set_relay_enabled(enabled: bool, *, username: str = "") -> None:
    """The admin's switch. Off keeps S, the instance and every member (no new pairing on)."""
    from vaf.core.config import Config
    with Config._locked():
        cfg = Config.load()
        block = cfg.get(CONFIG_KEY) if isinstance(cfg.get(CONFIG_KEY), dict) else {}
        cfg[CONFIG_KEY] = {**dict(Config.DEFAULTS.get(CONFIG_KEY) or {}), **block,
                           "relay_enabled": bool(enabled)}
        Config.save(cfg)
    _event("on" if enabled else "off", username)
    connector().wake(reset_hold=True)


def _parse_expiry(value: Any, now: float) -> float:
    try:
        number = float(value)
        return number / 1000.0 if number > 1e12 else number
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return now + 540


def _retry_after(response, default: float) -> float:
    try:
        return max(1.0, float(response.headers.get("retry-after")))
    except (TypeError, ValueError, AttributeError):
        return default


class RelayLink:
    """One relay connection's sessions: binary frames to and from link sessions by cid."""

    def __init__(self, send_frame: Callable[[bytes], None], connection_id: str):
        self._send_frame = send_frame
        self.connection_id = connection_id
        self.sessions: Dict[bytes, Any] = {}
        self.online: set = set()

    def _session(self, cid: bytes):
        session = self.sessions.get(cid)
        if session is not None and not session.closed:
            return session
        from vaf.core.app_link import bridge

        def _send(data: bytes, cid=cid) -> None:
            self._send_frame(pr.wrap_relay(cid, data))

        session = bridge.new_session("relay", _send, connection=f"relay:{self.connection_id}",
                                     peer=f"cid:{cid.hex()}", peer_label=f"relay cid {cid.hex()[:8]}",
                                     relay_cid=_cid_hex(cid))
        if session is not None:
            self.sessions[cid] = session
        return session

    def on_binary(self, frame: bytes) -> None:
        try:
            cid, data = pr.unwrap_relay(frame)
        except pr.FrameError:
            logger.debug("app link: a relay frame without the head was dropped")
            return
        if cid == _ZERO_CID:
            return
        session = self._session(cid)
        if session is None:
            return
        session.feed(data)
        if session.closed:
            self.sessions.pop(cid, None)

    def on_control(self, message: Dict[str, Any]) -> None:
        from vaf.core.app_link import bridge
        t = message.get("t")
        if t == "apps":
            self.online = {c for c in (_cid_hex(x) for x in message.get("cids") or []) if c}
        elif t == "app":
            cid = _cid_hex(message.get("cid"))
            if not cid:
                return
            if message.get("on"):
                self.online.add(cid)
                for row in _devices_with_cid(cid):
                    bridge._came_online(row["device_id"])
            else:
                self.online.discard(cid)
                session = self.sessions.pop(bytes.fromhex(cid), None)
                if session is not None:
                    session.close("app_offline", bye=False)
        elif t == "error":
            logger.info("app link: the relay reported %s", str(message.get("code") or "")[:64])

    def close_all(self) -> None:
        for session in list(self.sessions.values()):
            session.close("relay_closed", bye=False)
        self.sessions.clear()


def _devices_with_cid(cid: str) -> List[Dict[str, Any]]:
    from vaf.core.app_link import devices
    return [d for d in devices.all_devices() if d.get("relay_cid") == cid]


class RelayConnector:
    def __init__(self, *, http_factory: Optional[Callable[[], Any]] = None,
                 ws_connect: Optional[Callable[..., Any]] = None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 rng: Callable[[], float] = random.random):
        self._http_factory = http_factory or _default_http
        self._ws_connect = ws_connect or _default_ws_connect
        self._clock = clock
        self._wall = wall
        self._rng = rng
        self.state = "idle"
        self.connected_since: Optional[float] = None
        self.last_error = ""
        self._hold_until = 0.0
        self._claims_until = 0.0
        self._claims_stopped = False
        self._claim_times: List[float] = []
        self._next_claim = 0.0
        self._attempts = 0
        self._notify: Dict[str, Dict[str, float]] = {}
        self._members_retry: Dict[str, float] = {}
        self._link: Optional[RelayLink] = None
        self._ws = None
        self._ws_task: Optional[asyncio.Task] = None
        self._token_expires = 0.0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._wake: Optional[asyncio.Event] = None
        self._thread: Optional[threading.Thread] = None
        self._stopping = False
        self._lock = threading.Lock()
        self._http = None

    # ── the thread ──────────────────────────────────────────────────────────

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            self._thread = threading.Thread(target=self._thread_main, name="vaf-app-link-relay",
                                            daemon=True)
            self._thread.start()
        from vaf.core.app_link import bridge
        bridge.add_offline_hook(self.schedule_notify)
        bridge.add_online_hook(self.cancel_notify)

    def stop(self) -> None:
        self._stopping = True
        self.wake()

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(self._run())
        finally:
            loop.close()

    def wake(self, *, reset_hold: bool = False) -> None:
        if reset_hold:
            self._hold_until = 0.0
            self._claims_stopped = False
            self._attempts = 0
        loop, event = self._loop, self._wake
        if loop is not None and event is not None:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass

    async def _run(self) -> None:
        self._wake = asyncio.Event()
        self._http = self._http_factory()
        try:
            while not self._stopping:
                try:
                    await self.tick()
                except Exception:
                    logger.debug("app link: relay tick failed", exc_info=True)
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
            await self.disconnect(1000)
        finally:
            try:
                await self._http.aclose()
            except Exception:
                pass

    # ── one round ───────────────────────────────────────────────────────────

    def should_connect(self) -> bool:
        from vaf.core.app_link import devices, pairing, settings
        if not settings().get("relay_enabled", True) or not _secret():
            return False
        if pairing.claimed_relay_pairings():
            return True
        return any(d.get("relay_member") for d in devices.all_devices())

    async def tick(self) -> None:
        from vaf.core.app_link import settings
        if not settings().get("relay_enabled", True):
            self.state = "off"
            await self.disconnect(1000)
            return
        now = self._clock()
        await self.claim_tick(now)
        await self.reconcile_members(now)
        await self.notify_tick(now)
        if now < self._hold_until:
            return
        want = self.should_connect()
        running = self._ws_task is not None and not self._ws_task.done()
        if want and not running:
            self._ws_task = asyncio.ensure_future(self._connection())
        elif not want:
            if running:
                await self.disconnect(1000)
            if self.state not in ("replaced", "revoked", "secret_rejected", "unavailable"):
                self.state = "idle"

    # ── claims ──────────────────────────────────────────────────────────────

    def _claim_budget(self, now: float) -> bool:
        self._claim_times = [t for t in self._claim_times if now - t < 60]
        return len(self._claim_times) < CLAIMS_PER_MINUTE

    async def claim_tick(self, now: float) -> None:
        from vaf.core.app_link import pairing, settings
        if self._claims_stopped or now < self._claims_until or now < self._next_claim:
            return
        live = pairing.live_relay_pairings()
        if not live:
            return
        self._next_claim = now + CLAIM_EVERY_SECONDS
        base = str(settings().get("relay_api_base") or "").rstrip("/")
        for item in live:
            if not self._claim_budget(now):
                return
            self._claim_times.append(now)
            secret = _secret()
            headers = {"Authorization": f"Bearer {secret}"} if secret else {}
            try:
                response = await self._http.post(f"{base}/v1/relay/claim",
                                                 json={"pair": pr.b64url(item.p)}, headers=headers)
            except Exception as exc:
                self.last_error = f"claim:{type(exc).__name__}"
                return
            code = response.status_code
            if code == 202:
                continue
            if code == 200:
                self._claimed(item, response)
            elif code == 409:
                pairing.mark_relay_outcome(item.p, "used_elsewhere", "pairing_used")
            elif code == 410:
                pairing.mark_relay_outcome(item.p, "expired", "expired")
            elif code == 401:
                self.state, self.last_error = "secret_rejected", "invalid_relay_secret"
                self._claims_stopped = True
                pairing.set_relay_error("secret_rejected")
                return
            elif code == 429:
                self._claims_until = now + _retry_after(response, 60)
                self.last_error = "rate_limited"
                return
            elif code == 503:
                self._claims_until = now + _retry_after(response, 30)
                self.last_error = "relay_unavailable"
                pairing.set_relay_error("relay_unavailable")
                return
            else:
                self.last_error = f"claim:{code}"

    def _claimed(self, item, response) -> None:
        from vaf.core.app_link import pairing, settings
        from vaf.core.channel_secrets import set_channel_secret
        try:
            body = response.json()
        except Exception:
            body = {}
        instance = str(body.get("instanceId") or "")
        member = _member(body.get("member"))
        stored = str(settings().get("relay_instance_id") or "")
        if not instance or not member:
            self.last_error = "claim:malformed"
            return
        if stored and instance != stored:
            # Another VAF's instance answered for this P: nothing is overwritten.
            self.last_error = "instance_mismatch"
            pairing.set_relay_error("instance_mismatch")
            return
        secret = str(body.get("secret") or "")
        if secret and not _secret():
            set_channel_secret(CHANNEL, "relay_secret", secret)
        if not stored:
            _set_instance(instance)
            _event("claimed")
        pairing.mark_claimed(item.p, member, _cid_hex(body.get("cid")))
        self.wake()

    # ── the connection ──────────────────────────────────────────────────────

    async def _fetch_token(self) -> Dict[str, Any]:
        from vaf.core.app_link import settings
        base = str(settings().get("relay_api_base") or "").rstrip("/")
        response = await self._http.post(f"{base}/v1/relay/token",
                                         headers={"Authorization": f"Bearer {_secret()}"})
        if response.status_code == 401:
            raise _Refused("secret_rejected")
        if response.status_code == 503:
            raise _Refused("unavailable", _retry_after(response, 30))
        if response.status_code != 200:
            raise _Refused("backoff")
        body = response.json()
        url = str(body.get("url") or "")
        parts = urlsplit(url)
        if parts.scheme != "wss" or parts.username or parts.password or not parts.hostname:
            raise _Refused("backoff", reason="bad_url")
        self._token_expires = _parse_expiry(body.get("expiresAt"), self._wall())
        return {"token": str(body.get("token") or ""), "url": url}

    async def _connection(self) -> None:
        self.state = "connecting"
        close_code = None
        try:
            ticket = await self._fetch_token()
            outgoing: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_running_loop()

            def _send_frame(frame: bytes) -> None:
                if frame[1:pr.RELAY_HEAD] == _ZERO_CID:
                    return
                loop.call_soon_threadsafe(outgoing.put_nowait, frame)

            async with self._ws_connect(ticket["url"], {"Authorization": f"Bearer {ticket['token']}"}) as ws:
                self._ws = ws
                self.state = "connected"
                self.connected_since = self._wall()
                self.last_error = ""
                self._attempts = 0
                link = RelayLink(_send_frame, uuid.uuid4().hex[:12])
                self._link = link
                pong = {"at": self._clock()}
                try:
                    await self._pump(ws, link, outgoing, pong)
                finally:
                    link.close_all()
                    self._link = None
                    close_code = getattr(ws, "close_code", None)
        except _Refused as exc:
            self._after_refusal(exc)
            return
        except Exception as exc:
            self.last_error = f"connect:{type(exc).__name__}"
            close_code = close_code or getattr(exc, "code", None)
        finally:
            self._ws = None
            self.connected_since = None
        self.after_close(close_code)

    async def _pump(self, ws, link: RelayLink, outgoing: asyncio.Queue, pong: Dict[str, float]) -> None:
        import json

        async def _reader():
            async for message in ws:
                if isinstance(message, (bytes, bytearray)):
                    link.on_binary(bytes(message))
                    continue
                try:
                    obj = json.loads(message)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("t") == "pong":
                    pong["at"] = self._clock()
                else:
                    link.on_control(obj)

        async def _writer():
            while True:
                frame = await outgoing.get()
                await ws.send(frame)

        async def _keepalive():
            last_ping = self._clock()
            while True:
                await asyncio.sleep(1)
                now = self._clock()
                if now - last_ping >= PING_EVERY_SECONDS:
                    await ws.send(json.dumps({"t": "ping"}))
                    last_ping = now
                if now - pong["at"] > PING_EVERY_SECONDS + PONG_WITHIN_SECONDS:
                    self.last_error = "no_pong"
                    await ws.close(code=1000)
                    return
                if self._token_expires and self._wall() > self._token_expires - RENEW_BEFORE_SECONDS:
                    ticket = await self._fetch_token()
                    await ws.send(json.dumps({"t": "auth", "token": ticket["token"]}))

        tasks = [asyncio.ensure_future(t()) for t in (_reader, _writer, _keepalive)]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()

    def _after_refusal(self, exc: "_Refused") -> None:
        if exc.state == "secret_rejected":
            self.state, self.last_error = "secret_rejected", "invalid_relay_secret"
            self._hold_until = float("inf")
        elif exc.state == "unavailable":
            self.state, self.last_error = "unavailable", "relay_unavailable"
            self._hold_until = self._clock() + exc.wait
        else:
            self.last_error = exc.reason or "token"
            self._backoff()

    def _backoff(self) -> None:
        self._attempts += 1
        ceiling = min(BACKOFF_MAX_SECONDS, 2 ** (self._attempts - 1))
        self.state = "backoff"
        self._hold_until = self._clock() + self._rng() * ceiling

    def after_close(self, code: Optional[int]) -> None:
        """What a closed relay connection means for the next one."""
        now = self._clock()
        if code == 4001:
            self.state, self.last_error = "replaced", "replaced"
            self._hold_until = now + REPLACED_WAIT_SECONDS
        elif code == 4002:
            self.state = "connecting"
            self._hold_until = now
        elif code == 4003:
            self.state, self.last_error = "revoked", "revoked"
            self._hold_until = float("inf")
        elif code == 4008:
            self.state, self.last_error = "backoff", "too_fast"
            self._hold_until = now + TOO_FAST_WAIT_SECONDS
        elif code == 1000 and self.state in ("off", "idle"):
            pass
        else:
            if code == 1009:
                self.last_error = "frame_too_large"
            self._backoff()

    async def disconnect(self, code: int = 1000) -> None:
        ws, task = self._ws, self._ws_task
        if ws is not None:
            try:
                await ws.close(code=code)
            except Exception:
                pass
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=5)
            except Exception:
                task.cancel()
        self._ws_task = None
        self.connected_since = None

    # ── members and notify ──────────────────────────────────────────────────

    async def reconcile_members(self, now: float) -> None:
        from vaf.core.app_link import devices, settings
        secret = _secret()
        if not secret:
            return
        base = str(settings().get("relay_api_base") or "").rstrip("/")
        for member in devices.tombstones():
            if now < self._members_retry.get(member, 0):
                continue
            try:
                response = await self._http.delete(f"{base}/v1/relay/members",
                                                   params={"member": member},
                                                   headers={"Authorization": f"Bearer {secret}"})
                done = response.status_code in (200, 204, 404)
            except Exception:
                done = False
            if done:
                devices.drop_tombstone(member)
                self._members_retry.pop(member, None)
            else:
                tries = self._members_retry.get(f"{member}:n", 0) + 1
                self._members_retry[f"{member}:n"] = tries
                self._members_retry[member] = now + min(BACKOFF_MAX_SECONDS, 2 ** tries)

    def schedule_notify(self, row: Dict[str, Any]) -> None:
        """Something landed in the outbox of a device that is not connected: notify after
        30 seconds unless it comes back, then at most once per 5 minutes."""
        if not row.get("relay_member"):
            return
        did = row["device_id"]
        state = self._notify.setdefault(did, {"due": 0.0, "next_allowed": 0.0, "unreachable": 0.0})
        if not state["due"]:
            state["due"] = max(self._clock() + NOTIFY_AFTER_SECONDS, state["next_allowed"])

    def cancel_notify(self, device_id: str) -> None:
        state = self._notify.get(device_id)
        if state is not None:
            state["due"] = 0.0

    async def notify_tick(self, now: float) -> None:
        from vaf.core.app_link import bridge, devices, settings
        if not settings().get("relay_enabled", True):
            return
        secret = _secret()
        if not secret or self.state == "secret_rejected":
            return
        base = str(settings().get("relay_api_base") or "").rstrip("/")
        for did, state in list(self._notify.items()):
            if not state["due"] or now < state["due"] or now < state["unreachable"]:
                continue
            row = devices.get(did)
            if row is None or not row.get("relay_member") or not row.get("outbox") \
                    or bridge.is_connected(did):
                self._notify.pop(did, None)
                continue
            state["due"] = 0.0
            try:
                response = await self._http.post(f"{base}/v1/relay/notify",
                                                 json={"member": row["relay_member"]},
                                                 headers={"Authorization": f"Bearer {secret}"})
                code = response.status_code
            except Exception:
                code = 0
            state["next_allowed"] = now + NOTIFY_EVERY_SECONDS
            if code == 401:
                self.state, self.last_error = "secret_rejected", "invalid_relay_secret"
                return
            if code == 404:
                if not state["unreachable"]:
                    logger.info("app link: the relay cannot reach device %s with a notice", did[:8])
                state["unreachable"] = now + NOTIFY_UNREACHABLE_SECONDS
            # Still something waiting: the next notice no sooner than 5 minutes from now.
            state["due"] = state["next_allowed"]

    # ── admin actions ───────────────────────────────────────────────────────

    async def rotate(self, *, username: str = "") -> bool:
        from vaf.core.app_link import settings
        from vaf.core.channel_secrets import set_channel_secret
        base = str(settings().get("relay_api_base") or "").rstrip("/")
        async with self._http_factory() as http:
            response = await http.post(f"{base}/v1/relay/instance", json={"action": "rotate"},
                                       headers={"Authorization": f"Bearer {_secret()}"})
        if response.status_code != 200:
            self.last_error = f"rotate:{response.status_code}"
            return False
        secret = str((response.json() or {}).get("secret") or "")
        if not secret:
            return False
        set_channel_secret(CHANNEL, "relay_secret", secret)
        _event("rotated", username)
        self.wake(reset_hold=True)
        return True

    async def dissolve(self, *, username: str = "") -> bool:
        from vaf.core.app_link import devices, settings
        from vaf.core.channel_secrets import clear_channel_secrets
        base = str(settings().get("relay_api_base") or "").rstrip("/")
        async with self._http_factory() as http:
            response = await http.delete(f"{base}/v1/relay/instance",
                                         headers={"Authorization": f"Bearer {_secret()}"})
        if response.status_code not in (200, 204, 404):
            self.last_error = f"dissolve:{response.status_code}"
            return False
        clear_channel_secrets(CHANNEL)
        _set_instance("")
        devices.clear_relay_state()
        _event("dissolved", username)
        self.wake(reset_hold=True)
        return True

    def status(self) -> Dict[str, Any]:
        from vaf.core.app_link import devices, settings
        cfg = settings()
        rows = devices.all_devices()
        state = "off" if not cfg.get("relay_enabled", True) else self.state
        return {"enabled": bool(cfg.get("relay_enabled", True)), "state": state,
                "instance": bool(cfg.get("relay_instance_id")), "secret": bool(_secret()),
                "connected_since": self.connected_since, "last_error": self.last_error,
                "members": sum(1 for r in rows if r.get("relay_member")),
                "devices_on_relay": sum(1 for r in rows if r.get("last_transport") == "relay")}


class _Refused(Exception):
    def __init__(self, state: str, wait: float = 0.0, reason: str = ""):
        super().__init__(state)
        self.state, self.wait, self.reason = state, wait, reason


def _default_http():
    import httpx
    return httpx.AsyncClient(timeout=15.0, follow_redirects=False)


def _default_ws_connect(url: str, headers: Dict[str, str]):
    from websockets.asyncio.client import connect
    return connect(url, additional_headers=headers, max_size=pr.MAX_FRAME_PAYLOAD + 64,
                   ping_interval=None, open_timeout=15)


_connector: Optional[RelayConnector] = None
_connector_lock = threading.Lock()


def connector() -> RelayConnector:
    global _connector
    with _connector_lock:
        if _connector is None:
            _connector = RelayConnector()
        return _connector


def set_connector(value: Optional[RelayConnector]) -> None:
    """Replace the process's connector (tests)."""
    global _connector
    with _connector_lock:
        _connector = value


def start() -> None:
    connector().start()


def wake() -> None:
    connector().wake()


def status() -> Dict[str, Any]:
    return connector().status()
