# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The per-user browser pool, tested without docker.

The docker CLI sits behind one seam (`vaf.core.containers.docker`) and the config
behind another (`browser_pool._config_get`); these tests pin the DECISIONS:
by default ("auto") the pool sizes itself by free memory and never serves fewer
than two, an integer keeps a fixed count, an explicit 0 still switches the pool
off entirely, admissions cannot overshoot under concurrency, the environment overrides the config key,
capacity and the memory floor refuse politely (shared fallback, never an
error), instances are adopted across VAF restarts, container names carry a
scope HASH rather than the scope, and the manager registry hands out one
manager per instance with tickets routing to the right one.
"""

import json
import types

import pytest

import vaf.core.browser_interactive as bi
import vaf.core.browser_pool as bp
from vaf.core import containers


class _FakeDocker:
    """Scriptable docker CLI: state per container name."""

    def __init__(self):
        self.containers = {}      # name -> state ("running"|"exited")
        self.networks = set()
        self.calls = []
        self.next_ports = {"9222/tcp": "49222", "6901/tcp": "46901"}

    def __call__(self, args, timeout=60):
        self.calls.append(list(args))
        cmd = args[0]
        if cmd == "network":
            if args[1] == "create":
                self.networks.add(args[-1])
                return _done(0, args[-1])
            if args[1] == "inspect":
                name = args[2]
                return _done(0, name) if name in self.networks else _done(1, "", "no such network")
            return _done(1, "", f"unhandled network verb: {args}")
        if cmd == "inspect":
            name = args[1]
            if args[-1].startswith("{{.Config.Image}}"):
                if name in self.containers or name == "vaf-browser":
                    return _done(0, "vaf-vaf-browser\tvaf_vaf-browser-network")
                return _done(1, "", "no such container")
            if name in self.containers:
                return _done(0, self.containers[name])
            if name == "vaf-browser":
                return _done(0, "running")
            return _done(1, "", "no such container")
        if cmd == "ps":
            running = [n for n, s in self.containers.items() if s == "running"]
            return _done(0, "\n".join(running))
        if cmd == "run":
            name = args[args.index("--name") + 1]
            self.containers[name] = "running"
            return _done(0, "containerid")
        if cmd == "start":
            self.containers[args[1]] = "running"
            return _done(0, args[1])
        if cmd == "stop":
            self.containers[args[-1]] = "exited"
            return _done(0, "")
        if cmd == "port":
            port = self.next_ports.get(args[2])
            return _done(0, f"127.0.0.1:{port}") if port else _done(1, "")
        return _done(1, "", f"unhandled: {args}")


def _done(code, out="", err=""):
    return types.SimpleNamespace(returncode=code, stdout=out, stderr=err)


@pytest.fixture
def pool(monkeypatch):
    monkeypatch.delenv("VAF_BROWSER_POOL_MAX", raising=False)
    # The settings an instance is created with: the machine's own values must
    # not decide what the adoption tests compare.
    for var in ("VAF_BROWSER_TZ", "VAF_BROWSER_PROXY", "VAF_BROWSER_GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    # Neither the machine's own config file nor the environment may decide what
    # these tests measure: with the config seam answering None every knob falls
    # to the module default, which is the state a test then varies on purpose.
    monkeypatch.setattr(bp, "_config_get", lambda key: None)
    monkeypatch.delenv("VAF_BROWSER_POOL_STRICT", raising=False)
    fake = _FakeDocker()
    monkeypatch.setattr("vaf.core.containers.docker", fake)
    monkeypatch.setattr("vaf.core.containers.mem_available_mb", lambda: 16000)
    # Fallbacks emit security events (lazy import in resolve): record them
    # here instead of letting a unit test write the machine's real event log.
    events = []
    import vaf.core.security_events as sev
    monkeypatch.setattr(sev, "log_security_event",
                        lambda kind, **kw: events.append((kind, kw)))
    p = bp.BrowserPool()
    p._ensure_reaper = lambda: None            # no threads in unit tests
    monkeypatch.setattr(p, "_wait_healthy", lambda inst: True)
    p._test_docker = fake
    p._test_events = events
    return p


def test_the_suite_itself_can_never_start_a_browser_container():
    """Counter-proof for the conftest's `_browser_pool_off` fixture, deliberately
    written WITHOUT the `pool` fixture so it sees what every other test sees.

    With the default at 2, any test that reaches a browser resolution path on a
    machine with docker creates a real container, network and profile volume that
    nothing cleans up. This assertion is the half that makes that fixture more
    than a claim."""
    assert bp.pool_max() == 0


def test_two_people_get_a_browser_of_their_own_by_default(pool):
    """The default is a partition, not a lease: with nothing configured, two
    scopes resolve to two different containers instead of sharing one."""
    a = pool.resolve("scope-a")
    b = pool.resolve("scope-b")
    assert a is not None and b is not None
    assert a.container_name != b.container_name


def test_an_explicit_zero_still_switches_the_pool_off(pool, monkeypatch):
    """0 has to survive both lanes. The `or <default>` idiom this codebase uses
    everywhere else would read it as "unset" and hand back the default instead,
    which would make the off switch unreachable."""
    monkeypatch.setattr(bp, "_config_get", lambda key: 0 if key == "browser_pool_max" else None)
    assert bp.pool_max() == 0
    assert pool.resolve("scope-a") is None
    assert pool._test_docker.calls == []

    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "0")
    assert bp.pool_max() == 0


def test_the_environment_overrides_the_config_key(pool, monkeypatch):
    """A deployment pins the knob without writing the config file."""
    monkeypatch.setattr(bp, "_config_get", lambda key: 5 if key == "browser_pool_max" else None)
    assert bp.pool_max() == 5
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "1")
    assert bp.pool_max() == 1
    # An empty variable is not an override: it is how a shell unsets one.
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "")
    assert bp.pool_max() == 5


def test_the_config_key_drives_the_memory_floor_and_the_idle_timeout(pool, monkeypatch):
    values = {"browser_pool_min_free_mb": 4000, "browser_pool_idle_seconds": 120}
    monkeypatch.setattr(bp, "_config_get", lambda key: values.get(key))
    assert bp._min_free_mb() == 4000
    assert bp._idle_stop_s() == 120.0
    monkeypatch.setenv("VAF_BROWSER_POOL_MIN_FREE_MB", "1000")
    monkeypatch.setenv("VAF_BROWSER_POOL_IDLE_S", "60")
    assert bp._min_free_mb() == 1000
    assert bp._idle_stop_s() == 60.0


def test_module_defaults_and_config_defaults_cannot_drift():
    """Two copies of the same number: the module default serves an embedder with
    no config file, Config.DEFAULTS serves the app. A silent split would make
    the app and the library disagree about how many browsers may run."""
    from vaf.core.config import Config
    assert Config.DEFAULTS["browser_pool_max"] == bp.DEFAULT_POOL_MAX
    assert Config.DEFAULTS["browser_pool_min_free_mb"] == bp.DEFAULT_MIN_FREE_MB
    assert float(Config.DEFAULTS["browser_pool_idle_seconds"]) == bp.DEFAULT_IDLE_S
    assert Config.DEFAULTS["browser_pool_strict"] == bp.DEFAULT_POOL_STRICT


def test_the_pool_knobs_are_admin_only():
    """Every instance carries a 2 GB memory cap, so the count and the floor are
    the machine's RAM budget: a non-admin LAN account must not be able to raise
    them, and browser_ is not one of the admin-only PREFIXES. Strict decides
    whether users may ever share the fallback browser - the same class."""
    from vaf.core.config import Config
    for key in ("browser_pool_max", "browser_pool_min_free_mb",
                "browser_pool_idle_seconds", "browser_pool_strict"):
        assert Config.is_global_config_key(key), key


def test_instance_is_created_with_hashed_name_and_loopback_ports(pool, monkeypatch):
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "2")
    inst = pool.resolve("scope-a")
    assert inst is not None
    assert inst.container_name.startswith("vaf-browser-u-")
    assert "scope-a" not in inst.container_name        # scope never in container listings
    assert inst.cdp_base == "http://127.0.0.1:49222"
    assert inst.vnc_base == "http://127.0.0.1:46901"
    run_call = next(c for c in pool._test_docker.calls if c[0] == "run")
    assert "127.0.0.1::9222" in run_call and "127.0.0.1::6901" in run_call
    # per-user profile volume, also hashed
    assert any(a.startswith("vaf-browser-profile-") and ":/home/browser" in a for a in run_call)
    # the filtering resolvers compose gives the shared container are passed here too
    assert "1.1.1.2" in run_call and "1.0.0.2" in run_call
    # and the host-gateway name render_check aims localhost targets at
    assert "host.docker.internal:host-gateway" in run_call


def test_instance_carries_the_same_hardening_as_the_compose_browser(pool, monkeypatch):
    """The pool's docker run is the second copy of the compose service's start
    arguments: cap_drop ALL + SYS_CHROOT + no-new-privileges + the seccomp
    profile that lets Chromium run WITH its own sandbox. A lane that loses them
    silently runs an unsandboxed browser."""
    import vaf.core.browser_pool as bp
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "2")
    monkeypatch.setattr(bp, "_seccomp_profile_path", lambda: "/repo/docker/browser/chromium-seccomp.json")
    assert pool.resolve("scope-a") is not None
    run_call = next(c for c in pool._test_docker.calls if c[0] == "run")
    joined = " ".join(run_call)
    assert "--cap-drop ALL" in joined
    assert "--cap-add SYS_CHROOT" in joined
    assert "--security-opt no-new-privileges:true" in joined
    assert "--security-opt seccomp=/repo/docker/browser/chromium-seccomp.json" in joined
    # options belong to docker run, so they must precede the image argument
    assert run_call.index("no-new-privileges:true") < len(run_call) - 1
    seccomp_idx = next(i for i, a in enumerate(run_call) if a.startswith("seccomp="))
    assert seccomp_idx < len(run_call) - 1, "seccomp option must come before the image"


def test_a_missing_seccomp_profile_omits_the_option_but_still_hardens(pool, monkeypatch):
    """A wheel install ships no docker/ directory: the option is omitted (the
    entrypoint probe falls back loudly inside the container), while the
    capability and privilege hardening still applies."""
    import vaf.core.browser_pool as bp
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "2")
    monkeypatch.setattr(bp, "_seccomp_profile_path", lambda: None)
    assert pool.resolve("scope-a") is not None
    run_call = next(c for c in pool._test_docker.calls if c[0] == "run")
    joined = " ".join(run_call)
    assert "seccomp=" not in joined
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges:true" in joined


def test_each_instance_gets_a_network_of_its_own(pool, monkeypatch):
    """Inside the container CDP and KasmVNC listen on 0.0.0.0 with no auth -
    safe only because nothing can reach them. On one shared bridge network a
    page in A's browser could dial B's container IP and drive it, which is the
    isolation the pool is sold as providing."""
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "2")
    a = pool.resolve("scope-a")
    b = pool.resolve("scope-b")
    nets = [c[c.index("--network") + 1] for c in pool._test_docker.calls if c[0] == "run"]
    assert len(nets) == 2 and nets[0] != nets[1]
    assert all(n.startswith("vaf-browser-net-") for n in nets)
    # never the shared browser's own network
    assert "vaf_vaf-browser-network" not in nets
    assert a.container_name != b.container_name


def test_a_browser_auto_adds_is_partitioned_like_the_first_two(pool, monkeypatch):
    """The instances auto starts beyond the former two get exactly what the first two
    get: a scope-hash name, a network and a profile volume of their own, the full
    hardening and the memory cap. And the idle reaper still only stops one, never
    removes it."""
    monkeypatch.setattr(bp, "_seccomp_profile_path", lambda: "/repo/docker/browser/chromium-seccomp.json")
    got = _resolve_scopes(pool, 3)
    assert all(i is not None for i in got)
    runs = [c for c in pool._test_docker.calls if c[0] == "run"]
    assert len(runs) == 3
    nets = [c[c.index("--network") + 1] for c in runs]
    vols = [next(a for a in c if a.endswith(":/home/browser")) for c in runs]
    assert len(set(nets)) == 3 and all(n.startswith("vaf-browser-net-") for n in nets)
    assert len(set(vols)) == 3 and all(v.startswith("vaf-browser-profile-") for v in vols)
    for i, (inst, run) in enumerate(zip(got, runs)):
        assert inst.container_name == "vaf-browser-u-" + containers.scope_hash(f"scope-{i}")
        joined = " ".join(run)
        for flag in ("--cap-drop ALL", "--cap-add SYS_CHROOT",
                     "--security-opt no-new-privileges:true",
                     "--security-opt seccomp=/repo/docker/browser/chromium-seccomp.json",
                     "--dns 1.1.1.2", "--dns 1.0.0.2", "127.0.0.1::9222", "127.0.0.1::6901",
                     f"--memory {bp.INSTANCE_MEMORY_MB}m"):
            assert flag in joined, (i, flag)

    # The reaper: every instance idle for longer than the timeout is stopped, nothing more.
    for inst in got:
        inst.last_used = 0.0
    monkeypatch.setattr(bp.time, "sleep", lambda s: None)
    # Nobody holds a lease or runs an agent on any of them.
    monkeypatch.setattr(bp.BrowserPool, "_instance_busy", staticmethod(lambda inst: False))
    before = len(pool._test_docker.calls)
    pool._reaper_loop()
    verbs = [c[0] for c in pool._test_docker.calls[before:]]
    assert verbs == ["stop", "stop", "stop"], verbs



def test_capacity_gate_answers_shared_fallback(pool, monkeypatch):
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "1")
    assert pool.resolve("scope-a") is not None
    assert pool.resolve("scope-b") is None             # full: shared browser, not an error
    # the first scope keeps its instance through the gate
    assert pool.resolve("scope-a") is not None


def test_memory_floor_refuses_new_instances(pool, monkeypatch):
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "4")
    monkeypatch.setattr("vaf.core.containers.mem_available_mb", lambda: 800)
    assert pool.resolve("scope-a") is None


# -- auto: the pool sizes itself by free memory -------------------------------------

def _resolve_scopes(pool, n):
    return [pool.resolve(f"scope-{i}") for i in range(n)]


def _fallback_details(pool):
    return [kw["detail"] for kind, kw in pool._test_events if kind == "browser_pool_fallback"]


def test_auto_is_the_default_and_the_env_spellings(pool, monkeypatch):
    """Nothing configured means auto, in the module and in Config.DEFAULTS alike, and
    the operator's variable reads every spelling an admin will type. An integer stays
    a fixed count and 0 stays off. MUTATION: DEFAULT_POOL_MAX = 2 - red; parse "auto"
    as a fixed count - red."""
    from vaf.core.config import Config
    assert bp.pool_mode() == "auto" and bp.pool_max() == bp.AUTO_CEILING
    assert Config.DEFAULTS["browser_pool_max"] == "auto" == bp.DEFAULT_POOL_MAX
    for raw, mode, ceiling in (("0", "off", 0), ("4", "fixed", 4), ("auto", "auto", 16),
                               (" AUTO ", "auto", 16), ("x", "auto", 16), ("-3", "off", 0)):
        monkeypatch.setenv("VAF_BROWSER_POOL_MAX", raw)
        assert (bp.pool_mode(), bp.pool_max()) == (mode, ceiling), raw
    monkeypatch.delenv("VAF_BROWSER_POOL_MAX")
    for value, mode, ceiling in ((0, "off", 0), (3, "fixed", 3), ("auto", "auto", 16),
                                 ("5", "fixed", 5), (None, "auto", 16)):
        monkeypatch.setattr(bp, "_config_get",
                            lambda key, v=value: v if key == "browser_pool_max" else None)
        assert (bp.pool_mode(), bp.pool_max()) == (mode, ceiling), value


def test_auto_reserves_the_cap_of_every_running_browser(pool, monkeypatch):
    """Room for exactly five caps above the floor starts five, and the sixth person
    shares the fallback browser with a visible event that names memory as the limit.
    The fake memory stays constant while browsers start, so only the reservation for
    the RUNNING ones can stop the sixth. MUTATION: drop `running` from the
    reservation term - red (it would start sixteen)."""
    monkeypatch.setattr("vaf.core.containers.mem_available_mb",
                        lambda: bp.DEFAULT_MIN_FREE_MB + 5 * bp.INSTANCE_MEMORY_MB)
    got = _resolve_scopes(pool, 6)
    assert all(i is not None for i in got[:5]), got
    assert got[5] is None
    assert len({i.container_name for i in got[:5]}) == 5
    details = _fallback_details(pool)
    assert len(details) == 1 and details[0].startswith("low memory"), details
    assert f"{bp.INSTANCE_MEMORY_MB} MB reserved per browser" in details[0]


@pytest.mark.parametrize("free", [2600, None])
def test_auto_never_serves_fewer_than_the_former_default(pool, monkeypatch, free):
    """Just above the floor the reservation admits nobody, and auto still serves the two
    the fixed default served. Without a memory reading (macOS, Windows) auto is exactly
    those two. MUTATION: remove the baseline branch - red (none start at 2600 MB);
    treat a missing reading as unlimited memory - red (the third starts)."""
    monkeypatch.setattr("vaf.core.containers.mem_available_mb", lambda: free)
    got = _resolve_scopes(pool, 3)
    assert got[0] is not None and got[1] is not None
    assert got[2] is None


def test_auto_stops_at_the_ceiling(pool, monkeypatch):
    """Even with memory for a hundred, auto runs at most sixteen at once.
    MUTATION: remove the AUTO_CEILING clamp - red."""
    monkeypatch.setattr("vaf.core.containers.mem_available_mb", lambda: 10_000_000)
    got = _resolve_scopes(pool, 17)
    assert all(i is not None for i in got[:16])
    assert got[16] is None
    assert _fallback_details(pool) == ["at capacity (16/16)"]


def test_fixed_mode_keeps_the_floor_only_rule(pool, monkeypatch):
    """A fixed count means what it always meant: at most N while free memory is above
    the floor, no reservation. It is the documented way to pack more browsers onto a
    machine than auto would. MUTATION: apply the reservation in fixed mode - red."""
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "4")
    monkeypatch.setattr("vaf.core.containers.mem_available_mb", lambda: 2600)
    got = _resolve_scopes(pool, 5)
    assert all(i is not None for i in got[:4])
    assert got[4] is None
    assert _fallback_details(pool) == ["at capacity (4/4)"]


def test_concurrent_admissions_do_not_overshoot(pool, monkeypatch):
    """Two people open a browser at the same moment with room for one. The admission
    check and the pending count sit under one lock, and the admitted start counts
    until its `docker run` has returned (docker lists the container only from then
    on). The fakes make both races real: the first memory reading (inside the check,
    after the count) waits briefly for a second one, so without the lock both checks
    see the same count; and the first `docker run` blocks until the other resolve has
    decided, so without the pending count the second check sees nothing running.
    MUTATION: drop the `_pending` increment - red; take the check out of
    `_admit_lock` - red."""
    import threading
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "1")
    fake = pool._test_docker
    guard = threading.Lock()
    seen = {"mem": 0, "run": 0}
    second_reading = threading.Event()
    release = threading.Event()

    def nth(kind):
        with guard:
            seen[kind] = seen.get(kind, 0) + 1
            return seen[kind]

    def reading():
        if nth("mem") == 1:
            second_reading.wait(0.5)
        else:
            second_reading.set()
        return 16000

    def racing(args, timeout=60):
        if args[0] == "run" and nth("run") == 1:
            release.wait(3)
        return fake(args, timeout=timeout)

    monkeypatch.setattr("vaf.core.containers.mem_available_mb", reading)
    monkeypatch.setattr("vaf.core.containers.docker", racing)
    results = {}
    done = threading.Event()

    def resolve(scope):
        results[scope] = pool.resolve(scope)
        done.set()

    threads = [threading.Thread(target=resolve, args=(s,), daemon=True)
               for s in ("scope-a", "scope-b")]
    for t in threads:
        t.start()
    done.wait(3)          # the refused one answers while the admitted one is in docker run
    release.set()
    for t in threads:
        t.join(10)
    assert len([c for c in fake.calls if c[0] == "run"]) == 1, fake.calls
    assert sorted(v is None for v in results.values()) == [False, True]
    assert pool._pending == 0


def test_status_room_is_the_admission_decision(pool, monkeypatch):
    """The status row and the admission read one rule: for the same inputs the room
    the row reports is `admission_room`, and the next admission says yes exactly when
    that room is at least one. A start still under way counts. With the pool off the
    status asks docker nothing. MUTATION: compute `room` in pool_status with its own
    formula, off by one - red."""
    monkeypatch.setattr(bp, "_pool", pool)
    free = {"mb": 0}
    monkeypatch.setattr("vaf.core.containers.mem_available_mb", lambda: free["mb"])
    for mb in (None, 1000, 2600, 2500 + 2 * 2048, 2500 + 3 * 2048 - 1, 2500 + 9 * 2048):
        free["mb"] = mb
        for pending in (0, 1):
            pool._pending = pending
            status = bp.pool_status()
            running = status["running"]
            want = bp.admission_room("auto", bp.AUTO_CEILING, running + pending, mb,
                                     bp.DEFAULT_MIN_FREE_MB)
            assert status["room"] == want, (mb, pending, status)
            assert status["mode"] == "auto" and status["free_mb"] == mb
            assert status["reserve_mb"] == bp.INSTANCE_MEMORY_MB
            admitted = pool._admit()
            if admitted:
                pool._release()
            assert admitted == (status["room"] >= 1), (mb, pending, status)
    pool._pending = 0
    # One instance running: it is counted, and it lowers the room.
    free["mb"] = 2500 + 3 * 2048
    assert pool.resolve("scope-a") is not None
    status = bp.pool_status()
    assert status["running"] == 1 and status["room"] == 2
    assert "scope-a" not in json.dumps(status)

    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "0")
    pool._test_docker.calls.clear()
    off = bp.pool_status()
    assert off["mode"] == "off" and off["room"] == 0 and off["running"] == 0
    assert pool._test_docker.calls == []


def test_admission_room_rules():
    """The pure rule, table by table: fixed is floor-only, auto is the larger of the
    baseline and the reservation, clamped to the ceiling, and off admits nothing."""
    r = bp.admission_room
    assert r("off", 0, 0, 99999, 2500) == 0
    assert r("fixed", 4, 1, 2600, 2500) == 3
    assert r("fixed", 4, 1, 2400, 2500) == 0
    assert r("fixed", 4, 1, None, 2500) == 3
    assert r("auto", 16, 0, 2600, 2500) == 2              # the baseline
    assert r("auto", 16, 0, 2400, 2500) == 0              # below the floor: nothing
    assert r("auto", 16, 0, None, 2500) == 2              # no reading: the baseline
    assert r("auto", 16, 2, None, 2500) == 0
    assert r("auto", 16, 1, 2500 + 4 * 2048, 2500) == 3   # four caps, one taken
    assert r("auto", 16, 0, 10_000_000, 2500) == 16       # the ceiling
    assert r("auto", 1, 0, 2600, 2500) == 1               # a ceiling below the baseline


def test_refusal_hint_names_the_real_limit(pool, monkeypatch):
    """A strict refusal tells the person what would help. When memory refused, a
    higher count does nothing, so that hint must never say "raise browser_pool_max".
    Checked against the reasons the pool actually produces, and the agent's refusal
    is built from the hint. MUTATION: return the capacity hint for every reason - red."""
    from pathlib import Path
    monkeypatch.setenv("VAF_BROWSER_POOL_STRICT", "1")
    monkeypatch.setattr("vaf.core.containers.mem_available_mb", lambda: 2000)
    with pytest.raises(bp.PoolExhausted) as memory:
        pool.resolve("scope-a")
    assert str(memory.value).startswith("low memory")
    hint = bp.refusal_hint(str(memory.value))
    assert "raise browser_pool_max" not in hint and "browser_pool_idle_seconds" in hint

    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "1")
    monkeypatch.setattr("vaf.core.containers.mem_available_mb", lambda: 16000)
    assert pool.resolve("scope-a") is not None
    with pytest.raises(bp.PoolExhausted) as capacity:
        pool.resolve("scope-b")
    assert str(capacity.value).startswith("at capacity")
    assert "raise browser_pool_max" in bp.refusal_hint(str(capacity.value))
    assert "raise browser_pool_max" not in bp.refusal_hint("instance did not become healthy")

    # Auto without a memory reading (macOS, Windows) stops at two and is auto already:
    # its hint asks for a number. MUTATION: drop that branch - red.
    monkeypatch.delenv("VAF_BROWSER_POOL_MAX")
    monkeypatch.setattr("vaf.core.containers.mem_available_mb", lambda: None)
    assert pool.resolve("scope-c") is not None
    with pytest.raises(bp.PoolExhausted) as blind:
        pool.resolve("scope-d")
    assert "cannot be read" in str(blind.value)
    blind_hint = bp.refusal_hint(str(blind.value))
    assert "set browser_pool_max to a number" in blind_hint
    assert "set it to auto" not in blind_hint

    src = (Path(bp.__file__).resolve().parents[1] / "tools" / "browser_agent.py"
           ).read_text(encoding="utf-8")
    assert "refusal_hint(str(e))" in src
    assert "raise browser_pool_max" not in src


def test_an_unreadable_count_refuses_instead_of_counting_zero(pool, monkeypatch):
    """`docker ps` failing used to read as nobody running, so the admission saw only
    the starts still under way and admitted past the ceiling and past the reservation
    (sixteen running plus a failing `ps` admitted more). Without the count neither can
    be checked: the pool refuses, visibly, and the status row says no room and does
    not claim a number. MUTATION: read an unreadable count as 0 in `_admit` - red; in
    `pool_status` - red."""
    monkeypatch.setattr(bp, "_pool", pool)
    monkeypatch.setattr("vaf.core.containers.mem_available_mb", lambda: 10_000_000)
    assert all(i is not None for i in _resolve_scopes(pool, 16))
    fake = pool._test_docker

    def ps_fails(args, timeout=60):
        if args[0] == "ps":
            fake.calls.append(list(args))
            return _done(1, "", "Cannot connect to the Docker daemon")
        return fake(args, timeout=timeout)

    monkeypatch.setattr("vaf.core.containers.docker", ps_fails)
    runs = len([c for c in fake.calls if c[0] == "run"])
    assert pool.resolve("scope-new") is None
    assert len([c for c in fake.calls if c[0] == "run"]) == runs, "admitted without a count"
    assert _fallback_details(pool) == [bp._UNCOUNTED]
    assert bp.refusal_hint(bp._UNCOUNTED) == "Try again in a moment."
    status = bp.pool_status()
    assert status["running"] is None and status["room"] == 0
    from vaf.core import service_health as sh
    row = sh.derive_browser_pool_status(status)
    assert row["state"] == "unknown" and row["room"] == 0


def test_memory_cap_is_the_reservation(pool, monkeypatch):
    """The reservation is only honest while it IS the container's memory cap: one
    constant feeds both. MUTATION: write a literal for `--memory` that differs from
    INSTANCE_MEMORY_MB - red."""
    assert pool.resolve("scope-a") is not None
    run_call = next(c for c in pool._test_docker.calls if c[0] == "run")
    assert run_call[run_call.index("--memory") + 1] == f"{bp.INSTANCE_MEMORY_MB}m"
    assert bp.admission_room("auto", 16, 0, 2500 + 3 * bp.INSTANCE_MEMORY_MB, 2500) == 3


def test_exited_instance_is_adopted_and_restarted(pool, monkeypatch):
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "2")
    name = "vaf-browser-u-" + containers.scope_hash("scope-a")
    pool._test_docker.containers[name] = "exited"      # left over from an earlier VAF process
    inst = pool.resolve("scope-a")
    assert inst is not None and inst.container_name == name
    assert ["start", name] in pool._test_docker.calls
    assert not any(c[0] == "run" for c in pool._test_docker.calls)


def test_peek_never_calls_docker(pool, monkeypatch):
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "2")
    assert pool.peek("scope-a") is None
    assert pool._test_docker.calls == []
    pool.resolve("scope-a")
    n = len(pool._test_docker.calls)
    assert pool.peek("scope-a") is not None
    assert len(pool._test_docker.calls) == n


def test_docker_failure_falls_back_to_the_shared_browser(pool, monkeypatch):
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "2")
    monkeypatch.setattr("vaf.core.containers.docker", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no docker")))
    assert pool.resolve("scope-a") is None


# ── manager registry (vaf/core/browser_interactive.py) ─────────────────────

@pytest.fixture
def registry(monkeypatch, tmp_path):
    """A clean manager registry with a stubbed pool and stubbed network seams."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("VAF_USER_SCOPE_ID", raising=False)
    monkeypatch.delenv("VAF_IN_SUBAGENT_TERMINAL", raising=False)
    monkeypatch.delenv("VAF_BROWSER_SCRUB", raising=False)
    monkeypatch.setattr(bi, "resolve_cdp_ws_url", lambda base: "ws://stub")
    monkeypatch.setattr(bi, "park_browser_idle", lambda base: None)
    monkeypatch.setattr(bi, "_cookie_op", lambda *a, **k: None)
    monkeypatch.setattr(bi, "_manager", None)
    monkeypatch.setattr(bi, "_pool_managers", {})

    class _StubPool:
        def __init__(self):
            self.instances = {}
        def resolve(self, scope):
            return self.instances.get(scope)
        def peek(self, scope):
            return self.instances.get(scope)

    stub = _StubPool()
    import vaf.core.browser_pool as bp_mod
    monkeypatch.setattr(bp_mod, "get_browser_pool", lambda: stub)
    return stub


def _inst(scope, name, cdp="http://127.0.0.1:41111", vnc="http://127.0.0.1:42222"):
    import time
    return bp.BrowserInstance(user_scope_id=scope, container_name=name,
                              cdp_base=cdp, vnc_base=vnc, last_used=time.time())


def _quiet(mgr):
    mgr._emit = lambda payload, session_id: None
    mgr._ensure_janitor = lambda: None
    return mgr


def test_scopes_with_instances_get_their_own_manager(registry, monkeypatch):
    registry.instances["scope-a"] = _inst("scope-a", "vaf-browser-u-aaa")
    m_a = bi.get_manager_for_scope("scope-a")
    m_b = bi.get_manager_for_scope("scope-b")          # no instance: shared
    assert m_a is not m_b
    assert m_a.container_name() == "vaf-browser-u-aaa"
    assert m_a.cdp_base() == "http://127.0.0.1:41111"
    assert m_b is bi.get_interactive_manager()
    # stable: the same scope gets the same manager again
    assert bi.get_manager_for_scope("scope-a") is m_a


def test_parallel_leases_on_different_instances(registry, monkeypatch):
    """The pool's whole point: two users drive two browsers at the same time."""
    registry.instances["scope-a"] = _inst("scope-a", "vaf-browser-u-aaa")
    registry.instances["scope-b"] = _inst("scope-b", "vaf-browser-u-bbb",
                                          cdp="http://127.0.0.1:43333", vnc="http://127.0.0.1:44444")
    m_a = _quiet(bi.get_manager_for_scope("scope-a"))
    m_b = _quiet(bi.get_manager_for_scope("scope-b"))
    import vaf.core.subagent_ipc as ipc_mod

    class _FakeIPC:
        def get_active_tasks(self, session_id=None):
            return []
        def get_pending_tasks(self, session_id=None):
            return []

    monkeypatch.setattr(ipc_mod, "get_ipc", lambda: _FakeIPC())
    r_a = m_a.start("scope-a", "sess-a")
    r_b = m_b.start("scope-b", "sess-b")
    assert r_a["status"] == "active" and r_b["status"] == "active"
    # Tickets route to the manager (and so the endpoint) that issued them.
    t_a = r_a["streamPath"].split("/t/")[1].split("/")[0]
    t_b = r_b["streamPath"].split("/t/")[1].split("/")[0]
    assert bi.get_manager_by_ticket(t_a) is m_a
    assert bi.get_manager_by_ticket(t_b) is m_b
    assert bi.get_manager_by_ticket("forged") is None
    # Session lookup finds the right browser for the chat-context snapshot.
    assert bi.manager_for_session("sess-a") is m_a
    assert bi.manager_for_session("sess-b") is m_b


def test_stop_resolution_never_starts_a_container(registry, monkeypatch):
    """A stop must land on the browser that holds the lease, and must never
    resolve (= possibly start) an instance to do so."""
    calls = []
    registry.resolve = lambda scope: calls.append(scope) or None
    registry.instances["scope-a"] = _inst("scope-a", "vaf-browser-u-aaa")
    m_a = _quiet(bi._manager_for_instance(registry.instances["scope-a"]))
    import vaf.core.subagent_ipc as ipc_mod

    class _FakeIPC:
        def get_active_tasks(self, session_id=None):
            return []
        def get_pending_tasks(self, session_id=None):
            return []

    monkeypatch.setattr(ipc_mod, "get_ipc", lambda: _FakeIPC())
    m_a.start("scope-a", "sess-a")
    assert bi.get_manager_for_stop("scope-a", "sess-a") is m_a
    assert bi.get_manager_for_stop("scope-a", None) is m_a
    assert calls == []
    # nobody holds anything for this scope: the shared manager answers (a no-op stop)
    assert bi.get_manager_for_stop("scope-x", None) is bi.get_interactive_manager()


def test_run_hooks_route_by_the_container_the_run_pinned(registry, monkeypatch):
    """Addressed by CONTAINER, never re-resolved by scope: the pool's
    scope-to-instance map is mutable, so a scope lookup can answer differently
    at the end of a run than at its start - and clearing _agent_active on the
    wrong manager leaves the real one flagged for the life of the process."""
    registry.instances["scope-a"] = _inst("scope-a", "vaf-browser-u-aaa")
    m_a = _quiet(bi._manager_for_instance(registry.instances["scope-a"]))
    bi.stop_for_agent_run(container_name="vaf-browser-u-aaa")
    assert m_a._agent_active is True
    assert bi.get_interactive_manager()._agent_active is False
    bi.agent_stream_started("sess-a", container_name="vaf-browser-u-aaa")
    assert m_a._agent_stream is not None
    # The pool forgets the instance mid-run (idle stop, capacity change): the
    # end hook must still land on the manager that was flagged.
    registry.instances.pop("scope-a")
    bi.agent_run_ended(container_name="vaf-browser-u-aaa")
    assert m_a._agent_active is False and m_a._agent_stream is None


def test_a_dedicated_instance_is_never_scrubbed_as_a_stranger(registry, monkeypatch):
    """A pooled manager starts life knowing whose browser it is. Without that
    its own user reads as an unknown jar on the first use after every process
    start - and in full mode the profile wipe would delete the history,
    passwords and downloads the per-user volume exists to keep."""
    monkeypatch.setenv("VAF_BROWSER_SCRUB", "full")
    wipes, ops = [], []
    monkeypatch.setattr(bi, "request_profile_wipe", lambda *a: wipes.append(a))
    monkeypatch.setattr(bi, "_cookie_op", lambda base, op, cookies=None: ops.append(op))
    registry.instances["scope-a"] = _inst("scope-a", "vaf-browser-u-aaa")
    mgr = _quiet(bi._manager_for_instance(registry.instances["scope-a"]))
    assert mgr._dedicated_scope == "scope-a"
    # Half one of the fix: a fresh manager already knows whose browser this is,
    # so its own user cannot read as an unknown jar on the first use.
    assert mgr._last_cookie_scope == "scope-a"
    mgr.hand_jar_to_run(user_scope_id="scope-a", persistent=True)
    assert wipes == [] and ops == []
    # The clean-start promise of a non-persistent run still holds, without a wipe.
    mgr.hand_jar_to_run(user_scope_id="scope-a", persistent=False)
    assert wipes == [] and ops == ["scrub"]
    # Half two: the guard itself. Even asked for a scope that is not this
    # browser's, a dedicated instance is never profile-wiped - the volume holds
    # one person's history, passwords and downloads, and nobody else's.
    ops.clear()
    mgr.hand_jar_to_run(user_scope_id="scope-somebody-else", persistent=True)
    assert wipes == []


def test_the_jar_handover_sits_after_the_gate_and_after_the_watchdog():
    """Two orderings this lane got wrong in one round, both static and both
    cheap to pin: a handover BEFORE the concurrency gate scrubs a browser
    another run is still driving, and a handover before the stop watchdog is
    armed swallows a Stop for as long as an unresponsive browser makes it wait
    (up to the 30s CDP deadline)."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "vaf" / "tools" / "browser_agent.py").read_text(encoding="utf-8")
    run_body = src.split("    def run(self, **kwargs) -> str:", 1)[1]
    run_body = run_body.split("    async def _run_browser", 1)[0]
    assert "hand_jar_to_run" not in run_body, \
        "the handover must not run before the concurrency gate"
    inner = src.split("    async def _run_browser", 1)[1]
    assert "hand_jar_to_run" in inner
    assert inner.index("browser-stop-watchdog") < inner.index("hand_jar_to_run"), \
        "the stop watchdog must be armed before the handover can block"
    # and it must not block the event loop while it waits
    handover = inner[inner.index("hand_jar_to_run") - 400:inner.index("hand_jar_to_run") + 200]
    assert "run_in_executor" in handover


def test_the_watch_grant_is_minted_after_the_handover():
    """The run's watch-only stream is granted once the run holds the browser: before
    its handover the browser still shows the previous holder's state, or a page
    check of another process the handover waits out (measured: the run's watch
    socket streamed that page check's page of another user). The static half of the
    behavioural test in test_browser_interactive.py, which needs browser_use and is
    skipped without it. MUTATION: call `agent_stream_started` in `run()` again (red)."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "vaf" / "tools" / "browser_agent.py").read_text(encoding="utf-8")
    run_body = src.split("    def run(self, **kwargs) -> str:", 1)[1]
    run_body = run_body.split("    async def _run_browser", 1)[0]
    assert "agent_stream_started" not in run_body, \
        "the watch grant is minted before the run's handover"
    inner = src.split("    async def _run_browser", 1)[1]
    grant = inner.index("agent_stream_started(")
    assert inner.index("run_generation=_run_gen") < grant, \
        "the watch grant must follow the handover"
    assert grant < inner.index("sync_workspace_for_run("), \
        "the watch grant must exist by the browse phase"


def test_watch_only_is_enforced_by_the_relay_not_by_the_viewer():
    """A run's grant carries view_only in the client's URL and the window puts
    pointer-events off on top of it, but both live in the PAGE: anything that
    speaks the stream socket could still send RFB pointer and key events and
    drive the browser the agent is working in. The relay must therefore drop
    client-to-container traffic on an agent grant. Static guard, because the
    check lives in a closure inside the websocket route."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    route = src.split('@app.websocket("/api/browser-vnc/t/{ticket}/websockify")', 1)[1]
    route = route.split("\n@app.", 1)[0]
    assert "watch_only = isinstance(" in route and "AgentStream" in route
    upstream = route.split("async def _to_upstream", 1)[1].split("async def _to_client", 1)[0]
    assert "if watch_only:" in upstream
    # and it must not fall through to a send on that branch
    assert upstream.index("if watch_only:") < upstream.index("upstream.send")


def test_reaper_asks_the_registry_about_activity(registry):
    registry.instances["scope-a"] = _inst("scope-a", "vaf-browser-u-aaa")
    mgr = _quiet(bi._manager_for_instance(registry.instances["scope-a"]))
    assert bi.peek_manager_for_container("vaf-browser-u-aaa") is mgr
    assert mgr.has_activity() is False
    mgr._agent_runs = 1
    assert mgr.has_activity() is True
    assert bi.peek_manager_for_container("vaf-browser") is bi.get_interactive_manager()


@pytest.fixture
def wired_pool(pool, monkeypatch, tmp_path):
    """The real pool on the fake docker, wired to the real manager registry.

    The CDP probe answers only on the ports the fake docker currently publishes,
    which is what a real container does after a start: the old ports lead
    nowhere."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("VAF_USER_SCOPE_ID", raising=False)
    monkeypatch.delenv("VAF_IN_SUBAGENT_TERMINAL", raising=False)
    monkeypatch.delenv("VAF_BROWSER_SCRUB", raising=False)
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "2")
    fake = pool._test_docker

    def cdp_probe(base):
        if base.rsplit(":", 1)[1] != fake.next_ports["9222/tcp"]:
            raise ConnectionRefusedError(base)
        return "ws://stub"

    monkeypatch.setattr(bi, "resolve_cdp_ws_url", cdp_probe)
    monkeypatch.setattr(bi, "park_browser_idle", lambda base: None)
    monkeypatch.setattr(bi, "_cookie_op", lambda *a, **k: None)
    monkeypatch.setattr(bi, "_manager", None)
    monkeypatch.setattr(bi, "_pool_managers", {})
    monkeypatch.setattr(bp, "get_browser_pool", lambda: pool)
    import vaf.core.subagent_ipc as ipc_mod

    class _FakeIPC:
        def get_active_tasks(self, session_id=None):
            return []
        def get_pending_tasks(self, session_id=None):
            return []

    monkeypatch.setattr(ipc_mod, "get_ipc", lambda: _FakeIPC())
    return pool


def _reap_and_republish(pool, monkeypatch):
    """The idle reaper stops every instance; the next `docker start` publishes
    the container on new host ports, as Docker does for `127.0.0.1::<port>`."""
    for inst in list(pool._instances.values()):
        inst.last_used = 0.0
    monkeypatch.setattr(bp.time, "sleep", lambda s: None)
    pool._reaper_loop()
    assert all(s == "exited" for s in pool._test_docker.containers.values())
    pool._test_docker.next_ports = {"9222/tcp": "49333", "6901/tcp": "46902"}


def test_a_reaped_instance_reopens_on_its_new_ports_without_a_restart(wired_pool, monkeypatch):
    """The person's browser opens again after the idle reaper stopped it, and the
    stream proxy dials the container's NEW stream port. The manager is cached per
    container name and used to keep the ports of its first start, so every reopen
    answered browser_unavailable until VAF restarted. The manager itself (and with
    it the dedicated scope) stays the same object. MUTATION: drop the endpoint
    refresh in `_manager_for_instance` (red)."""
    mgr = _quiet(bi.get_manager_for_scope("scope-a"))
    assert mgr.start("scope-a", "sess-a")["status"] == "active"
    mgr.stop("user")
    _reap_and_republish(wired_pool, monkeypatch)

    again = bi.get_manager_for_scope("scope-a")
    assert again is mgr
    r = mgr.start("scope-a", "sess-a")
    assert r["status"] == "active", r          # was: error, browser_unavailable
    assert mgr.cdp_base() == "http://127.0.0.1:49333"
    ticket = r["streamPath"].split("/t/")[1].split("/")[0]
    # The VNC proxy builds its target from the ticket's manager at connect time.
    assert bi.get_manager_by_ticket(ticket).vnc_base() == "http://127.0.0.1:46902"
    assert mgr._dedicated_scope == "scope-a"


def test_the_agent_and_render_lanes_follow_the_new_ports_too(wired_pool, monkeypatch):
    """A browser_agent run and a render_check on a reaped-and-restarted instance
    reach its new ports through the manager as well: the run's hooks (jar
    handover, watch stream) address the manager by container name, and the page
    check probes through it. MUTATION: drop the endpoint refresh in
    `_manager_for_instance` (red)."""
    import vaf.core.browser_render as br
    mgr = _quiet(bi.get_manager_for_scope("scope-a"))
    _reap_and_republish(wired_pool, monkeypatch)

    # The agent lane: resolve, then register, exactly as BrowserAgentTool.run does.
    inst = wired_pool.resolve("scope-a")
    assert inst.cdp_base == "http://127.0.0.1:49333"
    bi.register_run_browser(inst)
    run_mgr = bi._manager_for_run(inst.container_name)
    assert run_mgr is mgr
    assert (run_mgr.cdp_base(), run_mgr.vnc_base()) == (
        "http://127.0.0.1:49333", "http://127.0.0.1:46902")

    # The render lane after one more stop and start.
    _reap_and_republish(wired_pool, monkeypatch)
    wired_pool._test_docker.next_ports = {"9222/tcp": "49444", "6901/tcp": "46903"}
    cdp, render_mgr = br._endpoint_for("scope-a")
    assert render_mgr is mgr
    assert cdp == render_mgr.cdp_base() == "http://127.0.0.1:49444"


def test_a_cached_instance_restarted_behind_the_pool_is_re_read(pool, monkeypatch):
    """Another VAF process (or `docker restart`) can restart a running instance this
    pool has cached, and Docker publishes it on new ports. The cached ports are
    checked against the container on every resolve: unchanged, the cached instance
    is handed out as it is; changed, the instance is re-read and re-probed before
    it is handed out. MUTATION: return the cached instance without the check (red)."""
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "2")
    first = pool.resolve("scope-a")
    assert pool.resolve("scope-a") is first
    probed = []
    monkeypatch.setattr(pool, "_wait_healthy", lambda inst: probed.append(inst.cdp_base) or True)
    pool._test_docker.next_ports = {"9222/tcp": "49555", "6901/tcp": "46904"}
    n = len(pool._test_docker.calls)
    moved = pool.resolve("scope-a")
    assert (moved.cdp_base, moved.vnc_base) == ("http://127.0.0.1:49555", "http://127.0.0.1:46904")
    assert probed == ["http://127.0.0.1:49555"]
    assert pool.peek("scope-a") is moved
    # Adopted as it runs: nothing is started, created or removed for it.
    assert not any(c[0] in ("run", "start", "rm") for c in pool._test_docker.calls[n:])


@pytest.fixture
def live_health_pool(monkeypatch):
    """Same wiring as `pool`, minus the `_wait_healthy` stub.

    The probe itself is what the tests below measure, and the `pool` fixture
    replaces it wholesale - measuring that would measure the stub.
    """
    monkeypatch.delenv("VAF_BROWSER_POOL_MAX", raising=False)
    monkeypatch.setattr(bp, "_config_get", lambda key: None)
    fake = _FakeDocker()
    monkeypatch.setattr("vaf.core.containers.docker", fake)
    monkeypatch.setattr("vaf.core.containers.mem_available_mb", lambda: 16000)
    p = bp.BrowserPool()
    p._ensure_reaper = lambda: None            # no threads in unit tests
    p._test_docker = fake
    return p


def test_a_browser_without_its_stream_is_refused(live_health_pool, monkeypatch):
    """An image built before the KasmVNC stream existed answers CDP perfectly and
    serves nothing on the stream port. That container used to pass the health
    probe, get handed to a user, and produce a 502 from the ticket route the
    moment a human clicked - the stream was never asked about.

    The refusal side is the point: resolve() returns None, which is the polite
    fall back to the shared container, never an error.
    """
    monkeypatch.setattr(bi, "resolve_cdp_ws_url", lambda base: "ws://stub")   # CDP half is healthy
    monkeypatch.setattr(bp, "_vnc_wait_s", lambda: 0.0)                       # no waiting in a unit test
    probed = []

    def _dead_stream(url, timeout=3.0, headers=None):
        probed.append(url)
        return False

    monkeypatch.setattr(bp, "_http_ok", _dead_stream)
    assert live_health_pool.resolve("scope-a") is None
    assert probed, "the stream half was never probed"
    assert probed[0].endswith("/index.html"), (
        f"probed {probed[0]}: the probe must fetch the same path the ticket route serves, "
        f"otherwise it checks something other than what a user gets"
    )


def test_both_halves_up_is_healthy(live_health_pool, monkeypatch):
    """Counterpart: with a live stream the same path hands out the instance."""
    monkeypatch.setattr(bi, "resolve_cdp_ws_url", lambda base: "ws://stub")
    monkeypatch.setattr(bp, "_http_ok", lambda url, timeout=3.0, headers=None: True)
    inst = live_health_pool.resolve("scope-a")
    assert inst is not None
    assert inst.vnc_base.startswith("http://127.0.0.1:")


def test_a_dead_cdp_is_refused_before_the_stream_is_probed(live_health_pool, monkeypatch):
    """Order matters: CDP is the cheap half and gates the wait on the stream, so a
    container with no CDP must not spend the stream budget before refusing."""
    def _no_cdp(base):
        raise RuntimeError("no CDP")

    monkeypatch.setattr(bi, "resolve_cdp_ws_url", _no_cdp)
    probed = []
    monkeypatch.setattr(bp, "_http_ok", lambda url, timeout=3.0, headers=None: (probed.append((url, headers)), True)[1])
    assert live_health_pool.resolve("scope-a") is None
    assert probed == [], "the stream was probed even though CDP was already dead"


# ── strict mode and the visible fallback (the banking round) ──────────────

def test_a_fallback_with_the_pool_active_emits_a_security_event(pool, monkeypatch):
    """Silent degradation was the defect: a user who believes they have a
    browser of their own must not land on the shared one without a trace."""
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "1")
    assert pool.resolve("scope-a") is not None
    assert pool.resolve("scope-b") is None            # capacity: shared fallback
    kinds = [k for k, _ in pool._test_events]
    assert kinds == ["browser_pool_fallback"]
    assert "capacity" in pool._test_events[0][1]["detail"]


def test_strict_mode_refuses_instead_of_sharing(pool, monkeypatch):
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "1")
    monkeypatch.setenv("VAF_BROWSER_POOL_STRICT", "1")
    assert pool.resolve("scope-a") is not None         # first user: own instance
    with pytest.raises(bp.PoolExhausted):
        pool.resolve("scope-b")
    # The refusal is still a recorded fallback event.
    assert [k for k, _ in pool._test_events] == ["browser_pool_fallback"]


def test_a_disabled_pool_answers_none_silently(pool, monkeypatch):
    """pool off (or no scope) is the configuration, not a failure: no event,
    no strict refusal - the shared browser IS the product then."""
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "0")
    monkeypatch.setenv("VAF_BROWSER_POOL_STRICT", "1")
    assert pool.resolve("scope-a") is None
    assert pool._test_events == []


def test_get_manager_for_scope_propagates_the_strict_refusal(monkeypatch):
    """The single conversion point from 'pool answered None' to 'use the shared
    manager' must not swallow PoolExhausted, or strict mode would quietly hand
    out the very browser it forbids."""
    import vaf.core.browser_interactive as bi

    class _StrictPool:
        def resolve(self, scope):
            raise bp.PoolExhausted("at capacity (1/1)")

    import vaf.core.browser_pool as bpm
    monkeypatch.setattr(bpm, "get_browser_pool", lambda: _StrictPool())
    with pytest.raises(bp.PoolExhausted):
        bi.get_manager_for_scope("scope-a")


def test_every_resolve_caller_names_the_strict_refusal():
    """Three lanes resolve the pool (interactive, agent run, render); each must
    handle PoolExhausted explicitly - the render lane's old blanket `except
    Exception` was a silent route back onto the shared browser."""
    from pathlib import Path
    root = Path(bp.__file__).resolve().parents[2]
    for rel in ("vaf/core/browser_interactive.py",
                "vaf/tools/browser_agent.py",
                "vaf/core/browser_render.py",
                "vaf/core/web_server.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert "PoolExhausted" in src, f"{rel} lost its strict-pool handling"


_OLD_ID = "sha256:0123456789ab" + "c" * 52
_NEW_ID = "sha256:fedcba987654" + "3" * 52
_CONTAINER_FMT = "{{.Image}}\t{{json .Config.Env}}"


def _created_env(pool):
    """The settings the fake `docker run` gave the last instance, as docker lists them."""
    run = [c for c in pool._test_docker.calls if c[0] == "run"][-1]
    return [run[i + 1] for i, a in enumerate(run) if a == "-e"]


def _inspecting(pool, *, current, have, env, fail=None, seen=None):
    """The fake docker with the two adoption inspects answered: the template's
    `{{.Id}}` (the image its name points to now) and the container's image ID
    plus the env it was created with. `fail` makes one of them fail."""
    real = pool._test_docker.__call__

    def fake(args, timeout=60):
        if args[0] == "inspect" and args[-1] in ("{{.Id}}", _CONTAINER_FMT):
            if seen is not None:
                seen.append(list(args))
            if args[-1] == fail:
                return _done(1, "", "no such object")
            if args[-1] == "{{.Id}}":
                return _done(0, current + "\n")
            return _done(0, f"{have}\t{json.dumps(env)}\n")
        return real(args, timeout=timeout)

    return fake


def test_a_pooled_instance_on_an_outdated_image_is_recreated(pool, monkeypatch):
    """A container is pinned to its image ID for life, so the browser people
    actually bank in would be the LAST to get a Chromium security fix: the
    stack start rebuilds and recreates the SHARED browser, and the pooled
    instance kept serving the old binary forever. The profile volume is named
    and remounted, so logins survive the recreate - only the binary is
    thrown away.

    The recreate line names both IDs, because a recreate at every start with no
    change to the image (a build that hands an unchanged image a new ID) is only
    visible as a new "current" ID each time. And the check stays one inspect of
    the template and one of the container per resolve: it sits on the blocking
    path of opening a browser.

    MUTATIONS: drop the IDs from the log line (red on content); read the IDs a
    second time, by a second helper or a second call of the check (red on the
    count)."""
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "2")
    first = pool.resolve("scope-a")
    assert first is not None
    name = first.container_name

    # The shared browser has been rebuilt: same tag, new image ID.
    inspects = []
    aged = _inspecting(pool, current=_NEW_ID, have=_OLD_ID, env=_created_env(pool), seen=inspects)
    logs = []
    monkeypatch.setattr(bp, "append_domain_log", lambda domain, msg: logs.append((domain, msg)))
    monkeypatch.setattr("vaf.core.containers.docker", aged)
    again = pool.resolve("scope-a")
    assert again is not None
    calls = [c for c in pool._test_docker.calls if c[0] == "rm"]
    assert any(name in c for c in calls), "the outdated container was adopted instead of recreated"

    outdated = [msg for domain, msg in logs if domain == "webui" and "outdated image" in msg]
    assert len(outdated) == 1, f"expected one recreate line, got: {logs}"
    line = outdated[0]
    assert "have 0123456789ab" in line and "current fedcba987654" in line, (
        f"the recreate line does not name both short image IDs: {line}")
    assert "sha256:" not in line and "0123456789abc" not in line, (
        f"the IDs are not shortened to docker's 12 hex digits: {line}")
    assert "settings" not in line, f"unchanged settings were reported as drifted: {line}"
    assert inspects == [["inspect", "vaf-vaf-browser", "--format", "{{.Id}}"],
                        ["inspect", name, "--format", _CONTAINER_FMT]]


@pytest.mark.parametrize("failing", ["{{.Id}}", _CONTAINER_FMT])
def test_an_instance_whose_image_cannot_be_read_is_adopted(pool, monkeypatch, failing):
    """A failed lookup is not evidence of an outdated image. Throwing the person's
    container away on a docker hiccup would cost them a recreate for nothing, so
    either side failing means: adopt.

    MUTATION: treat a failed lookup as outdated (compare without the emptiness
    guard of that side)."""
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "2")
    assert pool.resolve("scope-a") is not None
    broken = _inspecting(pool, current=_NEW_ID, have=_OLD_ID, env=_created_env(pool),
                         fail=failing)
    monkeypatch.setattr("vaf.core.containers.docker", broken)
    assert pool.resolve("scope-a") is not None
    assert not [c for c in pool._test_docker.calls if c[0] == "rm"], (
        "an instance was removed although its image ID could not be read")


def test_a_pooled_instance_on_the_current_image_is_adopted(pool, monkeypatch):
    """The counter-proof: matching image IDs and unchanged settings must NOT
    trigger a recreate, or every resolve would throw the user's browser away."""
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "2")
    assert pool.resolve("scope-a") is not None
    same = _inspecting(pool, current="sha256:SAME", have="sha256:SAME", env=_created_env(pool))
    monkeypatch.setattr("vaf.core.containers.docker", same)
    assert pool.resolve("scope-a") is not None
    assert not [c for c in pool._test_docker.calls if c[0] == "rm"]


_SECRETISH = "http://user:pw-not-for-logs@proxy.example:8080"


@pytest.mark.parametrize("env_var,setting,value", [
    ("VAF_BROWSER_PROXY", "VAF_BROWSER_PROXY", _SECRETISH),
    ("VAF_BROWSER_TZ", "TZ", "America/New_York"),
    ("VAF_BROWSER_GOOGLE_API_KEY", "VAF_BROWSER_GOOGLE_API_KEY", "key-not-for-logs"),
])
def test_an_instance_created_with_other_settings_is_recreated(pool, monkeypatch,
                                                              env_var, setting, value):
    """A container keeps the env of its `docker run` for life. A per-user browser
    created before the operator set a proxy kept browsing direct, its real IP
    exposed, while the shared browser went through the proxy; the same for the
    timezone (a tell against the proxy's region) and the Safe Browsing key. On
    adoption the settings are compared and a drifted instance is recreated with
    the current ones, still within the two inspects. The log names the setting,
    never its value: a proxy URL can carry a password.

    MUTATIONS: skip the settings comparison (red: adopted); log the value
    instead of the name (red on content)."""
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "2")
    first = pool.resolve("scope-a")
    assert first is not None
    created = _created_env(pool)
    assert not any(e.startswith(f"{setting}={value}") for e in created)

    monkeypatch.setenv(env_var, value)
    inspects = []
    drifted = _inspecting(pool, current=_NEW_ID, have=_NEW_ID, env=created, seen=inspects)
    logs = []
    monkeypatch.setattr(bp, "append_domain_log", lambda domain, msg: logs.append((domain, msg)))
    monkeypatch.setattr("vaf.core.containers.docker", drifted)
    assert pool.resolve("scope-a") is not None
    assert [c for c in pool._test_docker.calls if c[0] == "rm" and first.container_name in c], (
        f"an instance created without the current {setting} was adopted")
    assert f"{setting}={value}" in _created_env(pool), "the recreated instance lacks the setting"
    lines = [msg for domain, msg in logs if domain == "webui" and "recreating" in msg]
    assert len(lines) == 1 and setting in lines[0], f"the drifted setting is not named: {logs}"
    assert value not in lines[0] and "pw-not-for-logs" not in lines[0], (
        f"a setting's value reached the log: {lines[0]}")
    assert "outdated image" not in lines[0]
    assert len(inspects) == 2


def test_a_proxy_removed_since_creation_is_followed_too(pool, monkeypatch):
    """Compared both ways: an instance created WITH a proxy is recreated once the
    operator removes it. And empty counts as absent: a container whose env lists
    the variable empty (an image that declares it with an empty default, the way
    the compose service passes it) is not recreated for that.

    MUTATION: compare without treating empty and absent alike - red."""
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "2")
    monkeypatch.setenv("VAF_BROWSER_PROXY", _SECRETISH)
    first = pool.resolve("scope-a")
    created = _created_env(pool)
    assert f"VAF_BROWSER_PROXY={_SECRETISH}" in created

    monkeypatch.setenv("VAF_BROWSER_PROXY", "")
    monkeypatch.setattr("vaf.core.containers.docker",
                        _inspecting(pool, current=_NEW_ID, have=_NEW_ID, env=created))
    assert pool.resolve("scope-a") is not None
    assert [c for c in pool._test_docker.calls if c[0] == "rm" and first.container_name in c]

    # Recreated without it; a listed-but-empty value matches the absent one.
    rms = len([c for c in pool._test_docker.calls if c[0] == "rm"])
    monkeypatch.setattr("vaf.core.containers.docker",
                        _inspecting(pool, current=_NEW_ID, have=_NEW_ID,
                                    env=_created_env(pool) + ["VAF_BROWSER_PROXY="]))
    assert pool.resolve("scope-a") is not None
    assert len([c for c in pool._test_docker.calls if c[0] == "rm"]) == rms


def test_the_health_probe_authenticates_or_the_pool_dies_silently(live_health_pool, monkeypatch):
    """The stream port carries a credential now, and the probe fetches the very
    path the ticket route serves. Without the header the container answers 401,
    the instance is declared unhealthy, and every user is handed the SHARED
    browser instead - a security-relevant downgrade that presents as a
    switched-off pool rather than as a broken probe.

    MUTATION: drop `headers=` from the _http_ok call in _wait_healthy and this
    is the only test that says so."""
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "2")
    monkeypatch.setattr(bi, "resolve_cdp_ws_url", lambda base: "ws://stub")
    monkeypatch.setattr(bi, "browser_vnc_secret", lambda: "probe-secret")
    seen = []

    def _record(url, timeout=3.0, headers=None):
        seen.append((url, dict(headers or {})))
        return True

    monkeypatch.setattr(bp, "_http_ok", _record)
    assert live_health_pool.resolve("scope-a") is not None
    assert seen, "the stream half was never probed"
    url, headers = seen[0]
    assert url.endswith("/index.html")
    assert headers.get("Authorization", "").startswith("Basic "), (
        "the health probe asks the stream port without the credential; every "
        "instance would report unhealthy and the pool would fall back silently")


def test_a_pooled_container_is_created_with_the_stream_credential(pool, monkeypatch):
    """The pooled instance must get the same secret the compose service gets,
    or nobody can watch its stream and it never passes its own health probe."""
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "2")
    monkeypatch.setattr(bi, "browser_vnc_secret", lambda: "pool-secret")
    assert pool.resolve("scope-a") is not None
    run_call = next(c for c in pool._test_docker.calls if c[0] == "run")
    assert "VAF_BROWSER_VNC_SECRET=pool-secret" in run_call
