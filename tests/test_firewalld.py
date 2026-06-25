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
