# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""SSH to another machine as ONE account (vaf/core/ssh.py, vaf/tools/ssh.py).

Measured before this existed: `ssh` through host_bash asked its password on a terminal nobody
sees (VAF's own), asked "continue connecting?" the same way, and used the machine owner's
~/.ssh, config and agent for every account alike.

What is pinned here, against a FAKE `ssh` on PATH (a real server needs a daemon; the live
test covers that) and the REAL `ssh-keygen`, so the key's passphrase is proven by OpenSSH
itself:
- each account has its own folder, key, passphrase and known servers;
- a password or passphrase never appears in a command line, a result or a log, and one call
  never carries both;
- the first connection to a server needs a confirmed call, a changed server key is refused;
- the server name cannot smuggle an option, and a path with a space or a `%` survives;
- sudo reads its password and the command never can;
- a timeout stops the whole process group; Windows is refused clearly.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("ssh-keygen") is None,
    reason="POSIX OpenSSH client lane (Windows is refused, pinned separately)")

ALICE = "ab12cd34-0000-4000-8000-00000000a11c"     # synthetic scopes, never real ones
BOB = "ab12cd34-0000-4000-8000-000000000b0b"
PASSWORD = "Test-Server-Pass-4711"
SUDO_PASSWORD = "Test-Sudo-Pass-0815"


FAKE_SSH = r'''#!{python}
"""A stand-in for OpenSSH's client: known_hosts, the prompt answer, then the "remote" command
run HERE in a shell (a sudo command is recorded, never run)."""
import json, os, subprocess, sys, time
argv = sys.argv[1:]
opts, ident, port, dest, cmd, i = {{}}, None, "22", None, "", 0
while i < len(argv):
    a = argv[i]
    if a == "-o":
        k, _, v = argv[i + 1].partition("="); opts[k] = v; i += 2; continue
    if a in ("-F", "-i", "-p"):
        if a == "-i": ident = argv[i + 1].replace("%%", "%")
        if a == "-p": port = argv[i + 1]
        i += 2; continue
    if a == "-T":
        i += 1; continue
    if a == "--":
        cmd = " ".join(argv[i + 1:]); break
    dest = a; i += 1
record = {{"argv": argv, "password_env": "VAF_ASKPASS_PASSWORD" in os.environ,
           "passphrase_env": "VAF_ASKPASS_PASSPHRASE" in os.environ,
           "auth_sock": "SSH_AUTH_SOCK" in os.environ, "cmd": cmd}}
def done(code, out=b"", err=""):
    record["code"] = code
    with open(os.environ["FAKE_SSH_LOG"], "a") as f:
        f.write(json.dumps(record) + "\n")
    sys.stdout.buffer.write(out); sys.stderr.write(err); sys.exit(code)
kh = opts["UserKnownHostsFile"].strip('"').replace("%%", "%")
host = dest.split("@", 1)[1]
name = host if port == "22" else "[" + host + "]:" + port
server_key = os.environ["FAKE_SSH_HOSTKEY"]
lines = []
if os.path.exists(kh):
    lines = [l for l in open(kh).read().splitlines() if l.split(" ", 1)[0] == name]
if lines and lines[0].split(" ", 1)[1].strip() != server_key:
    done(255, err="@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\nHost key verification failed.\n")
if not lines:
    if opts.get("StrictHostKeyChecking") != "accept-new":
        done(255, err="Host key verification failed.\n")
    with open(kh, "a") as f:
        f.write(name + " " + server_key + "\n")
    added = "Warning: Permanently added '" + name + "' (ED25519) to the list of known hosts.\n"
else:
    added = ""
if opts.get("PubkeyAuthentication") == "no":
    r = subprocess.run([os.environ["SSH_ASKPASS"], dest + "'s password: "],
                       capture_output=True, text=True)
    ok = r.returncode == 0 and r.stdout.strip() == os.environ.get("FAKE_SSH_PASSWORD")
else:
    r = subprocess.run(["ssh-keygen", "-y", "-f", ident], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
    auth = os.path.join(os.environ["HOME"], ".ssh", "authorized_keys")
    installed = os.path.exists(auth) and open(auth).read()
    ok = r.returncode == 0 and bool(installed) and r.stdout.split()[1] in installed
record["auth_ok"] = ok
if not ok:
    done(255, err=added + dest + ": Permission denied (publickey,password).\n")
data = sys.stdin.buffer.read()
record["stdin"] = data[:200].decode("utf-8", "replace")
if cmd.startswith("sudo "):
    record["sudo"] = True
    done(0, out=b"SUDO-LINE:" + data.split(b"\n", 1)[0] + b"\n", err=added)
if os.environ.get("FAKE_SSH_SLEEP"):
    time.sleep(float(os.environ["FAKE_SSH_SLEEP"]))
p = subprocess.run(["sh", "-c", cmd], input=data, capture_output=True)
done(p.returncode, out=p.stdout, err=added + p.stderr.decode("utf-8", "replace"))
'''


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """A data dir (with a space and a %), the fake ssh first on PATH, a server host key."""
    from vaf.core import user_secrets
    from vaf.core.platform import Platform
    data = tmp_path / "Application Support 100%" / ".vaf"      # the key folder's home
    data.mkdir(parents=True)
    store = tmp_path / "data"                                    # the credential store's
    store.mkdir()
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: data))
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: store))
    monkeypatch.setattr(user_secrets, "_stores", {})
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "ssh"
    fake.write_text(FAKE_SSH.format(python=sys.executable), encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    home = tmp_path / "server-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "agent.sock"))
    log = tmp_path / "ssh.log"
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    monkeypatch.setenv("FAKE_SSH_PASSWORD", PASSWORD)
    monkeypatch.setenv("FAKE_SSH_HOSTKEY", _host_key(tmp_path / "hk"))
    user_secrets.set_secret("SERVER_PASS", PASSWORD, user_scope_id=ALICE)
    user_secrets.set_secret("SUDO_PASS", SUDO_PASSWORD, user_scope_id=ALICE)

    class Lab:
        root = tmp_path
        data_dir = data
        server_home = home

        @staticmethod
        def calls():
            if not log.exists():
                return []
            return [json.loads(line) for line in log.read_text().splitlines()]

        @staticmethod
        def rekey():
            monkeypatch.setenv("FAKE_SSH_HOSTKEY", _host_key(tmp_path / f"hk{time.time_ns()}"))

    return Lab


def _host_key(path):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)],
                   check=True, stdin=subprocess.DEVNULL)
    return " ".join(Path(str(path) + ".pub").read_text().split()[:2])


def _tool(**kw):
    from vaf.tools.ssh import SshTool
    args = {"server": "tester@127.0.0.1:2222", "user_scope_id": ALICE, "username": "alice",
            "user_role": "user"}
    args.update(kw)
    return SshTool().run(**args)


# ── the server name ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,user,host,port", [
    ("tester@127.0.0.1:2222", "tester", "127.0.0.1", 2222),
    ("root@example.org", "root", "example.org", 22),
    ("deploy@[::1]:2200", "deploy", "::1", 2200),
])
def test_a_server_is_read_as_user_host_and_port(text, user, host, port):
    from vaf.core.ssh import parse_server
    t = parse_server(text)
    assert (t.user, t.host, t.port) == (user, host, port)


@pytest.mark.parametrize("text", [
    "-oProxyCommand=touch /tmp/x", "u@-oProxyCommand=x", "u@host;rm -rf /", "u@host x",
    "host-without-user", "u@host:99999", "u@host:22x", "", "u@[zz::1]:22",
])
def test_nothing_else_is_a_server(text):
    """Measured: `ssh -G -F none localhost -oProxyCommand=echo` takes an option even after
    the host, so the name must never be able to start one."""
    from vaf.core.ssh import SshError, parse_server
    with pytest.raises(SshError):
        parse_server(text)


# ── the account's own identity ──────────────────────────────────────────────

def test_each_account_has_its_own_folder_key_and_passphrase(lab):
    from vaf.core import ssh, user_secrets
    a = ssh.ensure_identity(ALICE)
    b = ssh.ensure_identity(BOB)
    owner = ssh.ensure_identity(None)
    assert len({a.parent, b.parent, owner.parent}) == 3
    assert ssh.public_key(ALICE) != ssh.public_key(BOB)
    assert ssh._passphrase(ALICE) != ssh._passphrase(BOB)
    assert oct(a.parent.stat().st_mode & 0o777) == "0o700"
    assert oct(a.stat().st_mode & 0o777) == "0o600"
    # The passphrase is no command credential: not listed, never handed to a command.
    assert user_secrets.names(user_scope_id=ALICE) == ["SERVER_PASS", "SUDO_PASS"]
    assert user_secrets.env_for("$VAF_SECRET_KEY_PASSPHRASE", user_scope_id=ALICE) == {}


def test_the_key_is_encrypted_and_never_replaced(lab):
    """OpenSSH itself must refuse the key without its passphrase; a second call must not
    make a new one (that would lock the account out of every server)."""
    from vaf.core import ssh
    key = ssh.ensure_identity(ALICE)
    first = key.read_bytes()
    r = subprocess.run(["ssh-keygen", "-y", "-P", "", "-f", str(key)], capture_output=True,
                       text=True, stdin=subprocess.DEVNULL)
    assert r.returncode != 0, "the private key opened without its passphrase"
    ssh.ensure_identity(ALICE)
    assert key.read_bytes() == first
    assert ssh.public_key(ALICE).endswith(ssh.KEY_COMMENT)


def test_the_prompt_answer_matches_the_prompt_and_nothing_else(lab):
    from vaf.core import ssh
    helper = str(ssh.askpass_helper())

    def ask(prompt, **answers):
        env = {"PATH": os.environ["PATH"]}
        for keep in ("SYSTEMROOT", "SystemRoot", "SYSTEMDRIVE", "SystemDrive"):
            if keep in os.environ:
                env[keep] = os.environ[keep]
        env.update(answers)
        return subprocess.run([helper, prompt], capture_output=True, text=True, env=env)

    assert ask("Enter passphrase for key 'k': ", VAF_ASKPASS_PASSPHRASE="pp").stdout == "pp\n"
    assert ask("u@h's password: ", VAF_ASKPASS_PASSWORD="pw").stdout == "pw\n"
    assert ask("Are you sure you want to continue connecting (yes/no)? ",
               VAF_ASKPASS_PASSWORD="pw").returncode != 0
    # A key prompt with only the password around gets nothing, and the other way round.
    assert ask("Enter passphrase for key 'k': ", VAF_ASKPASS_PASSWORD="pw").returncode != 0
    assert ask("(u@h) Password: ", VAF_ASKPASS_PASSPHRASE="pp").returncode != 0


def test_one_call_never_carries_both_secrets():
    from vaf.core.ssh import SshError, _env
    with pytest.raises(SshError):
        _env(password="a", passphrase="b")


def test_the_command_line_keeps_the_owners_setup_out_and_survives_odd_paths(lab):
    from vaf.core import ssh
    t = ssh.parse_server("tester@127.0.0.1:2222")
    argv = ssh.build_argv(t, "uptime", user_scope_id=ALICE, password_login=False)
    joined = " ".join(argv)
    for option in ("-F none", "IdentitiesOnly=yes", "IdentityAgent=none",
                   "StrictHostKeyChecking=accept-new", "PasswordAuthentication=no",
                   "KbdInteractiveAuthentication=no"):
        assert option in joined, option
    assert argv[-2:] == ["--", "uptime"] and argv[-3] == "tester@127.0.0.1"
    kh = next(a for a in argv if a.startswith("UserKnownHostsFile="))
    assert kh.startswith('UserKnownHostsFile="') and "100%%" in kh, kh
    argv = ssh.build_argv(t, "uptime", user_scope_id=ALICE, password_login=True)
    assert "PubkeyAuthentication=no" in argv and "-i" not in argv


# ── the tool, end to end against the fake server ────────────────────────────

def test_an_unconfirmed_first_connection_is_refused(lab):
    out = _tool(command="uptime", login_credential="SERVER_PASS")
    assert out.startswith("Error:") and "not connected to yet" in out
    assert lab.calls() == [], "ssh ran before anyone confirmed the server"


def test_a_confirmed_first_connection_names_the_fingerprint_and_keeps_the_password_out(lab):
    out = _tool(command="echo hello-from-server", login_credential="SERVER_PASS",
                _call_confirmed=True)
    assert "hello-from-server" in out and out.rstrip().endswith("OK"), out
    assert "First connection" in out and "SHA256:" in out
    call = lab.calls()[-1]
    assert call["password_env"] and not call["passphrase_env"]
    assert not call["auth_sock"], "the owner's ssh-agent reached the call"
    assert PASSWORD not in json.dumps(call["argv"]) and PASSWORD not in out
    # Known now: the next call needs no confirmation.
    assert "hello-again" in _tool(command="echo hello-again", login_credential="SERVER_PASS")


def test_the_key_is_installed_once_and_then_logs_in_without_a_password(lab):
    _tool(command="true", login_credential="SERVER_PASS", _call_confirmed=True)
    out = _tool(action="install_key", login_credential="SERVER_PASS")
    assert "key is installed" in out, out
    out = _tool(command="echo by-key")
    assert "by-key" in out, out
    call = lab.calls()[-1]
    assert call["passphrase_env"] and not call["password_env"]
    installed = (lab.server_home / ".ssh" / "authorized_keys").read_text()
    assert installed.count("ssh-ed25519") == 1
    _tool(action="install_key", login_credential="SERVER_PASS")
    assert (lab.server_home / ".ssh" / "authorized_keys").read_text() == installed, \
        "installing twice added the key twice"


def test_a_refused_login_shows_the_public_key(lab):
    _tool(command="true", login_credential="SERVER_PASS", _call_confirmed=True)
    out = _tool(command="true")               # key login, key not installed
    from vaf.core import ssh
    assert "Login refused" in out and ssh.public_key(ALICE) in out


def test_a_changed_server_key_is_refused(lab):
    _tool(command="true", login_credential="SERVER_PASS", _call_confirmed=True)
    lab.rekey()
    out = _tool(command="echo should-not-run", login_credential="SERVER_PASS")
    assert "DIFFERENT key" in out and "should-not-run" not in out


def test_sudo_reads_its_password_and_the_output_never_shows_it(lab):
    _tool(command="true", login_credential="SERVER_PASS", _call_confirmed=True)
    out = _tool(command="apk update", as_root=True, login_credential="SERVER_PASS",
                sudo_credential="SUDO_PASS")
    call = lab.calls()[-1]
    assert call.get("sudo") and call["cmd"].startswith("sudo -S -p ''")
    assert "exec 0</dev/null" in call["cmd"], "the command could read the password line"
    assert call["stdin"].startswith(SUDO_PASSWORD)
    assert SUDO_PASSWORD not in out and "[VAF_SECRET_SUDO_PASS]" in out, out


def test_without_a_sudo_password_sudo_must_not_ask(lab):
    from vaf.core.ssh import as_root
    assert as_root("ls", with_password=False).startswith("sudo -n -- sh -c ")


def test_upload_and_download_go_through_the_same_login(lab, tmp_path):
    """As the machine owner (no file jail), so the transfer itself is what is measured."""
    from vaf.core import user_secrets
    user_secrets.set_secret("SERVER_PASS", PASSWORD)
    owner = {"user_scope_id": None, "username": None, "user_role": "admin"}
    _tool(command="true", login_credential="SERVER_PASS", _call_confirmed=True, **owner)
    local = tmp_path / "plugin.jar"
    local.write_bytes(b"\x00jar-bytes\xff" * 1000)
    remote = tmp_path / "remote-srv" / "plugin.jar"
    remote.parent.mkdir()
    out = _tool(action="upload", local_path=str(local), remote_path=str(remote),
                login_credential="SERVER_PASS", **owner)
    assert "Uploaded" in out and remote.read_bytes() == local.read_bytes(), out
    back = tmp_path / "back.jar"
    out = _tool(action="download", local_path=str(back), remote_path=str(remote),
                login_credential="SERVER_PASS", **owner)
    assert "Downloaded" in out and back.read_bytes() == local.read_bytes(), out
    assert not Path(str(back) + ".part").exists()


def test_a_folder_uploads_whole_without_links_or_history(lab, tmp_path):
    """A built site is a folder. MUTATION: drop the folder branch - red: "is not a file";
    drop the skip of links - red: a link to a file outside the folder travelled along."""
    from vaf.core import user_secrets
    user_secrets.set_secret("SERVER_PASS", PASSWORD)
    owner = {"user_scope_id": None, "username": None, "user_role": "admin"}
    _tool(command="true", login_credential="SERVER_PASS", _call_confirmed=True, **owner)
    site = tmp_path / "site"
    (site / "css").mkdir(parents=True)
    (site / "index.html").write_text("<h1>hi</h1>")
    (site / "css" / "a.css").write_text("body{}")
    (site / ".git").mkdir()
    (site / ".git" / "HEAD").write_text("ref: refs/heads/main")
    (tmp_path / "outside.txt").write_text("not part of the site")
    try:
        os.symlink(str(tmp_path / "outside.txt"), str(site / "leak"))
    except (OSError, NotImplementedError):
        pass
    remote = tmp_path / "remote-srv" / "www"
    out = _tool(action="upload", local_path=str(site), remote_path=str(remote),
                login_credential="SERVER_PASS", **owner)
    assert "Uploaded the folder" in out and "(2 files)" in out, out
    assert (remote / "index.html").read_text() == "<h1>hi</h1>"
    assert (remote / "css" / "a.css").read_text() == "body{}"
    assert not (remote / ".git").exists() and not os.path.lexists(remote / "leak")


def test_the_folder_limit_counts_the_archive_not_only_the_files(tmp_path):
    """Every file adds a 512-byte header. MUTATION: check only the summed sizes - red: sixty
    empty files passed a 10 kB limit as 0 bytes while the archive was over 30 kB."""
    from vaf.tools.ssh import SshTool
    folder = tmp_path / "many"
    folder.mkdir()
    for i in range(60):
        (folder / f"f{i}.txt").write_text("")
    with pytest.raises(ValueError, match="larger than"):
        SshTool._folder_stream(folder, 10_000)
    buf, count = SshTool._folder_stream(folder, 10_000_000)
    assert count == 60
    buf.close()


def test_a_regular_account_moves_only_its_own_files(lab, tmp_path):
    """The local side is under the account's write jail (file_access), like download_file."""
    _tool(command="true", login_credential="SERVER_PASS", _call_confirmed=True)
    elsewhere = tmp_path / "not-alices.txt"
    elsewhere.write_text("x")
    before = len(lab.calls())
    out = _tool(action="upload", local_path=str(elsewhere), remote_path="/tmp/x",
                login_credential="SERVER_PASS")
    assert out.startswith("Error") and "outside" in out, out
    assert len(lab.calls()) == before, "ssh ran for a file the account may not read"


def test_a_credential_file_on_this_computer_is_not_uploaded(lab):
    _tool(command="true", login_credential="SERVER_PASS", _call_confirmed=True)
    secret = lab.root / "server-home" / ".ssh"
    secret.mkdir(exist_ok=True)
    (secret / "id_rsa").write_text("PRIVATE")
    out = _tool(action="upload", local_path=str(secret / "id_rsa"), remote_path="/tmp/x",
                login_credential="SERVER_PASS")
    assert out.startswith("Error"), out


def test_a_catastrophic_remote_command_is_refused_before_it_is_sent(lab):
    _tool(command="true", login_credential="SERVER_PASS", _call_confirmed=True)
    before = len(lab.calls())
    out = _tool(command="rm -rf /", login_credential="SERVER_PASS")
    assert out.startswith("[BLOCKED]") and len(lab.calls()) == before


def test_an_unknown_credential_name_is_a_clear_error(lab):
    out = _tool(command="true", login_credential="NOT_STORED", _call_confirmed=True)
    assert out.startswith("Error:") and "store_credential" in out


def test_a_timeout_stops_the_whole_group(lab, monkeypatch):
    from vaf.core import ssh
    _tool(command="true", login_credential="SERVER_PASS", _call_confirmed=True)
    monkeypatch.setattr(ssh, "MIN_TIMEOUT_SECONDS", 1)
    monkeypatch.setenv("FAKE_SSH_SLEEP", "30")
    started = time.monotonic()
    out = _tool(command="echo late", login_credential="SERVER_PASS", timeout=1)
    assert time.monotonic() - started < 15, "the call waited for the command"
    assert "time limit" in out and "late" not in out


# ── the question before the call ────────────────────────────────────────────

def test_the_tool_asks_about_a_new_server_and_not_about_a_known_one(lab):
    from vaf.tools.ssh import SshTool
    tool = SshTool()
    reason = tool.ask_reason({"server": "tester@127.0.0.1:2222"}, user_scope_id=ALICE)
    assert reason and "First connection" in reason
    _tool(command="true", login_credential="SERVER_PASS", _call_confirmed=True)
    assert tool.ask_reason({"server": "tester@127.0.0.1:2222"}, user_scope_id=ALICE) is None
    assert tool.ask_reason({"server": "tester@127.0.0.1:2222"}, user_scope_id=BOB), \
        "a server one account confirmed is new to another"
    assert "could not be checked" in tool.ask_reason({"server": "-oX=y"}, user_scope_id=ALICE)


def test_the_contract_the_policy_reads():
    from vaf.tools.ssh import SshTool
    t = SshTool
    assert t.permission_level == "dangerous" and "channel" in t.channel_restrictions
    assert t.trusted_dir_grants is False and t.account_opt_in is True
    assert t.accepts_call_confirmation is True
    for name in ("login_credential", "sudo_credential"):
        assert "NAME" in t.parameters["properties"][name]["description"], \
            f"{name} must take a stored NAME, never a value"


def test_the_dialog_shows_which_stored_credential_a_call_uses():
    """Measured in the live test: named `password_secret`, the credential NAME showed as
    [redacted] in the confirmation dialog, so the person could not see which one was used."""
    from vaf.core.arg_preview import build_preview, mask_secret_args
    from vaf.tools.ssh import SshTool
    args = {"server": "u@h", "command": "uptime", "login_credential": "SERVER_PASS",
            "sudo_credential": "SUDO_PASS"}
    text = build_preview("ssh", mask_secret_args(args, SshTool.secret_args))["text"]
    assert "SERVER_PASS" in text and "SUDO_PASS" in text, text


def test_host_bash_points_a_login_elsewhere_at_the_ssh_tool():
    from vaf.tools.host_bash import HostBashTool
    out = HostBashTool().run(command="ssh root@example.org uptime")
    assert out.startswith("[BLOCKED]") and "ssh tool" in out


def test_windows_is_refused_clearly(monkeypatch):
    """Pinned on every OS (the GAP-CLOSING rule): the branch is only ever taken on Windows."""
    from vaf.core import ssh
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "is_windows", staticmethod(lambda: True))
    with pytest.raises(ssh.SshError, match="Windows"):
        ssh.require_openssh()


def test_no_file_tool_can_open_the_key(lab):
    """The folder lives under the VAF directory, which the file tools refuse for everyone."""
    from vaf.core import ssh
    from vaf.tools.filesystem import is_safe_path
    key = ssh.ensure_identity(None)
    ok, _ = is_safe_path(str(key))
    assert ok is False, "read_file could open the account's SSH key"


# ── Settings, Connections, SSH and `vaf ssh` ─────────────────────────────────

def _routes_client(role, scope, allowed):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from vaf.api.ssh_routes import router
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


def test_the_settings_route_is_closed_to_an_account_without_ssh(lab, _resolver_restored):
    """An unrestricted account ("everything") is not granted an opt-in tool."""
    client = _routes_client("user", ALICE, None)
    assert client.get("/api/ssh").status_code == 403
    assert client.post("/api/ssh/key").status_code == 403


def test_the_settings_route_shows_the_key_and_the_servers(lab, _resolver_restored):
    _tool(command="true", login_credential="SERVER_PASS", _call_confirmed=True)
    client = _routes_client("user", ALICE, frozenset({"ssh"}))
    first = client.get("/api/ssh").json()
    assert first["available"] and first["public_key"] is None
    assert first["hosts"][0]["host"] == "[127.0.0.1]:2222"
    made = client.post("/api/ssh/key").json()["public_key"]
    assert made.startswith("ssh-ed25519 ")
    assert client.post("/api/ssh/key").json()["public_key"] == made, "a key was replaced"
    assert "PRIVATE KEY" not in client.get("/api/ssh").text
    assert client.delete("/api/ssh/hosts/[127.0.0.1]:2222").json() == {"deleted": True}
    assert client.get("/api/ssh").json()["hosts"] == []
    assert client.delete("/api/ssh/hosts/[127.0.0.1]:2222").status_code == 404


def test_the_terminal_command_prints_the_key_and_forgets_a_server(lab, monkeypatch):
    from typer.testing import CliRunner
    import vaf.cli.cmd.ssh as cli
    from vaf.core import ssh
    monkeypatch.setattr(cli, "_scope", lambda: None)
    runner = CliRunner()
    res = runner.invoke(cli.app, ["key"])
    assert res.exit_code == 0 and res.output.strip() == ssh.public_key(None)
    assert "No servers yet" in runner.invoke(cli.app, ["hosts"]).output
    from vaf.core import user_secrets
    user_secrets.set_secret("SERVER_PASS", PASSWORD)
    _tool(command="true", login_credential="SERVER_PASS", _call_confirmed=True,
          user_scope_id=None, username=None, user_role="admin")
    assert "[127.0.0.1]:2222" in runner.invoke(cli.app, ["hosts"]).output
    assert runner.invoke(cli.app, ["forget", "[127.0.0.1]:2222"]).exit_code == 0
    assert runner.invoke(cli.app, ["forget", "[127.0.0.1]:2222"]).exit_code == 1


def test_every_tool_list_is_built_by_one_projection():
    """Five hand copies of the tool entry meant a new field (account_opt_in) reached some
    lists and not others; the account picker's "standard" preset reads it."""
    import re
    root = Path(__file__).resolve().parent.parent
    for rel in ("vaf/core/web_server.py", "vaf/core/web_interface.py"):
        src = (root / rel).read_bytes().decode("utf-8")
        assert not re.search(r'"category":\s*tool_category\(', src), f"{rel} builds an entry by hand"
    from vaf.core.tool_contract import tool_list_entry
    from vaf.tools.ssh import SshTool
    assert tool_list_entry("ssh", SshTool)["account_opt_in"] is True
    modal = (root / "web/components/SettingsModal.tsx").read_bytes().decode("utf-8")
    assert modal.count("!t.account_opt_in") == 2, "a preset of everything must leave it out"


def test_an_unreadable_store_is_named_as_such_not_as_a_missing_key(lab, monkeypatch):
    """Measured on a machine whose key store could not be opened: the key login failed and the
    tool said "the key is not installed on the server" - the wrong place to look."""
    from vaf.core import ssh, user_secrets
    _tool(command="true", login_credential="SERVER_PASS", _call_confirmed=True)
    ssh.ensure_identity(ALICE)

    def _broken():
        raise RuntimeError("raw KEK missing")

    monkeypatch.setattr(user_secrets, "_store", _broken)
    out = _tool(command="true")
    assert out.startswith("Error:") and "credential store is not readable" in out, out
    assert "not installed" not in out


@pytest.mark.parametrize("text,forced", [
    ("update meinen vps bitte", True),
    ("log dich per ssh auf den server ein", True),
    ("mein server ist tester@203.0.113.7, passwort kommt gleich", True),
    ("starte den minecraft server neu", False),      # "server" alone says nothing
    ("die version ist 1.21.4", False),               # not an address
    ("sshd läuft nicht", False),                     # a word containing ssh is not ssh
])
def test_the_router_offers_ssh_when_a_machine_is_named(text, forced):
    from vaf.core.agent import _SSH_ROUTE_RE
    assert bool(_SSH_ROUTE_RE.search(text.lower())) is forced


def test_a_download_target_that_cannot_be_written_is_an_error_before_ssh_runs(lab, tmp_path):
    """Review finding: the target was opened after ssh had started, so a target that could not
    be opened raised out of the call with a live child, its timer and its reader threads."""
    from vaf.core import user_secrets
    user_secrets.set_secret("SERVER_PASS", PASSWORD)
    owner = {"user_scope_id": None, "username": None, "user_role": "admin"}
    _tool(command="true", login_credential="SERVER_PASS", _call_confirmed=True, **owner)
    before = len(lab.calls())
    out = _tool(action="download", remote_path="/etc/hostname",
                local_path=str(tmp_path / "no-such-folder" / "x"), login_credential="SERVER_PASS",
                **owner)
    assert out.startswith("Error:") and "cannot be written" in out, out
    assert len(lab.calls()) == before, "ssh ran although the target could not be written"
