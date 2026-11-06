# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Request-limit gate: hold requests while a provider's per-key window is closed.

A provider that publishes its request window on every answer (the
``request_limit_headers`` flag in ``vaf/core/provider_registry.py``; today only
``veyllo``) is paced from those headers instead of being retried blindly:

    x-ratelimit-limit-requests      requests per minute for this key
    x-ratelimit-remaining-requests  what is left of the current window
    x-ratelimit-reset-requests      when it refills, a duration ("41s", "1m5s")
    retry-after                     on a 429, the seconds to wait

They come on every answer of that API, streams and the 429 included, and the
limit follows the account and can drop again within a minute, so nothing here
caches the limit itself; only the deadline the latest answer names is kept.

The rules (``RateLimitGate.observe``):
  * remaining 0 closes the gate until the reset has passed (60 s, the length of
    the per-minute window, when no reset can be read);
  * a 429 closes it for exactly ``retry-after`` seconds, without jitter and
    without ``api_retry_after_max``, else for the reset (the remaining-0 rule
    applies only when a 429 names no wait);
  * one answer holds for at most MAX_HOLD_SECONDS (600 s, ten windows);
  * the deadline only moves forward, and every lane sending with the same key in
    this process waits on the same object (``rate_limit_gate``), so the chat
    lane, the coder and speech-to-text share one window as the API does.

Where it sits: ``GatedHttpxClient.send`` for the SDK lane (the hook is
``send()`` and not a transport, because the proxy mounts httpx builds from
``HTTPS_PROXY`` are chosen before a transport and bypass it), and
``send_gated`` for the lanes that post by hand (the coder, speech-to-text).
The client never sleeps: a closed gate answers a local 429 that carries
``x-vaf-held: 1`` and the scheduler of the lane (``_with_retry``) waits.

While the gate holds, ONE warning goes to the ``vaf.rate_limit_gate`` logger
per closed window ("Veyllo API limit reached, continuing in 41 s").

Named boundaries:
  * ``/v1/models`` listings are not gated (``api_backend.list_models``, the web
    server's model listing, the CLI settings listing, the setup wizard's key
    check with a key that is not stored yet). The backend's contract does not
    list that endpoint, every caller is started by a person or runs once per
    selection, and a 429 there already falls back to the static model list.
    Revisit when the backend counts ``/v1/models`` against the window AND a
    listing storm is measured.
  * The gate is per process. A process whose gate is still open learns that the
    window is closed from its next answer (remaining 0, or a 429 with
    ``retry-after``) and holds from then on, so per closed window each live
    process sends at most one request into it, plus what it already had in
    flight. A shared deadline file was not built: P processes overshoot by at
    most P requests against a window of at least 180. Revisit on a measured 429
    burst across processes that stays after this gate.
  * Not on the public facade. An embedder reaches the provider only through
    ``Agent``, ``CoreAgent`` and ``complete()``, which all build their client in
    ``OpenAIProvider`` and are gated without embedder code; no public lane sends
    to such a provider by hand. Export it with the first one that does.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

_log = logging.getLogger("vaf.rate_limit_gate")

# Read at call time, so tests replace them with a fake clock.
_monotonic = time.monotonic
_sleep = time.sleep

# The window the headers describe is one minute long; a remaining-0 answer that
# names no reset holds for one whole window.
WINDOW_SECONDS = 60.0

# The longest hold one answer can set. The contract describes a window of one
# minute, so every wait it allows is honoured exactly; a longer value is outside
# it, and capping it makes such a value cost one probe request every ten minutes
# instead of failing every request on the key until the process restarts.
MAX_HOLD_SECONDS = 10 * WINDOW_SECONDS

# A deadline that moves less than this past the announced one is the same window
# seen again (a second answer, a waiting thread) and is not announced again.
_ANNOUNCE_STEP = 1.0


# ═══════════════════════════════════════════════════════════════════════════════
# WAIT PARSING (shared by the SDK lane, the coder's raw HTTP lane and the gate)
# ═══════════════════════════════════════════════════════════════════════════════
# OpenAI's limits are per organization and per model, measured per minute (RPM
# and TPM; this account: 500 requests, 200k tokens for the gpt-5.6 family). A
# 429 names its own remedy, but in THREE different places and formats, and the
# old header-only integer parse missed two of them on the live incident:
#
#   Retry-After header        "30" - and the docs allow it to be absent
#   x-ratelimit-reset-*       duration strings: "120ms", "0s", "6m0s" (measured)
#   the error message itself  "... Please try again in 186ms."
#
# The incident this exists for: a TPM 429 whose message said "try again in
# 186ms" surfaced to the user as a lost turn, because there was no header, the
# backoff waited 1s+2s and the attempt budget (2) ran out while the window was
# still saturated. 186ms of patience would have saved the turn.

# Both patterns are bounded so that a hostile header or error body cannot make
# them backtrack: the duration pattern runs only on at most _DURATION_MAX_CHARS
# characters with every space removed (it had adjacent optional `\s*` runs, and
# "1h", 500 spaces and an "x" took 26 s while holding the GIL, which stopped
# every thread in the process), and the body phrase captures and skips a bounded
# number of characters, so one search stays linear in the length of the text.
_DURATION_MAX_CHARS = 32
_DURATION_RE = re.compile(r"(?:(\d+)h)?(?:(\d+)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?")
_TRY_AGAIN_RE = re.compile(r"try again in\s{1,4}([0-9hms. ]{1,32}?)[.\s]{0,8}(?:$|Visit)", re.IGNORECASE)
_SPACE_RE = re.compile(r"\s+")


def _parse_duration_seconds(raw: str) -> Optional[float]:
    """'186ms' / '6.13s' / '6m0s' / '1h2m' / bare '30' -> seconds, else None."""
    s = str(raw or "").strip()
    if not s or len(s) > _DURATION_MAX_CHARS:
        return None
    try:  # bare number = seconds (the Retry-After header form, fractions included)
        return float(s)
    except ValueError:
        pass
    m = _DURATION_RE.fullmatch(_SPACE_RE.sub("", s))
    if not m or not any(m.groups()):
        return None
    h, mins, secs, ms = m.groups()
    return (float(h or 0) * 3600 + float(mins or 0) * 60
            + float(secs or 0) + float(ms or 0) / 1000.0)


def rate_limit_wait_seconds(headers, message_text: str = "") -> Optional[float]:
    """The wait a 429 asked for, from whichever source carried it, UNCAPPED.

    Sources in order of authority: the Retry-After header, then the "try again
    in <duration>" phrase in the error body. Callers apply their own cap
    (api_retry_after_max) - the cap is policy, the parse is fact, and mixing
    them made the old parser return None for a capped-out value instead of the
    cap. Returns None when neither source yields a number, so the caller falls
    back to exponential backoff.
    """
    try:
        raw = None
        if headers:
            raw = headers.get("retry-after") or headers.get("Retry-After")
        if raw is not None:
            got = _parse_duration_seconds(raw)
            if got is not None:
                return max(0.0, got)
        m = _TRY_AGAIN_RE.search(str(message_text or ""))
        if m:
            got = _parse_duration_seconds(m.group(1).strip())
            if got is not None:
                return max(0.0, got)
    except Exception:
        pass
    return None


def _header(headers, name: str):
    """One header from an httpx/requests header object or a plain dict, any case."""
    if not headers:
        return None
    try:
        value = headers.get(name)
        if value is None:
            for key, val in headers.items():
                if str(key).lower() == name:
                    return val
        return value
    except Exception:
        return None


def _header_seconds(headers, name: str) -> Optional[float]:
    raw = _header(headers, name)
    if raw is None:
        return None
    got = _parse_duration_seconds(raw)
    return None if got is None or got != got else max(0.0, got)  # got != got: NaN


def _remaining_is_zero(headers) -> bool:
    raw = _header(headers, "x-ratelimit-remaining-requests")
    if raw is None or len(str(raw)) > _DURATION_MAX_CHARS:
        return False
    try:
        return float(str(raw).strip()) == 0
    except (TypeError, ValueError):
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# THE GATE
# ═══════════════════════════════════════════════════════════════════════════════

class RateLimitHeld(Exception):
    """The window stays closed longer than the caller may wait. Nothing was sent."""

    def __init__(self, seconds: float, label: str = "API"):
        self.seconds = max(0.0, float(seconds))
        self.label = label
        super().__init__(
            f"{label} API request limit reached; held locally for "
            f"{math.ceil(self.seconds)} s, nothing was sent")


class RateLimitGate:
    """The closed-until deadline of one key's request window, in this process.

    Thread-safe: the state is guarded by a lock, and sleeping always happens
    outside it, so one waiting lane never blocks another from reading the gate.
    """

    def __init__(self, label: str, *, clock: Optional[Callable[[], float]] = None,
                 sleep: Optional[Callable[[float], Any]] = None):
        self.label = label
        self._clock = clock
        self._sleep_fn = sleep
        self._lock = threading.Lock()
        self._until = 0.0
        self._announced = float("-inf")

    def __repr__(self) -> str:
        return f"RateLimitGate({self.label!r}, closed_for={self.closed_for():.1f})"

    def _now(self) -> float:
        return (self._clock or _monotonic)()

    def _do_sleep(self, seconds: float) -> None:
        (self._sleep_fn or _sleep)(seconds)

    def observe(self, status_code, headers) -> None:
        """Feed one answer. Never raises: missing or unreadable headers mean no hold."""
        try:
            try:
                status = int(status_code)
            except (TypeError, ValueError):
                status = 0
            hold: Optional[float] = None
            if status == 429:
                hold = rate_limit_wait_seconds(headers, "")
                if hold is None:
                    hold = _header_seconds(headers, "x-ratelimit-reset-requests")
            # Only when a 429 named no wait of its own: a 429 waits what it names,
            # not the whole window a missing reset would stand for.
            if hold is None and _remaining_is_zero(headers):
                reset = _header_seconds(headers, "x-ratelimit-reset-requests")
                hold = WINDOW_SECONDS if reset is None else reset
            if not hold or hold != hold or hold <= 0 or math.isinf(hold):
                return
            self._extend(min(hold, MAX_HOLD_SECONDS))
        except Exception:
            return

    def _extend(self, hold: float) -> None:
        announce = None
        with self._lock:
            now = self._now()
            until = now + hold
            if until <= self._until:
                return
            self._until = until
            # Every extension moves the mark, so a deadline that creeps forward in
            # small steps (answers of one window, seconds apart) is announced once.
            if until - self._announced >= _ANNOUNCE_STEP:
                announce = math.ceil(until - now)
            self._announced = max(self._announced, until)
        if announce is not None:
            _log.warning("%s API limit reached, continuing in %d s", self.label, announce)

    def closed_for(self) -> float:
        """Seconds until the window is open again; 0 when it is open."""
        with self._lock:
            return max(0.0, self._until - self._now())

    def wait(self, max_wait: float,
             on_sleep: Optional[Callable[[float], Any]] = None) -> float:
        """Sleep until the window is open; return the seconds slept.

        Raises RateLimitHeld, without sleeping, when the hold left is longer than
        what remains of max_wait. Checks again after every sleep, because another
        lane may have pushed the deadline out meanwhile. ``on_sleep(seconds)`` runs
        before each sleep, so a lane can show and count the wait.
        """
        waited = 0.0
        while True:
            left = self.closed_for()
            if left <= 0:
                return waited
            if left > max(0.0, float(max_wait) - waited):
                raise RateLimitHeld(left, self.label)
            if on_sleep is not None:
                on_sleep(left)
            self._do_sleep(left)
            waited += left


def send_gated(gate: Optional[RateLimitGate], request_fn: Callable[[], Any], *,
               max_wait: float, on_sleep: Optional[Callable[[float], Any]] = None):
    """Run request_fn() behind the gate and feed its answer back to it.

    For the lanes that send by hand (requests or httpx, both answer with
    ``status_code`` and ``headers``). ``gate=None`` calls request_fn unchanged.
    Raises RateLimitHeld when the window stays closed longer than max_wait;
    ``on_sleep`` is passed to ``RateLimitGate.wait``.
    """
    if gate is None:
        return request_fn()
    gate.wait(max_wait, on_sleep)
    resp = request_fn()
    gate.observe(getattr(resp, "status_code", None), getattr(resp, "headers", None))
    return resp


# ═══════════════════════════════════════════════════════════════════════════════
# THE SDK LANE'S CLIENT
# ═══════════════════════════════════════════════════════════════════════════════
# Built on first use: httpx costs about 130 ms to import, and most importers of
# this module (api_backend, for the parsers) never build a gated client.

_GATED_CLIENT_CLASS = None
_GATED_CLIENT_LOCK = threading.Lock()


def _gated_client_class():
    global _GATED_CLIENT_CLASS
    with _GATED_CLIENT_LOCK:
        if _GATED_CLIENT_CLASS is not None:
            return _GATED_CLIENT_CLASS
        import httpx

        class GatedHttpxClient(httpx.Client):
            """httpx client whose send() answers a local 429 while the gate is closed.

            Every request the SDK makes passes send(), stream or not, so the
            headers of a streamed answer are seen before its body is read.
            """

            def __init__(self, gate: RateLimitGate, **kwargs):
                super().__init__(**kwargs)
                self.rate_gate = gate

            def send(self, request, **kwargs):
                left = self.rate_gate.closed_for()
                if left > 0:
                    held = RateLimitHeld(left, self.rate_gate.label)
                    return httpx.Response(
                        429,
                        headers={"retry-after": str(math.ceil(left)), "x-vaf-held": "1"},
                        json={"error": {"type": "rate_limit_exceeded",
                                        "code": "rate_limit_exceeded",
                                        "message": str(held)}},
                        request=request,
                    )
                resp = super().send(request, **kwargs)
                self.rate_gate.observe(resp.status_code, resp.headers)
                return resp

        _GATED_CLIENT_CLASS = GatedHttpxClient
        return GatedHttpxClient


def __getattr__(name: str):
    if name == "GatedHttpxClient":
        return _gated_client_class()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ═══════════════════════════════════════════════════════════════════════════════
# ONE GATE PER KEY
# ═══════════════════════════════════════════════════════════════════════════════

_GATES: Dict[Tuple[str, str], RateLimitGate] = {}
_GATES_LOCK = threading.Lock()


def _key_fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def rate_limit_gate(provider: str, api_key: str) -> Optional[RateLimitGate]:
    """The process-wide gate for this provider and key, or None when it has none.

    None for an empty key and for every provider whose registry row does not set
    ``request_limit_headers``, so local mode and the other API providers never
    touch a gate. Keyed on a fingerprint; the raw key is never stored.
    """
    if not api_key:
        return None
    from vaf.core.provider_registry import get_spec
    spec = get_spec(provider)
    if spec is None or not spec.request_limit_headers:
        return None
    ident = (spec.name, _key_fingerprint(str(api_key)))
    with _GATES_LOCK:
        gate = _GATES.get(ident)
        if gate is None:
            gate = RateLimitGate(spec.label)
            _GATES[ident] = gate
        return gate


def reset_gates() -> None:
    """Forget every gate (tests)."""
    with _GATES_LOCK:
        _GATES.clear()
