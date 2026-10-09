# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""firewalld LAN opening: the rule must be scoped to the LAN subnet (RFC1918), and elevation must use
pkexec on the desktop (native password dialog) but never an interactive sudo prompt headless."""
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
    assert fw._setup_firewall_linux_ufw(8443, 8001) is True
    froms = {argv[argv.index("from") + 1] for argv in seen if "from" in argv}
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
