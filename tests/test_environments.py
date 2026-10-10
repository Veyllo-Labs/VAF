# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Sandbox environments (vaf.core.environments), without a docker daemon.

A scriptable docker stands behind the one seam (`containers.docker`), the bounded exec
behind `containers.exec_bounded`. The tests pin the DECISIONS: who owns what (labels,
answered like a missing id for anyone else), what each network profile creates, the
limits and their reasons, that removal takes container, volume, network and state
together and can run twice, that the reaper asks docker whether an environment is busy,
that the scratch environment converges on one container, and that a missing scope means
the machine owner, never a shared bucket."""
import json
import re
import shlex
import types

import pytest

from vaf.core import containers, environment_image
from vaf.core import environments as envmod
from vaf.core.environments import EnvironmentManager, EnvironmentRefused

LABEL = envmod.LABEL
ALICE, BOB = "scope-alice", "scope-bob"


def _done(rc=0, out="", err=""):
    return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)


class FakeDocker:
    """Containers, volumes and networks with labels, enough of the CLI for the manager."""

    def __init__(self):
        self.containers = {}   # name -> {"state", "labels", "args", "networks"}
        self.volumes = {}      # name -> labels
        self.networks = {}     # name -> {"labels", "internal", "options"}
        self.calls = []
        self.busy = set()      # container names with a marked process
        self.fail_isolated = False
        self.fail_run = False
        self.image_ids = {}    # tag -> image id, for `image inspect` and a container's .Image

    @staticmethod
    def _labels(args):
        out = {}
        for i, a in enumerate(args):
            if a == "--label":
                k, _, v = args[i + 1].partition("=")
                out[k] = v
        return out

    @staticmethod
    def _filters(args):
        out = []
        for i, a in enumerate(args):
            if a == "--filter":
                out.append(args[i + 1])
        return out

    def _match(self, labels, filters):
        for f in filters:
            kind, _, rest = f.partition("=")
            if kind == "label":
                k, _, v = rest.partition("=")
                if labels.get(k) != v:
                    return False
        return True

    def __call__(self, args, timeout=60, *, input=None, env=None, binary=False):
        args = list(args)
        self.calls.append(args)
        head = args[0]
        if head == "network":
            sub, name = args[1], args[-1]
            if sub == "inspect":
                return _done(0, name + "\n") if name in self.networks else _done(1, "", "no such network")
            if sub == "create":
                opts = [args[i + 1] for i, a in enumerate(args) if a == "-o"]
                if self.fail_isolated and any("isolated" in o for o in opts):
                    return _done(1, "", "invalid option")
                self.networks[name] = {"labels": self._labels(args), "internal": "--internal" in args,
                                       "options": opts}
                return _done(0)
            if sub == "rm":
                return _done(0) if self.networks.pop(name, None) is not None else _done(1, "", "not found")
            if sub == "connect":
                net, cont = args[2], args[3]
                self.containers.setdefault(cont, {"state": "running", "labels": {}, "args": [], "networks": set()})
                self.containers[cont]["networks"].add(net)
                return _done(0)
            if sub == "disconnect":
                return _done(0)
            if sub == "ls":
                rows = [f"{n}\t{m['labels'].get(LABEL + '.id', '')}" for n, m in self.networks.items()
                        if self._match(m["labels"], self._filters(args))]
                return _done(0, "\n".join(rows))
        if head == "image" and args[1] == "inspect":
            tag = args[2]
            return _done(0, self.image_ids.get(tag, "sha256:" + tag) + "\n")
        if head == "volume":
            sub = args[1]
            if sub == "create":
                self.volumes[args[-1]] = self._labels(args)
                return _done(0)
            if sub == "rm":
                return _done(0) if self.volumes.pop(args[-1], None) is not None else _done(1, "", "no such volume")
            if sub == "ls":
                rows = [f"{n}\t{l.get(LABEL + '.id', '')}" for n, l in self.volumes.items()
                        if self._match(l, self._filters(args))]
                return _done(0, "\n".join(rows))
        if head == "run":
            if self.fail_run:
                return _done(1, "", "boom")
            name = args[args.index("--name") + 1]
            if name in self.containers:
                return _done(125, "", f'Conflict. The container name "/{name}" is already in use')
            net = args[args.index("--network") + 1]
            self.containers[name] = {"state": "running", "labels": self._labels(args), "args": args,
                                     "networks": {net}}
            return _done(0, "abc123\n")
        if head == "ps":
            filters = self._filters(args)
            rows = []
            for n, c in self.containers.items():
                l = c["labels"]
                if not l or not self._match(l, filters):
                    continue
                rows.append("\t".join([n, c["state"], l.get(LABEL + ".id", ""), l.get(LABEL + ".owner", ""),
                                       l.get(LABEL + ".kind", ""), l.get(LABEL + ".network", "")]))
            return _done(0, "\n".join(rows))
        if head == "start":
            c = self.containers.get(args[-1])
            if not c:
                return _done(1, "", "no such container")
            c["state"] = "running"
            return _done(0)
        if head == "stop":
            c = self.containers.get(args[-1])
            if c:
                c["state"] = "exited"
            return _done(0)
        if head == "rm":
            return _done(0) if self.containers.pop(args[-1], None) is not None else _done(1, "", "no such container")
        if head == "inspect":
            name = args[1]
            c = self.containers.get(name)
            if not c:
                return _done(1, "", "no such object")
            if "{{.State.Status}}" == args[-1]:
                return _done(0, c["state"] + "\n")
            fmt = args[-1]
            if ".proxy" in fmt:
                return _done(0, f"{c['state']}\t{c['labels'].get(LABEL + '.proxy', '')}\n")
            if fmt == "{{.Image}}":
                image = c["args"][c["args"].index("sleep") - 1] if "sleep" in c["args"] else ""
                return _done(0, self.image_ids.get(image, "sha256:" + image) + "\n")
            if "CapAdd" in fmt:
                caps = [c["args"][i + 1] for i, a in enumerate(c["args"]) if a == "--cap-add"]
                return _done(0, json.dumps(caps or None) + "\n")
            return _done(0, "\n")
        if head == "exec":
            name = next(a for a in args[1:] if a in self.containers)
            if containers.MARKED_PROCESSES_CMD in args:
                return _done(0, "42\n" if name in self.busy else "")
            return _done(0, "")
        raise AssertionError(f"unexpected docker call {args}")


@pytest.fixture
def docker(monkeypatch, tmp_path):
    fake = FakeDocker()
    monkeypatch.setattr(containers, "docker", fake)
    monkeypatch.setattr(containers, "mem_available_mb", lambda: 16000)
    monkeypatch.setattr(environment_image, "image_present", lambda tag=None: True)
    monkeypatch.setattr(environment_image, "usable_image", lambda: "vaf-sandbox-env:test")
    monkeypatch.setattr(environment_image, "image_tag", lambda: "vaf-sandbox-env:test")
    monkeypatch.setattr(environment_image, "start_background_build", lambda: None)
    for key in list(envmod.DEFAULTS):
        monkeypatch.delenv(f"VAF_SANDBOX_ENV_{key.upper()}", raising=False)
    import vaf.tools.filesystem as fs
    monkeypatch.setattr(fs, "jail_allows", lambda p, **k: True)
    return fake


@pytest.fixture
def mgr(tmp_path):
    return EnvironmentManager(state_dir=tmp_path / "envstate")


def _run_args(fake, name):
    return fake.containers[name]["args"]


# -- creating ----------------------------------------------------------------------

def test_a_temporary_environment_carries_identity_on_all_three_objects(docker, mgr):
    """Container, volume and network are labelled, so a crash between the three
    creates leaves nothing a label listing cannot find."""
    env = mgr.create(ALICE, kind="temporary", name="try")
    owner = containers.scope_hash(ALICE)
    assert env.container == f"vaf-env-{owner}-{env.id}"
    for labels in (docker.containers[env.container]["labels"], docker.volumes[env.volume],
                   docker.networks[env.net]["labels"]):
        assert labels[LABEL] == "1" and labels[LABEL + ".id"] == env.id
        assert labels[LABEL + ".owner"] == owner and labels[LABEL + ".kind"] == "temporary"
    assert ALICE not in json.dumps(docker.containers[env.container]["labels"])
    assert env.expires > env.created


def test_the_container_is_hardened(docker, mgr):
    env = mgr.create(ALICE)
    args = _run_args(docker, env.container)
    joined = " ".join(args)
    for flag in ("--init", "--cap-drop ALL", "no-new-privileges:true", "--pids-limit 512",
                 "--memory 1024m", "--restart no"):
        assert flag in joined, flag
    assert "--user" in args
    assert "docker.sock" not in joined and "--privileged" not in joined
    assert f"{env.volume}:/workspace" in args


@pytest.mark.parametrize("network, internal, isolated, proxy", [
    ("none", True, True, False),
    ("registries", True, True, True),
    ("open", False, False, False),
])
def test_each_network_profile(docker, mgr, network, internal, isolated, proxy):
    env = mgr.create(ALICE, network=network)
    net = docker.networks[env.net]
    assert net["internal"] is internal
    assert any("gateway_mode_ipv4=isolated" in o for o in net["options"]) is isolated
    args = _run_args(docker, env.container)
    assert ("HTTPS_PROXY=http://vaf-env-proxy:8888" in args) is proxy
    assert (env.net in docker.containers.get(envmod.PROXY_CONTAINER, {}).get("networks", set())) is proxy
    assert "host.docker.internal:host-gateway" not in args


def test_without_the_isolated_gateway_mode_the_environment_says_so(docker, mgr):
    """Docker before 28: an internal network still answers on its gateway. The
    environment is made, and degraded says why."""
    docker.fail_isolated = True
    env = mgr.create(ALICE, network="none")
    assert docker.networks[env.net]["internal"] is True
    assert "gateway" in env.degraded


def test_the_proxy_lets_only_anchored_registry_hosts_through(docker, mgr):
    mgr.create(ALICE, network="registries")
    proxy = docker.containers[envmod.PROXY_CONTAINER]
    assert proxy["labels"][LABEL + ".proxy"]
    flt = envmod.EnvironmentManager._proxy_filter(["pypi.org", "evil host", "nodot", "files.pythonhosted.org"])
    assert flt.splitlines() == [r"^pypi\.org$", r"^files\.pythonhosted\.org$"]


def test_the_admin_cap_on_the_network(docker, mgr, monkeypatch):
    monkeypatch.setenv("VAF_SANDBOX_ENV_NETWORK_MAX", "registries")
    with pytest.raises(EnvironmentRefused, match="above what the administrator allows"):
        mgr.create(ALICE, network="open")
    assert mgr.create(ALICE, network="registries")


def test_a_cap_that_is_no_profile_is_the_narrowest(docker, mgr, monkeypatch):
    """MUTATION: back to `cap in NETWORKS and ...` - red: a typo switched the cap off."""
    monkeypatch.setenv("VAF_SANDBOX_ENV_NETWORK_MAX", "registry")
    for network in ("open", "registries"):
        with pytest.raises(EnvironmentRefused, match="above what the administrator allows"):
            mgr.create(ALICE, network=network)
    assert mgr.create(ALICE, network="none")


def test_limits_refuse_with_a_reason(docker, mgr, monkeypatch):
    monkeypatch.setenv("VAF_SANDBOX_ENV_MAX_PER_USER", "2")
    mgr.create(ALICE)
    mgr.create(ALICE)
    with pytest.raises(EnvironmentRefused, match="the limit is 2"):
        mgr.create(ALICE)
    assert mgr.create(BOB)                                  # counted per person
    mgr.scratch_for(ALICE)                                  # scratch is not counted
    with pytest.raises(EnvironmentRefused, match="memory must be between"):
        mgr.create(BOB, memory_mb=99999)
    monkeypatch.setattr(containers, "mem_available_mb", lambda: 200)
    with pytest.raises(EnvironmentRefused, match="not enough free memory"):
        mgr.create(BOB)


def test_a_missing_image_starts_the_build_and_refuses_now(docker, mgr, monkeypatch):
    started = []
    monkeypatch.setattr(environment_image, "image_present", lambda tag=None: False)
    monkeypatch.setattr(environment_image, "start_background_build", lambda: started.append(1))
    with pytest.raises(EnvironmentRefused, match="being built"):
        mgr.create(ALICE)
    assert started == [1]
    assert not [n for n in docker.containers if n.startswith("vaf-env-")]


def test_a_failed_start_leaves_nothing_behind(docker, mgr):
    docker.fail_run = True
    with pytest.raises(EnvironmentRefused):
        mgr.create(ALICE)
    assert not docker.volumes and not [n for n in docker.networks if n.startswith("vaf-env-net")]
    assert not list((mgr._state_dir()).glob("*.json"))


def test_a_project_mount_must_be_the_persons_own_and_safe(docker, mgr, monkeypatch, tmp_path):
    """The project is bound read-write and relabelled (:z), so the same three questions
    as everywhere decide it: a safe project dir, not VAF's code or HOME, inside the
    person's file jail. MUTATION: skip the jail check - red."""
    import vaf.tools.filesystem as fs
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setattr(fs, "jail_allows", lambda p, **k: False)
    with pytest.raises(EnvironmentRefused, match="outside your own project folder"):
        mgr.create(ALICE, kind="project", project_path=str(proj))
    monkeypatch.setattr(fs, "jail_allows", lambda p, **k: True)
    env = mgr.create(ALICE, kind="project", project_path=str(proj))
    assert f"{proj.resolve()}:/workspace:z" in _run_args(docker, env.container)
    assert env.volume == ""
    from vaf.core import workspace_guard
    with pytest.raises(EnvironmentRefused, match="cannot be mounted"):
        mgr.create(ALICE, kind="project", project_path=str(workspace_guard._VAF_ROOT))
    with pytest.raises(EnvironmentRefused, match="not a directory"):
        mgr.create(ALICE, kind="project", project_path=str(tmp_path / "missing"))


# -- owning --------------------------------------------------------------------------

def test_someone_elses_environment_answers_like_a_missing_one(docker, mgr):
    env = mgr.create(ALICE)
    for op in (lambda: mgr.get(BOB, env.id), lambda: mgr.exec(BOB, env.id, "id"),
               lambda: mgr.delete(BOB, env.id), lambda: mgr.read_file(BOB, env.id, "x")):
        with pytest.raises(EnvironmentRefused, match=f"no environment '{env.id}'"):
            op()
    assert env.container in docker.containers
    assert [e.id for e in mgr.list(BOB)] == []
    assert [e.id for e in mgr.list(ALICE)] == [env.id]


def test_an_admin_may_delete_but_not_run(docker, mgr):
    env = mgr.create(ALICE)
    assert mgr.get(BOB, env.id, admin=True).id == env.id
    with pytest.raises(EnvironmentRefused):
        mgr.exec(BOB, env.id, "id")              # exec takes no admin flag at all
    mgr.delete(BOB, env.id, admin=True)
    assert env.container not in docker.containers
    assert {e.id for e in mgr.list(everyone=True)} == set()


def test_a_missing_scope_is_the_owner_never_a_shared_bucket(docker, mgr, monkeypatch):
    import vaf.core.config as cfg
    monkeypatch.setattr(cfg, "get_local_admin_scope_id", lambda: "scope-owner")
    env = mgr.create(None)
    assert env.owner == containers.scope_hash("scope-owner")
    monkeypatch.setattr(cfg, "get_local_admin_scope_id", lambda: "")
    with pytest.raises(EnvironmentRefused, match="no account"):
        mgr.create(None)
    with pytest.raises(EnvironmentRefused, match="no account"):
        mgr.scratch_for("")


def test_ids_that_are_not_ids_are_refused(docker, mgr):
    for bad in ("../x", "a b", "", "x" * 41):
        with pytest.raises(EnvironmentRefused):
            mgr.get(ALICE, bad)


# -- removing --------------------------------------------------------------------------

def test_delete_removes_all_four_and_can_run_twice(docker, mgr):
    env = mgr.create(ALICE, network="registries")
    mgr.delete(ALICE, env.id)
    assert env.container not in docker.containers
    assert env.volume not in docker.volumes and env.net not in docker.networks
    assert mgr._read_state(env.id) is None
    mgr._remove(env)                                    # a racing second remover: no error
    disconnects = [c for c in docker.calls if c[:2] == ["network", "disconnect"]]
    assert disconnects and disconnects[0][-1] == envmod.PROXY_CONTAINER


# -- the reaper --------------------------------------------------------------------------

def test_the_reaper_removes_expired_ones_and_spares_busy_ones(docker, mgr):
    old = mgr.create(ALICE)
    busy = mgr.create(ALICE)
    fresh = mgr.create(ALICE)
    for env in (old, busy):
        env.expires = 1.0
        mgr._write_state(env)
    docker.busy.add(busy.container)
    summary = mgr.reap_once()
    assert summary["removed"] == 1
    assert old.container not in docker.containers
    assert busy.container in docker.containers and fresh.container in docker.containers


def test_the_reaper_stops_idle_project_environments(docker, mgr, tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    import vaf.core.workspace_guard as wg
    monkeypatch.setattr(wg, "is_unsafe_project_dir", lambda p: False)
    env = mgr.create(ALICE, kind="project", project_path=str(proj))
    env.last_used = 1.0
    mgr._write_state(env)
    assert mgr.reap_once()["stopped"] == 1
    assert docker.containers[env.container]["state"] == "exited"
    assert env.container in docker.containers                  # stopped, never removed


def test_the_reaper_stops_the_proxy_when_no_registries_environment_runs(docker, mgr):
    env = mgr.create(ALICE, network="registries")
    mgr.reap_once()
    assert docker.containers[envmod.PROXY_CONTAINER]["state"] == "running"
    mgr.stop(ALICE, env.id)
    mgr.reap_once()
    assert docker.containers[envmod.PROXY_CONTAINER]["state"] == "exited"


def test_the_reaper_clears_orphans_from_a_crash(docker, mgr):
    docker.volumes["vaf-env-vol-dead0001"] = {LABEL: "1", LABEL + ".id": "dead0001"}
    docker.networks["vaf-env-net-dead0001"] = {"labels": {LABEL: "1", LABEL + ".id": "dead0001"},
                                               "internal": True, "options": []}
    summary = mgr.reap_once()
    assert summary["orphans"] == 2
    assert not docker.volumes and "vaf-env-net-dead0001" not in docker.networks


def test_the_reaper_keeps_the_process_records_of_live_environments(docker, mgr):
    """`<id>.procs.json` sits next to the state files. MUTATION: drop the skip in
    _clear_orphans - red: its stem matched no container, so every pass deleted the record
    of a running dev server, and with it the wake turn and host_process's handle."""
    env = mgr.create(ALICE)
    mgr._write_procs(env.id, {"p0000abcd": {"command": "npm run dev", "session_id": "s1"}})
    mgr.reap_once()
    assert mgr._read_procs(env.id) == {"p0000abcd": {"command": "npm run dev", "session_id": "s1"}}
    assert mgr._read_state(env.id) is not None


def test_a_docker_that_does_not_answer_clears_nothing(docker, mgr, monkeypatch):
    """MUTATION: let reap_once read a failed listing as an empty one - red: during a
    docker outage every record older than ten minutes was cleared as an orphan."""
    env = mgr.create(ALICE)
    env.created = 1.0
    mgr._write_state(env)

    def _down(args, timeout=60, **kw):
        if args[0] == "ps":
            return _done(1, "", "Cannot connect to the Docker daemon")
        return docker(args, timeout, **kw)

    monkeypatch.setattr(containers, "docker", _down)
    assert mgr.reap_once() == {"removed": 0, "stopped": 0, "orphans": 0}
    assert mgr._read_state(env.id) is not None and env.volume in docker.volumes
    assert mgr.list(ALICE) == []                    # an ordinary listing still reads it as none


def test_a_recreated_proxy_rejoins_every_registries_network(docker, mgr, monkeypatch):
    """The allowed hosts changed, so the proxy is recreated for the second environment.
    MUTATION: drop _reconnect_registries from _ensure_proxy - red: the first environment
    keeps no route to its package registries."""
    first = mgr.create(ALICE, network="registries")
    monkeypatch.setenv("VAF_SANDBOX_ENV_REGISTRY_HOSTS", "pypi.org")
    second = mgr.create(BOB, network="registries")
    nets = docker.containers[envmod.PROXY_CONTAINER]["networks"]
    assert {first.net, second.net, envmod.PROXY_NETWORK} <= nets


def test_busy_is_asked_of_docker_and_unknown_counts_as_busy(docker, mgr, monkeypatch):
    env = mgr.create(ALICE)
    env.state = "running"
    assert mgr.busy(env) is False
    docker.busy.add(env.container)
    assert mgr.busy(env) is True
    monkeypatch.setattr(containers, "docker", lambda *a, **k: _done(1, "", "daemon gone"))
    assert mgr.busy(env) is True


def test_the_suite_runs_without_housekeeping():
    """conftest switches it off, so no test's reaper can judge real environments."""
    assert envmod.housekeeping_off()
    m = EnvironmentManager()
    m.start_reaper()
    assert m._reaper_alive is False


def test_quit_stops_what_is_not_busy(docker, mgr, monkeypatch):
    monkeypatch.delenv("VAF_SANDBOX_ENV_HOUSEKEEPING_OFF", raising=False)
    a = mgr.create(ALICE)
    b = mgr.create(ALICE)
    docker.busy.add(b.container)
    assert mgr.stop_all_at_quit() == 1
    assert docker.containers[a.container]["state"] == "exited"
    assert docker.containers[b.container]["state"] == "running"


def test_quit_keeps_the_proxy_for_a_busy_registries_environment(docker, mgr, monkeypatch):
    """MUTATION: stop the proxy on every quit again - red: a pip install left running in
    another terminal lost its package access halfway."""
    monkeypatch.delenv("VAF_SANDBOX_ENV_HOUSEKEEPING_OFF", raising=False)
    busy = mgr.create(ALICE, network="registries")
    docker.busy.add(busy.container)
    mgr.stop_all_at_quit()
    assert docker.containers[envmod.PROXY_CONTAINER]["state"] == "running"
    docker.busy.discard(busy.container)
    mgr.stop_all_at_quit()
    assert docker.containers[envmod.PROXY_CONTAINER]["state"] == "exited"
    assert docker.containers[busy.container]["state"] == "exited"


def test_revocation_stops_the_accounts_environments(docker, mgr):
    a = mgr.create(ALICE)
    b = mgr.create(BOB)
    assert mgr.stop_all_for(ALICE) == 1
    assert docker.containers[a.container]["state"] == "exited"
    assert docker.containers[b.container]["state"] == "running"


# -- the scratch environment -----------------------------------------------------------

def test_scratch_converges_on_one_container_and_reaches_the_bridge(docker, mgr):
    first = mgr.scratch_for(ALICE)
    second = mgr.scratch_for(ALICE)
    assert first.container == second.container == f"vaf-env-{containers.scope_hash(ALICE)}-scratch"
    assert len([n for n in docker.containers if n.endswith("-scratch")]) == 1
    args = _run_args(docker, first.container)
    assert "host.docker.internal:host-gateway" in args
    assert "--memory" in args and args[args.index("--memory") + 1] == "512m"
    assert first.network == "open" and first.kind == "scratch"
    assert mgr.scratch_for(BOB).container != first.container


def test_scratch_moves_to_the_built_image_unless_something_runs(docker, mgr, monkeypatch):
    """Every use extends the scratch environment, so one made on the fallback image (or on an
    image a refresh replaced) never expired into the right one. MUTATION: drop the image
    check in scratch_for - red; recreate a busy one - red."""
    monkeypatch.setattr(environment_image, "usable_image", lambda: environment_image.FALLBACK_IMAGE)
    builds = []
    monkeypatch.setattr(environment_image, "start_background_build", lambda: builds.append(1))
    old = mgr.scratch_for(ALICE)
    assert builds, "running on the fallback must have the image built"
    assert environment_image.FALLBACK_IMAGE in _run_args(docker, old.container)
    monkeypatch.setattr(environment_image, "usable_image", lambda: "vaf-sandbox-env:test")
    docker.busy.add(old.container)
    mgr.scratch_for(ALICE)
    assert environment_image.FALLBACK_IMAGE in _run_args(docker, old.container)   # busy: kept
    docker.busy.discard(old.container)
    new = mgr.scratch_for(ALICE)
    assert new.container == old.container
    assert "vaf-sandbox-env:test" in _run_args(docker, new.container)
    assert environment_image.FALLBACK_IMAGE not in _run_args(docker, new.container)


def test_scratch_adopts_a_container_another_process_created(docker, mgr):
    owner = containers.scope_hash(ALICE)
    name = f"vaf-env-{owner}-scratch"
    docker.containers[name] = {"state": "exited", "labels": {
        LABEL: "1", LABEL + ".id": f"s-{owner}", LABEL + ".owner": owner,
        LABEL + ".kind": "scratch", LABEL + ".network": "open"},
        "args": ["run", "vaf-sandbox-env:test", "sleep", "infinity"], "networks": set()}
    env = mgr.scratch_for(ALICE)
    assert env.container == name and docker.containers[name]["state"] == "running"
    assert not [c for c in docker.calls if c[0] == "run"]


# -- working in it ------------------------------------------------------------------------

def test_exec_runs_bounded_with_a_marker_in_the_workspace(docker, mgr, monkeypatch):
    seen = {}

    def _exec_bounded(container, argv, **kw):
        seen.update(container=container, argv=argv, **kw)
        return 0, "hello\n", "", False, False

    monkeypatch.setattr(containers, "exec_bounded", _exec_bounded)
    env = mgr.create(ALICE)
    r = mgr.exec(ALICE, env.id, "echo hello", timeout=30)
    assert r.stdout == "hello\n" and r.returncode == 0
    assert seen["container"] == env.container and seen["argv"] == ["sh", "-c", "echo hello"]
    assert seen["workdir"] == "/workspace" and re.fullmatch(r"[0-9a-f]{12}", seen["run_id"])


def test_the_root_lane_runs_as_root_and_hands_the_workspace_back(docker, mgr, monkeypatch):
    """MUTATION: drop user="0:0", or the give-back after it - red: a root-owned file in a
    person's project folder could not be removed without sudo."""
    seen = {}

    def _exec_bounded(container, argv, **kw):
        seen.update(argv=argv, **kw)
        return 0, "", "", False, False

    monkeypatch.setattr(containers, "exec_bounded", _exec_bounded)
    env = mgr.create(ALICE, kind="temporary")
    caps = [a for i, a in enumerate(docker.containers[env.container]["args"])
            if i and docker.containers[env.container]["args"][i - 1] == "--cap-add"]
    assert tuple(caps) == envmod.ROOT_LANE_CAPS
    mgr.exec_in(env, ["sh", "-c", "apt-get install -y tree"], as_root=True)
    assert seen["user"] == "0:0"
    give_back = [c for c in docker.calls if c[:4] == ["exec", "-u", "0:0", env.container]]
    assert give_back and "chown" in give_back[-1][-1] and envmod._host_user() in give_back[-1][-1]
    # MUTATION: drop the chmod - red: a setuid file in a host folder is a way to root there.
    script = give_back[-1][-1]
    assert "chmod ug-s" in script and script.index("chmod ug-s") < script.index("chown")
    mgr.exec_in(env, ["sh", "-c", "id"])
    assert seen["user"] is None


def test_a_give_back_that_failed_is_said_not_swallowed(docker, mgr, monkeypatch):
    """MUTATION: ignore the give-back's exit code again - red: root's files stayed root's in
    the person's folder and nobody heard of it."""
    monkeypatch.setattr(containers, "exec_bounded", lambda *a, **k: (0, "installed\n", "", False, False))
    env = mgr.create(ALICE, kind="temporary")
    real = docker.__call__

    def _docker(args, timeout=60, **kw):
        if args[:3] == ["exec", "-u", "0:0"] and "chown" in args[-1]:
            assert timeout >= 600                       # a large tree needs minutes
            return _done(1, "", "find: /workspace/x: Permission denied")
        return real(args, timeout, **kw)

    monkeypatch.setattr(containers, "docker", _docker)
    r = mgr.exec_in(env, ["sh", "-c", "apt-get install -y tree"], as_root=True)
    assert r.returncode == 0 and r.stdout == "installed\n"
    assert "[warning]" in r.stderr and "Permission denied" in r.stderr


def test_no_root_lane_where_it_cannot_work(docker, mgr, monkeypatch):
    """The scratch environment has none, and an environment made before the lane lacks its
    capabilities. MUTATION: skip _require_root_lane - red."""
    monkeypatch.setattr(containers, "exec_bounded", lambda *a, **k: (0, "", "", False, False))
    scratch = mgr.scratch_for(ALICE)
    assert "--cap-add" not in docker.containers[scratch.container]["args"]
    with pytest.raises(EnvironmentRefused, match="no root lane"):
        mgr.exec_in(scratch, ["id"], as_root=True)
    old = mgr.create(ALICE)
    args = docker.containers[old.container]["args"]
    docker.containers[old.container]["args"] = [a for i, a in enumerate(args)
                                                if a != "--cap-add" and (i == 0 or args[i - 1] != "--cap-add")]
    with pytest.raises(EnvironmentRefused, match="create a new environment"):
        mgr.exec_in(old, ["id"], as_root=True)


def test_container_paths_stay_posix_and_resolve_against_the_workspace():
    assert envmod._container_path("src/a.py") == "/workspace/src/a.py"
    assert envmod._container_path("/tmp/x") == "/tmp/x"
    assert envmod._container_path("a/../../etc") == "/etc"     # normalised, not jailed: it is the env's own fs


def test_a_copy_starts_a_stopped_environment_first(docker, mgr, tmp_path):
    """MUTATION: drop _ensure_running from copy_in or copy_out - red: the transfer ran
    docker exec against a stopped container and failed."""
    env = mgr.create(ALICE)
    src = tmp_path / "a.txt"
    src.write_text("x")
    for move in (lambda: mgr.copy_in(ALICE, env.id, str(src)),
                 lambda: mgr.copy_out(ALICE, env.id, "a.txt", str(tmp_path / "out"))):
        mgr.stop(ALICE, env.id)
        docker.calls.clear()
        try:
            move()
        except Exception:
            pass                                            # the fake has no tar stream
        heads = [c[0] for c in docker.calls]
        assert "start" in heads and heads.index("start") < heads.index("exec")


def test_copy_out_drops_links_and_escapes(docker, mgr, monkeypatch, tmp_path):
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        def add(name, data=b"", kind=tarfile.REGTYPE, link=""):
            info = tarfile.TarInfo(name)
            info.type = kind
            info.size = len(data)
            info.linkname = link
            tar.addfile(info, io.BytesIO(data) if data else None)
        add("out/ok.txt", b"fine")
        add("out/link", kind=tarfile.SYMTYPE, link="/home/user/.ssh/id_ed25519")
        add("../escape.txt", b"no")
        add("out/dev", kind=tarfile.CHRTYPE)
    payload = buf.getvalue()
    env = mgr.create(ALICE)

    real = docker.__call__

    def _docker(args, timeout=60, **kw):
        if args[0] == "exec" and "tar" in args and "-cf" in args:
            return types.SimpleNamespace(returncode=0, stdout=payload, stderr=b"")
        return real(args, timeout, **kw)

    monkeypatch.setattr(containers, "docker", _docker)
    dest = tmp_path / "dest"
    written = mgr.copy_out(ALICE, env.id, "out", str(dest))
    from pathlib import Path as _P
    assert [_P(p).relative_to(dest.resolve()).as_posix() for p in written] == ["out/ok.txt"]
    assert not (dest / "out" / "link").exists() and not (dest / "out" / "link").is_symlink()
    assert not (tmp_path / "escape.txt").exists()


def test_copy_out_refuses_an_oversized_source_before_reading_it(docker, mgr, monkeypatch, tmp_path):
    """The tar stream is held in memory whole. MUTATION: drop the measurement inside the
    container - red: the stream was read first and measured afterwards."""
    env = mgr.create(ALICE)
    real = docker.__call__
    reads = []

    def _docker(args, timeout=60, **kw):
        if args[0] == "exec" and "du" in args:
            return _done(0, f"{envmod.TRANSFER_LIMIT_BYTES + 1}\t/workspace/big\n")
        if args[0] == "exec" and "tar" in args:
            reads.append(args)
            return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        return real(args, timeout, **kw)

    monkeypatch.setattr(containers, "docker", _docker)
    with pytest.raises(EnvironmentRefused, match="larger than"):
        mgr.copy_out(ALICE, env.id, "big", str(tmp_path / "dest"))
    assert reads == []


def test_a_root_vaf_still_runs_environments_non_root(monkeypatch):
    """MUTATION: drop the uid check in _host_user - red: a VAF running as root started
    every environment as root."""
    import os
    monkeypatch.setattr(os, "getuid", lambda: 0, raising=False)
    monkeypatch.setattr(os, "getgid", lambda: 0, raising=False)
    assert envmod._host_user() == envmod.IMAGE_UID
    if os.name == "posix":
        monkeypatch.setattr(os, "getuid", lambda: 1000, raising=False)
        monkeypatch.setattr(os, "getgid", lambda: 1000, raising=False)
        assert envmod._host_user() == "1000:1000"


# -- limits and their defaults ----------------------------------------------------------

def test_the_module_defaults_match_the_config_defaults():
    from vaf.core.config import Config
    for key, value in envmod.DEFAULTS.items():
        assert Config.DEFAULTS[f"sandbox_env_{key}"] == value, key


def test_every_limit_is_admin_only():
    """A key's name decides who may write it. MUTATION: drop "sandbox_env_" from
    GLOBAL_CONFIG_KEY_PREFIXES - red."""
    from vaf.core.config import Config
    for key in envmod.DEFAULTS:
        assert Config.is_global_config_key(f"sandbox_env_{key}"), key
    assert Config.is_global_config_key("sandbox_env_image_max_age_days")


def test_settings_read_env_then_config_and_never_switch_a_limit_off(monkeypatch):
    monkeypatch.setenv("VAF_SANDBOX_ENV_MAX_PER_USER", "7")
    assert envmod.setting("max_per_user") == 7
    monkeypatch.setenv("VAF_SANDBOX_ENV_MAX_PER_USER", "seven")
    assert envmod.setting("max_per_user") == envmod.DEFAULTS["max_per_user"]
    monkeypatch.setenv("VAF_SANDBOX_ENV_REGISTRY_HOSTS", "pypi.org, example.org")
    assert envmod.setting("registry_hosts") == ["pypi.org", "example.org"]
