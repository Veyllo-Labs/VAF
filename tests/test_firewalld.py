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


def _wire_firewalld(monkeypatch, recorder, tmp_path):
    monkeypatch.setattr(fw, "_lan_subnet_cidr", lambda: "192.168.2.0/24")
    monkeypatch.setattr(fw, "_firewalld_zone", lambda: "public")
    monkeypatch.setattr(fw, "_elevation_argv", lambda: ["fake-elevate"])
    monkeypatch.setattr(fw, "_firewalld_marker_path", lambda: tmp_path / "firewalld_lan.json")
    monkeypatch.setattr(fw.subprocess, "run", recorder)


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
    fw._firewalld_marker_write("public", fw._firewalld_rich_rule("192.168.2.0/24", 8443))
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
    assert fw._firewalld_marker_matches("public", fw._firewalld_rich_rule("192.168.2.0/24", 8443))


def test_stale_marker_for_other_port_still_elevates(monkeypatch, tmp_path):
    """A marker for yesterday's port must not silence today's setup - the
    failure direction of the marker is a CLOSED port, never a skipped opening."""
    rec = _RunRecorder()
    _wire_firewalld(monkeypatch, rec, tmp_path)
    fw._firewalld_marker_write("public", fw._firewalld_rich_rule("192.168.2.0/24", 9999))
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
