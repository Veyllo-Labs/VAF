# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""collect_snapshot: the one collector behind `vaf top`.

Pinned here: the never-raises contract (a failing probe becomes None, not an
exception - a live dashboard must keep rendering), the network rate computed
from two snapshots, and the nvidia-smi CSV parse. The suite must never spawn
real vendor tools, so every subprocess seam is patched."""
import time

import psutil

import vaf.core.system_stats as stats


def test_a_failing_probe_becomes_none_not_an_exception(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("probe down")
    monkeypatch.setattr(psutil, "virtual_memory", boom)
    monkeypatch.setattr(psutil, "disk_usage", boom)
    monkeypatch.setattr(psutil, "net_io_counters", boom)
    monkeypatch.setattr(stats, "gpu_live", lambda: [])

    snap = stats.collect_snapshot()
    assert snap["mem"] is None
    assert snap["disk"] is None
    assert snap["net"] is None
    assert snap["gpus"] == []
    assert snap["vaf"]["version"]  # identity still present


def test_network_rate_needs_two_snapshots(monkeypatch):
    class _IO:
        bytes_sent = 2000
        bytes_recv = 4000
    monkeypatch.setattr(psutil, "net_io_counters", lambda: _IO())
    monkeypatch.setattr(stats, "gpu_live", lambda: [])

    first = stats.collect_snapshot()
    assert first["net"]["sent_rate_bps"] is None, "no rate without a previous snapshot"

    prev = {"monotonic": time.monotonic() - 2.0,
            "net": {"bytes_sent": 0, "bytes_recv": 0}}
    snap = stats.collect_snapshot(prev=prev)
    # dt is slightly above 2.0s by the time the collector reads the clock
    assert 900 <= snap["net"]["sent_rate_bps"] <= 1000
    assert 1800 <= snap["net"]["recv_rate_bps"] <= 2000


def test_gpu_live_parses_the_nvidia_smi_csv(monkeypatch):
    monkeypatch.setattr(stats.shutil, "which", lambda name: "/usr/bin/nvidia-smi")

    def run(argv, **kw):
        assert argv[0] == "nvidia-smi"
        return type("R", (), {"returncode": 0, "stderr": "",
                              "stdout": "NVIDIA GeForce RTX 3080, 26, 2012, 10240\n"})()
    monkeypatch.setattr(stats.subprocess, "run", run)

    gpus = stats.gpu_live()
    assert gpus == [{"vendor": "nvidia", "name": "NVIDIA GeForce RTX 3080",
                     "util_percent": 26, "mem_used_mb": 2012, "mem_total_mb": 10240}]


def test_gpu_live_without_nvidia_falls_back_to_the_static_inventory(monkeypatch):
    monkeypatch.setattr(stats.shutil, "which", lambda name: None)
    monkeypatch.setattr(stats, "_gpu_inventory",
                        [{"vendor": "amd", "name": "Radeon", "vram_total_mb": 8192}])
    gpus = stats.gpu_live()
    assert gpus == [{"vendor": "amd", "name": "Radeon", "vram_total_mb": 8192,
                     "util_percent": None, "mem_used_mb": None, "mem_total_mb": 8192}]


def test_service_process_reads_the_whole_tree(monkeypatch):
    class FakeProc:
        def __init__(self, pid, rss):
            self.pid = pid
            self._rss = rss
        def is_running(self):
            return True
        def children(self, recursive=False):
            return [FakeProc(43, 50 * 1024 * 1024)] if self.pid == 42 else []
        def cpu_percent(self, interval=None):
            return 1.5
        def memory_info(self):
            return type("M", (), {"rss": self._rss})()
        def create_time(self):
            return time.time() - 90

    monkeypatch.setattr(psutil, "Process", lambda pid: FakeProc(pid, 100 * 1024 * 1024))
    monkeypatch.setattr(stats, "_proc_cache", {})

    svc = stats._service_process(42)
    assert svc["pid"] == 42
    assert svc["processes"] == 2
    assert svc["rss_mb"] == 150
    assert 85 <= svc["uptime_s"] <= 95
    assert svc["cpu_percent"] == 3.0

    assert stats._service_process(None) is None


def test_api_provider_shows_its_api_model_not_the_local_one(monkeypatch):
    """Live finding: with provider veyllo the header showed the LOCAL gguf model,
    because the bare "model" key only means anything in local mode - API providers
    keep their active model in api_model_{provider}."""
    from vaf.core.config import Config
    values = {"provider": "veyllo", "model": "local-thing.gguf",
              "api_model_veyllo": "veyllo-chat"}
    monkeypatch.setattr(Config, "get", lambda key, default=None: values.get(key, default))

    summary = stats._config_summary()
    assert summary["provider"] == "veyllo"
    assert summary["model"] == "veyllo-chat"

    values["provider"] = "local"
    assert stats._config_summary()["model"] == "local-thing.gguf"


def test_lan_summary_lists_the_hostname_url_first(monkeypatch):
    import vaf.network.binding as binding
    monkeypatch.setattr(binding, "resolve_lan_access_ports",
                        lambda wait_for_proxy=False: (8443, 8001))
    monkeypatch.setattr(binding, "get_all_local_ips",
                        lambda: [("eth0", "192.168.1.10")])

    lan = stats._lan_summary(True, "srv1")
    assert lan["urls"] == ["https://srv1:8443", "https://192.168.1.10:8443"]
    assert lan["access_port"] == 8443

    assert stats._lan_summary(False, "srv1") == {"enabled": False, "access_port": None, "urls": []}


def test_service_clients_counts_only_inbound_connections(monkeypatch):
    """The service's own OUTBOUND connections (postgres, provider APIs) must not
    appear as clients: a connection counts only when its local port is one the
    process tree is listening on."""
    class _Addr:
        def __init__(self, ip, port):
            self.ip, self.port = ip, port

    class _Conn:
        def __init__(self, status, laddr, raddr=None):
            self.status, self.laddr, self.raddr = status, laddr, raddr

    conns = [
        _Conn(psutil.CONN_LISTEN, _Addr("0.0.0.0", 8443)),
        # two LAN clients on the listening port
        _Conn(psutil.CONN_ESTABLISHED, _Addr("192.168.1.5", 8443), _Addr("192.168.1.50", 51000)),
        _Conn(psutil.CONN_ESTABLISHED, _Addr("192.168.1.5", 8443), _Addr("192.168.1.50", 51002)),
        # loopback client
        _Conn(psutil.CONN_ESTABLISHED, _Addr("127.0.0.1", 8443), _Addr("127.0.0.1", 40000)),
        # OUTBOUND to postgres - local port is ephemeral, must be excluded
        _Conn(psutil.CONN_ESTABLISHED, _Addr("127.0.0.1", 43210), _Addr("127.0.0.1", 5432)),
    ]

    class FakeProc:
        def net_connections(self, kind="tcp"):
            return conns

    monkeypatch.setattr(stats, "_proc_cache", {42: FakeProc()})
    result = stats._service_clients(42)

    assert result["listen_ports"] == [8443]
    by_ip = {c["ip"]: c for c in result["clients"]}
    assert by_ip["192.168.1.50"]["connections"] == 2
    assert by_ip["192.168.1.50"]["local"] is False
    assert by_ip["127.0.0.1"]["local"] is True
    assert "5432" not in str(result), "outbound postgres must not be listed"

    assert stats._service_clients(None) is None
