# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The ftp tool (vaf/tools/ftp.py): its contract with the dispatcher and the person, against
the FTPS stub (tests/ftp_stub.py).

Pinned: the first connection is the person's to confirm and says when it is unencrypted, the
password is a stored NAME and never comes back, the transfers land where they should, and
host_bash hands FTP to this tool."""
import pytest

from tests.ftp_stub import FtpStub, make_cert
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


def _tool(server, **kw):
    from vaf.tools.ftp import FtpTool
    args = {"server": f"ftps://alice@127.0.0.1:{server.port}", "user_scope_id": ALICE,
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
                "ncftpput -u u example.org /x f", "bash -c 'lftp example.org'"):
        v = classify_command(cmd, profile="host")
        assert v.blocked and "ftp_transfer" in v.categories, cmd
        assert "ftp tool" in v.reason, cmd
    for cmd in ("curl https://example.org", "echo ftp://example.org", "git push"):
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
