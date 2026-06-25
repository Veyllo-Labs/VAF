"""The LAN-enable crash regression.

Enabling LAN/server mode writes SEVERAL config keys in ONE save (local_network_enabled +
local_network_tls_enabled + local_network_https_port + ...). The tray's config observer fires once per
changed key. The old code spawned one restart thread per key, so the whole burst ran CONCURRENTLY —
multiple threads tearing down the same FrontendManager singleton and the global uvicorn server at the
same instant. That race is the untraceable hard crash users hit on "Apply Change".

_schedule_network_restart must collapse such a burst into a SINGLE restart, while still running a second
restart for a genuinely later change (so it doesn't over-coalesce and ignore real updates).
"""
import time

import vaf.tray as tray


def _drive(keys, gap=0.02):
    for k in keys:
        tray._schedule_network_restart(k, True)
        time.sleep(gap)


def test_key_change_burst_coalesces_into_one_restart(monkeypatch):
    calls = []
    # Replace the real (process-killing) restart with a short stand-in that records each invocation.
    monkeypatch.setattr(tray, "_do_network_restart", lambda key, value: (calls.append(key), time.sleep(0.2)))

    # Simulate one 'enable LAN' save flipping four keys in rapid succession.
    _drive([
        "local_network_enabled",
        "local_network_tls_enabled",
        "local_network_https_port",
        "local_network_port",
    ])

    # Wait past the debounce window + the stand-in restart.
    time.sleep(1.8)
    assert len(calls) == 1, f"burst should coalesce into one restart, got {len(calls)}: {calls}"


def test_separated_changes_each_restart(monkeypatch):
    calls = []
    monkeypatch.setattr(tray, "_do_network_restart", lambda key, value: (calls.append(key), time.sleep(0.2)))

    tray._schedule_network_restart("local_network_enabled", False)
    time.sleep(1.8)  # let the first restart fully complete
    tray._schedule_network_restart("local_network_enabled", True)
    time.sleep(1.8)

    assert len(calls) == 2, f"two separated changes should each restart, got {len(calls)}: {calls}"
