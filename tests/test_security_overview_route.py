# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Contract tests for the security-overview sandbox status derivation.

The dashboard's traffic light depends on these exact semantics:
ok (container-enforced, running or ephemeral-on-demand), warn (docker down ->
execution blocked, fail-closed), and honest hardening booleans derived from the
LIVE docker inspect payload rather than compose claims.
"""
from vaf.api.security_routes import collect_sandbox_status, derive_sandbox_status


def _inspect_payload(running=True, cap_drop=("ALL",), sec_opt=("no-new-privileges:true",),
                     memory=536870912, nano_cpus=500000000, networks=("vaf-sandbox-network",)):
    return {
        "State": {"Running": running},
        "HostConfig": {
            "CapDrop": list(cap_drop),
            "SecurityOpt": list(sec_opt),
            "Memory": memory,
            "NanoCpus": nano_cpus,
        },
        "NetworkSettings": {"Networks": {n: {} for n in networks}},
    }


def test_running_hardened_container_is_ok():
    s = derive_sandbox_status(True, _inspect_payload())
    assert s["state"] == "ok"
    assert s["reason"] == "container_running"
    assert s["container_running"] is True
    h = s["hardening"]
    assert h["cap_drop_all"] is True
    assert h["no_new_privileges"] is True
    assert h["isolated_network"] is True
    assert h["memory_bytes"] == 536870912


def test_docker_down_is_warn_fail_closed():
    """Daemon down: executions are BLOCKED (fail-closed) -> attention, not critical."""
    s = derive_sandbox_status(False, None)
    assert s["state"] == "warn"
    assert s["reason"] == "docker_unavailable"
    assert s["container_running"] is False


def test_stopped_container_with_docker_up_is_still_enforced():
    """No persistent container: executions fall back to ephemeral --network none runs."""
    s = derive_sandbox_status(True, None)
    assert s["state"] == "ok"
    assert s["reason"] == "ephemeral_on_demand"
    assert s["container_running"] is False


def test_weakened_hardening_is_reported_honestly():
    """A container without cap_drop ALL / extra networks must not report hardened booleans."""
    s = derive_sandbox_status(True, _inspect_payload(cap_drop=(), sec_opt=(), networks=("bridge", "vaf-sandbox-network")))
    h = s["hardening"]
    assert h["cap_drop_all"] is False
    assert h["no_new_privileges"] is False
    assert h["isolated_network"] is False


def test_collect_uses_probes_and_skips_inspect_when_docker_down():
    calls = {"inspect": 0}

    def fake_inspect():
        calls["inspect"] += 1
        return _inspect_payload()

    s = collect_sandbox_status(docker_probe=lambda: False, inspect_probe=fake_inspect)
    assert s["state"] == "warn"
    assert calls["inspect"] == 0  # no pointless inspect when the daemon is gone

    s2 = collect_sandbox_status(docker_probe=lambda: True, inspect_probe=fake_inspect)
    assert s2["state"] == "ok" and calls["inspect"] == 1


def test_dir_size_bounded(tmp_path):
    from vaf.api.security_routes import dir_size_bounded
    (tmp_path / "a").write_bytes(b"x" * 100)
    sub = tmp_path / "sub"; sub.mkdir()
    (sub / "b").write_bytes(b"y" * 50)
    # symlink loops must be skipped, not followed
    try:
        (tmp_path / "loop").symlink_to(tmp_path)
    except OSError:
        pass
    info = dir_size_bounded(tmp_path)
    assert info["size_bytes"] == 150 and info["files"] == 2 and info["truncated"] is False


def test_dir_size_bounded_cap(tmp_path):
    from vaf.api.security_routes import dir_size_bounded
    for i in range(5):
        (tmp_path / f"f{i}").write_bytes(b"z")
    info = dir_size_bounded(tmp_path, file_cap=3)
    assert info["truncated"] is True and info["files"] == 3


def _container(name, networks=("vaf-network",), bindings=None, running=True):
    return {
        "Name": f"/{name}",
        "State": {"Running": running},
        "NetworkSettings": {"Networks": {n: {} for n in networks}},
        "HostConfig": {"PortBindings": bindings or {}},
    }


def test_docker_isolation_all_loopback_is_ok():
    from vaf.api.security_routes import derive_docker_isolation
    d = derive_docker_isolation([
        _container("vaf-memory-db", bindings={"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5432"}]}),
        _container("vaf-sandbox", networks=("vaf-sandbox-network",)),
        _container("vaf-browser", networks=("vaf-browser-network",), bindings={"9222/tcp": [{"HostIp": "127.0.0.1", "HostPort": "9222"}]}),
    ])
    assert d["state"] == "ok" and d["any_lan_exposed"] is False
    assert d["sandbox_off_internal"] is True


def test_docker_isolation_flags_lan_exposed_binding():
    """A port bound to 0.0.0.0 (or empty HostIp) is reachable from the LAN -> warn."""
    from vaf.api.security_routes import derive_docker_isolation
    d = derive_docker_isolation([
        _container("vaf-memory-db", bindings={"5432/tcp": [{"HostIp": "0.0.0.0", "HostPort": "5432"}]}),
    ])
    assert d["state"] == "warn" and d["any_lan_exposed"] is True
    assert d["containers"][0]["lan_exposed"] is True


def test_docker_isolation_sandbox_on_internal_net_is_not_isolated():
    from vaf.api.security_routes import derive_docker_isolation
    d = derive_docker_isolation([
        _container("vaf-sandbox", networks=("vaf-sandbox-network", "vaf-network")),
    ])
    assert d["sandbox_off_internal"] is False


def test_workspace_metrics_aggregate_per_user(tmp_path):
    """Folders aggregate PER USER: username dirs directly, uid8/scope_ dirs via the
    map; legacy project dirs land in the unassigned bucket; hidden dirs skipped."""
    from vaf.api.security_routes import UNASSIGNED, collect_workspace_metrics
    users = tmp_path / "users"; users.mkdir()
    (users / "alice").mkdir(); (users / "alice" / "a").write_bytes(b"x" * 100)
    (users / "scope_ab12cd34").mkdir(); (users / "scope_ab12cd34" / "b").write_bytes(b"y" * 50)
    projects = tmp_path / "VAF_Projects"; projects.mkdir()
    (projects / "ab12cd34").mkdir(); (projects / "ab12cd34" / "c").write_bytes(b"z" * 25)
    (projects / "Legacy Project").mkdir(); (projects / "Legacy Project" / "d").write_bytes(b"w" * 10)
    (projects / ".git").mkdir(); (projects / ".git" / "e").write_bytes(b"v" * 999)

    out = collect_workspace_metrics(uid8_names={"ab12cd34": "bob"}, roots=[("users", users), ("projects", projects)])
    assert out["workspace_count"] == 4 and out["total_size_bytes"] == 185  # .git skipped
    by_name = {u["name"]: u for u in out["user_folders"]}
    assert by_name["alice"]["folders"] == 1 and by_name["alice"]["size_bytes"] == 100
    # scope_ab12cd34 (users root) + ab12cd34 (projects root) merge under "bob"
    assert by_name["bob"]["folders"] == 2 and by_name["bob"]["size_bytes"] == 75
    assert by_name[UNASSIGNED]["folders"] == 1 and by_name[UNASSIGNED]["size_bytes"] == 10


def test_channels_status_derivation():
    from vaf.api.security_routes import derive_channels_status
    channels = [
        {"name": "telegram", "enabled": True, "paired": 3, "last_ts": 123.0, "mode": "paired_only", "contact_fallback": False},
        {"name": "whatsapp", "enabled": True, "paired": 1, "last_ts": None, "mode": "permissive", "contact_fallback": False},
        {"name": "discord", "enabled": False, "paired": 0, "last_ts": None, "mode": "permissive", "contact_fallback": False},
    ]
    out = derive_channels_status(channels, {"telegram": 2, "whatsapp": 1})
    # an ENABLED permissive channel -> warn; a disabled permissive one alone would not
    assert out["state"] == "warn" and out["any_permissive"] is True
    assert out["rejected_today"] == 3
    tg = next(c for c in out["channels"] if c["name"] == "telegram")
    assert tg["rejected_today"] == 2 and tg["paired"] == 3

    out_ok = derive_channels_status([dict(channels[0])], {})
    assert out_ok["state"] == "ok" and out_ok["rejected_today"] == 0


def test_guardrails_derivation_shape_and_unrestricted_passthrough():
    from vaf.api.security_routes import derive_guardrails_status
    out = derive_guardrails_status(
        {"plan_gate": True, "proactive_reply_gate": True, "ask_first_drain_gate": False, "channel_tools_unrestricted": True},
        {"total": 40, "read": 20, "write": 12, "dangerous": 6, "system": 2, "admin_only": 2, "channel_restricted": 7},
        {"trusted_dirs": ["/home/user/proj"], "allow_always_tools": ["run_command"]},
    )
    # deliberate: unrestricted flag surfaces but the module stays ok (default-True
    # must not amber every install - row-level warning only)
    assert out["state"] == "ok" and out["channel_tools_unrestricted"] is True
    assert out["confirmation_gate"] is True  # structural, always on
    assert out["tools"]["dangerous"] == 6 and out["tools"]["total"] == 40
    assert out["trust"]["allow_always_tools"] == ["run_command"]
    assert out["ask_first_drain_gate"] is False


def test_skills_status_derivation():
    from vaf.api.security_routes import derive_skills_status
    skills = {
        "clean-one": {"scan": {"score": 0, "level": "clean", "count": 0}},
        "risky": {"scan": {"score": 20, "level": "medium", "count": 2}},
        "no-scan-block": {},
    }
    events = [{"kind": "skill_blocked"}, {"kind": "skill_blocked"}, {"kind": "skill_override"}, {"kind": "skill_scan_alert"}, {"kind": "skill_quarantined"}]
    out = derive_skills_status(skills, events, {"ts": "2026-01-01T00:00:00", "scanned": 3})
    assert out["state"] == "warn" and out["worst"] == "medium"
    assert out["counts"]["clean"] == 2 and out["counts"]["medium"] == 1  # missing scan counts as clean
    assert out["blocked_today"] == 2 and out["overrides_today"] == 1 and out["alerts_today"] == 2  # scan_alert + quarantined
    assert out["skills"][0]["id"] == "risky"  # riskiest first

    out_high = derive_skills_status({"bad": {"scan": {"score": 90, "level": "high", "count": 5}}}, [], None)
    assert out_high["state"] == "critical"
