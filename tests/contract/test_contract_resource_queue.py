# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The embedder contract for `vaf.ResourceQueue` (docs/EMBEDDING.md, "Sharing a scarce
resource fairly").

A stranger puts their own scarce thing (one GPU, one licence, one serial device) behind
the queue, from the FACADE alone: no Agent, no config, no speech stack.
"""
import os
import subprocess
import sys
import textwrap
import threading
import time

import vaf


def test_the_queue_names_resolve_from_the_facade():
    from vaf import QueueBusy, ResourceQueue, WaitInfo, queue_metrics, resource_queue
    import vaf.core.fair_queue as fq  # the re-export source named in EMBEDDING.md

    assert ResourceQueue is fq.ResourceQueue and QueueBusy is fq.QueueBusy
    assert WaitInfo is fq.WaitInfo
    assert resource_queue is fq.resource_queue and queue_metrics is fq.queue_metrics


def test_import_vaf_does_not_load_the_queue(tmp_path):
    """Lazy like every facade name: a fresh process is the only honest probe.
    MUTATION: import fair_queue eagerly in vaf/__init__.py - red."""
    script = textwrap.dedent(
        """
        import sys
        import vaf
        assert "vaf.core.fair_queue" not in sys.modules, "import vaf loaded the queue"
        from vaf import ResourceQueue
        assert "vaf.core.fair_queue" in sys.modules
        """
    )
    env = dict(os.environ)
    result = subprocess.run([sys.executable, "-c", script], cwd=str(tmp_path), env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def test_a_stranger_shares_one_device_fairly():
    """Two people, one device: alice queues two more jobs, then bob one. Bob is served
    before alice's third job (round robin), learns his place, and the numbers carry no
    names."""
    q = vaf.ResourceQueue("one-printer", slots=1, max_waiting=4)
    order, places = [], []
    release, held = threading.Event(), threading.Event()

    def first_job():
        with q.slot("alice"):
            held.set()
            release.wait(5)
            order.append("alice-1")

    def job(who, name, on_wait=None):
        with q.slot(who, on_wait=on_wait, poll_s=0.01):
            order.append(name)

    t0 = threading.Thread(target=first_job, daemon=True)
    t0.start()
    assert held.wait(5)
    threads = []
    for who, name, cb in (("alice", "alice-2", None), ("alice", "alice-3", None),
                          ("bob", "bob-1", lambda info: places.append(info.position))):
        before = q.metrics()["waiting"]
        threads.append(threading.Thread(target=job, args=(who, name, cb), daemon=True))
        threads[-1].start()
        end = time.monotonic() + 5
        while q.metrics()["waiting"] == before and time.monotonic() < end:
            time.sleep(0.005)
    # alice-2 arrived first and goes first; bob comes next, ahead of alice-3.
    assert places and places[0] == 2
    release.set()
    for t in (t0, *threads):
        t.join(5)
    assert order == ["alice-1", "alice-2", "bob-1", "alice-3"]
    assert places[-1] == 0
    assert "alice" not in repr(q.metrics()) and "bob" not in repr(q.metrics())


def test_a_full_queue_says_busy_with_a_reason():
    q = vaf.ResourceQueue("one-licence", slots=1, max_waiting=0)
    refused = []

    def bob():
        try:
            with q.slot("bob"):
                refused.append("admitted")
        except vaf.QueueBusy as busy:
            refused.append(busy)

    with q.slot("alice"):
        t = threading.Thread(target=bob, daemon=True)
        t.start()
        t.join(5)
    assert len(refused) == 1 and isinstance(refused[0], vaf.QueueBusy)
    assert refused[0].reason == "full" and refused[0].resource == "one-licence"
    assert str(refused[0])
