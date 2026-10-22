# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf server cert` and `vaf server ca`: the terminal half of the certificate.

The framework runs for real against a scratch ssl directory; only the question to the
running VAF (POST /api/network/certificate/reload on the internal channel) is answered here:
it answered, nothing listens, or a VAF that does not know the route. The exit codes are a
renewal hook's contract: 0 served (now, or at the next start), 1 refused and nothing
changed, 3 changed but the running VAF still serves the previous certificate.
"""
import json

import pytest
from typer.testing import CliRunner

import vaf.cli.cmd.server as server_cmd
import vaf.network.binding as binding
from cert_factory import fingerprint, isolated_home, key_pem, make_key, own_chain, pem
from vaf.core.config import Config
from vaf.network import ssl_utils

runner = CliRunner()


class _Answer:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(binding, "get_all_local_ips", lambda: [("enp3s0", "192.168.1.20")])
    monkeypatch.setattr(binding, "local_interfaces", lambda: [])
    monkeypatch.setattr(binding, "machine_dns_names", lambda: ["vafhost"])
    monkeypatch.setattr(binding, "get_local_network_ip", lambda: "192.168.1.20")
    monkeypatch.setattr(server_cmd, "_access_port_suffix", lambda: ":8443")
    ssl_dir = isolated_home(tmp_path, monkeypatch, local_network_enabled=False,
                            local_network_tls_enabled=True)
    ssl_utils.ensure_ssl_certificates()
    return ssl_dir


@pytest.fixture
def running(monkeypatch):
    """The running VAF's answer to the reload; set ``running.answer`` per test."""
    import requests

    class _Running:
        answer = _Answer(200, {"ok": True, "listeners": {"proxy": "loaded"}, "refused": None,
                               "source": "own"})
        calls = []
    state = _Running()

    def _post(url, timeout=None):
        state.calls.append(url)
        if isinstance(state.answer, Exception):
            raise state.answer
        return state.answer
    monkeypatch.setattr(requests, "post", _post)
    monkeypatch.setattr(server_cmd, "_backend_port_listens", lambda: False)
    return state


def _files(tmp_path, *, key=None, names=("vafhost", "192.168.1.20")):
    leaf, leaf_key, inter, root = own_chain(names=names)
    (tmp_path / "cert.pem").write_bytes(pem(leaf, inter, root))
    (tmp_path / "key.pem").write_bytes(key_pem(key or leaf_key))
    return leaf, root


def _flat(text: str) -> str:
    """The output without whitespace: the console wraps long lines at its width."""
    return "".join(text.split())


def _stored(key):
    return json.loads(Config.CONFIG_FILE.read_text(encoding="utf-8")).get(key, "")


def test_set_refuses_a_mismatched_key_and_changes_nothing(home, running, tmp_path):
    """MUTATION: install without the check (a mismatch exits 0) - red."""
    _files(tmp_path, key=make_key())
    result = runner.invoke(server_cmd.app, ["cert", "set", "--cert", str(tmp_path / "cert.pem"),
                                            "--key", str(tmp_path / "key.pem")])
    assert result.exit_code == 1, result.output
    assert _flat("does not belong") in _flat(result.output)
    assert _stored("local_network_ssl_cert") == "" and not (home / "custom").exists()
    assert running.calls == []


@pytest.mark.parametrize("good, code", [(True, 0), (False, 1)])
def test_check_changes_nothing(home, running, tmp_path, good, code):
    _files(tmp_path, key=None if good else make_key())
    result = runner.invoke(server_cmd.app, ["cert", "set", "--check", "--cert",
                                            str(tmp_path / "cert.pem"), "--key",
                                            str(tmp_path / "key.pem")])
    assert result.exit_code == code, result.output
    assert _stored("local_network_ssl_cert") == "" and not (home / "custom").exists()
    assert running.calls == []


def test_set_serves_now_when_the_running_vaf_loaded_it(home, running, tmp_path):
    leaf, root = _files(tmp_path)
    result = runner.invoke(server_cmd.app, ["cert", "set", "--cert", str(tmp_path / "cert.pem"),
                                            "--key", str(tmp_path / "key.pem")])
    assert result.exit_code == 0, result.output
    assert _flat("serves it now") in _flat(result.output)
    assert running.calls and running.calls[0].endswith("/api/network/certificate/reload")
    assert ssl_utils.served_certificate().source == "own"
    # The authority changed from VAF's own to the chain's root: the peers' line is printed,
    # with the address the certificate names (the one an invitation hands out), and the
    # line about browsers that cannot click past the warning.
    colon = ssl_utils.colon_fingerprint(fingerprint(root))
    assert _flat(f"vaf a2a trust wss://192.168.1.20:8443 --ca-fp {colon}") in _flat(result.output)
    assert _flat("cannot click past that warning") in _flat(result.output)


def test_set_when_vaf_is_not_running(home, running, tmp_path):
    import requests
    running.answer = requests.ConnectionError("refused")
    _files(tmp_path)
    result = runner.invoke(server_cmd.app, ["cert", "set", "--cert", str(tmp_path / "cert.pem"),
                                            "--key", str(tmp_path / "key.pem")])
    assert result.exit_code == 0, result.output
    assert _flat("not running") in _flat(result.output)


@pytest.mark.parametrize("answer", [
    _Answer(200, {"ok": False, "listeners": {"proxy": "restored"}, "refused": None,
                  "source": "own"}),
    _Answer(404, {"detail": "Not Found"}),
])
def test_set_says_restart_when_the_running_vaf_did_not_load_it(home, running, tmp_path, answer):
    """MUTATION: exit 0 when the running VAF answered `restored` - red."""
    running.answer = answer
    _files(tmp_path)
    result = runner.invoke(server_cmd.app, ["cert", "set", "--cert", str(tmp_path / "cert.pem"),
                                            "--key", str(tmp_path / "key.pem")])
    assert result.exit_code == 3, result.output
    assert "restart" in result.output
    assert ssl_utils.served_certificate().source == "own", "installed all the same"


def test_set_against_vaf_run_web_says_restart(home, running, tmp_path, monkeypatch):
    """`vaf run --web` serves the backend itself and has no internal channel to ask."""
    import requests
    running.answer = requests.ConnectionError("refused")
    monkeypatch.setattr(server_cmd, "_backend_port_listens", lambda: True)
    _files(tmp_path)
    result = runner.invoke(server_cmd.app, ["cert", "set", "--cert", str(tmp_path / "cert.pem"),
                                            "--key", str(tmp_path / "key.pem")])
    assert result.exit_code == 3, result.output
    assert _flat("vaf run --web") in _flat(result.output)


def test_reset(home, running, tmp_path):
    _files(tmp_path)
    runner.invoke(server_cmd.app, ["cert", "set", "--cert", str(tmp_path / "cert.pem"),
                                   "--key", str(tmp_path / "key.pem")])
    result = runner.invoke(server_cmd.app, ["cert", "reset"])
    assert result.exit_code == 0, result.output
    assert _flat("own certificate is back") in _flat(result.output)
    assert ssl_utils.served_certificate().source == "automatic"
    assert _stored("local_network_ssl_cert") == ""


def test_show(home, running, tmp_path):
    leaf, _root = _files(tmp_path)
    runner.invoke(server_cmd.app, ["cert", "set", "--cert", str(tmp_path / "cert.pem"),
                                   "--key", str(tmp_path / "key.pem")])
    result = runner.invoke(server_cmd.app, ["cert", "show"])
    assert result.exit_code == 0, result.output
    assert "your own" in result.output
    assert ssl_utils.colon_fingerprint(fingerprint(leaf)) in _flat(result.output)


def test_ca_shows_the_fingerprint_and_whether_it_is_restricted(home):
    result = runner.invoke(server_cmd.app, ["ca"])
    assert result.exit_code == 0, result.output
    assert ssl_utils.colon_fingerprint(ssl_utils.ca_fingerprint()) in _flat(result.output)
    assert _flat("yes, to private addresses") in _flat(result.output)


def test_ca_renew_asks_first(home, running):
    before = ssl_utils.ca_fingerprint()
    result = runner.invoke(server_cmd.app, ["ca", "renew"], input="n\n")
    assert result.exit_code == 0 and "Nothing was changed" in result.output
    assert ssl_utils.ca_fingerprint() == before
    running.answer = _Answer(200, {"ok": True, "listeners": {}, "refused": None,
                                   "source": "automatic"})
    result = runner.invoke(server_cmd.app, ["ca", "renew", "--yes"])
    assert result.exit_code == 0, result.output
    after = ssl_utils.ca_fingerprint()
    assert after != before
    flat = _flat(result.output)
    assert ssl_utils.colon_fingerprint(before) in flat and ssl_utils.colon_fingerprint(after) in flat
    assert "--ca-fp" in flat


def test_status_has_a_certificate_line(home, monkeypatch):
    import vaf.core.inference_profile as profile
    monkeypatch.setattr(profile, "current", lambda: "local")
    result = runner.invoke(server_cmd.app, ["status"])
    assert result.exit_code == 0, result.output
    assert "Certificate:" in result.output and _flat("VAF's own") in _flat(result.output)
    assert _flat("more days") in _flat(result.output)


def test_a_fingerprint_is_printed_as_it_is(home, monkeypatch):
    """Rich reads ":CD:" and ":AB:" as emoji codes, so AB:CD:EF came out with a disc symbol
    in it (measured): a fingerprint a person compares character by character. MUTATION:
    print the line through UI.print (emoji codes on) - red."""
    monkeypatch.setattr(ssl_utils, "local_authority", lambda: {
        "path": "/srv/ca.pem", "fingerprint": "abcd" * 16, "not_after": "2036-01-01T00:00:00",
        "restricted": True})
    result = runner.invoke(server_cmd.app, ["ca"])
    assert result.exit_code == 0, result.output
    assert ":".join(["AB", "CD"] * 16) in _flat(result.output)


def test_names_print_as_text_not_markup(home, tmp_path):
    """A SAN can carry console markup ("[link=...]"); printed with markup on it became a
    hyperlink or bold red text. MUTATION: drop the escape of the names - red."""
    _files(tmp_path, names=("[bold red]VERIFIED[/bold red]", "192.168.1.20"))
    result = runner.invoke(server_cmd.app, ["cert", "set", "--check", "--cert",
                                            str(tmp_path / "cert.pem"), "--key",
                                            str(tmp_path / "key.pem")])
    assert "[bold red]VERIFIED[/bold red]" in result.output


def test_one_day_is_said_in_the_singular(home, running, tmp_path, monkeypatch):
    """MUTATION: "1 days" again - red."""
    from cert_factory import make_ca, make_leaf
    root, root_key = make_ca()
    leaf, key = make_leaf(root, root_key, days=2, names=("vafhost", "192.168.1.20"))
    (tmp_path / "cert.pem").write_bytes(pem(leaf, root))
    (tmp_path / "key.pem").write_bytes(key_pem(key))
    runner.invoke(server_cmd.app, ["cert", "set", "--cert", str(tmp_path / "cert.pem"),
                                   "--key", str(tmp_path / "key.pem")])
    import vaf.core.inference_profile as profile
    monkeypatch.setattr(profile, "current", lambda: "local")
    shown = _flat(runner.invoke(server_cmd.app, ["cert", "show"]).output)
    status = _flat(runner.invoke(server_cmd.app, ["status"]).output)
    assert "(1day)" in shown and "1days" not in shown
    assert "validfor1moreday" in status


def test_renewing_with_tls_off_says_so(tmp_path, monkeypatch):
    """MUTATION: report an own certificate staying served for every source but automatic -
    red."""
    import vaf.network.binding as binding
    monkeypatch.setattr(binding, "get_all_local_ips", lambda: [("enp3s0", "192.168.1.20")])
    monkeypatch.setattr(binding, "local_interfaces", lambda: [])
    isolated_home(tmp_path, monkeypatch, local_network_tls_enabled=False)
    result = runner.invoke(server_cmd.app, ["ca", "renew", "--yes"])
    assert result.exit_code == 0, result.output
    assert _flat("TLS is off") in _flat(result.output)
    assert _flat("Your own certificate stays served") not in _flat(result.output)


def test_an_unconfirmed_load_says_it_may_already_be_served(home, running, tmp_path):
    """The load started and had not finished when the wait ended: it may have applied, so
    "still serves the previous certificate" would be false. MUTATION: drop the unconfirmed
    branch - red."""
    running.answer = _Answer(200, {"ok": False, "listeners": {"proxy": "unconfirmed"},
                                   "refused": None, "source": "own"})
    _files(tmp_path)
    result = runner.invoke(server_cmd.app, ["cert", "set", "--cert", str(tmp_path / "cert.pem"),
                                            "--key", str(tmp_path / "key.pem")])
    assert result.exit_code == 3
    assert _flat("may already serve it") in _flat(result.output)
    assert _flat("still serves the previous certificate") not in _flat(result.output)


def test_no_trust_command_for_a_host_the_certificate_does_not_name(home, running, tmp_path,
                                                                    monkeypatch):
    """A peer's name check refuses an address the certificate does not name, so the line
    says why instead of printing that command. MUTATION: print the address anyway - red."""
    import vaf.network.binding as binding
    monkeypatch.setattr(binding, "get_local_network_ip", lambda: "192.168.9.9")
    monkeypatch.setattr(ssl_utils, "_addresses_of", lambda _name: frozenset())
    _files(tmp_path, names=("example.org", "www.example.org"))
    result = runner.invoke(server_cmd.app, ["cert", "set", "--cert", str(tmp_path / "cert.pem"),
                                            "--key", str(tmp_path / "key.pem")])
    flat = _flat(result.output)
    assert "vafa2atrustwss://" not in flat
    assert _flat("cannot reach it now") in flat


def test_the_reason_for_no_trust_command_prints_as_text(home, monkeypatch, capsys):
    """The reason quotes the certificate's names, which whoever made the certificate chose:
    a name with console markup prints as it is. MUTATION: drop the escape of the reason -
    red."""
    marked = "[bold red]VERIFIED[/bold red]"
    monkeypatch.setattr(ssl_utils, "room_host_choice",
                        lambda _address: (None, f"your certificate names {marked}"))
    server_cmd._peer_trust_line("ab" * 32)
    assert _flat(marked) in _flat(capsys.readouterr().out)


def test_without_a_lan_address_the_line_keeps_the_placeholder(home, monkeypatch, capsys):
    """The reasons quote the address; without one they read "does not name , which...".
    MUTATION: choose a host for the empty address again - red."""
    asked = []
    monkeypatch.setattr(binding, "get_local_network_ip", lambda: "")
    monkeypatch.setattr(ssl_utils, "room_host_choice",
                        lambda address: asked.append(address) or (None, "reason"))
    server_cmd._peer_trust_line("ab" * 32)
    assert asked == []
    assert "wss://<address>:8443" in _flat(capsys.readouterr().out)


def test_renewing_lists_the_names_left_out(home, monkeypatch):
    """MUTATION: drop the list from ca renew - red."""
    import vaf.network.binding as binding
    monkeypatch.setattr(binding, "machine_dns_names", lambda: ["mypc", "mypc.fritz.box"])
    result = runner.invoke(server_cmd.app, ["ca", "renew"], input="n\n")
    flat = _flat(result.output)
    assert _flat("leaves mypc, mypc.fritz.box out of VAF's certificate") in flat
    assert _flat("its address or mypc.local") in flat or _flat("its address") in flat
