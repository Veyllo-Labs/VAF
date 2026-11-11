# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""vaf/core/ftp.py against a real FTP/FTPS server on the loopback (tests/ftp_stub.py).

What is pinned: who decides that a server is trusted (the person, once; then the
certificate authority or the remembered fingerprint), that a changed certificate is refused,
that plain FTP happens only when written so, that the server's passive address is not
followed, what a folder upload leaves out, the size bound, Stop, and the forbidden
addresses."""
import ssl
import threading

import pytest

from ftp_stub import FtpStub, make_ca, make_cert
from vaf.core import ftp

ALICE, BOB = "scope-alice", "scope-bob"
PASSWORD = "s3cret-pass"


@pytest.fixture
def lab(tmp_path, monkeypatch):
    from vaf.core.platform import Platform
    home = tmp_path / "vafhome"
    home.mkdir()
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: home))
    root = tmp_path / "server"
    root.mkdir()
    return tmp_path, root


def _target(server, *, tls=True, user="alice"):
    return ftp.parse_server(f"{'ftps' if tls else 'ftp'}://{user}@127.0.0.1:{server.port}")


def _trust_test_authority(monkeypatch, ca_path):
    monkeypatch.setattr(ftp, "_client_context",
                        lambda: ssl.create_default_context(cafile=str(ca_path)))


# ── the server text ───────────────────────────────────────────────────────────

def test_a_server_is_ftps_unless_written_otherwise():
    t = ftp.parse_server("web123@example.org")
    assert (t.user, t.host, t.port, t.tls) == ("web123", "example.org", 21, True)
    assert t.name == "ftps://example.org"
    plain = ftp.parse_server("ftp://web123@example.org:2121")
    assert plain.tls is False and plain.name == "ftp://example.org:2121"
    # Hosters name FTP accounts like mail addresses: the host is after the LAST @.
    mail = ftp.parse_server("ftps://upload@shop.example@ftp.example.org")
    assert (mail.user, mail.host) == ("upload@shop.example", "ftp.example.org")
    assert ftp.parse_server("ftp.example.org").user == "anonymous"
    assert ftp.parse_server("ftps://u@[2001:db8::1]:990").host == "2001:db8::1"


@pytest.mark.parametrize("bad", ["", "-oProxy=x@h", "u@h/htdocs", "sftp://u@h", "u@h:99999",
                                 "u@bad host", "u@h;rm"])
def test_what_is_no_server_is_refused(bad):
    with pytest.raises(ftp.FtpError):
        ftp.parse_server(bad)


# ── trust ─────────────────────────────────────────────────────────────────────

def test_an_unconfirmed_server_is_not_even_connected_to(lab):
    """MUTATION: drop the known/confirmed check - red: the stub sees a connection."""
    tmp, root = lab
    with FtpStub(root, cert=make_cert(tmp, "leaf")) as server:
        with pytest.raises(ftp.FtpError, match="not connected to yet"):
            ftp.connect(_target(server), user_scope_id=ALICE, password=PASSWORD, confirmed=False)
        assert server.connections == 0


def test_a_self_signed_server_is_pinned_and_a_new_certificate_refused(lab):
    """MUTATION: skip the fingerprint comparison - red: a different certificate went through."""
    tmp, root = lab
    with FtpStub(root, cert=make_cert(tmp, "first")) as server:
        s = ftp.connect(_target(server), user_scope_id=ALICE, password=PASSWORD, confirmed=True)
        assert s.first_contact and s.trust == "pinned" and s.fingerprint.startswith("SHA256:")
        assert s.notes and s.fingerprint in s.notes[0]
        ftp.close(s)
        again = ftp.connect(_target(server), user_scope_id=ALICE, password=PASSWORD, confirmed=False)
        assert not again.first_contact
        ftp.close(again)
        port = server.port
    assert ftp.servers(ALICE)[0]["trust"] == "pinned"
    # The same address, another certificate.
    with FtpStub(root, cert=make_cert(tmp, "second")) as other:
        t = ftp.parse_server(f"ftps://alice@127.0.0.1:{other.port}")
        data = ftp._load(ALICE)
        data[t.name] = data.pop(f"ftps://127.0.0.1:{port}")
        ftp._save(ALICE, data)
        with pytest.raises(ftp.FtpError, match="DIFFERENT certificate"):
            ftp.connect(t, user_scope_id=ALICE, password=PASSWORD, confirmed=True)
    # Another account has confirmed nothing.
    assert not ftp.is_known(t, BOB)


def test_an_authority_vouched_server_survives_a_renewed_certificate(lab, monkeypatch):
    """Remembered as "authority", not by fingerprint: a renewal (Let's Encrypt every two
    months) must not lock the person out. MUTATION: pin authority servers too - red."""
    tmp, root = lab
    ca = make_ca(tmp)
    _trust_test_authority(monkeypatch, ca[0])
    with FtpStub(root, cert=make_cert(tmp, "leaf1", ca)) as server:
        s = ftp.connect(_target(server), user_scope_id=ALICE, password=PASSWORD, confirmed=True)
        assert s.trust == "authority" and not s.notes
        ftp.close(s)
        port = server.port
    with FtpStub(root, cert=make_cert(tmp, "leaf2", ca)) as renewed:
        t = ftp.parse_server(f"ftps://alice@127.0.0.1:{renewed.port}")
        data = ftp._load(ALICE)
        data[t.name] = data.pop(f"ftps://127.0.0.1:{port}")
        ftp._save(ALICE, data)
        ftp.close(ftp.connect(t, user_scope_id=ALICE, password=PASSWORD, confirmed=False))
    # Later no authority vouches any more: refused, not silently pinned.
    with FtpStub(root, cert=make_cert(tmp, "selfsigned")) as swapped:
        t2 = ftp.parse_server(f"ftps://alice@127.0.0.1:{swapped.port}")
        data = ftp._load(ALICE)
        data[t2.name] = data.pop(t.name)
        ftp._save(ALICE, data)
        with pytest.raises(ftp.FtpError, match="none does"):
            ftp.connect(t2, user_scope_id=ALICE, password=PASSWORD, confirmed=True)


def test_plain_ftp_only_when_written_and_confirmed_on_its_own(lab):
    """ftp:// and ftps:// to the same host are two servers: confirming one does not confirm
    the other."""
    tmp, root = lab
    with FtpStub(root) as server:                       # no certificate: plain only
        with pytest.raises(ftp.FtpError, match="does not offer encryption"):
            ftp.connect(_target(server), user_scope_id=ALICE, password=PASSWORD, confirmed=True)
        s = ftp.connect(_target(server, tls=False), user_scope_id=ALICE, password=PASSWORD,
                        confirmed=True)
        assert s.trust == "none"
        ftp.close(s)
        assert ftp.is_known(_target(server, tls=False), ALICE)
        assert not ftp.is_known(_target(server), ALICE)


def test_a_wrong_password_is_a_refused_login(lab):
    tmp, root = lab
    with FtpStub(root, cert=make_cert(tmp, "leaf")) as server:
        with pytest.raises(ftp.FtpError, match="login refused"):
            ftp.connect(_target(server), user_scope_id=ALICE, password="wrong", confirmed=True)


def test_forbidden_addresses_are_refused_before_any_connection():
    with pytest.raises(ftp.FtpError, match="no FTP connection may go to"):
        ftp.check_address("169.254.169.254")
    ftp.check_address("127.0.0.1")                      # the house is allowed, as for ssh


# ── transfers ─────────────────────────────────────────────────────────────────

@pytest.fixture
def session(lab):
    tmp, root = lab
    with FtpStub(root, cert=make_cert(tmp, "leaf"), pasv_host="10.255.255.1") as server:
        s = ftp.connect(_target(server), user_scope_id=ALICE, password=PASSWORD, confirmed=True)
        yield tmp, root, server, s
        ftp.close(s)


def test_the_passive_address_the_server_names_is_not_followed(session):
    """The stub names 10.255.255.1 in its PASV answer; the transfer still reaches the stub.
    MUTATION: trust_server_pasv_ipv4_address = True - red (the connection goes nowhere)."""
    tmp, root, server, s = session
    (root / "a.txt").write_text("hello")
    assert any("a.txt" in line for line in ftp.list_dir(s, "."))


def test_the_data_connection_resumes_the_control_session(session, monkeypatch):
    """vsftpd and pure-ftpd refuse a data connection without it."""
    tmp, root, server, s = session
    seen = {}
    real = s.ftp.context.wrap_socket

    def _wrap(sock, **kw):
        seen.update(kw)
        return real(sock, **kw)

    monkeypatch.setattr(s.ftp.context, "wrap_socket", _wrap)
    ftp.list_dir(s, ".")
    assert "session" in seen and seen["server_hostname"] == "127.0.0.1"


def test_a_folder_upload_leaves_out_links_git_and_notes(session):
    """MUTATION: drop the FOLDER_SKIP check - red."""
    tmp, root, server, s = session
    site = tmp / "site"
    (site / "css").mkdir(parents=True)
    (site / "index.html").write_text("<h1>hi</h1>")
    (site / "css" / "a.css").write_text("body{}")
    (site / ".git").mkdir()
    (site / ".git" / "config").write_text("secret")
    (site / ".vaf").mkdir()
    (site / ".vaf" / "notes.md").write_text("notes")
    outside = tmp / "outside.txt"
    outside.write_text("not for the server")
    (site / "link.txt").symlink_to(outside)
    result = ftp.upload(s, site, "htdocs/www")
    assert result["files"] == 2
    assert (root / "htdocs" / "www" / "index.html").read_text() == "<h1>hi</h1>"
    assert (root / "htdocs" / "www" / "css" / "a.css").exists()
    assert not (root / "htdocs" / "www" / ".git").exists()
    assert not (root / "htdocs" / "www" / ".vaf").exists()
    assert not (root / "htdocs" / "www" / "link.txt").exists()


def test_a_folder_too_large_is_refused_before_the_first_byte(session):
    tmp, root, server, s = session
    site = tmp / "big"
    site.mkdir()
    (site / "a.bin").write_bytes(b"x" * 600)
    (site / "b.bin").write_bytes(b"x" * 600)
    before = list(server.commands)
    with pytest.raises(ftp.FtpError, match="larger than"):
        ftp.upload(s, site, "big", limit=1000)
    assert "STOR" not in server.commands[len(before):]


def test_a_file_round_trip_and_delete(session):
    tmp, root, server, s = session
    src = tmp / "report.pdf"
    src.write_bytes(b"%PDF" + b"\0" * 5000)
    assert ftp.upload(s, src, "docs/report.pdf")["bytes"] == 5004
    back = tmp / "back.pdf"
    assert ftp.download(s, "docs/report.pdf", back) == 5004
    assert back.read_bytes() == src.read_bytes()
    assert not (tmp / "back.pdf.part").exists()
    ftp.delete(s, "docs/report.pdf")
    assert not (root / "docs" / "report.pdf").exists()


def test_a_download_over_the_bound_or_stopped_leaves_nothing(session):
    """MUTATION: write straight to the target instead of a .part file - red."""
    tmp, root, server, s = session
    (root / "big.bin").write_bytes(b"y" * 200000)
    target = tmp / "big.bin"
    with pytest.raises(ftp.FtpError, match="larger than"):
        ftp.download(s, "big.bin", target, limit=100000)
    assert not target.exists() and not (tmp / "big.bin.part").exists()


def test_stop_ends_a_transfer(lab):
    tmp, root = lab
    (root / "big.bin").write_bytes(b"z" * 300000)
    with FtpStub(root, cert=make_cert(tmp, "leaf")) as server:
        s = ftp.connect(_target(server), user_scope_id=ALICE, password=PASSWORD, confirmed=True)
        stop = threading.Event()
        stop.set()
        with pytest.raises(ftp.FtpError, match="stopped"):
            ftp.download(s, "big.bin", tmp / "big.bin", check_stop=stop.is_set)
        assert not (tmp / "big.bin").exists() and not (tmp / "big.bin.part").exists()
        ftp.close(s)


def test_list_falls_back_to_list_without_mlsd(lab):
    tmp, root = lab
    (root / "x.html").write_text("x")
    with FtpStub(root, cert=make_cert(tmp, "leaf"), mlsd=False) as server:
        s = ftp.connect(_target(server), user_scope_id=ALICE, password=PASSWORD, confirmed=True)
        assert any("x.html" in line for line in ftp.list_dir(s, "."))
        ftp.close(s)


def test_forget_and_the_server_list(lab):
    tmp, root = lab
    with FtpStub(root, cert=make_cert(tmp, "leaf")) as server:
        ftp.close(ftp.connect(_target(server), user_scope_id=ALICE, password=PASSWORD,
                              confirmed=True))
        name = _target(server).name
    assert [r["name"] for r in ftp.servers(ALICE)] == [name]
    assert ftp.forget(name, ALICE) is True
    assert ftp.servers(ALICE) == [] and ftp.forget(name, ALICE) is False


def test_a_damaged_server_entry_is_refused_not_trusted_blindly(lab):
    """A pinned entry without its fingerprint, or a plain-FTP trust under an ftps:// name,
    reached the unverified handshake with nothing to compare. MUTATION: drop the check - red."""
    tmp, root = lab
    with FtpStub(root, cert=make_cert(tmp, "leaf")) as server:
        t = _target(server)
        for damaged in ({"trust": "pinned", "fingerprint": ""}, {"trust": "none"}, {"trust": "?"}):
            ftp._save(ALICE, {t.name: damaged})
            with pytest.raises(ftp.FtpError, match="damaged"):
                ftp.connect(t, user_scope_id=ALICE, password=PASSWORD, confirmed=True)
        assert server.connections == 0


def test_a_folder_upload_never_enters_a_linked_folder(session):
    """Before Python 3.13 rglob's ** followed linked folders: a file under site/linked was no
    link itself and went up. MUTATION: stop pruning linked folders - red (followlinks=True
    alone stays green: the pruning is the second guard)."""
    tmp, root, server, s = session
    site = tmp / "linked_site"
    site.mkdir()
    (site / "index.html").write_text("ok")
    secret = tmp / "dotssh"
    secret.mkdir()
    (secret / "id_ed25519").write_text("PRIVATE")
    (site / "keys").symlink_to(secret, target_is_directory=True)
    from vaf.core.path_jail import walk_without_links
    assert walk_without_links(site) == [("index.html", False)]
    assert ftp.upload(s, site, "w")["files"] == 1
    assert not (root / "w" / "keys").exists()


# ── a stored credential's name in the server text ─────────────────────────────

def test_a_dollar_in_the_server_names_its_cause():
    """The model wrote `ftp://$VAF_SECRET_FTP_USER@$VAF_SECRET_FTP_HOST:...` in a live chat and
    got "not a host name or address", which hid the cause. MUTATION: delete the `$` check in
    parse_server - red: the message falls back to "not a host name"."""
    for text in ("ftp://$X@$Y:21", "ftps://$VAF_SECRET_FTP_USER@ftp.example.org",
                 "alice@$FTP_HOST", "ftp://u@${FTP_HOST}"):
        with pytest.raises(ftp.FtpError) as caught:
            ftp.parse_server(text)
        message = str(caught.value)
        assert "stored credential" in message and "fills in $NAME" in message, text
        assert "not a host name or address" not in message, text
    # The coder's deploy_to takes the server only as written out, and says so.
    from vaf.tools import coder
    target, err = coder._ftp_deploy_target("ftps://$FTP_USER@$FTP_HOST/htdocs", "s")
    assert target is None and "deploy_to" in err and "stored credential" in err


# ── read and stat ─────────────────────────────────────────────────────────────

def _stub_session(lab, **options):
    tmp, root = lab
    server = FtpStub(root, cert=make_cert(tmp, "leaf"), **options).__enter__()
    s = ftp.connect(_target(server), user_scope_id=ALICE, password=PASSWORD, confirmed=True)
    return server, s


def _done(server, s):
    ftp.close(s)
    server.__exit__(None, None, None)


def test_read_from_the_end_and_at_an_offset(lab):
    """The tail of a big log without downloading it: SIZE, then REST at size - N.
    MUTATION: call retrbinary without `rest` - red (the bytes come from the start);
    MUTATION: drop the MAX_READ_BYTES clamp - red (a 1 MB read returns more than 64 KB)."""
    tmp, root = lab
    body = bytes(range(256)) * 400                       # 102400 bytes, every offset distinct
    (root / "server.log").write_bytes(body)
    server, s = _stub_session(lab)
    try:
        tail = ftp.read(s, "server.log", offset=-1000, max_bytes=5000)
        assert tail == {"data": body[-1000:], "start": len(body) - 1000, "size": len(body)}
        assert f"REST {len(body) - 1000}" in server.lines
        middle = ftp.read(s, "server.log", offset=300, max_bytes=50)
        assert middle["data"] == body[300:350] and middle["start"] == 300
        # The same session still works after a read that stopped early.
        assert ftp.stat(s, "server.log")["size"] == len(body)
        (root / "big.log").write_bytes(b"a" * (1024 * 1024))
        big = ftp.read(s, "big.log", max_bytes=10 ** 6)
        assert len(big["data"]) == ftp.MAX_READ_BYTES == 65536
        # From the start: no REST at all; past the end: nothing, without a transfer.
        before = len(server.lines)
        assert ftp.read(s, "server.log", max_bytes=10)["data"] == body[:10]
        assert not any(line.startswith("REST") for line in server.lines[before:])
        assert ftp.read(s, "server.log", offset=len(body) + 5)["data"] == b""
    finally:
        _done(server, s)


def test_read_without_rest_or_size_says_why(lab):
    """MUTATION: drop the REST mapping - red (a bare 502 comes back); drop the SIZE check for a
    negative offset - red."""
    tmp, root = lab
    (root / "a.log").write_bytes(b"x" * 5000)
    server, s = _stub_session(lab, rest=False)
    try:
        with pytest.raises(ftp.FtpError, match=r"middle of a file \(REST\); download it instead"):
            ftp.read(s, "a.log", offset=100)
        assert ftp.read(s, "a.log", max_bytes=3)["data"] == b"xxx"      # from 0 needs no REST
    finally:
        _done(server, s)
    server, s = _stub_session(lab, size=False)
    try:
        with pytest.raises(ftp.FtpError, match=r"does not tell the file size \(SIZE\)"):
            ftp.read(s, "a.log", offset=-10)
        assert ftp.read(s, "a.log", offset=4990)["data"] == b"x" * 10
    finally:
        _done(server, s)


def test_stat_by_mlst_and_by_size_and_mdtm(lab):
    """MUTATION: skip the SIZE+MDTM fallback - red (a server without MLST errors)."""
    import os
    tmp, root = lab
    (root / "site").mkdir()
    (root / "site" / "index.html").write_bytes(b"<h1>hi</h1>")
    os.utime(root / "site" / "index.html", (1700000000, 1700000000))
    expected = {"type": "file", "size": 11, "modified": "2023-11-14 22:13:20 UTC"}
    server, s = _stub_session(lab)
    try:
        assert ftp.stat(s, "site/index.html") == expected
        assert "MLST" in server.commands and "MDTM" not in server.commands
        assert ftp.stat(s, "site")["type"] == "folder"
        with pytest.raises(ftp.FtpError, match="was not found, or it is a folder"):
            ftp.stat(s, "site/missing.html")
    finally:
        _done(server, s)
    server, s = _stub_session(lab, mlst_single=False)
    try:
        assert ftp.stat(s, "site/index.html") == expected
        assert {"SIZE", "MDTM"} <= set(server.commands)
        with pytest.raises(ftp.FtpError, match="was not found, or it is a folder"):
            ftp.stat(s, "site/missing.html")
    finally:
        _done(server, s)


# ── names that are not ASCII ──────────────────────────────────────────────────

def test_non_ascii_names_round_trip(session):
    """MUTATION: construct the client with encoding="latin-1" - red (the stub reads UTF-8)."""
    tmp, root, server, s = session
    src = tmp / "local.txt"
    src.write_bytes("Grüße aus dem Ordner\n".encode("utf-8"))
    remote = "Ordner ü/Grüße ä.txt"
    assert ftp.upload(s, src, remote)["bytes"] == src.stat().st_size
    assert (root / "Ordner ü" / "Grüße ä.txt").read_bytes() == src.read_bytes()
    assert any("Grüße ä.txt" in line for line in ftp.list_dir(s, "Ordner ü"))
    back = tmp / "back.txt"
    assert ftp.download(s, remote, back) == src.stat().st_size
    assert back.read_bytes() == src.read_bytes()
    assert ftp.read(s, remote, offset=-7)["data"] == src.read_bytes()[-7:]
    assert ftp.stat(s, remote)["size"] == src.stat().st_size


def test_a_non_utf8_listing_names_the_encoding(lab):
    """A server keeping names in Latin-1 sends `caf\\xe9.txt`; ftplib raised a bare codec error.
    MUTATION: remove the UnicodeDecodeError mapping - red (the raw codec text comes back)."""
    tmp, root = lab
    (root / "a.txt").write_text("a")
    extra = b"type=file;size=1; caf\xe9.txt\r\n"
    for mlsd in (True, False):
        server, s = _stub_session(lab, mlsd=mlsd, listing_extra=extra)
        try:
            with pytest.raises(ftp.FtpError, match="not UTF-8") as caught:
                ftp.list_dir(s, ".")
            assert "codec" not in str(caught.value)
        finally:
            _done(server, s)
