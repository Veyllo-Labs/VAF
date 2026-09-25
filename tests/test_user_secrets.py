# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A person's stored credentials reach a command as an environment variable, never the model.

The measured gap: a credential typed into the chat travels with every later turn - in the session,
the timeline and the request to the model provider. Now the person stores it under a name, the
agent writes `$VAF_SECRET_<NAME>`, and the host tools hand over exactly the names a command uses,
scrubbed out of what the command prints.

Pinned here: per person (a scoped person does not see the owner's and the owner does not see a
scoped person's, although the owner's key prefix starts every scoped key); a command gets only
the secrets it names; the value never comes back from the tool, the route or the prompt.

MUTATION: hand over the whole store in `env_for` (drop the `wanted` filter) and the
only-what-it-names test goes red; drop the `scrub` of stdout in host_bash and the host_bash test
goes red; drop the `":" not in` check in `_own` and the owner sees a tenant's names.
"""
import os
import sys
import time

import pytest

from vaf.core import user_secrets as us

OWNER = None                  # the machine owner: no scope, the unscoped key form
TENANT = "ab12cd34-0000-4000-8000-00000000000a"
OTHER = "ab12cd34-0000-4000-8000-00000000000b"
VALUE = "hunter2-very-secret"
PY = f'"{sys.executable}"'


@pytest.fixture(autouse=True)
def _store_here(monkeypatch, tmp_path):
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(us, "_stores", {})


def test_names_are_normalized_and_bad_ones_refused():
    assert us.normalize_name("gportal ftp-password") == "GPORTAL_FTP_PASSWORD"
    # The variable as the agent writes it names the same entry.
    assert us.normalize_name("$VAF_SECRET_token") == us.normalize_name("VAF_SECRET_token") == "TOKEN"
    for bad in ("", "123abc", "___"):
        with pytest.raises(ValueError):
            us.normalize_name(bad)
    with pytest.raises(ValueError):
        us.set_secret("TOKEN", "   ", user_scope_id=TENANT)


def test_each_person_sees_only_their_own():
    us.set_secret("owner_token", "owner-value-1", user_scope_id=OWNER)
    us.set_secret("tenant_token", "tenant-value-1", user_scope_id=TENANT)
    assert us.names(user_scope_id=OWNER) == ["OWNER_TOKEN"], "the owner's prefix starts every key"
    assert us.names(user_scope_id=TENANT) == ["TENANT_TOKEN"]
    assert us.names(user_scope_id=OTHER) == []
    assert us.env_for("$VAF_SECRET_OWNER_TOKEN", user_scope_id=TENANT) == {}
    assert us.delete_secret("tenant_token", user_scope_id=OTHER) is False
    assert us.delete_secret("tenant_token", user_scope_id=TENANT) is True
    assert us.names(user_scope_id=TENANT) == []


def test_a_command_gets_only_what_it_names():
    us.set_secret("FTP_PASSWORD", VALUE, user_scope_id=TENANT)
    us.set_secret("API_TOKEN", "another-value-2", user_scope_id=TENANT)
    env = us.env_for("lftp -u me,$VAF_SECRET_FTP_PASSWORD host; echo $VAF_SECRET_MISSING",
                     user_scope_id=TENANT)
    assert env == {"VAF_SECRET_FTP_PASSWORD": VALUE}
    assert us.scrub(f"login {VALUE} ok", env) == "login [VAF_SECRET_FTP_PASSWORD] ok"


def test_host_bash_hands_it_over_and_never_shows_it():
    from vaf.tools.host_bash import HostBashTool
    us.set_secret("FTP_PASSWORD", VALUE, user_scope_id=TENANT)
    us.set_secret("API_TOKEN", "another-value-2", user_scope_id=TENANT)
    # The second secret is looked for WITHOUT naming it (the prefix is split), so the command
    # names exactly one.
    code = ("import os; v=os.environ.get('VAF_SECRET_FTP_PASSWORD'); "
            "print('len', len(v or '')); print(v); "
            "print('handed', sorted(k for k in os.environ if k.startswith('VAF_SEC' + 'RET_')))")
    out = HostBashTool().run(command=f'{PY} -c "{code}"', user_scope_id=TENANT, username="tenant",
                             user_role="user")
    assert f"len {len(VALUE)}" in out, out
    assert VALUE not in out and "[VAF_SECRET_FTP_PASSWORD]" in out
    assert "handed ['VAF_SECRET_FTP_PASSWORD']" in out, "a secret it did not name stays unset"


def test_python_exec_hands_it_over_and_never_shows_it(monkeypatch):
    import vaf.tools.python_exec as pe
    monkeypatch.setattr(pe, "get_tool_policy", lambda name, scope=None: "allow")
    us.set_secret("API_TOKEN", VALUE, user_scope_id=TENANT)
    out = pe.PythonExecTool().run(code="import os; print(os.environ['VAF_SECRET_API_TOKEN'])",
                                  user_scope_id=TENANT)
    assert VALUE not in out and "[VAF_SECRET_API_TOKEN]" in out, out


def test_a_background_command_log_is_read_back_scrubbed(monkeypatch, tmp_path):
    from vaf.core import processes, task_queue
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "config_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(processes, "_registry", {})
    monkeypatch.setattr(task_queue.TaskQueue, "add", lambda self, **kw: None)
    env = {"VAF_SECRET_API_TOKEN": VALUE}
    record = processes.start(f'{PY} -c "import os; print(os.environ[\'VAF_SECRET_API_TOKEN\'])"',
                             session_id="web_chat-1", user_scope_id=TENANT, secret_env=env)
    end = time.monotonic() + 10
    while record.running and time.monotonic() < end:
        time.sleep(0.05)
    try:
        tail = processes.read_tail(record)
        assert VALUE not in tail and "[VAF_SECRET_API_TOKEN]" in tail, tail
        assert VALUE not in processes.wake_text(record)
    finally:
        processes.terminate_all()


def test_the_model_reads_names_only():
    us.set_secret("FTP_PASSWORD", VALUE, user_scope_id=TENANT)
    note = us.prompt_note(user_scope_id=TENANT)
    assert "$VAF_SECRET_FTP_PASSWORD" in note and VALUE not in note
    assert us.prompt_note(user_scope_id=OTHER) == ""


def test_the_routes_never_return_a_value():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from vaf.api.secrets_routes import router

    app = FastAPI()

    @app.middleware("http")
    async def _as_tenant(request, call_next):
        request.state.user = {"user_scope_id": TENANT, "username": "tenant", "role": "user"}
        return await call_next(request)

    app.include_router(router)
    client = TestClient(app)
    put = client.put("/api/secrets/ftp password", json={"value": VALUE})
    assert put.status_code == 200 and put.json() == {"name": "FTP_PASSWORD",
                                                     "env": "VAF_SECRET_FTP_PASSWORD"}
    listed = client.get("/api/secrets")
    assert VALUE not in listed.text and listed.json()["names"][0]["name"] == "FTP_PASSWORD"
    assert client.put("/api/secrets/9", json={"value": "x1234"}).status_code == 400
    assert client.delete("/api/secrets/FTP_PASSWORD").json() == {"deleted": True}
    assert client.delete("/api/secrets/FTP_PASSWORD").status_code == 404
    assert us.names(user_scope_id=OWNER) == [], "the tenant's writes stayed the tenant's"


def test_the_terminal_command_stores_for_the_machine_owner(monkeypatch):
    from typer.testing import CliRunner
    import vaf.cli.cmd.secrets as cli
    monkeypatch.setattr(cli, "_identity", lambda: ("owner", None))
    runner = CliRunner()
    res = runner.invoke(cli.app, ["set", "db password"], input=VALUE + "\n")
    assert res.exit_code == 0 and VALUE not in res.output
    assert us.names(user_scope_id=None, username="owner") == ["DB_PASSWORD"]
    listed = runner.invoke(cli.app, ["list"])
    assert "VAF_SECRET_DB_PASSWORD" in listed.output and VALUE not in listed.output
    assert runner.invoke(cli.app, ["rm", "DB_PASSWORD"]).exit_code == 0
    assert us.names(user_scope_id=None, username="owner") == []


def test_the_turn_block_carries_the_note_when_a_host_tool_is_loaded():
    import inspect
    from vaf.core.system_prompt import SystemPromptManager
    src = inspect.getsource(SystemPromptManager._build_tool_documentation)
    assert '"host_bash" in tool_names or "python_exec" in tool_names' in src
    assert "{_vision_note}{_secrets_note}" in src
