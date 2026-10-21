# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""firewalld LAN opening: the rule must be scoped to the LAN subnet (RFC1918), and elevation must use
pkexec on the desktop (native password dialog) but never an interactive sudo prompt headless,
and a process that cannot gain root at all (the server's systemd service) must not try."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

import vaf.network.firewall as fw


def test_rich_rule_is_subnet_scoped_not_world_open():
    r = fw._firewalld_rich_rule("192.168.2.0/24", 8443)
    assert r == ('rule family="ipv4" source address="192.168.2.0/24" '
                 'port port="8443" protocol="tcp" accept')
    # Scoped to the LAN subnet + the exact port — NOT 0.0.0.0/anywhere.
    assert "192.168.2.0/24" in r and 'port="8443"' in r
    assert "0.0.0.0" not in r


def test_elevation_uses_pkexec_on_desktop(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(fw.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0})())  # `which pkexec` → found
    assert fw._elevation_argv() == ["pkexec"]


def test_elevation_falls_back_to_noninteractive_sudo_headless(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    # No display → never pkexec, and `sudo -n` so a headless run fails fast instead of hanging on a TTY.
    assert fw._elevation_argv() == ["sudo", "-n"]


class _RunRecorder:
    """Records every subprocess.run; elevation calls return rc=0."""
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()


def _wire_firewalld(monkeypatch, recorder, tmp_path, sources=("192.168.2.0/24",), zones=("public",)):
    monkeypatch.setattr(fw, "_sources", lambda narrow_lan=False: list(sources))
    monkeypatch.setattr(fw, "_firewalld_zones", lambda: list(zones))
    monkeypatch.setattr(fw, "_elevation_argv", lambda: ["fake-elevate"])
    # The gate reads this process's own /proc; pinned so a run from inside a hardened
    # service (NoNewPrivileges) tests the same thing as a run from a login shell.
    monkeypatch.setattr(fw, "elevation_unavailable", lambda: None)
    monkeypatch.setattr(fw, "_firewalld_marker_path", lambda: tmp_path / "firewalld_lan.json")
    monkeypatch.setattr(fw.subprocess, "run", recorder)


LAN_RULE = ("public", fw._firewalld_rich_rule("192.168.2.0/24", 8443))


def test_marker_hit_runs_no_firewall_command_at_all(monkeypatch, tmp_path):
    """The normal start: this install already set the rule up, the marker says
    so, and NOT ONE firewall-cmd runs - not even a read. Deliberate: the
    unprivileged --query-rich-rule is an auth_admin polkit action on common
    distros, so the old presence CHECK was itself the root password dialog the
    idempotence promise was supposed to prevent (live incident: a dialog on
    every start for weeks while the permanent rule existed; every password went
    into the check, never into a change). Mutation: query firewalld before
    trusting the marker - red."""
    rec = _RunRecorder()
    _wire_firewalld(monkeypatch, rec, tmp_path)
    fw._firewalld_marker_write([LAN_RULE])
    assert fw._setup_firewall_linux_firewalld(8443, 8001) == "present"
    assert rec.calls == []


def test_marker_miss_elevates_once_with_check_and_add_inside(monkeypatch, tmp_path):
    """First run (or subnet/port/zone changed): exactly ONE elevation, and the
    query rides INSIDE it together with the runtime and permanent adds - as
    root all three are free, so one password covers everything and an already
    existing rule is not added twice."""
    rec = _RunRecorder()
    _wire_firewalld(monkeypatch, rec, tmp_path)
    assert fw._setup_firewall_linux_firewalld(8443, 8001) == "created"
    assert len(rec.calls) == 1 and rec.calls[0][0] == "fake-elevate"
    inner = rec.calls[0][-1]
    assert "--query-rich-rule" in inner, "the check must run inside the elevation"
    assert "--add-rich-rule" in inner and "--permanent" in inner
    # and the success is remembered: the next start is silent
    assert fw._firewalld_marker_matches([LAN_RULE])


def test_stale_marker_for_other_port_still_elevates(monkeypatch, tmp_path):
    """A marker for yesterday's port must not silence today's setup - the
    failure direction of the marker is a CLOSED port, never a skipped opening."""
    rec = _RunRecorder()
    _wire_firewalld(monkeypatch, rec, tmp_path)
    fw._firewalld_marker_write([("public", fw._firewalld_rich_rule("192.168.2.0/24", 9999))])
    assert fw._setup_firewall_linux_firewalld(8443, 8001) == "created"
    assert len(rec.calls) == 1


def test_corrupt_marker_is_treated_as_missing(monkeypatch, tmp_path):
    rec = _RunRecorder()
    _wire_firewalld(monkeypatch, rec, tmp_path)
    (tmp_path / "firewalld_lan.json").write_bytes(b"not json {")
    assert fw._setup_firewall_linux_firewalld(8443, 8001) == "created"
    assert len(rec.calls) == 1


def _linux_only(monkeypatch):
    monkeypatch.setattr(fw.Platform, "is_windows", lambda *a: False)
    monkeypatch.setattr(fw.Platform, "is_macos", lambda *a: False)
    monkeypatch.setattr(fw.Platform, "is_linux", lambda *a: True)
    monkeypatch.setattr(fw, "_attempted_ports", {})
    monkeypatch.setattr(fw, "_sources", lambda narrow_lan=False: ["192.168.0.0/16"])


def test_one_elevation_attempt_per_process(monkeypatch):
    """TLS mode runs two lifespans of the same app; both spawn the firewall
    setup within milliseconds. The second call must never reach the platform
    path - with a dialog open, a racing twin means TWO password prompts for one
    start. Mutation: claim the key on success instead of entry - red."""
    calls = []
    monkeypatch.setattr(fw, "_setup_firewall_linux", lambda p, pf: calls.append(p) or True)
    _linux_only(monkeypatch)
    assert fw.setup_firewall(8443, 8001) is True
    assert fw.setup_firewall(8443, 8001) is True
    assert calls == [8443], "second call must not re-run the platform setup"


def test_a_cancelled_dialog_is_never_reported_as_success(monkeypatch):
    """The twin lifespan must learn what the FIRST attempt actually did. With a
    blanket "present" the log said the rule was in place while the port stayed
    closed, because the user had cancelled the password dialog. Mutation: return
    "present" for any repeat call - red."""
    calls = []
    monkeypatch.setattr(fw, "_setup_firewall_linux", lambda p, pf: calls.append(p) or False)
    _linux_only(monkeypatch)
    assert fw.setup_firewall(8443, 8001) is False
    assert fw.setup_firewall(8443, 8001) is False, \
        "a failed attempt must not turn truthy for the twin lifespan"
    assert calls == [8443]


def test_engine_detection_never_runs_firewall_cmd(monkeypatch):
    """`firewall-cmd --state` is polkit action org.fedoraproject.FirewallD1.config
    on openSUSE (measured live) - a root password dialog for an unprivileged
    caller, fired on EVERY start before the marker was even consulted. The
    running check must ask systemd instead; the only allowed firewall-cmd
    contact is the `which` lookup. Mutation: put --state back - red."""
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(fw.subprocess, "run", run)
    assert fw._firewalld_running() is True
    assert all(a[0] != "firewall-cmd" for a in calls), calls
    assert any(a[:2] == ["systemctl", "is-active"] for a in calls)


def test_a_failed_netsh_attempt_skips_further_windows_setup(monkeypatch):
    """The skip flag must actually persist across calls: it is assigned inside the
    function, which without a global declaration created a function-local and made
    the documented "no repeated netsh dialogs" guard a no-op. Mutation: drop the
    global declaration again - red."""
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": "denied"})()

    monkeypatch.setattr(fw, "_windows_firewall_skip", False)
    monkeypatch.setattr(fw.subprocess, "run", run)

    assert fw._setup_firewall_windows(8443, 8001) is False
    assert fw._windows_firewall_skip is True
    first = len(calls)
    assert first > 0
    assert fw._setup_firewall_windows(8443, 8001) is False
    assert len(calls) == first, "the second attempt must short-circuit before any netsh call"


# ── rule sets: the admitted networks, every relevant zone, stale rules removed ──

def test_a_marker_from_before_the_rule_sets_still_counts(monkeypatch, tmp_path):
    """An install that set up its one LAN rule before the rule sets existed must not
    get a password dialog after the update when nothing else changed.
    MUTATION: read only the new marker shape - red."""
    import json
    rec = _RunRecorder()
    _wire_firewalld(monkeypatch, rec, tmp_path)
    (tmp_path / "firewalld_lan.json").write_bytes(
        json.dumps({"zone": LAN_RULE[0], "rule": LAN_RULE[1]}).encode())
    assert fw._setup_firewall_linux_firewalld(8443, 8001) == "present"
    assert rec.calls == []


def test_every_admitted_network_gets_a_rule_in_every_zone(monkeypatch, tmp_path):
    """A WireGuard interface is usually in no zone, so its packets are judged in the
    default zone; a rule only in the LAN's zone would leave the VPN closed."""
    rec = _RunRecorder()
    _wire_firewalld(monkeypatch, rec, tmp_path, sources=("10.8.0.0/24", "192.168.2.0/24"),
                    zones=("home", "public"))
    assert fw._setup_firewall_linux_firewalld(8443, 8001) == "created"
    inner = rec.calls[0][-1]
    for zone in ("home", "public"):
        for src in ("10.8.0.0/24", "192.168.2.0/24"):
            assert f"--zone={zone} --add-rich-rule=" in inner
            assert f'source address="{src}"' in inner
    assert len(fw._firewalld_marker_read()) == 4


def test_a_network_that_is_no_longer_admitted_is_closed_again(monkeypatch, tmp_path):
    """The marker remembers what this install opened, so dropping a network removes
    its rule in the same single elevation. MUTATION: skip the stale removal - red."""
    rec = _RunRecorder()
    _wire_firewalld(monkeypatch, rec, tmp_path, sources=("192.168.2.0/24",))
    tailscale = ("public", fw._firewalld_rich_rule("100.64.0.0/10", 8443))
    fw._firewalld_marker_write([LAN_RULE, tailscale])
    assert fw._setup_firewall_linux_firewalld(8443, 8001) == "created"
    assert len(rec.calls) == 1
    inner = rec.calls[0][-1]
    assert "--remove-rich-rule=" in inner and "100.64.0.0/10" in inner
    assert fw._firewalld_marker_read() == [LAN_RULE]


def test_vpn_only_without_a_vpn_removes_and_opens_nothing(monkeypatch, tmp_path):
    rec = _RunRecorder()
    _wire_firewalld(monkeypatch, rec, tmp_path, sources=())
    fw._firewalld_marker_write([LAN_RULE])
    assert fw._setup_firewall_linux_firewalld(8443, 8001) == "created"
    inner = rec.calls[0][-1]
    assert "--remove-rich-rule=" in inner and "--add-rich-rule" not in inner
    assert fw._firewalld_marker_read() == []
    # and the next start is silent again
    assert fw._setup_firewall_linux_firewalld(8443, 8001) == "present"
    assert len(rec.calls) == 1


def test_a_change_of_the_admitted_networks_is_applied_in_the_same_process(monkeypatch):
    """The admission settings change without a restart, so the once-per-process claim
    must be per set of networks. MUTATION: key the claim on the ports only - red."""
    calls = []
    sources = ["192.168.0.0/16"]
    monkeypatch.setattr(fw, "_setup_firewall_linux", lambda p, pf: calls.append(p) or True)
    _linux_only(monkeypatch)
    monkeypatch.setattr(fw, "_sources", lambda narrow_lan=False: list(sources))
    assert fw.setup_firewall(8443, 8001) is True
    sources.append("100.64.0.0/10")
    assert fw.setup_firewall(8443, 8001) is True
    assert calls == [8443, 8443]


def test_a_network_added_inside_the_wide_list_is_still_applied(monkeypatch):
    """An admin adds 10.20.0.0/16: the wide list (10.0.0.0/8, ...) stays the same, the
    firewalld list gains the network. Keyed on the wide list alone, the second setup
    was swallowed as "already attempted" and firewalld kept the network closed while
    the app admitted it. MUTATION: key the claim on the wide list only - red."""
    calls = []
    narrow = ["192.168.2.0/24"]
    monkeypatch.setattr(fw, "_setup_firewall_linux", lambda p, pf: calls.append(p) or True)
    _linux_only(monkeypatch)
    monkeypatch.setattr(fw, "_sources", lambda narrow_lan=False:
                        list(narrow) if narrow_lan else ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"])
    assert fw.setup_firewall(8443, 8001) is True
    narrow.append("10.20.0.0/16")
    assert fw.setup_firewall(8443, 8001) is True
    assert calls == [8443, 8443]

def test_the_other_platforms_open_the_same_networks(monkeypatch, tmp_path):
    """netsh, pf, iptables and ufw all take their sources from the one decision;
    none keeps a list of its own any more."""
    sources = ["10.8.0.0/24", "100.64.0.0/10"]
    monkeypatch.setattr(fw, "_sources", lambda narrow_lan=False: list(sources))
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(fw.subprocess, "run", run)
    monkeypatch.setattr(fw, "_windows_firewall_skip", False)
    assert fw._setup_firewall_windows(8443, 8001) is True
    remote = [a for argv in seen for a in argv if str(a).startswith("remoteip=")]
    assert remote and all(r == "remoteip=10.8.0.0/24,100.64.0.0/10,127.0.0.1" for r in remote)

    seen.clear()
    monkeypatch.setattr(fw, "_ufw_marker_path", lambda: tmp_path / "ufw_sources.json")
    assert fw._setup_firewall_linux_ufw(8443, 8001) is True
    froms = {argv[argv.index("from") + 1] for argv in seen if "from" in argv and "delete" not in argv}
    assert froms == set(sources)

    seen.clear()
    assert fw._setup_firewall_linux_iptables(8443, 8001) is True
    srcs = {argv[argv.index("-s") + 1] for argv in seen if "-s" in argv}
    assert srcs == set(sources), "RFC 1918 is gone under VPN only"

    written = {}

    class _Tmp:
        def __init__(self, *a, **k):
            self.name = str(tmp_path / "pf.conf")
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def write(self, text):
            written["rules"] = text

    monkeypatch.setattr(fw.tempfile, "NamedTemporaryFile", _Tmp)
    (tmp_path / "pf.conf").write_text("x")
    assert fw._setup_firewall_macos(8443, 8001) is True
    rules = written["rules"]
    for src in sources:
        assert f"from {src} to any port" in rules
    assert "192.168.0.0/16" not in rules and rules.rstrip().endswith("port {8443, 8001}")



def _ufw_wire(monkeypatch, tmp_path, sources):
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(fw.subprocess, "run", run)
    monkeypatch.setattr(fw, "_sources", lambda narrow_lan=False: list(sources))
    monkeypatch.setattr(fw, "_ufw_marker_path", lambda: tmp_path / "ufw_sources.json")
    return seen


def _ufw_deleted(seen):
    return {(int(a[a.index("port") + 1]), a[a.index("from") + 1]) for a in seen if "delete" in a}


def test_ufw_closes_a_network_that_is_no_longer_admitted(monkeypatch, tmp_path):
    """ufw only ever adds. Since the admitted networks change without a restart, a dropped
    network stayed open there; the setup now deletes, by the same rule spec, the allows it
    made last time and no longer wants. MUTATION: skip the deletes - red."""
    sources = ["10.8.0.0/24", "100.64.0.0/10"]
    seen = _ufw_wire(monkeypatch, tmp_path, sources)
    assert fw._setup_firewall_linux_ufw(8443, 8001) is True
    seen.clear()
    sources.remove("100.64.0.0/10")
    assert fw._setup_firewall_linux_ufw(8443, 8001) is True
    assert _ufw_deleted(seen) == {(8443, "100.64.0.0/10"), (8001, "100.64.0.0/10")}
    adds = {a[a.index("from") + 1] for a in seen if "allow" in a and "delete" not in a}
    assert adds == {"10.8.0.0/24"}


def test_the_ufw_setup_never_reloads_ufw(monkeypatch, tmp_path):
    """`ufw reload` on an enabled ufw is a stop and a start (ufw 0.36, frontend.py), and with
    MANAGE_BUILTINS=yes both flush every built-in chain, Docker's rules included; the allow
    and the delete already apply at once. MUTATION: put the reload back - red."""
    seen = _ufw_wire(monkeypatch, tmp_path, ["10.8.0.0/24"])
    assert fw._setup_firewall_linux_ufw(8443, 8001) is True
    assert any("allow" in argv and "delete" not in argv for argv in seen)
    assert not [argv for argv in seen if "reload" in argv]


def test_ufw_without_a_marker_assumes_the_old_rfc1918_allows(monkeypatch, tmp_path):
    """An install from before the admitted networks has RFC 1918 allowed on both ports and
    no marker. Switched to "VPN only", those allows must go too."""
    seen = _ufw_wire(monkeypatch, tmp_path, ["10.8.0.0/24"])
    assert fw._setup_firewall_linux_ufw(8443, 8001) is True
    assert _ufw_deleted(seen) == {(p, n) for p in (8443, 8001)
                                  for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")}


def test_unreadable_admitted_networks_apply_nothing_and_are_retried(monkeypatch):
    """An unreadable policy must not run a platform setup on an empty list nor be cached
    as "already attempted": the next setup has to try again once it is readable.
    MUTATION: fall back to an empty key and carry on - red."""
    calls = []
    state = {"broken": True}

    def sources(narrow_lan=False):
        if state["broken"]:
            raise OSError("config unreadable")
        return ["192.168.0.0/16"]

    monkeypatch.setattr(fw, "_setup_firewall_linux", lambda p, pf: calls.append(p) or True)
    _linux_only(monkeypatch)
    monkeypatch.setattr(fw, "_sources", sources)
    assert fw.setup_firewall(8443, 8001) is False
    assert calls == [] and fw._attempted_ports == {}
    state["broken"] = False
    assert fw.setup_firewall(8443, 8001) is True
    assert calls == [8443]



def test_a_failed_removal_fails_the_setup_so_the_marker_keeps_the_rule(monkeypatch, tmp_path):
    """A removal that fails must not vanish into `|| true`: the marker would forget the
    rule and its permanent copy would come back at the next boot. The removal asks first
    (a rule already gone is fine) and is chained into the elevation's result.
    MUTATION: swallow removal failures again - red."""
    rec = _RunRecorder()
    _wire_firewalld(monkeypatch, rec, tmp_path, sources=("192.168.2.0/24",))
    tailscale = ("public", fw._firewalld_rich_rule("100.64.0.0/10", 8443))
    fw._firewalld_marker_write([LAN_RULE, tailscale])
    assert fw._setup_firewall_linux_firewalld(8443, 8001) == "created"
    inner = rec.calls[0][-1]
    assert "|| true" not in inner
    assert "! firewall-cmd --zone=public --query-rich-rule=" in inner
    assert "! firewall-cmd --permanent --zone=public --query-rich-rule=" in inner

    def failing(argv, **kw):
        raise fw.subprocess.CalledProcessError(1, argv)

    fw._firewalld_marker_write([LAN_RULE, tailscale])
    monkeypatch.setattr(fw.subprocess, "run", failing)
    assert fw._setup_firewall_linux_firewalld(8443, 8001) is False
    assert fw._firewalld_marker_read() == sorted([LAN_RULE, tailscale])


# ── a process that cannot gain root does not try ──

_NO_NEW_PRIVS = "the no-new-privileges flag is set on this process, as systemd's NoNewPrivileges= sets it"


def _status(flag):
    return f"Name:\tpython3\nUid:\t1000\t1000\t1000\t1000\nNoNewPrivs:\t{flag}\nSeccomp:\t0\n"


@pytest.mark.parametrize("linux, euid, status, uid_map, expected", [
    (True, 1000, _status(1), "         0          0 4294967295\n", "no-new-privileges"),
    (True, 1000, _status(0), "      1000       1000          1\n", "user namespace"),
    (True, 1000, _status(0), "", "user namespace"),
    (True, 1000, _status(0), "         0          0 4294967295\n", None),
    (True, 1000, _status(0), "         0       1000      65536\n", None),
    (True, 1000, None, None, None),
    (True, 1000, _status(0), "not a map\n", None),
    (False, 1000, _status(1), "      1000       1000          1\n", None),
    (True, 0, _status(1), "         0          0 4294967295\n", None),
], ids=["no-new-privs", "namespace-without-root", "empty-map", "plain-process",
        "rootless-container", "files-missing", "map-unreadable", "not-linux",
        "root-with-no-new-privs"])
def test_elevation_unavailable_reads_the_flag_and_the_namespace(monkeypatch, tmp_path, linux, euid,
                                                                status, uid_map, expected):
    """Measured with `systemd-run --user`: the no-new-privileges flag, and a user namespace
    that does not map root (what PrivateTmp= or ProtectSystem= alone give a user service),
    each make sudo and pkexec fail for a caller that is not root. A caller that IS root (a
    root-run unit or container with the flag) needs no setuid helper, and sudo checks the flag
    only when it is not running as root, so it is never refused. Anything unknown must NOT
    refuse, or a desktop that can elevate would lose its password dialog. MUTATIONS: drop the
    NoNewPrivs branch; drop the uid_map branch; treat an unreadable file as refusing; drop the
    non-Linux return; drop the euid check - red."""
    monkeypatch.setattr(fw.Platform, "is_linux", lambda *a: linux)
    # raising=False: Windows has no geteuid, and this test runs there too.
    monkeypatch.setattr(fw.os, "geteuid", lambda: euid, raising=False)
    status_path, uid_map_path = tmp_path / "status", tmp_path / "uid_map"
    if status is not None:
        status_path.write_bytes(status.encode())
    if uid_map is not None:
        uid_map_path.write_bytes(uid_map.encode())
    reason = fw.elevation_unavailable(status_path=str(status_path), uid_map_path=str(uid_map_path))
    if expected is None:
        assert reason is None
    else:
        assert reason and expected in reason


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl and /proc are Linux")
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="a root child needs no elevation and is rightly not refused")
def test_a_process_with_no_new_privileges_is_seen_as_such():
    """The defaults read this very process: a child that set the flag itself (what systemd's
    NoNewPrivileges= does before exec) is refused. MUTATION: default status_path to
    /proc/self/stat - red."""
    repo = str(Path(__file__).resolve().parents[1])
    env = dict(os.environ)
    env["PYTHONPATH"] = repo + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    code = ("import ctypes\n"
            "libc = ctypes.CDLL(None, use_errno=True)\n"
            "assert libc.prctl(38, 1, 0, 0, 0) == 0, 'PR_SET_NO_NEW_PRIVS refused'\n"
            "from vaf.network.firewall import elevation_unavailable\n"
            "print(elevation_unavailable() or 'NONE')\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=repo, env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "no-new-privileges" in out.stdout, out.stdout


def test_no_sudo_is_spawned_where_root_cannot_be_gained(monkeypatch, tmp_path):
    """The server's service: no firewalld, ufw installed. Every ufw and iptables step is a
    `sudo -n`, which cannot work here and only put sudo's refusal into the journal at every
    start. MUTATION: remove the gate in _setup_firewall_linux (the ufw branch runs sudo) - red."""
    rec = _RunRecorder()
    monkeypatch.setattr(fw, "_firewalld_running", lambda: False)
    monkeypatch.setattr(fw, "elevation_unavailable", lambda: _NO_NEW_PRIVS)
    monkeypatch.setattr(fw, "_sources", lambda narrow_lan=False: ["192.168.2.0/24"])
    monkeypatch.setattr(fw, "_ufw_marker_path", lambda: tmp_path / "ufw_sources.json")
    monkeypatch.setattr(fw.subprocess, "run", rec)   # `which ufw` answers 0: ufw "exists"
    assert fw._setup_firewall_linux(8443, 8001) is False
    spawned = [a for argv in rec.calls for a in argv]
    assert "sudo" not in spawned and "pkexec" not in spawned, rec.calls


def test_a_marker_miss_does_not_elevate_without_root(monkeypatch, tmp_path):
    """firewalld with a change to make, in a process that cannot gain root: no elevation,
    and the marker keeps what it said (nothing was changed). MUTATION: remove the gate in
    _setup_firewall_linux_firewalld (it elevates and rewrites the marker) - red."""
    rec = _RunRecorder()
    _wire_firewalld(monkeypatch, rec, tmp_path)
    monkeypatch.setattr(fw, "elevation_unavailable", lambda: _NO_NEW_PRIVS)
    fw._firewalld_marker_write([("public", fw._firewalld_rich_rule("192.168.2.0/24", 9999))])
    before = (tmp_path / "firewalld_lan.json").read_bytes()
    assert fw._setup_firewall_linux_firewalld(8443, 8001) is False
    assert all(argv[0] != "fake-elevate" for argv in rec.calls), rec.calls
    assert (tmp_path / "firewalld_lan.json").read_bytes() == before


def test_a_marker_hit_still_reports_present_without_root(monkeypatch, tmp_path):
    """Rules already in place need no root, so the service still says so.
    MUTATION: move the firewalld gate above the marker check - red."""
    rec = _RunRecorder()
    _wire_firewalld(monkeypatch, rec, tmp_path)
    monkeypatch.setattr(fw, "elevation_unavailable", lambda: _NO_NEW_PRIVS)
    fw._firewalld_marker_write([LAN_RULE])
    assert fw._setup_firewall_linux_firewalld(8443, 8001) == "present"
    assert rec.calls == []


def _apply_wired(monkeypatch, reason):
    from vaf.core.config import Config
    import vaf.network.binding as binding
    cleanups = []
    monkeypatch.setattr(Config, "get_bool", classmethod(lambda cls, key, default=False: True))
    monkeypatch.setattr(binding, "resolve_lan_access_ports", lambda wait_for_proxy=False: (8443, 8001))
    monkeypatch.setattr(fw, "setup_firewall", lambda port, port_frontend: False)
    monkeypatch.setattr(fw, "register_cleanup_on_exit", lambda: cleanups.append(1))
    monkeypatch.setattr(fw, "elevation_unavailable", lambda: reason)
    monkeypatch.setattr(fw, "_no_root_reported", set())
    return cleanups


def test_apply_says_once_why_the_firewall_was_not_changed(monkeypatch):
    """The start and every re-apply in the server's service: one line per process and port,
    naming the reason and where the step lives, never "passwordless sudo" (it would not help)
    nor an "in-app firewall step" (there is none). MUTATIONS: drop the once-guard (two lines);
    restore the old text - red."""
    cleanups = _apply_wired(monkeypatch, _NO_NEW_PRIVS)
    lines = []
    assert fw.apply_lan_firewall(log=lines.append) is False
    assert fw.apply_lan_firewall(log=lines.append) is False
    assert len(lines) == 1, lines
    line = lines[0]
    assert "8443" in line and _NO_NEW_PRIVS in line and "vaf server provision" in line
    assert "passwordless sudo" not in line and "in-app" not in line
    assert cleanups == []

    cleanups = _apply_wired(monkeypatch, None)
    lines = []
    assert fw.apply_lan_firewall(log=lines.append) is False
    assert len(lines) == 1 and "in-app" not in lines[0], lines
    assert cleanups == []


# --------------------------------------------------------------------------- ufw that is off

# /etc/ufw/ufw.conf as Ubuntu 24.04.5 ships it (measured on a test server): ufw is off.
UBUNTU_DEFAULT_UFW_CONF = """# /etc/ufw/ufw.conf
#

# Set to yes to start on boot. If setting this remotely, be sure to add a rule
# to allow your remote connection before starting ufw. Eg: 'ufw allow 22/tcp'
ENABLED=no

# Please use the 'ufw' command to set the loglevel. Eg: 'ufw logging medium'.
# See 'man ufw' for details.
LOGLEVEL=low
"""

# /etc/default/ufw as Ubuntu 24.04 ships it (measured on a test server), its long comments
# shortened: plain assignments only, no ENABLED line.
UBUNTU_DEFAULT_UFW_DEFAULTS = """# /etc/default/ufw
#

# Set to yes to apply rules to support IPv6 (no means only IPv6 on loopback
# accepted).
IPV6=yes

DEFAULT_INPUT_POLICY="DROP"
DEFAULT_OUTPUT_POLICY="ACCEPT"
DEFAULT_FORWARD_POLICY="DROP"
DEFAULT_APPLICATION_POLICY="SKIP"

# By default, ufw only touches its own chains. Set this to 'yes' to have ufw
# manage the built-in chains too.
MANAGE_BUILTINS=no

IPT_SYSCTL=/etc/ufw/sysctl.conf
IPT_MODULES=""
"""


@pytest.mark.parametrize("text, expected", [
    (UBUNTU_DEFAULT_UFW_CONF, False),
    (UBUNTU_DEFAULT_UFW_CONF.replace("ENABLED=no", "ENABLED=yes"), True),
    ("LOGLEVEL=low\n", False),
    ("# ENABLED=yes\nENABLED=no\n", False),
    # Each row below as dash reads it when ufw-init sources the file (measured):
    ("ENABLED=no\nenabled=yes\n", False),        # names are case-sensitive
    ("ENABLED=yes#note\n", False),                # the value is `yes#note`
    ("ENABLED=yes # note\n", True),               # `#` after a blank opens a comment
    ('  ENABLED="yes"\n', True),
    ("ENABLED='YES'\n", True),                    # ufw's start accepts yes and YES
    ("ENABLED=Yes\n", False),
    ("ENABLED=yes\r\n", False),                   # the carriage return is in the value
    ('ENABLED="ye"s\n', True),                    # adjacent pieces join
    ("A=1 ENABLED=yes\n", True),                  # an assignment-only line sets both
    # Lines outside plain assignments: unknown, never on.
    ("ENABLED=yes true\n", None),                 # dash: set for `true` only
    ('ENABLED = "YES"\n', None),                  # dash: a command named ENABLED
    ("ENABLED=$ON\n", None),
    ("if false; then\nENABLED=yes\nfi\n", None),
    ("ENABLED=no\nexport LOGLEVEL\n", None),
    ('ENABLED=yes\nX="$((ENABLED=0))"\n', None),  # dash: the expansion sets ENABLED=0
    ("Ä=1\nENABLED=yes\n", None),            # dash: `Ä=1` is a command, the start aborts
])
def test_ufw_enabled_reads_the_config_file(tmp_path, text, expected):
    """systemd says `active` and `enabled` for ufw's unit while ufw itself is off, so only
    ufw's own switch answers, and by the shell's rules, since ufw-init sources the file.
    MUTATION: True for any ENABLED= line; False for a missing file; keys case-insensitive;
    `#` a comment anywhere; lowercase the value; split at a carriage return; read on past a
    line outside plain assignments; read a double-quoted `$` literally; take a non-ASCII
    name for an assignment - red."""
    defaults, conf = tmp_path / "ufw", tmp_path / "ufw.conf"
    defaults.write_bytes(UBUNTU_DEFAULT_UFW_DEFAULTS.encode("utf-8"))
    conf.write_bytes(text.encode("utf-8"))
    assert fw.ufw_enabled((str(defaults), str(conf))) is expected


def test_ufw_enabled_is_unknown_without_the_file(tmp_path):
    defaults = tmp_path / "ufw"
    defaults.write_bytes(UBUNTU_DEFAULT_UFW_DEFAULTS.encode("utf-8"))
    assert fw.ufw_enabled((str(defaults), str(tmp_path / "missing.conf"))) is None


@pytest.mark.parametrize("defaults, conf, expected", [
    (UBUNTU_DEFAULT_UFW_DEFAULTS, "ENABLED=yes\n", True),
    (UBUNTU_DEFAULT_UFW_DEFAULTS, "ENABLED=no\n", False),
    # Each row below as dash reads it in ufw-init's order (ufw.conf, /etc/default/ufw,
    # ufw.conf) under `set -e` (measured):
    ("IPV6 = yes\n", "ENABLED=yes\n", None),                 # a command: exit 127
    ('DEFAULT_FORWARD_POLICY = "ACCEPT"\n', "ENABLED=yes\n", None),
    (UBUNTU_DEFAULT_UFW_DEFAULTS.replace("\n", "\r\n"), "ENABLED=yes\n", None),  # `\r`: 127
    ("Ä=1\n", "ENABLED=yes\n", None),
    ("ENABLED=yes\n", "LOGLEVEL=low\n", True),               # the default file alone: on
    ("ENABLED=yes\n", "ENABLED=no\n", False),                # ufw.conf is read last
    ("", "ENABLED=yes\n", None),                              # `[ -s ]` fails: aborted
    (None, "ENABLED=yes\n", None),
])
def test_ufw_enabled_reads_the_default_file_first(monkeypatch, tmp_path, defaults, conf,
                                                  expected):
    """ufw-init sources /etc/default/ufw between its two reads of ufw.conf, under `set -e`: a
    line the shell fails on there aborts the start and ufw stays off, while ufw.conf alone
    said on, and so did VAF. MUTATION: read ufw.conf alone; take an empty file - red."""
    defaults_path, conf_path = tmp_path / "ufw", tmp_path / "ufw.conf"
    if defaults is not None:
        defaults_path.write_bytes(defaults.encode("utf-8"))
    conf_path.write_bytes(conf.encode("utf-8"))
    monkeypatch.setattr(fw, "UFW_DEFAULTS", str(defaults_path))
    monkeypatch.setattr(fw, "UFW_CONF", str(conf_path))
    assert fw.ufw_enabled() is expected


def _engines(monkeypatch, *, firewalld=False, ufw=None, linux=True, unit_inactive=False):
    """ufw: None when it is not installed, else what its switch says (True or False);
    unit_inactive: what systemd says of ufw's unit."""
    monkeypatch.setattr(fw.Platform, "is_linux", lambda *a: linux)
    monkeypatch.setattr(fw, "_firewalld_running", lambda: firewalld)
    monkeypatch.setattr(fw, "_ufw_installed", lambda: ufw is not None)
    monkeypatch.setattr(fw, "ufw_enabled", lambda paths=None: ufw)
    monkeypatch.setattr(fw, "_ufw_unit_inactive", lambda: unit_inactive)


def test_host_firewall_is_one_answer(monkeypatch):
    """MUTATION: leave out the ufw_enabled read (the binary alone decided) - red."""
    _engines(monkeypatch, firewalld=True, ufw=False)
    assert fw.host_firewall() == "firewalld"
    _engines(monkeypatch, ufw=False)
    assert fw.host_firewall() == "ufw_off"
    _engines(monkeypatch, ufw=True)
    assert fw.host_firewall() == "ufw"
    monkeypatch.setattr(fw, "_ufw_installed", lambda: True)
    monkeypatch.setattr(fw, "ufw_enabled", lambda paths=None: None)
    assert fw.host_firewall() == "ufw", "an unreadable switch keeps the old behaviour"
    _engines(monkeypatch)
    assert fw.host_firewall() == "iptables"
    _engines(monkeypatch, ufw=False, linux=False)
    assert fw.host_firewall() is None


@pytest.mark.parametrize("engine, switch, unit, why", [
    ("ufw", None, "active", "/etc/default/ufw or /etc/ufw/ufw.conf could not be read"),
    ("ufw", True, "active", None),
    # ufw-init's start aborted (a oneshot unit then says `failed`, measured): which of
    # ufw's rules are loaded is not known.
    ("ufw", True, "failed", "systemd reports its unit as failed"),
    # Any other state but active says no more: starting, or systemd cannot tell.
    ("ufw", True, "activating", "systemd reports its unit as activating"),
    ("ufw", True, "unknown", "systemd reports its unit as unknown"),
    # No systemd at all (no systemctl, or no answer): not counted, or every such host would be.
    ("ufw", True, "", None),
    # The caller read "ufw" before this read: the unit stopped (its stop flushes ufw's rules)
    # or the switch went off since.
    ("ufw", True, "inactive", "systemd reports its unit as inactive"),
    ("ufw", False, "active", "its switch reads as off"),
    ("ufw_off", None, "failed", None),
    ("firewalld", None, "failed", None),
    (None, None, "failed", None),
])
def test_an_uncertain_switch_still_counts_as_ufw_but_is_never_confirmed(monkeypatch, engine,
                                                                        switch, unit, why):
    """The allow rule is harmless on a ufw that is off and needed on one that is on, so an
    uncertain switch keeps "ufw"; what reports the result asks this instead of claiming a
    guard, and says why. MUTATION: answer None always; ignore the engine; count only a
    failed unit; count an empty answer too; confirm a unit read as inactive or a switch read
    as off; read the host firewall again when the caller passed it - red."""
    monkeypatch.setattr(fw, "UFW_DEFAULTS", "/etc/default/ufw")
    monkeypatch.setattr(fw, "UFW_CONF", "/etc/ufw/ufw.conf")
    monkeypatch.setattr(fw, "host_firewall", lambda: engine)
    monkeypatch.setattr(fw, "ufw_enabled", lambda paths=None: switch)
    monkeypatch.setattr(fw, "_ufw_unit_state", lambda: unit)
    monkeypatch.setattr(fw, "_ufw_command_reads_on", lambda paths=None: True)
    reason = fw.ufw_switch_unconfirmed()
    if why is None:
        assert reason is None
    else:
        assert why in reason

    def read_again():
        raise AssertionError("the caller already read the host firewall")

    monkeypatch.setattr(fw, "host_firewall", read_again)
    if engine is not None:
        assert fw.ufw_switch_unconfirmed(engine) == reason


def test_a_switch_ufws_own_command_cannot_read_is_unconfirmed(monkeypatch):
    """The shell read the files a moment earlier, but whether `ufw allow` applies the rule at
    once depends on the command's own read, and that one failed. MUTATION: count only a
    command read of off - red."""
    monkeypatch.setattr(fw, "UFW_DEFAULTS", "/etc/default/ufw")
    monkeypatch.setattr(fw, "UFW_CONF", "/etc/ufw/ufw.conf")
    monkeypatch.setattr(fw, "ufw_enabled", lambda paths=None: True)
    monkeypatch.setattr(fw, "_ufw_command_reads_on", lambda paths=None: None)
    monkeypatch.setattr(fw, "_ufw_unit_state", lambda: "active")
    assert "could not be read with certainty" in fw.ufw_switch_unconfirmed("ufw")


# Each row as ufw 0.36's own `_get_defaults` lines read it (measured with them on a test
# server); every switch below is on for the shell, so `ufw_enabled()` says True.
@pytest.mark.parametrize("defaults, conf, command_on", [
    (UBUNTU_DEFAULT_UFW_DEFAULTS, "ENABLED=yes\n", True),
    (UBUNTU_DEFAULT_UFW_DEFAULTS, 'ENABLED="yes"\n', True),
    (UBUNTU_DEFAULT_UFW_DEFAULTS, "ENABLED=YES\n", True),         # lowercased
    (UBUNTU_DEFAULT_UFW_DEFAULTS, "ENABLED=yes \n", True),        # the line is stripped
    ("ENABLED=yes\n", "LOGLEVEL=low\n", True),
    (UBUNTU_DEFAULT_UFW_DEFAULTS, "ENABLED=yes # note\n", False),  # the value `yes # note`
    (UBUNTU_DEFAULT_UFW_DEFAULTS, "ENABLED='yes'\n", False),      # no match: no ENABLED
    (UBUNTU_DEFAULT_UFW_DEFAULTS, "  ENABLED=yes\n", False),      # no match at a blank
    (UBUNTU_DEFAULT_UFW_DEFAULTS, 'ENABLED="ye"s\n', False),      # the value `ye"s`
    (UBUNTU_DEFAULT_UFW_DEFAULTS, "A=1 ENABLED=yes\n", False),    # read as A
    (UBUNTU_DEFAULT_UFW_DEFAULTS, "ENABLED=yes\nenabled=no\n", False),  # names lowercased
])
def test_a_switch_line_ufws_own_command_reads_as_off_is_unconfirmed(monkeypatch, tmp_path,
                                                                    defaults, conf, command_on):
    """ufw-init loads the rules at boot by the shell's reading, but `ufw allow` decides by the
    command's own pattern whether to apply a rule at once: where that reads off, the rule is
    only recorded until ufw next starts, while provision said "Firewall opened". MUTATION:
    leave the command's reading out; read it without lowercasing; take a line with a blank
    before the name - red."""
    defaults_path, conf_path = tmp_path / "ufw", tmp_path / "ufw.conf"
    defaults_path.write_bytes(defaults.encode("utf-8"))
    conf_path.write_bytes(conf.encode("utf-8"))
    monkeypatch.setattr(fw, "UFW_DEFAULTS", str(defaults_path))
    monkeypatch.setattr(fw, "UFW_CONF", str(conf_path))
    monkeypatch.setattr(fw, "_ufw_unit_state", lambda: "active")
    assert fw.ufw_enabled() is True
    assert fw._ufw_command_reads_on() is command_on
    reason = fw.ufw_switch_unconfirmed("ufw")
    if command_on:
        assert reason is None
    else:
        assert "ufw's own command reads its ENABLED line as off" in reason
        assert "`ENABLED=yes`" in reason


def test_a_ufw_whose_unit_is_stopped_counts_as_off(monkeypatch):
    """`systemctl disable --now ufw` runs `ufw-init stop`, which flushes the rules, while
    ufw.conf keeps `ENABLED=yes`: provision said "Firewall opened" and the doctor was silent.
    MUTATION: leave the unit's state out of the answer - red."""
    _engines(monkeypatch, ufw=True, unit_inactive=True)
    assert fw.host_firewall() == "ufw_off"
    _engines(monkeypatch, ufw=True, unit_inactive=False)
    assert fw.host_firewall() == "ufw"


class _Done:
    def __init__(self, stdout):
        self.stdout, self.returncode = stdout, 0 if stdout == b"active\n" else 3


@pytest.mark.parametrize("answer, inactive, state", [
    (b"inactive\n", True, "inactive"),
    (b"active\n", False, "active"),    # proves nothing: Ubuntu says it while ufw is off
    (b"failed\n", False, "failed"),    # not off: unconfirmed (ufw_switch_unconfirmed)
    (b"unknown\n", False, "unknown"),
    (b"activating\n", False, "activating"),
    (b"\xff\xfe\n", False, "unknown"),     # not one of systemd's words: never named as one
    (b"", False, ""),                     # systemctl answered nothing: no systemd
    (FileNotFoundError("systemctl"), False, ""),
])
def test_only_systemds_inactive_is_trusted(monkeypatch, answer, inactive, state):
    """MUTATION: read the exit code (3 for every state that is not active); pass an answer
    that is not one plain word through - red."""
    def run(argv, **kw):
        assert argv == ["systemctl", "is-active", "ufw"]
        if isinstance(answer, Exception):
            raise answer
        return _Done(answer)

    monkeypatch.setattr(fw.subprocess, "run", run)
    assert fw._ufw_unit_inactive() is inactive
    assert fw._ufw_unit_state() == state


# The state Ubuntu ships ufw in: the switch off (ENABLED=no), the unit running, and ufw
# touching only its own chains (MANAGE_BUILTINS=no).
_SHIPPED = {"switch_on": False, "unit_stopped": False, "manages_builtins": False}


def test_the_ufw_advice_names_the_ssh_port_and_the_vpn_port(monkeypatch):
    """`ufw allow OpenSSH` opens port 22 only, and nothing allowed the VPN's port: following
    the advice locked out an SSH on another port and cut the VPN. A VPN other than WireGuard
    (Tailscale, NetBird) is pointed at its own port. MUTATION: always name OpenSSH; drop the
    VPN port - red."""
    on_22 = fw.ufw_off_advice(22, **_SHIPPED)
    assert "`sudo ufw allow OpenSSH`" in on_22 and "<port>" not in on_22
    on_2222 = fw.ufw_off_advice(2222, **_SHIPPED)
    assert "`sudo ufw allow 2222/tcp`" in on_2222 and "OpenSSH" not in on_2222
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    unknown = fw.ufw_off_advice(**_SHIPPED)
    assert "`sudo ufw allow OpenSSH` when it listens on port 22" in unknown
    assert "`sudo ufw allow <port>/tcp`" in unknown
    for text in (on_22, on_2222, unknown):
        assert text.index("ufw allow") < text.index("51820/udp") < text.index("sudo ufw enable")
        assert "another VPN: its own port" in text


def test_the_ufw_advice_reads_the_port_this_session_came_in_on(monkeypatch):
    monkeypatch.setenv("SSH_CONNECTION", "192.168.1.5 50122 192.168.1.10 2222")
    assert fw.ssh_session_port() == 2222
    assert "`sudo ufw allow 2222/tcp`" in fw.ufw_off_advice(**_SHIPPED)
    monkeypatch.setenv("SSH_CONNECTION", "garbage")
    assert fw.ssh_session_port() is None
    monkeypatch.delenv("SSH_CONNECTION")
    assert fw.ssh_session_port() is None


@pytest.mark.parametrize("port", ["\u00b2", "\u0663", "99999", "0", "+22"])
def test_a_hand_set_session_port_names_no_port_and_raises_nothing(monkeypatch, port):
    """sshd writes ASCII digits; a variable set by hand may not. A superscript two passes
    `str.isdigit` and then made `int()` raise inside provision's message. MUTATION: go back
    to `isdigit()` then `int()` - red (first row)."""
    monkeypatch.setenv("SSH_CONNECTION", f"192.168.1.5 50122 192.168.1.10 {port}")
    assert fw.ssh_session_port() is None
    assert "`sudo ufw allow OpenSSH` when it listens on port 22" in fw.ufw_off_advice(**_SHIPPED)


def test_a_stopped_unit_is_enabled_and_started_after_ufw_enable():
    """`ufw enable` writes ENABLED=yes and runs `ufw-init start` itself; it never touches the
    systemd unit, so after `systemctl disable --now ufw` the rules it loads are gone at the
    next boot and systemd keeps saying `inactive`. MUTATION: leave the unit step out - red;
    name it for a running unit too - red."""
    for switch_on in (False, True):
        text = fw.ufw_off_advice(22, switch_on=switch_on, unit_stopped=True,
                                 manages_builtins=False)
        assert (text.index("51820/udp") < text.index("`sudo ufw enable`")
                < text.index("`sudo systemctl enable --now ufw`")
                < text.index("vaf server provision"))
    assert "systemctl" not in fw.ufw_off_advice(22, **_SHIPPED)


def test_with_the_switch_on_and_the_unit_stopped_the_switch_goes_off_first():
    """That pair is what `systemctl disable --now ufw` leaves: ufw.conf still says ENABLED=yes,
    so `ufw allow` writes the IPv4 rule, fails reloading the chains the stop deleted and never
    writes the IPv6 one. `sudo ufw disable` first records the allows whole. MUTATION: drop the
    disable step - red; say it whenever the unit is stopped - red."""
    text = fw.ufw_off_advice(22, switch_on=True, unit_stopped=True, manages_builtins=False)
    assert (text.index("`sudo ufw disable`") < text.index("`sudo ufw allow OpenSSH`")
            < text.index("51820/udp") < text.index("`sudo ufw enable`"))
    assert "ufw disable" not in fw.ufw_off_advice(22, switch_on=False, unit_stopped=True,
                                                  manages_builtins=False)
    assert "ufw disable" not in fw.ufw_off_advice(22, **_SHIPPED)


def test_the_advice_reads_ufws_state_the_way_host_firewall_does(monkeypatch):
    """Called without a state, as provision and the doctor call it, the advice asks the same
    two readers `host_firewall()` asks. MUTATION: default either state to off - red."""
    monkeypatch.setattr(fw, "ufw_manages_builtins", lambda paths=None: False)
    monkeypatch.setattr(fw, "ufw_enabled", lambda paths=None: True)
    monkeypatch.setattr(fw, "_ufw_unit_inactive", lambda: True)
    text = fw.ufw_off_advice(22)
    assert "`sudo ufw disable`" in text and "`sudo systemctl enable --now ufw`" in text
    monkeypatch.setattr(fw, "ufw_enabled", lambda paths=None: None)
    monkeypatch.setattr(fw, "_ufw_unit_inactive", lambda: False)
    text = fw.ufw_off_advice(22)
    assert "ufw disable" not in text and "systemctl" not in text


def test_an_unreadable_switch_with_a_stopped_unit_still_gets_the_disable_step(monkeypatch):
    """ufw.conf unreadable means the switch may be on, and then `ufw allow` fails before it
    writes the IPv6 rule; `ufw disable` is harmless in every state. MUTATION: read an
    unreadable switch as off (`is True`) - red."""
    monkeypatch.setattr(fw, "ufw_manages_builtins", lambda paths=None: False)
    monkeypatch.setattr(fw, "ufw_enabled", lambda paths=None: None)
    monkeypatch.setattr(fw, "_ufw_unit_inactive", lambda: True)
    text = fw.ufw_off_advice(22)
    assert text.index("`sudo ufw disable`") < text.index("`sudo ufw allow OpenSSH`")
    assert "`sudo systemctl enable --now ufw`" in text


@pytest.mark.parametrize("defaults, conf, expected", [
    ("MANAGE_BUILTINS=no\n", "ENABLED=no\n", False),            # what Ubuntu ships
    ("MANAGE_BUILTINS=yes\n", "ENABLED=no\n", True),
    ('MANAGE_BUILTINS="yes"  # managed\n', "ENABLED=no\n", True),
    ("MANAGE_BUILTINS=YES\n", "ENABLED=no\n", False),           # ufw-init compares "yes"
    ("MANAGE_BUILTINS=no\n", "MANAGE_BUILTINS=yes\n", True),    # ufw.conf is sourced last
    ("IPV6=yes\n", "ENABLED=no\n", False),
    (None, "ENABLED=no\n", None),
    ("MANAGE_BUILTINS=yes#x\n", "ENABLED=no\n", False),       # dash: the value is `yes#x`
    ("manage_builtins=yes\n", "ENABLED=no\n", False),         # another variable
    ("MANAGE_BUILTINS=no\n", "MANAGE_BUILTINS=$X\n", None),
    ("MANAGE_BUILTINS=no\nif true; then\n", "MANAGE_BUILTINS=no\n", None),
])
def test_ufw_manages_builtins_is_read_the_way_ufw_init_reads_it(monkeypatch, tmp_path, defaults,
                                                                conf, expected):
    """MUTATION: tolerate case in the value; read /etc/default/ufw alone - red."""
    defaults_path, conf_path = tmp_path / "ufw", tmp_path / "ufw.conf"
    if defaults is not None:
        defaults_path.write_bytes(defaults.encode())
    conf_path.write_bytes(conf.encode())
    monkeypatch.setattr(fw, "UFW_DEFAULTS", str(defaults_path))
    monkeypatch.setattr(fw, "UFW_CONF", str(conf_path))
    assert fw.ufw_manages_builtins() is expected


@pytest.mark.parametrize("manages", [True, False, None])
@pytest.mark.parametrize("disable_first", [True, False])
def test_the_advice_says_what_a_ufw_that_manages_the_builtins_does_to_docker(monkeypatch, manages,
                                                                              disable_first):
    """With MANAGE_BUILTINS=yes ufw's stop and its start both run `flush_builtins`, which also
    removes Docker's rules, and ufw puts nothing back. Unreadable defaults get the same
    sentence as a condition; Ubuntu's `no` gets nothing. MUTATION: name it for `yes` only
    - red (None rows); never - red (True rows); always - red (False rows)."""
    monkeypatch.setattr(fw, "ufw_manages_builtins", lambda paths=None: manages)
    text = fw.ufw_off_advice(22, switch_on=disable_first, unit_stopped=disable_first)
    if manages is False:
        assert "docker" not in text.lower() and "MANAGE_BUILTINS" not in text
        return
    assert text.index("`sudo ufw enable`") < text.index("`sudo systemctl restart docker`")
    assert "Docker's included" in text
    if manages:
        assert "This ufw manages the built-in chains (`MANAGE_BUILTINS=yes`" in text
    else:
        # Unknown may come from either file, and ufw.conf, read last, decides.
        assert "If `MANAGE_BUILTINS` ends up `yes` (set in /etc/default/ufw, or in" in text
        assert "/etc/ufw/ufw.conf, which is read last and wins)" in text
    flushes = ("`ufw disable` and `ufw enable` also flush" if disable_first
               else "`ufw enable` also flushes")
    assert flushes in text


def test_ufw_counts_as_installed_by_its_config_file(monkeypatch, tmp_path):
    """The service's PATH may lack /usr/sbin, where Ubuntu puts ufw; the file says it too."""
    monkeypatch.setattr(fw.subprocess, "run",
                        lambda argv, **kw: type("R", (), {"returncode": 1})())
    monkeypatch.setattr(fw, "UFW_CONF", str(tmp_path / "ufw.conf"))
    assert fw._ufw_installed() is False
    (tmp_path / "ufw.conf").write_bytes(b"ENABLED=no\n")
    assert fw._ufw_installed() is True


def test_a_switched_off_ufw_is_not_reported_as_protecting(monkeypatch, tmp_path):
    """Rules added to a ufw that is off do nothing, and the port was reported as opened.
    MUTATIONS: take the ufw path whenever it is installed; let apply_lan_firewall treat
    "inactive" as a success (the exit cleanup is registered, "created" is logged) - red."""
    rec = _RunRecorder()
    monkeypatch.setattr(fw.subprocess, "run", rec)
    monkeypatch.setattr(fw, "elevation_unavailable", lambda: None)
    monkeypatch.setattr(fw, "_sources", lambda narrow_lan=False: ["192.168.2.0/24"])
    monkeypatch.setattr(fw, "_ufw_marker_path", lambda: tmp_path / "ufw_sources.json")
    _engines(monkeypatch, ufw=False)
    assert fw._setup_firewall_linux(8443, 8001) == fw.FIREWALL_INACTIVE
    assert rec.calls == []

    _engines(monkeypatch, ufw=True)
    assert fw._setup_firewall_linux(8443, 8001) is True
    assert any(argv[:4] == ["sudo", "-n", "ufw", "allow"] for argv in rec.calls)

    from vaf.core.config import Config
    import vaf.network.binding as binding
    cleanups = []
    monkeypatch.setattr(Config, "get_bool", classmethod(lambda cls, key, default=False: True))
    monkeypatch.setattr(binding, "resolve_lan_access_ports",
                        lambda wait_for_proxy=False: (8443, 8001))
    monkeypatch.setattr(fw, "setup_firewall", lambda port, port_frontend: fw.FIREWALL_INACTIVE)
    monkeypatch.setattr(fw, "register_cleanup_on_exit", lambda: cleanups.append(1))
    lines = []
    assert fw.apply_lan_firewall(log=lines.append) == fw.FIREWALL_INACTIVE
    assert len(lines) == 1
    assert "8443" in lines[0] and "switched off" in lines[0] and "created" not in lines[0]
    assert cleanups == []
