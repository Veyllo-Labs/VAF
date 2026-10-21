# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The person's own certificate is served as it is, never replaced, and checked before it is
installed.

Measured before: an own certificate with under 30 days left, or without the authority key
identifier, made the start serve VAF's own certificate instead AND overwrite both config keys
with VAF's paths, so the person's choice was gone for good. Nothing checked the key, the chain
or the names. Certificates are minted in the test (tests/cert_factory.py).
"""
import json
import logging
import os
import sys
from pathlib import Path

import pytest
from cryptography.x509.oid import ExtendedKeyUsageOID

from cert_factory import (fingerprint, isolated_home, key_pem, make_ca, make_key, make_leaf,
                          own_chain, pem)
from vaf.core.config import Config
from vaf.network import ssl_utils

TLS_ON = {"local_network_tls_enabled": True}


def _write_pair(directory: Path, cert_bytes: bytes, key_bytes: bytes, stem: str = "own"):
    directory.mkdir(parents=True, exist_ok=True)
    cert_file, key_file = directory / f"{stem}.pem", directory / f"{stem}.key"
    cert_file.write_bytes(cert_bytes)
    key_file.write_bytes(key_bytes)
    return cert_file, key_file


def _stored(key: str) -> str:
    return json.loads(Config.CONFIG_FILE.read_text(encoding="utf-8")).get(key, "")


@pytest.fixture
def no_interfaces(monkeypatch):
    import vaf.network.binding as binding
    monkeypatch.setattr(binding, "get_all_local_ips", lambda: [("enp3s0", "192.168.1.20")])
    monkeypatch.setattr(binding, "local_interfaces", lambda: [])


# ── an own certificate that loads is served, whatever its age ───────────────

def _own(case):
    root, root_key = make_ca()
    if case == "no_aki":
        leaf, key = make_leaf(root, root_key, identifiers=False)
    else:
        leaf, key = make_leaf(root, root_key, days={"200": 200, "10": 10, "expired": -5}[case])
    return pem(leaf, root), key_pem(key)


@pytest.mark.parametrize("case", ["200", "10", "expired", "no_aki"])
def test_an_own_certificate_is_never_replaced(tmp_path, monkeypatch, no_interfaces, case):
    """AC 1. MUTATION: hold an own certificate to the 30-day and key-identifier rule again
    (the fall-through to VAF's own) - red for 10, expired and no_aki."""
    cert_file, key_file = _write_pair(tmp_path / "mine", *_own(case))
    isolated_home(tmp_path, monkeypatch, **TLS_ON, local_network_ssl_cert=str(cert_file),
                  local_network_ssl_key=str(key_file))

    served = ssl_utils.ensure_ssl_certificates()

    assert served == (str(cert_file), str(key_file))
    assert _stored("local_network_ssl_cert") == str(cert_file)
    assert _stored("local_network_ssl_key") == str(key_file)
    assert ssl_utils.served_certificate().source == "own"


def test_an_expired_own_certificate_is_served_with_an_error_line(tmp_path, monkeypatch,
                                                                 no_interfaces, caplog):
    cert_file, key_file = _write_pair(tmp_path / "mine", *_own("expired"))
    isolated_home(tmp_path, monkeypatch, **TLS_ON, local_network_ssl_cert=str(cert_file),
                  local_network_ssl_key=str(key_file))
    with caplog.at_level(logging.WARNING, logger="vaf.network.ssl_utils"):
        ssl_utils.ensure_ssl_certificates()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors and "expired" in errors[0].getMessage()


# ── an own certificate that cannot load: VAF's own for this run, config untouched ──

def _unloadable(case, tmp_path):
    root, root_key = make_ca()
    leaf, key = make_leaf(root, root_key)
    directory = tmp_path / "mine"
    if case == "missing":
        return directory / "gone.pem", directory / "gone.key", "missing"
    if case == "mismatch":
        return (*_write_pair(directory, pem(leaf, root), key_pem(make_key())), "key_mismatch")
    if case == "encrypted":
        return (*_write_pair(directory, pem(leaf, root), key_pem(key, b"secret")),
                "key_encrypted")
    return (*_write_pair(directory, b"not a certificate at all\n", key_pem(key)), "unreadable")


@pytest.mark.parametrize("case", ["missing", "mismatch", "encrypted", "garbage"])
def test_an_unloadable_own_certificate_falls_back_without_touching_config(
        tmp_path, monkeypatch, no_interfaces, caplog, case):
    """AC 2. The encrypted case also proves no passphrase prompt: OpenSSL's own prompt would
    wait on the terminal. MUTATION: write VAF's paths into the keys on the fallback - red."""
    cert_file, key_file, code = _unloadable(case, tmp_path)
    ssl_dir = isolated_home(tmp_path, monkeypatch, **TLS_ON,
                            local_network_ssl_cert=str(cert_file),
                            local_network_ssl_key=str(key_file))
    before = Config.CONFIG_FILE.read_bytes()

    with caplog.at_level(logging.ERROR, logger="vaf.network.ssl_utils"):
        served = ssl_utils.ensure_ssl_certificates()

    assert served == (str(ssl_dir / "server.pem"), str(ssl_dir / "server-key.pem"))
    assert Config.CONFIG_FILE.read_bytes() == before
    state = ssl_utils.served_certificate()
    assert (state.source, state.problem, state.own_cert_path) == ("automatic", code,
                                                                  str(cert_file))
    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any(ssl_utils.REPORT_REASONS[code] in line and str(cert_file) in line
               for line in lines), lines


# ── automatic mode writes neither key ────────────────────────────────────────

def test_automatic_mode_writes_no_key(tmp_path, monkeypatch, no_interfaces):
    """AC 3. MUTATION: put the Config.set of VAF's paths back - red."""
    ssl_dir = isolated_home(tmp_path, monkeypatch, **TLS_ON)
    calls = []
    real_save = Config.save.__func__
    monkeypatch.setattr(Config, "save", classmethod(
        lambda cls, cfg: calls.append(dict(cfg)) or real_save(cls, cfg)))

    first = ssl_utils.ensure_ssl_certificates()
    second = ssl_utils.ensure_ssl_certificates()

    assert first == second == (str(ssl_dir / "server.pem"), str(ssl_dir / "server-key.pem"))
    assert calls == [], "the start saved the configuration"
    assert _stored("local_network_ssl_cert") == "" and _stored("local_network_ssl_key") == ""


def test_old_config_with_automatic_paths_is_automatic(tmp_path, monkeypatch):
    """AC 3. An older install wrote VAF's own paths into the keys; they read as VAF's own,
    and a new address still gets a new certificate. MUTATION: drop the resolved-path
    comparison (they then read as an own certificate, which is never re-issued) - red."""
    import vaf.network.binding as binding
    addresses = [("enp3s0", "192.168.1.20")]
    monkeypatch.setattr(binding, "get_all_local_ips", lambda: list(addresses))
    monkeypatch.setattr(binding, "local_interfaces", lambda: [])
    ssl_dir = tmp_path / "home" / "ssl"
    isolated_home(tmp_path, monkeypatch, **TLS_ON,
                  local_network_ssl_cert=str(ssl_dir / "server.pem"),
                  local_network_ssl_key=str(ssl_dir / "server-key.pem"))
    ssl_utils.ensure_ssl_certificates()
    issued = (ssl_dir / "server.pem").read_bytes()
    assert ssl_utils.served_certificate().source == "automatic"

    addresses.append(("wg0", "10.8.0.1"))
    ssl_utils.ensure_ssl_certificates()
    assert (ssl_dir / "server.pem").read_bytes() != issued
    assert "10.8.0.1" in ssl_utils.certificate_ip_addresses()


# ── the check ───────────────────────────────────────────────────────────────

def _inspect(cert_bytes, key_bytes=b"", chain=b"", **kw):
    kw.setdefault("addresses", ["192.168.1.20"])
    kw.setdefault("host_names", ["vaf"])
    kw.setdefault("current_authority", "")
    return ssl_utils.inspect_certificate(cert_bytes, key_bytes, chain, **kw)


def _case(code):
    """(cert part, key part, chain part, extra keywords) that produce exactly ``code``."""
    leaf, key, inter, root = own_chain(names=("vaf", "192.168.1.20"))
    good = (pem(leaf, inter, root), key_pem(key), b"", {})
    if code == "too_large":
        return (b"x" * (ssl_utils.PEM_PART_MAX_BYTES + 1), key_pem(key), b"", {})
    if code == "unreadable":
        return (b"hello\n", key_pem(key), b"", {})
    if code == "no_key":
        return (pem(leaf, inter, root), b"", b"", {})
    if code == "key_encrypted":
        return (pem(leaf, inter, root), key_pem(key, b"pw"), b"", {})
    if code == "key_mismatch":
        return (pem(leaf, inter, root), key_pem(make_key()), b"", {})
    if code in ("expired", "not_yet_valid", "not_for_servers", "weak_key", "no_names",
                "expires_soon", "no_key_identifiers"):
        options = {"expired": {"days": -3}, "not_yet_valid": {"starts_in_days": 3, "days": 60},
                   "not_for_servers": {"eku": (ExtendedKeyUsageOID.CLIENT_AUTH,)},
                   "weak_key": {"key": make_key("ec521")}, "no_names": {"names": ()},
                   "expires_soon": {"days": 12}, "no_key_identifiers": {"identifiers": False},
                   }[code]
        options.setdefault("names", ("vaf", "192.168.1.20"))
        root2, root2_key = make_ca()
        other, other_key = make_leaf(root2, root2_key, **options)
        return (pem(other, root2), key_pem(other_key), b"", {})
    if code == "chain_broken":
        return (pem(leaf, root, inter), key_pem(key), b"", {})
    if code == "names_missing":
        return (*good[:3], {"addresses": ["192.168.1.20", "10.8.0.1"]})
    if code == "no_issuer":
        return (pem(leaf), key_pem(key), b"", {})
    if code == "authority_changes":
        return (*good[:3], {"current_authority": "ab" * 32})
    raise AssertionError(code)


ERROR_CODES = ["too_large", "unreadable", "no_key", "key_encrypted", "key_mismatch",
               "expired", "not_yet_valid", "not_for_servers", "weak_key", "chain_broken",
               "no_names"]
WARNING_CODES = ["expires_soon", "names_missing", "no_key_identifiers", "no_issuer",
                 "authority_changes"]


@pytest.mark.parametrize("code", ERROR_CODES + WARNING_CODES)
def test_report_codes(code):
    """AC 9: each code for its case, and nothing else that refuses. MUTATION: remove the
    key-match check (key_mismatch passes), the issuer-chain check (chain_broken passes), or
    the 30-day warning - red."""
    cert_bytes, key_bytes, chain, kw = _case(code)
    report = _inspect(cert_bytes, key_bytes, chain, **kw)
    if code in ERROR_CODES:
        assert code in [i.code for i in report.errors], report.to_dict()
        assert not report.ok
    else:
        assert report.ok, report.to_dict()
        assert code in [i.code for i in report.warnings], report.to_dict()


def test_a_clean_chain_has_no_findings_and_its_facts():
    leaf, key, inter, root = own_chain(names=("vaf", "192.168.1.20"))
    report = _inspect(pem(leaf, inter, root), key_pem(key))
    assert report.ok and report.warnings == []
    facts = report.facts
    assert facts["sha256"] == fingerprint(leaf)
    assert facts["authority_sha256"] == fingerprint(root)
    assert facts["chain_length"] == 3 and facts["self_signed"] is False
    assert facts["names"] == ["vaf", "192.168.1.20"]
    assert (facts["key_type"], facts["key_size"]) == ("RSA", 2048)
    assert set(facts) >= {"subject", "issuer", "not_before", "not_after", "days_left",
                          "authority_subject"}


def test_one_bundle_may_carry_chain_and_key():
    """The certificate part may hold the whole chain and the key (one PEM file)."""
    leaf, key, inter, root = own_chain(names=("vaf", "192.168.1.20"))
    assert _inspect(pem(leaf) + key_pem(key) + pem(inter, root)).ok
    assert _inspect(pem(leaf), key_pem(key), chain=pem(inter, root)).ok


def test_key_match_is_decided_by_a_throwaway_server_context(monkeypatch):
    """AC 9: the same load uvicorn makes, on a context nobody serves with."""
    import ssl
    seen = []
    real = ssl.SSLContext.load_cert_chain

    def _spy(self, *a, **kw):
        seen.append(self)
        return real(self, *a, **kw)
    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", _spy)
    leaf, key, inter, root = own_chain(names=("vaf", "192.168.1.20"))
    assert _inspect(pem(leaf, inter, root), key_pem(key)).ok
    assert len(seen) == 1 and seen[0].protocol == ssl.PROTOCOL_TLS_SERVER


def test_report_carries_no_key_material():
    """AC 9. Neither the PEM nor the private numbers of the key may reach the report."""
    leaf, key, inter, root = own_chain(names=("vaf", "192.168.1.20"))
    for key_part in (key_pem(key), key_pem(make_key())):
        text = json.dumps(_inspect(pem(leaf, inter, root), key_part).to_dict())
        assert "PRIVATE KEY" not in text
        body = key_part.decode().splitlines()[1]
        assert body not in text
        assert str(key.private_numbers().d) not in text


# ── install and reset ────────────────────────────────────────────────────────

@pytest.fixture
def events(monkeypatch):
    import vaf.core.security_events as se
    seen = []
    monkeypatch.setattr(se, "log_security_event",
                        lambda kind, **kw: seen.append((kind, kw)))
    return seen


def test_install_refuses_and_writes_nothing(tmp_path, monkeypatch, no_interfaces, events):
    """AC 10. MUTATION: write the files before the check - red (custom/ exists)."""
    ssl_dir = isolated_home(tmp_path, monkeypatch, **TLS_ON)
    before = Config.CONFIG_FILE.read_bytes()
    saves = []
    monkeypatch.setattr(Config, "save", classmethod(lambda cls, cfg: saves.append(cfg)))
    leaf, _key, inter, root = own_chain()

    with pytest.raises(ssl_utils.CertificateRefused) as refused:
        ssl_utils.install_certificate(pem(leaf, inter, root), key_pem(make_key()),
                                      username="admin")

    assert "key_mismatch" in [i.code for i in refused.value.report.errors]
    assert not (ssl_dir / "custom").exists()
    assert saves == [] and Config.CONFIG_FILE.read_bytes() == before
    assert events == []


def test_install_switches_both_keys_once(tmp_path, monkeypatch, no_interfaces, events):
    """AC 10. MUTATION: two Config.set calls instead of one save - red (two saves seen)."""
    ssl_dir = isolated_home(tmp_path, monkeypatch, **TLS_ON)
    saves = []
    real_save = Config.save.__func__
    monkeypatch.setattr(Config, "save", classmethod(
        lambda cls, cfg: saves.append(dict(cfg)) or real_save(cls, cfg)))
    reloads = []
    real_reload = ssl_utils.reload_certificate
    monkeypatch.setattr(ssl_utils, "reload_certificate",
                        lambda: reloads.append(1) or real_reload())
    leaf, key, inter, root = own_chain(names=("vaf", "192.168.1.20"))

    result = ssl_utils.install_certificate(pem(leaf, inter, root), key_pem(key),
                                           username="admin", via="in Settings")

    stem = fingerprint(leaf)[:16]
    cert_file, key_file = ssl_dir / "custom" / f"{stem}.pem", ssl_dir / "custom" / f"{stem}.key"
    assert len(saves) == 1
    assert (saves[0]["local_network_ssl_cert"], saves[0]["local_network_ssl_key"]) == (
        str(cert_file), str(key_file))
    assert cert_file.read_bytes() == pem(leaf, inter, root)
    assert reloads == [1] and result.reload is not None
    assert ssl_utils.served_certificate().source == "own"
    assert result.new_authority == fingerprint(root)
    assert [k for k, _ in events] == ["tls_certificate_changed"]
    assert events[0][1]["username"] == "admin"
    detail = events[0][1]["detail"]
    assert stem in detail and fingerprint(root)[:16] in detail and "in Settings" in detail


def test_install_removes_the_files_of_the_certificate_it_replaced(tmp_path, monkeypatch,
                                                                  no_interfaces, events):
    ssl_dir = isolated_home(tmp_path, monkeypatch, **TLS_ON)
    first = own_chain(names=("vaf", "192.168.1.20"))
    second = own_chain(names=("vaf", "192.168.1.20"))
    ssl_utils.install_certificate(pem(first[0], first[2], first[3]), key_pem(first[1]))
    ssl_utils.install_certificate(pem(second[0], second[2], second[3]), key_pem(second[1]))
    names = sorted(p.name for p in (ssl_dir / "custom").iterdir())
    stem = fingerprint(second[0])[:16]
    assert names == [f"{stem}.key", f"{stem}.pem"]


def test_reset_returns_to_a_fresh_automatic_leaf(tmp_path, monkeypatch, no_interfaces, events):
    """AC 13. MUTATION: keep the old leaf (no re-issue) - red; replace the CA too - red."""
    ssl_dir = isolated_home(tmp_path, monkeypatch, **TLS_ON)
    ssl_utils.ensure_ssl_certificates()
    ca_before = ssl_utils.ca_fingerprint()
    leaf_before = (ssl_dir / "server.pem").read_bytes()
    leaf, key, inter, root = own_chain(names=("vaf", "192.168.1.20"))
    ssl_utils.install_certificate(pem(leaf, inter, root), key_pem(key))
    saves = []
    real_save = Config.save.__func__
    monkeypatch.setattr(Config, "save", classmethod(
        lambda cls, cfg: saves.append(dict(cfg)) or real_save(cls, cfg)))
    events.clear()

    result = ssl_utils.reset_certificate(username="admin")

    assert len(saves) == 1
    assert (saves[0]["local_network_ssl_cert"], saves[0]["local_network_ssl_key"]) == ("", "")
    assert ssl_utils.served_certificate().source == "automatic"
    assert (ssl_dir / "server.pem").read_bytes() != leaf_before
    assert ssl_utils.ca_fingerprint() == ca_before
    assert result.new_authority == ca_before and result.reload is not None
    assert list((ssl_dir / "custom").iterdir()) == []
    assert [k for k, _ in events] == ["tls_certificate_changed"]


# ── owner-only from creation ─────────────────────────────────────────────────

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes; Windows ignores chmod")
def test_keys_are_owner_only_from_creation(tmp_path, monkeypatch, no_interfaces, events):
    """AC 8. os.chmod is switched off while the keys are written, so only the mode a file
    was CREATED with counts. MUTATION: write a key with write_bytes and chmod it after - red
    (0644 under umask 022)."""
    ssl_dir = isolated_home(tmp_path, monkeypatch, **TLS_ON)
    old_umask = os.umask(0o022)
    try:
        with monkeypatch.context() as m:
            m.setattr(os, "chmod", lambda *a, **kw: None)
            ssl_utils.ensure_ssl_certificates()
            leaf, key, inter, root = own_chain(names=("vaf", "192.168.1.20"))
            ssl_utils.install_certificate(pem(leaf, inter, root), key_pem(key))
        custom = ssl_dir / "custom"
        for path in (ssl_dir / "ca-key.pem", ssl_dir / "server-key.pem",
                     *custom.glob("*.key")):
            assert oct(path.stat().st_mode & 0o777) == "0o600", path
        ssl_utils.install_certificate(pem(leaf, inter, root), key_pem(key))
        assert oct(custom.stat().st_mode & 0o777) == "0o700"
    finally:
        os.umask(old_umask)


# ── the generic saves cannot set them ────────────────────────────────────────

def test_without_certificate_keys_drops_both():
    payload = {"local_network_ssl_cert": "/tmp/x.pem", "local_network_ssl_key": "/tmp/x.key",
               "theme": "dark"}
    assert ssl_utils.without_certificate_keys(payload) == {"theme": "dark"}
    assert ssl_utils.without_certificate_keys({"theme": "dark"}) == {"theme": "dark"}
