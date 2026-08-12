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
    """Scripted subprocess.run: query-rich-rule answers from a list, elevation
    calls are recorded. Everything returns rc=0 unless scripted otherwise."""
    def __init__(self, query_answers):
        self.query_answers = list(query_answers)
        self.elevations = []

    def __call__(self, argv, **kw):
        rc = 0
        if "--query-rich-rule" in argv:
            rc = 0 if self.query_answers.pop(0) else 1
        elif argv and argv[0] == "fake-elevate":
            self.elevations.append(argv)
        out = "yes" if rc == 0 else "no"
        return type("R", (), {"returncode": rc, "stdout": out, "stderr": ""})()


def _wire_firewalld(monkeypatch, recorder):
    monkeypatch.setattr(fw, "_lan_subnet_cidr", lambda: "192.168.2.0/24")
    monkeypatch.setattr(fw, "_firewalld_zone", lambda: "public")
    monkeypatch.setattr(fw, "_elevation_argv", lambda: ["fake-elevate"])
    monkeypatch.setattr(fw.time, "sleep", lambda s: None)
    monkeypatch.setattr(fw.subprocess, "run", recorder)


def test_present_rule_never_elevates(monkeypatch):
    """The idempotence promise (NETWORK_FEATURES.md): rule already active means
    NO password dialog. Mutation: skip the presence check - red."""
    rec = _RunRecorder(query_answers=[True])
    _wire_firewalld(monkeypatch, rec)
    assert fw._setup_firewall_linux_firewalld(8443, 8001) == "present"
    assert rec.elevations == []


def test_transient_query_miss_retries_before_prompting(monkeypatch):
    """Setup runs during app start while uvicorn, frontend and docker all spin
    up - a busy firewall-cmd answering 'no'/late once must not cost a root
    dialog (live incident: a password prompt on every start while the permanent
    rule existed). Mutation: remove the retry - red."""
    rec = _RunRecorder(query_answers=[False, True])
    _wire_firewalld(monkeypatch, rec)
    assert fw._setup_firewall_linux_firewalld(8443, 8001) == "present"
    assert rec.elevations == []


def test_real_miss_elevates_exactly_once(monkeypatch):
    """A genuinely missing rule still elevates - the retry must not turn into
    fail-open (a subnet change must still open the new subnet's port)."""
    rec = _RunRecorder(query_answers=[False, False])
    _wire_firewalld(monkeypatch, rec)
    assert fw._setup_firewall_linux_firewalld(8443, 8001) == "created"
    assert len(rec.elevations) == 1
    inner = rec.elevations[0][-1]
    assert "--permanent" in inner, "the permanent half must ride the same elevation"


def test_one_elevation_attempt_per_process(monkeypatch):
    """TLS mode runs two lifespans of the same app; both spawn the firewall
    setup within milliseconds. The second call must never reach the platform
    path - with a dialog open, a racing twin means TWO password prompts for one
    start. Mutation: claim the key on success instead of entry - red."""
    calls = []
    monkeypatch.setattr(fw, "_setup_firewall_linux", lambda p, pf: calls.append(p) or True)
    monkeypatch.setattr(fw.Platform, "is_windows", lambda *a: False)
    monkeypatch.setattr(fw.Platform, "is_macos", lambda *a: False)
    monkeypatch.setattr(fw.Platform, "is_linux", lambda *a: True)
    monkeypatch.setattr(fw, "_attempted_ports", set())
    assert fw.setup_firewall(8443, 8001) is True
    assert fw.setup_firewall(8443, 8001) == "present"
    assert calls == [8443], "second call must not re-run the platform setup"
