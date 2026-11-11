# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The ftp tool (vaf/tools/ftp.py): its contract with the dispatcher and the person, against
the FTPS stub (tests/ftp_stub.py).

Pinned: the first connection is the person's to confirm and says when it is unencrypted, the
password is a stored NAME and never comes back, the transfers land where they should, and
host_bash hands FTP to this tool."""
import pytest

from ftp_stub import FtpStub, make_cert
from vaf.core import ftp

ALICE, BOB = "scope-alice", "scope-bob"
PASSWORD = "s3cret-pass"


@pytest.fixture
def lab(tmp_path, monkeypatch):
    from vaf.core import user_secrets
    from vaf.core.platform import Platform
    data = tmp_path / ".vaf"
    data.mkdir()
    store = tmp_path / "data"
    store.mkdir()
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: data))
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: store))
    monkeypatch.setattr(user_secrets, "_stores", {})
    user_secrets.set_secret("FTP_PASS", PASSWORD, user_scope_id=ALICE)
    root = tmp_path / "server"
    root.mkdir()
    # The local side must be inside the person's own files: the test folder counts as such.
    import vaf.tools.filesystem as fs
    monkeypatch.setattr(fs, "is_safe_path", lambda p, **k: (True, p))
    with FtpStub(root, cert=make_cert(tmp_path, "leaf")) as server:
        yield tmp_path, root, server


def _tool(stub, **kw):
    from vaf.tools.ftp import FtpTool
    args = {"server": f"ftps://alice@127.0.0.1:{stub.port}", "user_scope_id": ALICE,
            "username": "alice", "user_role": "user", "login_credential": "FTP_PASS"}
    args.update(kw)
    return FtpTool().run(**args)


def test_the_contract_the_policy_reads():
    from vaf.tools.ftp import FtpTool
    t = FtpTool
    assert t.permission_level == "dangerous" and "channel" in t.channel_restrictions
    assert t.trusted_dir_grants is False and t.account_opt_in is True
    assert t.accepts_call_confirmation is True and t.file_access == "write"
    assert "NAME" in t.parameters["properties"]["login_credential"]["description"]


def test_the_dialog_shows_which_stored_credential_a_call_uses():
    from vaf.core.arg_preview import build_preview, mask_secret_args
    from vaf.tools.ftp import FtpTool
    args = {"server": "ftps://u@h", "action": "list", "login_credential": "FTP_PASS"}
    assert "FTP_PASS" in build_preview("ftp", mask_secret_args(args, FtpTool.secret_args))["text"]


def test_a_first_connection_is_asked_and_plain_ftp_says_so(lab):
    """MUTATION: return None from ask_reason for a new server - red."""
    from vaf.tools.ftp import FtpTool
    tmp, root, server = lab
    t = FtpTool()
    new = t.ask_reason({"server": f"ftps://alice@127.0.0.1:{server.port}"}, user_scope_id=ALICE)
    assert new and "First connection" in new and "certificate" in new
    plain = t.ask_reason({"server": "ftp://alice@example.org"}, user_scope_id=ALICE)
    assert "WITHOUT encryption" in plain and "readable" in plain
    assert "could not be checked" in t.ask_reason({"server": "-x"}, user_scope_id=ALICE)
    _tool(server, _call_confirmed=True)
    assert t.ask_reason({"server": f"ftps://alice@127.0.0.1:{server.port}"},
                        user_scope_id=ALICE) is None
    # Another account confirmed nothing.
    assert t.ask_reason({"server": f"ftps://alice@127.0.0.1:{server.port}"},
                        user_scope_id=BOB) is not None


def test_an_unconfirmed_first_connection_is_refused_and_nothing_connects(lab):
    tmp, root, server = lab
    out = _tool(server)
    assert out.startswith("Error:") and "confirmed by the user" in out
    assert server.connections == 0


def test_upload_list_download_delete_and_the_password_never_comes_back(lab):
    """MUTATION: drop the scrub - red: a server echoing the password brought it back."""
    tmp, root, server = lab
    site = tmp / "site"
    site.mkdir()
    (site / "index.html").write_text("<h1>hi</h1>")
    out = _tool(server, action="upload", local_path=str(site), remote_path="htdocs",
                _call_confirmed=True)
    assert "Uploaded the folder" in out and "1 files" in out and out.endswith("OK")
    assert "First connection" in out and "SHA256:" in out
    assert (root / "htdocs" / "index.html").read_text() == "<h1>hi</h1>"
    listed = _tool(server, action="list", remote_path="htdocs")
    assert "index.html" in listed and "First connection" not in listed
    back = tmp / "back.html"
    assert "Downloaded 11 bytes" in _tool(server, action="download", remote_path="htdocs/index.html",
                                         local_path=str(back))
    assert back.read_text() == "<h1>hi</h1>"
    assert "Deleted htdocs/index.html" in _tool(server, action="delete",
                                                 remote_path="htdocs/index.html")
    assert not (root / "htdocs" / "index.html").exists()
    for text in (out, listed):
        assert PASSWORD not in text


def test_a_server_error_comes_back_scrubbed(lab, monkeypatch):
    tmp, root, server = lab
    _tool(server, _call_confirmed=True)

    def _echo(session, remote):
        raise ftp.FtpError(f"550 cannot delete {remote} for alice:{PASSWORD}")

    monkeypatch.setattr(ftp, "delete", _echo)
    out = _tool(server, action="delete", remote_path="x")
    assert PASSWORD not in out and "[VAF_SECRET_FTP_PASS]" in out


def test_a_credential_that_is_not_stored_is_named(lab):
    tmp, root, server = lab
    out = _tool(server, login_credential="NOPE", _call_confirmed=True)
    assert "no stored credential is named 'NOPE'" in out and server.connections == 0


def test_actions_that_need_a_remote_path_say_so(lab):
    tmp, root, server = lab
    for action in ("download", "delete"):
        assert "needs remote_path" in _tool(server, action=action, _call_confirmed=True)
    assert "unknown action" in _tool(server, action="rm", _call_confirmed=True)


def test_host_bash_hands_ftp_to_this_tool():
    """MUTATION: drop ftp_transfer from the host profile's blocking list - red."""
    from vaf.core.command_policy import classify_command
    from vaf.tools.host_bash import HostBashTool
    for cmd in ("curl -T dist/index.html ftp://u:pw@example.org/htdocs/",
                "curl --ftp-ssl -T f ftps://example.org/x", "wget ftp://example.org/file",
                "lftp -u u,p example.org -e 'mirror -R dist /htdocs; quit'", "ftp -n example.org",
                "ncftpput -u u example.org /x f", "bash -c 'lftp example.org'",
                "curl --url=ftp://example.org/x -T f"):
        v = classify_command(cmd, profile="host")
        assert v.blocked and "ftp_transfer" in v.categories, cmd
        assert "ftp tool" in v.reason, cmd
    for cmd in ("curl https://example.org", "echo ftp://example.org", "git push",
                "curl https://example.org/?next=ftp://example.net"):
        assert not classify_command(cmd, profile="host").blocked, cmd
    # On the other machine, and in the network-less jail, it is not this computer's business.
    assert not classify_command("lftp example.org", profile="remote").blocked
    out = HostBashTool().run(command="curl -T a.zip ftp://example.org/")
    assert out.startswith("[BLOCKED]") and "ftp tool" in out


def test_the_router_forces_ftp_for_a_web_space():
    """MUTATION: drop the forced ftp route - red."""
    import inspect
    from vaf.core import agent
    for msg in ("lade die seite auf meinen webspace", "upload it via ftp", "ftps zugang ist da",
                "wie in filezilla"):
        assert agent._FTP_ROUTE_RE.search(msg), msg
    for msg in ("a minecraft server", "software", "after the deadline"):
        assert not agent._FTP_ROUTE_RE.search(msg), msg
    assert 'forced_tools.add("ftp")' in inspect.getsource(agent.Agent)


def test_the_terminal_lists_and_forgets_a_server(lab, monkeypatch):
    """MUTATION: drop the forget command's failure exit - red."""
    import pathlib
    import re
    from typer.testing import CliRunner
    import vaf.cli.cmd.ftp as cli
    tmp, root, server = lab
    monkeypatch.setattr(cli, "_scope", lambda: ALICE)
    runner = CliRunner()
    assert "No servers yet" in runner.invoke(cli.app, ["servers"]).output
    _tool(server, _call_confirmed=True)
    name = f"ftps://127.0.0.1:{server.port}"
    listed = runner.invoke(cli.app, ["servers"]).output
    assert name in listed and "remembered certificate" in listed and "SHA256:" in listed
    assert runner.invoke(cli.app, ["forget", name]).exit_code == 0
    assert runner.invoke(cli.app, ["forget", name]).exit_code == 1
    main = (pathlib.Path(cli.__file__).resolve().parents[2] / "main.py").read_text(encoding="utf-8")
    assert re.search(r'add_typer\(ftp\.app, name="ftp"[^)]*callback=_terminal_door', main, re.S)


# -- Settings, Connections, FTP -----------------------------------------------------

def _routes_client(role, scope, allowed):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from vaf.api.ftp_routes import router
    from vaf.core.tool_dispatch import set_account_allowlist_resolver
    set_account_allowlist_resolver(lambda s: allowed)
    app = FastAPI()

    @app.middleware("http")
    async def _as(request, call_next):
        request.state.user = {"user_scope_id": scope, "username": "alice", "role": role}
        return await call_next(request)

    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def _resolver_restored():
    from vaf.core.tool_dispatch import (get_account_allowlist_resolver,
                                        set_account_allowlist_resolver)
    previous = get_account_allowlist_resolver()
    yield
    set_account_allowlist_resolver(previous)


def test_the_settings_route_is_closed_to_an_account_without_ftp(lab, _resolver_restored):
    """An unrestricted account ("everything") is not granted an opt-in tool.
    MUTATION: drop the 403 - red."""
    client = _routes_client("user", ALICE, None)
    assert client.get("/api/ftp").status_code == 403
    assert client.delete("/api/ftp/servers/ftps://x").status_code == 403


def test_the_settings_route_lists_and_removes_a_server(lab, _resolver_restored):
    tmp, root, server = lab
    _tool(server, _call_confirmed=True)
    name = f"ftps://127.0.0.1:{server.port}"
    client = _routes_client("user", ALICE, frozenset({"ftp"}))
    listed = client.get("/api/ftp").json()["servers"]
    assert listed[0]["name"] == name and listed[0]["trust"] == "pinned"
    assert listed[0]["fingerprint"].startswith("SHA256:")
    assert PASSWORD not in client.get("/api/ftp").text
    assert client.delete(f"/api/ftp/servers/{name}").json() == {"deleted": True}
    assert client.get("/api/ftp").json()["servers"] == []
    assert client.delete(f"/api/ftp/servers/{name}").status_code == 404


def test_an_upload_of_nothing_is_refused_before_any_connection(lab):
    """A missing local file reached the server first (and, the first time, remembered it).
    MUTATION: drop the check before connect - red: the stub saw a connection."""
    tmp, root, server = lab
    out = _tool(server, action="upload", local_path=str(tmp / "missing.html"),
                remote_path="htdocs/x.html", _call_confirmed=True)
    assert out.startswith("Error:") and "not a file or a folder" in out
    assert server.connections == 0


def test_a_remembered_server_is_removed_only_on_the_second_click():
    """Removing loses the pinned certificate or host key, and the next connection trusts what
    the server then shows. MUTATION: drop the confirming step in either section - red."""
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for name, key in (("FtpSection.tsx", "server.name"), ("SshSection.tsx", "host.host")):
        src = (root / "web" / "components" / "settings" / name).read_text(encoding="utf-8")
        forget = src[src.index("const forget = async"):]
        assert forget.index(f"if (confirming !== {key}) {{ setConfirming({key}); return; }}") \
            < forget.index("method: 'DELETE'"), name
        assert "t('confirmForget')" in src, name
    for loc in ("en", "de", "ja", "zh", "ko", "th", "tr"):
        cat = json.loads((root / "web" / "messages" / f"{loc}.json").read_text(encoding="utf-8"))
        assert cat["ftp"]["confirmForget"] and cat["ssh"]["confirmForget"], loc


# -- a server from stored credentials, read and stat ------------------------------------

def _store_server(server, host="127.0.0.1"):
    from vaf.core import user_secrets
    for name, value in (("FTP_HOST", host), ("FTP_PORT", str(server.port)),
                        ("FTP_USER", "alice")):
        user_secrets.set_secret(name, value, user_scope_id=ALICE)


NAMED = "ftps://$FTP_USER@$FTP_HOST:$FTP_PORT"


def test_the_server_can_come_from_stored_credentials(lab):
    """The live chat had host, port and login stored and wrote them as names; the tool took the
    text literally. MUTATION: name the resolved target in the not-confirmed error - red: the
    login comes back (the port and 127.0.0.1 are scrubbed whole)."""
    tmp, root, server = lab
    (root / "world").mkdir()
    (root / "world" / "level.dat").write_bytes(b"x" * 10)
    _store_server(server)
    refused = _tool(server, server=NAMED, action="list")
    assert refused.startswith(f"Error: {NAMED} is a server this account has not"), refused
    out = _tool(server, server=NAMED, action="list", _call_confirmed=True)
    assert out.startswith(f"[FTP {NAMED}]") and "world" in out and out.endswith("OK"), out
    listed = _tool(server, server=NAMED, action="list", remote_path="world")
    assert "level.dat" in listed
    for text in (refused, out, listed):
        for value in ("127.0.0.1", str(server.port), "alice", PASSWORD):
            assert value not in text, (value, text)
    # Confirmed under the resolved name, as a server written out would be.
    assert ftp.is_known(ftp.parse_server(f"ftps://x@127.0.0.1:{server.port}"), ALICE)


def test_a_server_written_with_names_is_asked_once_and_shows_where(lab):
    """The question names where the password goes (the agent can store any value under a name
    itself), never the login or the password. MUTATION: parse the raw server in ask_reason -
    red (a confirmed server is asked again); build the question from the raw text - red (the
    host is missing)."""
    from vaf.tools.ftp import FtpTool
    tmp, root, server = lab
    _store_server(server)
    t = FtpTool()
    new = t.ask_reason({"server": NAMED}, user_scope_id=ALICE, username="alice")
    assert new.startswith(f"First connection to ftps://127.0.0.1:{server.port}"), new
    assert "FTP_HOST" in new and "certificate" in new
    assert "alice" not in new.replace("$FTP_USER", "") and PASSWORD not in new
    unknown = t.ask_reason({"server": "ftps://u@$NOT_STORED"}, user_scope_id=ALICE)
    assert "could not be checked" in unknown
    _tool(server, server=NAMED, _call_confirmed=True)
    assert t.ask_reason({"server": NAMED}, user_scope_id=ALICE, username="alice") is None
    plain = t.ask_reason({"server": "ftp://$FTP_USER@$FTP_HOST:$FTP_PORT"}, user_scope_id=ALICE)
    assert f"127.0.0.1:{server.port}" in plain and "WITHOUT encryption" in plain


def test_every_answer_is_scrubbed(lab, monkeypatch):
    """A stored value that breaks the server text is answered by name, and a fault that is no
    FtpError used to reach the dispatcher raw. MUTATION: drop the scrub in run - red (the
    ValueError's host); drop the connect except branch - red (no [FTP ...] header)."""
    tmp, root, server = lab
    from vaf.core import user_secrets
    user_secrets.set_secret("FTP_HOST", "bad host value", user_scope_id=ALICE)
    out = _tool(server, server="ftps://alice@$FTP_HOST", _call_confirmed=True)
    assert out.startswith("Error: server: ftps://alice@$FTP_HOST is not a server once "
                          "FTP_HOST is filled in"), out
    assert "bad" not in out and "value" not in out
    missing = _tool(server, server="ftps://alice@$FTP_NOWHERE", _call_confirmed=True)
    assert "no stored credential is named FTP_NOWHERE" in missing
    _store_server(server)

    def _boom(target, **kw):
        raise ValueError(f"unexpected answer from {target.host}:{target.port}")

    monkeypatch.setattr(ftp, "connect", _boom)
    out = _tool(server, server=NAMED, _call_confirmed=True)
    assert out.startswith(f"[FTP {NAMED}] Error: unexpected answer"), out
    assert "127.0.0.1" not in out and str(server.port) not in out


def test_read_and_stat_through_the_tool(lab):
    """MUTATION: drop "read" from the enum check - red."""
    from vaf.tools.ftp import FtpTool
    tmp, root, server = lab
    (root / "logs").mkdir()
    log = b"".join(b"line %05d\n" % i for i in range(10000))     # 110000 bytes
    (root / "logs" / "latest.log").write_bytes(log)
    (root / "logs" / "world.bin").write_bytes(b"\0\1\2" * 100)
    _tool(server, _call_confirmed=True)
    out = _tool(server, action="read", remote_path="logs/latest.log", offset=-22)
    assert f"Bytes {len(log) - 22}-{len(log) - 1} of {len(log)}:" in out
    assert out.endswith("line 09998\nline 09999\n\nOK"), out
    capped = _tool(server, action="read", remote_path="logs/latest.log", max_bytes=999999)
    assert "Bytes 0-65535 of" in capped
    assert "Binary content (300 bytes); use download." in _tool(
        server, action="read", remote_path="logs/world.bin")
    assert f"file, {len(log)} bytes, modified " in _tool(server, action="stat",
                                                         remote_path="logs/latest.log")
    assert "\nfolder" in _tool(server, action="stat", remote_path="logs")
    assert "needs remote_path" in _tool(server, action="read")
    assert "whole numbers" in _tool(server, action="read", remote_path="x", offset="end")
    assert set(FtpTool.parameters["properties"]["action"]["enum"]) >= {"read", "stat"}
    # A path MLST named without saying which kind it is (ftp.stat's type None).
    assert FtpTool._stat_line({"type": None, "size": None, "modified": None}) == (
        "there, but the server does not say whether file or folder")


def _substrings(value, n=4):
    return {value[i:i + n] for i in range(len(value) - n + 1)}


def test_a_server_that_does_not_parse_is_answered_by_its_names_only(lab):
    """A parse error quotes the part it refused, split at the last '@' or at ':', and that part
    is a fragment of the stored value, which the whole-value scrub does not recognise
    (measured: 'Zr9#Lm' from a password holding '@'). MUTATION: answer a parse error after an
    expansion with the parser's own text - red."""
    from vaf.core import user_secrets
    tmp, root, server = lab
    values = {"MAIL_PASS": "Hx7!q@Zr9#Lm", "COLON_PASS": "Qw9zz:Tk!42xy",
              "DOLLAR_PASS": "Pq7$Wm4@Vb8x", "SPACED": "Ab12 Cd34:Ef56"}
    for name, value in values.items():
        user_secrets.set_secret(name, value, user_scope_id=ALICE)
    for written in ("$MAIL_PASS", "ftp://u@$COLON_PASS", "ftp://u@$DOLLAR_PASS",
                    "ftps://$DOLLAR_PASS@h.example", "ftp://u@h.example:$SPACED",
                    "ftp://$MAIL_PASS@$COLON_PASS"):
        out = _tool(server, server=written, _call_confirmed=True)
        assert out.startswith(f"Error: server: {written} is not a server once "), out
        for value in values.values():
            leaked = [s for s in _substrings(value) if s in out]
            assert not leaked, (written, leaked, out)


def test_an_error_masks_a_short_stored_login_and_port(lab, monkeypatch):
    """The scrub leaves a value under 4 characters, so the not-confirmed error rendered a
    stored login `bob` and an error naming the server rendered its port. MUTATION: return
    the text unchanged from _hide_stored_parts - red; name the resolved target in the
    not-confirmed error - red."""
    from vaf.core import user_secrets
    tmp, root, server = lab
    user_secrets.set_secret("FTP_HOST", "127.0.0.1", user_scope_id=ALICE)
    user_secrets.set_secret("FTP_USER", "bob", user_scope_id=ALICE)
    user_secrets.set_secret("FTP_PORT", "990", user_scope_id=ALICE)
    named = "ftps://$FTP_USER@$FTP_HOST:$FTP_PORT"
    refused = _tool(server, server=named)
    assert refused.startswith(f"Error: {named} is a server"), refused

    def _refuse(target, **kw):
        raise ftp.FtpError(f"the server does not offer encryption at {target.name}. If it is "
                           f"plain FTP, write ftp://{target.user}@... (530 user {target.user})")

    monkeypatch.setattr(ftp, "connect", _refuse)
    out = _tool(server, server=named, _call_confirmed=True)
    assert out.startswith(f"[FTP {named}]"), out
    for value in ("bob", "990", "127.0.0.1"):
        assert value not in out.replace(named, ""), (value, out)
    assert "ftp://<stored login>@..." in out and "<stored host>:<stored port>" in out
    # A part the model wrote itself is not a stored value and stays readable.
    out = _tool(server, server="ftps://$FTP_USER@127.0.0.1:2121", _call_confirmed=True)
    assert "ftps://127.0.0.1:2121" in out and "bob" not in out


def test_a_password_that_starts_with_the_login_is_masked_whole(lab, monkeypatch):
    """The login was masked first, inside the password too, so `bob-secret-123` became
    `<stored login>-secret-123` and the scrub after it no longer matched the password. And
    the login inside a longer word (`/srv/ftp/bob123`) was not masked at all. Pins the connect
    stage's catch-all branch as well.
    MUTATION: leave the complete values out of the one pass (parts first, scrub after) - red;
    MUTATION: mask a part only as a whole word again - red;
    MUTATION: return the raw text from the connect stage's catch-all branch - red."""
    from vaf.core import user_secrets
    tmp, root, server = lab
    user_secrets.set_secret("FTP_USER", "bob", user_scope_id=ALICE)
    user_secrets.set_secret("BOB_PASS", "bob-secret-123", user_scope_id=ALICE)
    named = f"ftps://$FTP_USER@127.0.0.1:{server.port}"

    def _fail(target, *, password=None, **kw):
        raise RuntimeError(f"login {password} refused for /srv/ftp/{target.user}123")

    monkeypatch.setattr(ftp, "connect", _fail)
    out = _tool(server, server=named, login_credential="BOB_PASS", _call_confirmed=True)
    rest = out.replace(named, "")
    for piece in ("bob", "secret", "-123"):
        assert piece not in rest, (piece, out)
    assert "[VAF_SECRET_BOB_PASS]" in out and "/srv/ftp/<stored login>123" in out, out


def test_an_error_masks_a_password_shorter_than_the_scrub_minimum(lab, monkeypatch):
    """Settings stores a value of any length, and the scrub leaves one under 4 characters, so
    a server reply quoting a stored password `Q7z` reached the answer unmasked.
    MUTATION: skip a value in `secrets` under user_secrets.MIN_SCRUB_LENGTH in
    _hide_stored_parts - red."""
    from vaf.core import user_secrets
    tmp, root, server = lab
    user_secrets.set_secret("SHORT_PASS", "Q7z", user_scope_id=ALICE)

    def _refuse(target, *, password=None, **kw):
        raise ftp.FtpError(f"530 password {password} rejected")

    monkeypatch.setattr(ftp, "connect", _refuse)
    out = _tool(server, server=f"ftps://alice@127.0.0.1:{server.port}",
                login_credential="SHORT_PASS", _call_confirmed=True)
    assert "Q7z" not in out, out
    assert "530 password [VAF_SECRET_SHORT_PASS] rejected" in out, out


@pytest.mark.parametrize("operation, raised", [
    ("list_dir", lambda user: ftp.FtpError(f"550 /srv/ftp/{user}2: no access")),
    ("list_dir", lambda user: OSError(f"550 /srv/ftp/{user}2: no access")),
])
def test_an_operation_error_masks_a_stored_login(lab, monkeypatch, operation, raised):
    """The two error branches after a connection (an FtpError, and anything else such as the
    server's own refusal) mask what came from a stored value, like the connect stage.
    MUTATION: return the raw text from either operation-stage branch - red."""
    from vaf.core import user_secrets
    tmp, root, server = lab
    user_secrets.set_secret("FTP_USER", "alice", user_scope_id=ALICE)
    named = f"ftps://$FTP_USER@127.0.0.1:{server.port}"
    error = raised("alice")

    def _refuse(*a, **k):
        raise error

    monkeypatch.setattr(ftp, operation, _refuse)
    out = _tool(server, server=named, _call_confirmed=True)
    assert out.startswith(f"[FTP {named}]") and "Error:" in out, out
    assert "alice" not in out.replace(named, ""), out
    assert "/srv/ftp/<stored login>2" in out, out


def test_where_nobody_is_asked_the_model_never_reads_the_resolved_server(lab):
    """The first-connection question names the resolved host and port for the PERSON. A lane
    that cannot ask (another agent's turn, a VAF_NONINTERACTIVE run, an embedder by default)
    returned that question to the model, and $NAME takes ANY stored credential (measured: an
    API token as the host, a PIN as the port). MUTATION: pass model_reason=None from the
    funnel - red."""
    from vaf.core import user_secrets
    from vaf.core.tool_dispatch import ToolCaller
    from vaf.tools.ftp import FtpTool
    tmp, root, server = lab
    user_secrets.set_secret("API_TOKEN", "tok3n-Abcdef987654", user_scope_id=ALICE)
    user_secrets.set_secret("BANK_PIN", "48213", user_scope_id=ALICE)
    asked = []
    for written in ("ftp://u@$API_TOKEN", "ftp://u@h.example:$BANK_PIN"):
        caller = ToolCaller({"ftp": FtpTool()}, interactive=False, user_scope_id=ALICE,
                            username="alice", user_role="admin", session_id="web_chat-1")
        out = caller.execute("ftp", {"server": written})
        assert out.startswith("[ERROR] Tool 'ftp' requires confirmation"), out
        assert "tok3n" not in out and "48213" not in out, out
        caller = ToolCaller({"ftp": FtpTool()}, interactive=True, user_scope_id=ALICE,
                            username="alice", user_role="admin", session_id="web_chat-1",
                            decide=lambda name, reason, **k: asked.append(reason) or "cancel")
        caller.execute("ftp", {"server": written})
    # The person, asked, sees where the password would go.
    assert "tok3n-Abcdef987654" in asked[0] and "h.example:48213" in asked[1], asked
