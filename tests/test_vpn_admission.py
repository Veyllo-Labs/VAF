# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Who may reach VAF in network mode, and on which of this machine's addresses.

One decision, `binding.inbound_policy()`, feeds the access check, every OS firewall
backend and the shown access addresses. These tests pin the decision on a fake set of
interfaces that has everything a real machine mixes together: a LAN, container
bridges, a WireGuard and a Tailscale interface, a down interface, a public address.
"""
import ipaddress
import socket
from collections import namedtuple

import psutil
import pytest

import vaf.network.binding as binding
from vaf.core.config import Config

_Addr = namedtuple("_Addr", "family address netmask broadcast ptp")
_Stat = namedtuple("_Stat", "isup")


def _v4(ip, mask):
    return _Addr(socket.AF_INET, ip, mask, None, None)


# name -> (up, [addresses])
MACHINE = {
    "lo": (True, [_v4("127.0.0.1", "255.0.0.0")]),
    "enp3s0": (True, [_v4("192.168.2.10", "255.255.255.0")]),
    "docker0": (True, [_v4("172.17.0.1", "255.255.0.0")]),
    "br-0b126469ca19": (True, [_v4("172.18.0.1", "255.255.0.0")]),
    "veth9a1": (True, []),
    "wg0": (True, [_v4("10.8.0.1", "255.255.255.0")]),
    "tailscale0": (True, [_v4("100.101.102.103", "255.255.255.255")]),
    "eth1": (False, [_v4("192.168.5.5", "255.255.255.0")]),
    "eth2": (True, [_v4("203.0.113.20", "255.255.255.255")]),
    "vEthernet (WSL)": (True, [_v4("172.25.0.1", "255.255.240.0")]),
}


def _machine(monkeypatch, interfaces):
    monkeypatch.setattr(psutil, "net_if_addrs",
                        lambda: {n: addrs for n, (_up, addrs) in interfaces.items()})
    monkeypatch.setattr(psutil, "net_if_stats",
                        lambda: {n: _Stat(up) for n, (up, _addrs) in interfaces.items()})


@pytest.fixture
def machine(monkeypatch):
    _machine(monkeypatch, MACHINE)


def _settings(monkeypatch, **values):
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: {**Config.DEFAULTS, **values}))


# ── which interfaces count ─────────────────────────────────────────────────

def test_lan_and_vpn_interfaces_are_found_and_the_rest_is_left_out(machine):
    """MUTATION: drop the container-bridge skip, the up-check or the private-range filter."""
    found = {(i.name, i.ip, i.network, i.kind) for i in binding.local_interfaces()}
    assert found == {
        ("enp3s0", "192.168.2.10", "192.168.2.0/24", "lan"),
        ("wg0", "10.8.0.1", "10.8.0.0/24", "vpn"),
        # A mesh VPN's interface carries one address; its devices are the whole block.
        ("tailscale0", "100.101.102.103", "100.64.0.0/10", "vpn"),
    }


def test_public_addresses_are_the_public_ipv4_ones(monkeypatch):
    """A public address on ANY interface that is up puts the machine on the internet, a bridge
    included: `bridge0` is a name local_interfaces() leaves out as a container bridge.
    MUTATION: build it on local_interfaces() (empty); drop the up-check (eth9 listed); skip
    names like local_interfaces does (bridge0 missing)."""
    _machine(monkeypatch, {
        **MACHINE,
        "eth0": (True, [_v4("9.9.9.9", "255.255.255.0"),
                        _Addr(socket.AF_INET6, "2001:4860:4860::8888", None, None, None)]),
        "bridge0": (True, [_v4("8.8.4.4", "255.255.255.0")]),
        "eth9": (False, [_v4("9.9.9.10", "255.255.255.0")]),
    })
    # eth2 carries 203.0.113.20, a documentation address: classify_address calls it forbidden.
    assert binding.public_addresses() == [("eth0", "9.9.9.9"), ("bridge0", "8.8.4.4")]


def test_public_addresses_fall_back_to_the_default_route(monkeypatch):
    def _broken():
        raise OSError("no interface listing here")

    monkeypatch.setattr(psutil, "net_if_addrs", _broken)
    monkeypatch.setattr(binding, "_default_route_ip", lambda: "9.9.9.9")
    assert binding.public_addresses() == [("default", "9.9.9.9")]
    monkeypatch.setattr(binding, "_default_route_ip", lambda: "192.168.1.5")
    assert binding.public_addresses() == []
    monkeypatch.setattr(binding, "_default_route_ip", lambda: None)
    assert binding.public_addresses() == []


@pytest.mark.parametrize("name, kind", [
    ("wg0", "vpn"), ("wt0", "vpn"), ("tun0", "vpn"), ("tap1", "vpn"), ("utun3", "vpn"),
    ("tailscale0", "vpn"), ("Tailscale", "vpn"), ("OpenVPN Wintun", "vpn"),
    ("WireGuard Tunnel", "vpn"), ("ztabcdef12", "vpn"),
    ("enp3s0", "lan"), ("eth0", "lan"), ("en0", "lan"), ("wlan0", "lan"), ("Wi-Fi", "lan"),
    ("Ethernet", "lan"), ("br0", "lan"),
    ("docker0", None), ("br-0b126469ca19", None), ("veth9a1", None), ("virbr0", None),
    ("vEthernet (WSL)", None), ("vEthernet (Default Switch)", None), ("bridge100", None),
    ("lo", None),
])
def test_interface_names_are_told_apart(name, kind):
    assert binding._interface_kind(name) == kind


def test_the_local_address_is_a_lan_interface_never_a_vpn(machine):
    assert binding.get_local_network_ip() == "192.168.2.10"


def test_a_server_with_only_a_vpn_has_no_local_address(monkeypatch):
    """A rented server: a public address and a WireGuard interface. There is no LAN,
    and the VPN address must not be passed off as one."""
    _machine(monkeypatch, {"eth0": MACHINE["eth2"], "wg0": MACHINE["wg0"]})
    monkeypatch.setattr(binding, "_default_route_ip", lambda: "203.0.113.20")
    with pytest.raises(RuntimeError):
        binding.get_local_network_ip()
    assert binding.get_all_local_ips() == [("wg0", "10.8.0.1")]


# ── what an admin may add ──────────────────────────────────────────────────

def test_admitted_network_entries_are_normalized_or_refused():
    taken, refused = binding.normalize_allowed_networks(
        ["100.64.0.0/10", "10.8.0.7/24", "192.168.7.9", "10.8.0.0/24", "",
         "8.8.8.0/24", "0.0.0.0/0", "127.0.0.1", "fd7a:115c:a1e0::/48", "nonsense",
         "169.254.0.0/16", "198.18.0.0/15"])
    assert taken == ["100.64.0.0/10", "10.8.0.0/24", "192.168.7.9/32"]
    assert dict(refused) == {
        "8.8.8.0/24": "not_private", "0.0.0.0/0": "everything", "127.0.0.1": "loopback",
        "fd7a:115c:a1e0::/48": "ipv6", "nonsense": "invalid",
        "169.254.0.0/16": "not_private", "198.18.0.0/15": "not_private",
    }
    assert set(code for _, code in refused) <= set(binding.REFUSAL_REASONS)


def test_a_hand_edited_text_setting_reads_like_a_list():
    assert binding.normalize_allowed_networks("10.8.0.0/24, 100.64.0.0/10")[0] == [
        "10.8.0.0/24", "100.64.0.0/10"]


# ── who is admitted ────────────────────────────────────────────────────────

def _admitted(ip):
    return binding.is_allowed_ip(ip)


def test_by_default_this_machine_and_the_local_networks(machine, monkeypatch):
    _settings(monkeypatch)
    assert _admitted("127.0.0.1") and _admitted("::1") and _admitted("localhost")
    assert _admitted("192.168.2.20") and _admitted("10.8.0.5")
    assert not _admitted("100.101.0.1"), "a mesh VPN is admitted only when an admin says so"
    assert not _admitted("203.0.113.7")


def test_an_admitted_network_is_let_in(machine, monkeypatch):
    """MUTATION: ignore local_network_allowed_networks in inbound_policy."""
    _settings(monkeypatch, local_network_allowed_networks=["100.64.0.0/10"])
    assert _admitted("100.101.0.1")


def test_a_refused_entry_admits_nobody(machine, monkeypatch):
    _settings(monkeypatch, local_network_allowed_networks=["203.0.113.0/24", "0.0.0.0/0"])
    assert not _admitted("203.0.113.7")
    assert not _admitted("8.8.8.8")


def test_vpn_only_locks_the_local_network_out(machine, monkeypatch):
    """MUTATION: keep the RFC 1918 base under vpn_only."""
    _settings(monkeypatch, local_network_vpn_only=True)
    assert _admitted("10.8.0.5"), "the WireGuard network is in"
    assert _admitted("100.101.0.1"), "the detected Tailscale network is in"
    assert not _admitted("192.168.2.20"), "the home network is out"
    assert _admitted("127.0.0.1")


def test_vpn_only_without_a_vpn_admits_only_this_machine(monkeypatch):
    _machine(monkeypatch, {"enp3s0": MACHINE["enp3s0"]})
    _settings(monkeypatch, local_network_vpn_only=True)
    assert not _admitted("192.168.2.20") and not _admitted("10.8.0.5")
    assert _admitted("127.0.0.1")


def test_vpn_only_keeps_what_the_admin_added(monkeypatch):
    _machine(monkeypatch, {"enp3s0": MACHINE["enp3s0"]})
    _settings(monkeypatch, local_network_vpn_only=True, local_network_allowed_networks=["192.168.2.0/24"])
    assert _admitted("192.168.2.20")


def test_unreadable_settings_fall_back_to_the_local_networks_never_wider(monkeypatch):
    def broken(cls):
        raise OSError("config unreadable")
    monkeypatch.setattr(Config, "load", classmethod(broken))
    assert _admitted("192.168.1.5")
    assert not _admitted("100.101.0.1") and not _admitted("8.8.8.8")


def test_a_string_flag_is_read_like_the_switch(machine, monkeypatch):
    _settings(monkeypatch, local_network_vpn_only="true")
    assert not _admitted("192.168.2.20")


def test_the_per_request_check_does_not_scan_interfaces(monkeypatch):
    """The IP check runs on every request; without "VPN only" it must not list interfaces."""
    _settings(monkeypatch)
    monkeypatch.setattr(binding, "local_interfaces",
                        lambda: (_ for _ in ()).throw(AssertionError("scanned")))
    assert _admitted("192.168.2.20")


# ── what the firewall opens and what is shown ─────────────────────────────

def test_firewall_sources_follow_the_same_decision(machine, monkeypatch):
    _settings(monkeypatch)
    assert binding.firewall_sources() == ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
    # firewalld's narrow opening: the LAN subnet, plus the admitted WireGuard network,
    # which the old single-subnet rule left closed.
    assert binding.firewall_sources(narrow_lan=True) == ["10.8.0.0/24", "192.168.2.0/24"]

    _settings(monkeypatch, local_network_allowed_networks=["100.64.0.0/10", "10.8.0.0/24"])
    assert binding.firewall_sources() == ["10.0.0.0/8", "100.64.0.0/10", "172.16.0.0/12",
                                          "192.168.0.0/16"], "a network inside another is not repeated"

    _settings(monkeypatch, local_network_vpn_only=True)
    assert binding.firewall_sources() == ["10.8.0.0/24", "100.64.0.0/10"]
    assert binding.firewall_sources(narrow_lan=True) == ["10.8.0.0/24", "100.64.0.0/10"]


def test_access_addresses_are_the_admitted_ones_lan_first(machine, monkeypatch):
    _settings(monkeypatch)
    assert [i.name for i in binding.access_addresses()] == ["enp3s0", "wg0"]
    _settings(monkeypatch, local_network_allowed_networks=["100.64.0.0/10"])
    assert [i.name for i in binding.access_addresses()] == ["enp3s0", "tailscale0", "wg0"]
    _settings(monkeypatch, local_network_vpn_only=True)
    assert [i.name for i in binding.access_addresses()] == ["tailscale0", "wg0"]


# ── the certificate names the VPN addresses ───────────────────────────────

def test_a_new_vpn_address_gets_a_new_certificate(monkeypatch, tmp_path):
    """MUTATION: require only the LAN address again (the old required_ips)."""
    from vaf.network import ssl_utils

    values = {"local_network_tls_enabled": True, "local_network_ssl_cert": "",
              "local_network_ssl_key": ""}
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, k, d=None: values.get(k, d)))
    monkeypatch.setattr(Config, "set", classmethod(lambda cls, k, v: values.__setitem__(k, v)))
    monkeypatch.setattr(ssl_utils, "_get_ssl_dir", lambda: tmp_path)

    addresses = [("enp3s0", "192.168.2.10"), ("wg0", "10.8.0.1")]
    monkeypatch.setattr(binding, "get_all_local_ips", lambda: list(addresses))

    def sans(path):
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(open(path, "rb").read())
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        return {str(v) for v in ext.get_values_for_type(x509.IPAddress)}

    first, _ = ssl_utils.ensure_ssl_certificates()
    assert {"192.168.2.10", "10.8.0.1"} <= sans(first)
    issued = open(first, "rb").read()

    addresses.append(("tailscale0", "100.101.102.103"))
    second, _ = ssl_utils.ensure_ssl_certificates()
    assert "100.101.102.103" in sans(second)
    assert open(second, "rb").read() != issued

    addresses.pop()   # the VPN went down: the certificate still verifies, no new one
    third, _ = ssl_utils.ensure_ssl_certificates()
    assert open(third, "rb").read() == open(second, "rb").read()


# ── the doctor ─────────────────────────────────────────────────────────────

def _findings(public=(), host=None, events=(), switch=True, unit="active", **cfg):
    """The doctor's findings for a network-mode config. The checks that read the machine
    itself (its public addresses, its host firewall, ufw's switch and its unit, today's
    security log) are pinned, so the answer does not depend on the runner: Ubuntu runners
    ship ufw switched off. `host` may be a function, to count the reads."""
    import vaf.core.security_events as se
    import vaf.network.firewall as fw
    from vaf.core.security_misconfig import collect_security_findings
    base = {"local_network_enabled": True, "local_network_tls_enabled": True,
            "local_network_firewall_enabled": True, "local_network_require_2fa": True}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(binding, "public_addresses", lambda: list(public))
        mp.setattr(binding, "resolve_lan_access_ports", lambda wait_for_proxy=False: (8443, 8001))
        mp.setattr(fw, "host_firewall", host if callable(host) else (lambda: host))
        mp.setattr(fw, "ufw_enabled", lambda paths=None: switch)
        mp.setattr(fw, "_ufw_unit_state", lambda: unit)
        mp.setattr(fw, "_ufw_command_reads_on", lambda paths=None: True)
        mp.setattr(fw, "UFW_DEFAULTS", "/etc/default/ufw")
        mp.setattr(fw, "UFW_CONF", "/etc/ufw/ufw.conf")
        mp.delenv("SSH_CONNECTION", raising=False)
        mp.setattr(se, "read_security_events", lambda date, limit=100: list(events))
        return {f["code"]: f for f in collect_security_findings({**base, **cfg})}


def _codes(**cfg):
    return set(_findings(**cfg))


def test_doctor_no_longer_claims_login_is_not_required():
    """The check read a key registered nowhere and reported HIGH on every server,
    while the auth middleware demands a token from every network client.
    MUTATION: put the check back - red."""
    assert "network_login_not_required" not in _codes()
    assert _codes() == set()


def test_doctor_reports_ignored_entries_and_a_vpn_only_without_vpn(monkeypatch):
    _machine(monkeypatch, {"enp3s0": MACHINE["enp3s0"]})
    assert "network_allowed_entries_ignored" in _codes(local_network_allowed_networks=["8.8.8.8"])
    assert "network_vpn_only_without_vpn" in _codes(local_network_vpn_only=True)
    assert "network_vpn_only_without_vpn" not in _codes(
        local_network_vpn_only=True, local_network_allowed_networks=["192.168.2.0/24"])


# ── a change of who is admitted re-applies the firewall, nothing restarts ──

def test_an_admission_change_reapplies_the_firewall_without_a_restart(monkeypatch):
    import threading
    import vaf.tray as tray
    import vaf.network.firewall as fw

    applied = threading.Event()
    restarts = []
    monkeypatch.setattr(fw, "apply_lan_firewall", lambda log=None: applied.set())
    monkeypatch.setattr(tray, "_schedule_network_restart", lambda k, v: restarts.append(k))
    monkeypatch.setattr(tray.time, "sleep", lambda s: None)

    tray.on_config_changed("local_network_allowed_networks", ["100.64.0.0/10"], [])
    assert applied.wait(2), "the firewall must be re-applied for the new networks"
    assert restarts == []

    tray.on_config_changed("local_network_port", 8002, 8001)
    assert restarts == ["local_network_port"]


def test_saving_an_admission_change_notifies_the_observers(monkeypatch, tmp_path):
    """On a config file of its own: the suite shares one, and a "VPN only" left switched
    on there would refuse every LAN client in the tests that run after this one."""
    seen = []
    monkeypatch.setattr(Config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(Config, "_filelock", None, raising=False)
    monkeypatch.setattr(Config, "notify_observers",
                        classmethod(lambda cls, key, new, old: seen.append(key)))
    Config.save({**Config.load(), "local_network_vpn_only": True})
    assert "local_network_vpn_only" in seen
    assert Config.load()["local_network_vpn_only"] is True



def test_doctor_names_a_point_to_point_vpn_under_vpn_only(monkeypatch):
    _machine(monkeypatch, {"enp3s0": MACHINE["enp3s0"],
                           "wg0": (True, [_v4("10.8.0.1", "255.255.255.255")])})
    assert "network_vpn_single_address" in _codes(local_network_vpn_only=True)
    assert "network_vpn_single_address" not in _codes()


# ── the doctor: does the access port face the internet ───────────────────────

PUBLIC = [("eth0", "9.9.9.9")]


def test_doctor_sees_a_public_address_a_switched_off_ufw_and_public_refusals():
    """MUTATIONS: drop each finding in turn; count private refusals; give `low` regardless
    of "VPN only" - red."""
    found = _findings(public=PUBLIC)["network_public_address"]
    assert found["severity"] == "medium"
    assert "9.9.9.9 (eth0)" in found["message"] and "port 8443" in found["message"]
    assert "`vaf server vpn-only on`" in found["message"]
    found = _findings(public=PUBLIC, local_network_vpn_only=True)["network_public_address"]
    assert found["severity"] == "low" and "vpn-only on" not in found["message"]

    found = _findings(host="ufw_off")["network_ufw_disabled"]
    assert found["severity"] == "medium"
    assert found["message"].index("sudo ufw allow OpenSSH") < found["message"].index("sudo ufw enable")
    assert "network_ufw_disabled" not in _codes(host="ufw_off", local_network_firewall_enabled=False)
    assert "network_ufw_disabled" not in _codes(host="ufw")

    refused = [{"kind": "ip_blocked", "ip": "9.9.9.9", "detail": "/login"},
               {"kind": "ip_blocked", "ip": "9.9.9.9", "detail": "/api/users"}]
    found = _findings(events=refused)["network_public_refusals"]
    assert found["severity"] == "high"
    assert "1 public address" in found["message"] and "9.9.9.9" in found["message"]
    for ip in ("192.168.1.5", "100.101.102.103", "unknown"):
        assert "network_public_refusals" not in _codes(
            events=[{"kind": "ip_blocked", "ip": ip, "detail": "/login"}]), ip
    assert "network_public_refusals" not in _codes(
        events=[{"kind": "login_failed", "ip": "9.9.9.9"}])


def test_doctor_says_when_ufws_switch_cannot_be_confirmed():
    """An uncertain switch still counts as ufw, so the allow rule is added, and the doctor
    said nothing: silence read as a guarded port. The same for a unit whose start failed.
    MUTATION: drop the finding - red; report it for a confirmed switch, or for a ufw that is
    off - red."""
    found = _findings(host="ufw", switch=None)["network_ufw_unknown"]
    assert found["severity"] == "low"
    assert ("could not confirm that ufw is switched on: /etc/default/ufw or /etc/ufw/ufw.conf "
            "could not be read with certainty") in found["message"]
    assert "`sudo ufw status`" in found["message"]
    found = _findings(host="ufw", switch=True, unit="failed")["network_ufw_unknown"]
    assert "systemd reports its unit as failed" in found["message"]
    assert "network_ufw_unknown" not in _codes(host="ufw", switch=True)
    assert "network_ufw_unknown" not in _codes(host="ufw", switch=None,
                                               local_network_firewall_enabled=False)
    codes = _codes(host="ufw_off", switch=None)
    assert "network_ufw_disabled" in codes and "network_ufw_unknown" not in codes
    assert "network_ufw_unknown" not in _codes(host="firewalld", switch=None)


def test_doctor_reads_the_host_firewall_once():
    """The read runs `which` and `systemctl`; the doctor asked it once for the switched-off
    check and again inside the unconfirmed one. MUTATION: let the second check read it
    again - red."""
    reads = []
    found = _findings(host=lambda: reads.append(1) or "ufw", switch=None)
    assert "network_ufw_unknown" in found
    assert len(reads) == 1


def test_a_port_that_cannot_be_read_keeps_the_public_address_finding(monkeypatch):
    """Only the message hangs on the port; the finding is the machine on the internet.
    MUTATION: let the port read raise again - red."""
    import vaf.core.security_events as se
    import vaf.network.firewall as fw
    from vaf.core.security_misconfig import collect_security_findings

    def broken(wait_for_proxy=False):
        raise OSError("no runtime status")

    monkeypatch.setattr(binding, "public_addresses", lambda: list(PUBLIC))
    monkeypatch.setattr(binding, "resolve_lan_access_ports", broken)
    monkeypatch.setattr(fw, "host_firewall", lambda: None)
    monkeypatch.setattr(se, "read_security_events", lambda date, limit=100: [])
    found = {f["code"]: f for f in collect_security_findings(
        {"local_network_enabled": True, "local_network_tls_enabled": True,
         "local_network_firewall_enabled": True, "local_network_require_2fa": True})}
    assert "port 8443" in found["network_public_address"]["message"]


def test_doctor_says_nothing_about_the_internet_in_local_mode():
    off = {"local_network_enabled": False}
    assert not _codes(public=PUBLIC, host="ufw_off",
                      events=[{"kind": "ip_blocked", "ip": "9.9.9.9"}], **off) & {
        "network_public_address", "network_ufw_disabled", "network_public_refusals"}
    assert "network_ufw_unknown" not in _codes(host="ufw", switch=None, **off)
