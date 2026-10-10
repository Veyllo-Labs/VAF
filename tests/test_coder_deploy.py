# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The coder deploys only where its caller sent it: coding_agent(deploy_to=...).

The main agent decides, from what the user asked, whether a run's result goes onto a server.
Only then does the coder get the ssh tool, for that one server, with uploads under that one
folder, and the server must be one the user already confirmed - the coder runs unattended,
and nobody could be asked about a new fingerprint. Without deploy_to it has no ssh at all."""
import types

import pytest

from vaf.core import ssh
from vaf.tools import coder


@pytest.fixture
def known(monkeypatch):
    seen = set()
    monkeypatch.setattr(ssh, "is_known", lambda target, scope: str(target) in seen)
    return seen


def test_a_deploy_target_is_a_server_and_a_folder(known):
    known.add(str(ssh.parse_server("deploy@example.org:2222")))
    d, err = coder._deploy_target("deploy@example.org:2222:/var/www/site/", "s")
    assert err is None and str(d.target) == str(ssh.parse_server("deploy@example.org:2222"))
    assert d.root == "/var/www/site"
    for bad in ("deploy@example.org", "deploy@example.org:/", "/var/www", ""):
        assert coder._deploy_target(bad, "s")[0] is None, bad


def test_a_server_nobody_confirmed_is_refused_before_anything_is_built(known):
    """MUTATION: drop the is_known check - red: the run would find out at its very end."""
    d, err = coder._deploy_target("deploy@new.example.org:/srv/app", "s")
    assert d is None and "not confirmed" in err and 'ssh(server="deploy@new.example.org"' in err


def test_the_run_refuses_early_and_spawns_nothing(known, monkeypatch):
    import vaf.core.subagent_spawn as spawn
    spawned = []
    monkeypatch.setattr(spawn, "spawn_subagent", lambda *a, **k: spawned.append(a))
    out = coder.CodingAgentTool().run(task="build and deploy", deploy_to="u@new.example.org:/srv/x")
    assert out.startswith("Error:") and "not confirmed" in out and spawned == []


def test_the_spawn_passes_the_target_as_an_argument(known, monkeypatch):
    """Rule 4.5, like --environment. MUTATION: drop --deploy-to from the spawn - red."""
    import vaf.core.config as cfg
    import vaf.core.subagent_spawn as spawn
    known.add(str(ssh.parse_server("u@example.org")))
    monkeypatch.delenv("VAF_IN_SUBAGENT_TERMINAL", raising=False)
    real_get = cfg.Config.get
    monkeypatch.setattr(cfg.Config, "get", classmethod(
        lambda cls, k, d=None: True if k == "sub_agents_in_separate_terminals" else real_get(k, d)))
    seen = {}

    def _spawn(kind, task, args=(), extra_env=None, **kw):
        seen.update(args=args, env=extra_env or {})
        return types.SimpleNamespace(marker="[SUBAGENT_ASYNC:t:coding_agent]")

    monkeypatch.setattr(spawn, "spawn_subagent", _spawn)
    coder.CodingAgentTool().run(task="deploy it", project_path="/tmp/proj-y",
                                deploy_to="u@example.org:/var/www/site")
    assert seen["args"][-2:] == ("--deploy-to", f"{ssh.parse_server('u@example.org')}:/var/www/site")
    assert not any("example.org" in str(v) for v in seen["env"].values())


def _deploy():
    return coder.DeployTarget(ssh.parse_server("deploy@example.org"), "/var/www/site")


def test_without_a_target_there_is_no_ssh_and_with_one_only_that_server():
    """MUTATION: return None from _deploy_refusal - red."""
    assert "not available in this run" in coder._deploy_refusal("ssh", {"command": "ls"}, None)
    args = {"command": "systemctl restart site"}
    assert coder._deploy_refusal("ssh", args, _deploy()) is None
    assert args["server"] == str(_deploy().target)                 # pinned
    assert "only" in coder._deploy_refusal("ssh", {"server": "root@evil.example", "command": "x"}, _deploy())
    assert "install" in coder._deploy_refusal("ssh", {"action": "install_key"}, _deploy())
    assert coder._deploy_refusal("bash", {"command": "ls"}, None) is None


def test_uploads_land_under_the_target_folder_only():
    up = {"action": "upload", "local_path": "dist", "remote_path": "assets"}
    assert coder._deploy_refusal("ssh", up, _deploy()) is None
    assert up["remote_path"] == "/var/www/site/assets"
    for outside in ("/etc", "/var/www/site/../other", "../../root/.ssh/authorized_keys"):
        msg = coder._deploy_refusal("ssh", {"action": "upload", "remote_path": outside}, _deploy())
        assert msg and "under /var/www/site" in msg, outside
    assert coder._deploy_refusal("ssh", {"action": "upload", "remote_path": "/var/www/site"}, _deploy()) is None


def test_a_relative_local_path_is_the_projects_not_the_chats():
    """The ssh tool reads a relative path against the chat workspace. MUTATION: drop the
    base_dir join - red: `dist` was looked for in the wrong folder."""
    import os
    up = {"action": "upload", "local_path": "dist", "remote_path": "/var/www/site"}
    assert coder._deploy_refusal("ssh", up, _deploy(), "/home/user/proj") is None
    assert up["local_path"] == os.path.join("/home/user/proj", "dist")
    absolute = {"action": "upload", "local_path": "/tmp/build", "remote_path": "/var/www/site"}
    coder._deploy_refusal("ssh", absolute, _deploy(), "/home/user/proj")
    assert absolute["local_path"] == "/tmp/build"
    down = {"action": "download", "remote_path": "logs/error.log"}
    coder._deploy_refusal("ssh", down, _deploy(), "/home/user/proj")
    assert down["local_path"] == os.path.join("/home/user/proj", "error.log")


def test_ssh_is_registered_and_advertised_only_with_a_target():
    """MUTATION: let auto-discovery add ssh, or advertise it without a target - red."""
    import inspect
    src = inspect.getsource(coder.CodingAgentTool.run)
    assert '"ssh",      # only with deploy_to' in src
    schema_at = src.index('"name": "ssh"')
    assert "if _deploy is not None:" in src[schema_at - 300:schema_at]
    assert 'self.local_tools.pop("ssh", None)' in src
    assert "or _deploy_refusal(fn_name, fn_args, _deploy, base_dir))" in src
    from vaf.core.coder_tools import CODER_ALLOWED_TOOLS
    assert "ssh" in CODER_ALLOWED_TOOLS


def test_the_child_cli_hands_the_target_to_the_coder():
    import ast
    from pathlib import Path
    import vaf.cli.cmd.subagent as sub
    tree = ast.parse(Path(sub.__file__).read_text(encoding="utf-8"))
    run = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run_subagent")
    src = ast.unparse(run)
    assert "kwargs['deploy_to'] = deploy_to" in src and "'--deploy-to'" in src


def test_the_main_agent_reads_who_deploys_from_both_tools():
    """The sandbox lesson: a link that runs one way only left the main agent searching. The
    ssh tool says the coder deploys what it builds; coding_agent says how and for what.
    MUTATION: drop either sentence - red."""
    from vaf.tools.ssh import SshTool
    assert "coding_agent with deploy_to=" in SshTool.description
    assert "server work that builds nothing" in SshTool.description
    hint = coder.CodingAgentTool.parameters["properties"]["deploy_to"]["description"]
    assert "ssh(server=" in hint and "SSH servers only" in hint
