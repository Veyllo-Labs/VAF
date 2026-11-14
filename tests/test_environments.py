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
import os
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
        self.busy = set()      # container names with a marked process of their own user
        self.busy_root = set()  # ... with one of root's, visible only to root (no CAP_SYS_PTRACE)
        self.fail_isolated = False
        self.fail_run = False
        self.image_ids = {}    # tag -> image id, for `image inspect` and a container's .Image
        # What the host sees mounted at /workspace (`_mounted_identity`): None is the mount
        # source's own identity, resolved at the time of the call (as docker resolves `-v` at
        # every start); an exception is raised as it is.
        self.mount_identity = None
        self.shares_kernel = True
        self.fail_stop = False     # `stop` and `kill` leave the container running
        self.fail_rm = False       # `rm -f` leaves the container listed
        self.identity_calls = []

    def mounted_identity(self, container, inside=envmod.WORKSPACE):
        self.identity_calls.append(container)
        c = self.containers.get(container)
        if not c or c["state"] != "running":
            raise OSError("the container has no running process")
        if isinstance(self.mount_identity, Exception):
            raise self.mount_identity
        if self.mount_identity is not None:
            return self.mount_identity
        c_args = c["args"]
        src = next(c_args[i + 1] for i, a in enumerate(c_args)
                   if a == "-v" and c_args[i + 1].endswith(f":{envmod.WORKSPACE}:z"))
        st = os.stat(src.rsplit(":", 2)[0])
        return f"{st.st_dev}:{st.st_ino}"

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
        if head in ("stop", "kill"):
            c = self.containers.get(args[-1])
            if self.fail_stop:
                return _done(1, "", "cannot stop container: permission denied")
            if c:
                c["state"] = "exited"
            return _done(0)
        if head == "rm":
            if self.fail_rm:
                return _done(1, "", "cannot remove container: permission denied")
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
                seen = self.busy_root if args[1:3] == ["-u", "0:0"] else self.busy
                return _done(0, "42\n" if name in seen else "")
            return _done(0, "")
        raise AssertionError(f"unexpected docker call {args}")


@pytest.fixture
def docker(monkeypatch, tmp_path):
    fake = FakeDocker()
    monkeypatch.setattr(containers, "docker", fake)
    monkeypatch.setattr(containers, "docker_shares_kernel", lambda: fake.shares_kernel)
    monkeypatch.setattr(EnvironmentManager, "_mounted_identity",
                        staticmethod(fake.mounted_identity))
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


# -- the mounted folder is the folder that was checked, at every start --------------

def _symlink_or_skip(link, target):
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")


def _project(tmp_path, monkeypatch, name="proj"):
    import vaf.core.workspace_guard as wg
    monkeypatch.setattr(wg, "is_unsafe_project_dir", lambda p: False)
    folder = tmp_path / name
    folder.mkdir()
    return folder


def test_a_restart_refuses_a_folder_swapped_while_the_environment_was_stopped(
        docker, mgr, tmp_path, monkeypatch):
    """`docker start` resolves the stored path again (measured: it mounted, and relabelled, a
    folder linked in while the environment was stopped). MUTATION: drop _held_mount in
    _ensure_running - red (a start is issued)."""
    proj = _project(tmp_path, monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    env = mgr.create(ALICE, kind="project", project_path=str(proj))
    mgr.stop(ALICE, env.id)
    os.rename(proj, tmp_path / "proj-aside")
    _symlink_or_skip(proj, other)
    docker.calls.clear()
    with pytest.raises(EnvironmentRefused, match="replaced by a link"):
        mgr.exec(ALICE, env.id, "ls")
    assert not [c for c in docker.calls if c[0] == "start"]
    assert docker.containers[env.container]["state"] == "exited"


def test_a_restart_on_the_checked_folder_passes_the_check(docker, mgr, tmp_path, monkeypatch):
    proj = _project(tmp_path, monkeypatch)
    env = mgr.create(ALICE, kind="project", project_path=str(proj))
    mgr.stop(ALICE, env.id)
    docker.calls.clear()
    docker.identity_calls.clear()
    mgr._ensure_running(mgr.get(ALICE, env.id))
    assert [c[0] for c in docker.calls if c[0] in ("start", "exec")] == ["start"]
    assert docker.identity_calls == [env.container]
    assert docker.containers[env.container]["state"] == "running"
    assert not mgr._unverified_file(env.id).exists()


def test_a_start_that_mounted_another_folder_is_stopped_and_refused(docker, mgr, tmp_path,
                                                                    monkeypatch):
    """After `docker run`, /workspace must show the held folder's dev:ino, asked of the host,
    before anything runs: no exec at all, so nothing in the container answers.
    MUTATION: skip the comparison in _check_mount - red."""
    proj = _project(tmp_path, monkeypatch)
    docker.mount_identity = "1:2"
    with pytest.raises(EnvironmentRefused, match="it was moved or replaced while"):
        mgr.create(ALICE, kind="project", project_path=str(proj))
    assert ["stop", "-t", "0"] in [c[:3] for c in docker.calls]
    assert not [n for n in docker.containers if n.startswith("vaf-env-")]   # create removed it
    assert not [c for c in docker.calls if c[0] == "exec"]
    assert not list(mgr._state_dir().glob("*.json"))
    assert not list(mgr._state_dir().glob("*.mount-unverified"))
    # The same check after a restart: stopped again, the state stays not running.
    docker.mount_identity = None
    env = mgr.create(ALICE, kind="project", project_path=str(proj))
    mgr.stop(ALICE, env.id)
    docker.mount_identity = "1:2"
    with pytest.raises(EnvironmentRefused, match="nothing was run in it"):
        mgr.exec(ALICE, env.id, "ls")
    assert docker.containers[env.container]["state"] == "exited"


def test_a_mount_that_cannot_be_checked_is_refused_with_its_reason(docker, mgr, tmp_path,
                                                                  monkeypatch):
    """A daemon whose container's root the host cannot read (rootless, userns-remap) fails
    closed, and says that it could not check rather than that the folder changed."""
    proj = _project(tmp_path, monkeypatch)
    docker.mount_identity = PermissionError(13, "Permission denied")
    with pytest.raises(EnvironmentRefused, match="could not check which folder is mounted") as e:
        mgr.create(ALICE, kind="project", project_path=str(proj))
    assert "Permission denied" in str(e.value) and "rootless" in str(e.value)


def test_a_refused_mount_that_could_not_be_stopped_stays_refused(docker, mgr, tmp_path,
                                                                monkeypatch):
    """A stop that fails leaves the container running; the next command must not take that
    as a checked start. MUTATION: clear the unverified mark when the check refuses - red."""
    proj = _project(tmp_path, monkeypatch)
    ran = []
    monkeypatch.setattr(containers, "exec_bounded",
                        lambda *a, **k: ran.append(a) or (0, "", "", False, False))
    env = mgr.create(ALICE, kind="project", project_path=str(proj))
    mgr.stop(ALICE, env.id)
    docker.mount_identity = "1:2"
    docker.fail_stop = True
    with pytest.raises(EnvironmentRefused, match="could not be stopped"):
        mgr.exec(ALICE, env.id, "ls")
    assert docker.containers[env.container]["state"] == "running"
    with pytest.raises(EnvironmentRefused, match="is not"):
        mgr.exec(ALICE, env.id, "ls")              # running, and still refused
    assert ran == []
    docker.mount_identity = None                    # the right folder after all
    mgr.exec(ALICE, env.id, "ls")
    assert len(ran) == 1 and not mgr._unverified_file(env.id).exists()


def test_a_second_command_waits_for_the_check_of_a_start_in_progress(docker, mgr, tmp_path,
                                                                    monkeypatch):
    """One request is between `docker start` and its check; a second one finds the container
    running. It must check the mount itself, not run in it. MUTATION: skip _verify_running
    in _ensure_running - red (the second command runs in the swapped-in folder)."""
    import threading
    proj = _project(tmp_path, monkeypatch)
    ran = []
    monkeypatch.setattr(containers, "exec_bounded",
                        lambda *a, **k: ran.append(a) or (0, "", "", False, False))
    env = mgr.create(ALICE, kind="project", project_path=str(proj))
    mgr.stop(ALICE, env.id)
    paused, release = threading.Event(), threading.Event()
    real = docker.mounted_identity
    calls = []

    def _slow_first(container, inside=envmod.WORKSPACE):
        calls.append(container)
        if len(calls) == 1:
            paused.set()
            release.wait(10)
        return real(container, inside)

    monkeypatch.setattr(EnvironmentManager, "_mounted_identity", staticmethod(_slow_first))
    first_error = []

    def _first():
        try:
            mgr.exec(ALICE, env.id, "ls")
        except EnvironmentRefused as e:
            first_error.append(e)

    t = threading.Thread(target=_first)
    t.start()
    try:
        assert paused.wait(10)
        assert docker.containers[env.container]["state"] == "running"
        docker.mount_identity = "1:2"               # the started mount is another folder
        with pytest.raises(EnvironmentRefused):
            mgr.exec(ALICE, env.id, "ls")
        assert ran == []
    finally:
        release.set()
        t.join(10)
    assert first_error and ran == []


def test_a_refused_start_whose_container_cannot_be_removed_keeps_its_record_and_mark(
        docker, mgr, tmp_path, monkeypatch):
    """create refuses the mount and removes the environment; when docker keeps the container
    (stop, kill and rm all fail), the record and the mark stay, so the running container is
    still checked and refused. MUTATION: delete record and mark whatever rm answered - red."""
    proj = _project(tmp_path, monkeypatch)
    ran = []
    monkeypatch.setattr(containers, "exec_bounded",
                        lambda *a, **k: ran.append(a) or (0, "", "", False, False))
    docker.mount_identity = "1:2"
    docker.fail_stop = docker.fail_rm = True
    with pytest.raises(EnvironmentRefused):
        mgr.create(ALICE, kind="project", project_path=str(proj))
    (name,) = [n for n in docker.containers if n.startswith("vaf-env-")]
    env_id = name.rsplit("-", 1)[1]
    assert docker.containers[name]["state"] == "running"
    assert mgr._read_state(env_id) is not None and mgr._unverified_file(env_id).exists()
    with pytest.raises(EnvironmentRefused):
        mgr.exec(ALICE, env_id, "ls")
    assert ran == []
    with pytest.raises(EnvironmentRefused, match="could not be removed"):
        mgr.delete(ALICE, env_id)


def test_a_second_request_compares_with_the_folder_held_for_the_start(docker, mgr, tmp_path,
                                                                     monkeypatch):
    """The folder is replaced by another real folder (renamed into place) between the first
    request's open and docker's start. A second request that opened the path again would hold
    the new folder, find it mounted and let its command run; it compares with the identity
    held for the start instead. MUTATION: verify against a folder opened again - red."""
    import threading
    proj = _project(tmp_path, monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    ran = []
    monkeypatch.setattr(containers, "exec_bounded",
                        lambda *a, **k: ran.append(a) or (0, "", "", False, False))
    env = mgr.create(ALICE, kind="project", project_path=str(proj))
    mgr.stop(ALICE, env.id)
    real_call = docker.__class__.__call__

    def _swapping_start(self, args, *a, **k):
        if args and args[0] == "start":
            os.rename(proj, tmp_path / "proj-aside")
            os.rename(other, proj)                  # a real folder, no link on the way
        return real_call(self, args, *a, **k)

    monkeypatch.setattr(docker.__class__, "__call__", _swapping_start)
    paused, release = threading.Event(), threading.Event()
    real = docker.mounted_identity
    calls = []

    def _slow_first(container, inside=envmod.WORKSPACE):
        calls.append(container)
        if len(calls) == 1:
            paused.set()
            release.wait(10)
        return real(container, inside)

    monkeypatch.setattr(EnvironmentManager, "_mounted_identity", staticmethod(_slow_first))
    errors = []
    t = threading.Thread(target=lambda: _catch(errors, mgr.exec, ALICE, env.id, "ls"))
    t.start()
    try:
        assert paused.wait(10)
        with pytest.raises(EnvironmentRefused, match="is not"):
            mgr.exec(ALICE, env.id, "ls")
        assert ran == []
    finally:
        release.set()
        t.join(10)
    assert errors and ran == []


def _catch(errors, fn, *args):
    try:
        fn(*args)
    except EnvironmentRefused as e:
        errors.append(e)


def test_a_late_check_never_clears_a_newer_starts_mark(docker, mgr, tmp_path, monkeypatch):
    """Ordering: start 1 is checked and pauses before it clears its mark; start 2 marks itself
    (and its check refuses); start 1 then clears. Only start 1's mark may go.
    MUTATION: clear whatever mark is there - red."""
    from vaf.core.folder_mount import HeldFolder
    proj = _project(tmp_path, monkeypatch)
    env = mgr.create(ALICE, kind="project", project_path=str(proj))
    with HeldFolder(proj) as held:
        first = mgr._mark_unverified(env, held)
        second = mgr._mark_unverified(env, held)
    assert first and second and first != second
    mgr._clear_unverified(env, first)
    assert mgr._read_mark(env.id)["token"] == second
    mgr._clear_unverified(env, second)
    assert mgr._read_mark(env.id) is None
    # The same through the lifecycle: a late completion of an older start leaves the newer
    # (refused, still running) start marked, so a command is checked and refused.
    ran = []
    monkeypatch.setattr(containers, "exec_bounded",
                        lambda *a, **k: ran.append(a) or (0, "", "", False, False))
    mgr.stop(ALICE, env.id)
    with HeldFolder(proj) as held:
        older = mgr._mark_unverified(env, held)
    docker.mount_identity = "1:2"
    docker.fail_stop = True
    with pytest.raises(EnvironmentRefused):
        mgr.exec(ALICE, env.id, "ls")               # the newer start: refused, still running
    mgr._clear_unverified(env, older)               # the older check completes late
    with pytest.raises(EnvironmentRefused):
        mgr.exec(ALICE, env.id, "ls")
    assert ran == []


def test_a_daemon_on_another_kernel_is_not_asked(docker, mgr, tmp_path, monkeypatch):
    """A daemon in a VM shows other numbers: the mount stays by name there.
    MUTATION: always run the check - red."""
    proj = _project(tmp_path, monkeypatch)
    docker.shares_kernel = False
    docker.mount_identity = "1:2"
    env = mgr.create(ALICE, kind="project", project_path=str(proj))
    assert env.state == "running" and docker.identity_calls == []
    # Nothing to check without a project folder either.
    docker.shares_kernel = True
    mgr.create(ALICE, kind="temporary")
    assert docker.identity_calls == []


def test_a_restored_record_whose_folder_is_a_link_now_is_not_started(docker, mgr, tmp_path,
                                                                    monkeypatch):
    """MUTATION: drop _held_mount in _start_new - red (a run is issued)."""
    monkeypatch.setattr(environment_image, "ensure_image", lambda: "vaf-sandbox-env:test")
    proj = _project(tmp_path, monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    env = mgr.create(ALICE, kind="project", project_path=str(proj))
    docker.containers.clear()                      # the new machine: no containers
    os.rename(proj, tmp_path / "proj-aside")
    _symlink_or_skip(proj, other)
    docker.calls.clear()
    report = mgr.readopt()
    assert report["failed"] == [env.id]
    assert not [c for c in docker.calls if c[0] == "run"]


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


def test_the_summary_counts_everybodys_environments_and_names_nobody(docker, mgr, monkeypatch):
    """What the health dialog and `vaf repair --check` show. The registries proxy shares
    the label and has no id: not an environment. MUTATION: count every row as running -
    red; read a failed listing as an empty machine - red."""
    a = mgr.create(ALICE)
    mgr.create(BOB, network="registries")
    mgr.stop(ALICE, a.id)
    assert envmod.PROXY_CONTAINER in docker.containers
    assert mgr.summary() == {"environments": 2, "running": 1, "image_built": True}
    monkeypatch.setattr(environment_image, "image_present", lambda tag=None: False)
    assert mgr.summary()["image_built"] is False

    def _down(args, timeout=60, **kw):
        if args[0] == "ps":
            return _done(1, "", "Cannot connect to the Docker daemon")
        return docker(args, timeout, **kw)

    monkeypatch.setattr(containers, "docker", _down)
    with pytest.raises(EnvironmentRefused):
        mgr.summary()


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


def test_a_proxy_another_process_started_in_the_same_moment_is_taken(docker, mgr, monkeypatch):
    """Two processes create registries environments at once: both find no proxy, both run
    one, the second gets a name conflict. MUTATION: raise on any failed run again - red: the
    second environment was refused while the right proxy was running."""
    real = containers.docker
    raced = {"done": False}

    def _racing(args, timeout=60, **kw):
        if (args[:1] == ["run"] and envmod.PROXY_CONTAINER in args and not raced["done"]):
            raced["done"] = True
            real(args, timeout, **kw)                     # the other process's proxy
            return _done(125, "", 'Conflict. The container name "/vaf-env-proxy" is already in use')
        return real(args, timeout, **kw)

    monkeypatch.setattr(containers, "docker", _racing)
    env = mgr.create(ALICE, network="registries")
    assert raced["done"] and env.network == "registries"
    assert docker.containers[envmod.PROXY_CONTAINER]["state"] == "running"
    # A running proxy of OTHER allowed hosts took the name: no race to accept.
    del docker.containers[envmod.PROXY_CONTAINER]

    def _foreign(args, timeout=60, **kw):
        if args[:1] == ["run"] and envmod.PROXY_CONTAINER in args:
            docker.containers[envmod.PROXY_CONTAINER] = {
                "state": "running", "labels": {LABEL: "1", LABEL + ".proxy": "other"},
                "args": [], "networks": set()}
            return _done(125, "", "Conflict")
        return real(args, timeout, **kw)

    monkeypatch.setattr(containers, "docker", _foreign)
    with pytest.raises(EnvironmentRefused, match="proxy could not be started"):
        mgr._ensure_proxy()


def test_a_running_registries_environment_gets_its_proxy_back(docker, mgr, monkeypatch):
    """The reaper can stop the proxy between its listing and a registries environment's
    start. MUTATION: return early for a running environment again - red: the environment
    kept running without package access until it was restarted."""
    monkeypatch.setattr(containers, "exec_bounded", lambda *a, **k: (0, "", "", False, False))
    env = mgr.create(ALICE, network="registries")
    docker.containers[envmod.PROXY_CONTAINER]["state"] = "exited"
    mgr.exec_in(mgr.get(ALICE, env.id), ["true"])
    assert docker.containers[envmod.PROXY_CONTAINER]["state"] == "running"
    # A proxy that cannot come back does not refuse a command that needs no package.
    docker.containers[envmod.PROXY_CONTAINER]["state"] = "exited"
    monkeypatch.setattr(mgr, "_ensure_proxy", lambda: (_ for _ in ()).throw(EnvironmentRefused("no image")))
    assert mgr.exec_in(mgr.get(ALICE, env.id), ["ls"]).returncode == 0


def test_a_root_run_counts_as_busy_though_the_user_cannot_see_it(docker, mgr):
    """Each user reads only its own processes' environment (measured with the root lane's
    capabilities): the reaper asked as the environment's user alone and stopped a project
    environment in the middle of a root apt-get, or of the give-back after it. MUTATION:
    ask as the user only - red; as root only - red (the dev server is the user's)."""
    env = mgr.create(ALICE)
    env.state = "running"
    docker.busy_root.add(env.container)
    assert mgr.busy(env) is True
    docker.busy_root.clear()
    docker.busy.add(env.container)
    assert mgr.busy(env) is True
    docker.busy.clear()
    assert mgr.busy(env) is False
    # The scratch environment has no root lane: one question is enough there.
    scratch = mgr.scratch_for(ALICE)
    scratch.state = "running"
    before = len(docker.calls)
    assert mgr.busy(scratch) is False
    assert len(docker.calls) - before == 1


def test_a_root_run_touches_first_and_its_give_back_is_marked(docker, mgr, monkeypatch):
    """MUTATION: drop the touch before the run - red (the reaper judged the old expiry
    while the run started); drop the give-back's marker - red (busy() could not see it)."""
    touched = []
    env = mgr.create(ALICE, kind="temporary")

    def _exec_bounded(container, argv, **kw):
        touched.append(mgr._read_state(env.id)["last_used"])
        return 0, "", "", False, False

    monkeypatch.setattr(containers, "exec_bounded", _exec_bounded)
    env.last_used = 1.0
    mgr._write_state(env)
    mgr.exec_in(env, ["sh", "-c", "apt-get install -y tree"], as_root=True)
    assert touched and touched[0] > 1.0
    give_back = [c for c in docker.calls if "chown" in c[-1]][-1]
    assert give_back[1:3] == ["-u", "0:0"]
    marker = give_back[give_back.index("-e") + 1]
    assert re.fullmatch(r"VAF_RUN_ID=[0-9a-f]{12}", marker)


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
    give_back = [c for c in docker.calls if c[:3] == ["exec", "-u", "0:0"] and env.container in c]
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


def test_copy_out_into_home_refuses_a_member_that_lands_in_its_configuration(docker, mgr,
                                                                            monkeypatch, tmp_path):
    """``sandbox_transfer copy_out`` with ``host_path=~``: home itself passes the tool's path
    check, and a member named .bashrc would run at the next shell start. The whole stream is
    judged first, so the harmless member ahead of it is not written either.

    ``~/Library`` holds no VAF state, only ``LaunchAgents``: there the host half alone arms
    the member check.

    MUTATION: drop the per-member is_host_config check - red (.bashrc is written). Drop
    holds_host_config from copy_out's check - the Library case goes red."""
    import io
    import tarfile
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    payloads = {}
    for target, members in (("home", (("notes.txt", b"harmless"), (".bashrc", b"curl x | sh"))),
                            ("library", (("notes.txt", b"harmless"),
                                         ("LaunchAgents/x.plist", b"<plist/>")))):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for name, body in members:
                info = tarfile.TarInfo(name)
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
        payloads[target] = buf.getvalue()
    current = {"payload": payloads["home"]}
    env = mgr.create(ALICE)
    real = docker.__call__

    def _docker(args, timeout=60, **kw):
        if args[0] == "exec" and "tar" in args and "-cf" in args:
            return types.SimpleNamespace(returncode=0, stdout=current["payload"], stderr=b"")
        return real(args, timeout, **kw)

    monkeypatch.setattr(containers, "docker", _docker)
    with pytest.raises(envmod.EnvironmentRefused, match="home directory's configuration"):
        mgr.copy_out(ALICE, env.id, "out", str(home))
    assert [p for p in home.rglob("*")] == []
    library = home / "Library"
    library.mkdir()
    current["payload"] = payloads["library"]
    with pytest.raises(envmod.EnvironmentRefused, match="home directory's configuration"):
        mgr.copy_out(ALICE, env.id, "out", str(library))
    assert [p for p in library.rglob("*")] == []
    # Straight into a dot folder of home is refused before the stream is read.
    with pytest.raises(envmod.EnvironmentRefused, match="home directory's configuration"):
        mgr.copy_out(ALICE, env.id, "out", str(home / ".config" / "autostart"))
    assert not (home / ".config").exists()


def test_copy_out_refuses_a_member_that_lands_in_vafs_own_data(docker, mgr, monkeypatch, tmp_path):
    """The folders of the tar stream are chosen inside the environment. Into the PARENT of
    the data directory (not itself VAF's data, so the tool's own host check passes) a member
    named vaf/custom_tools/... would plant code VAF executes at the next agent load. The
    whole stream is judged before anything is written, so the harmless member ahead of it
    is not written either.

    The target is the folder above XDG_DATA_HOME: XDG_DATA_HOME itself is the home
    directory's configuration and refused before the stream is read.

    MUTATION: drop the pass over the members before the writes in copy_out - red."""
    import io
    import tarfile
    xdg = tmp_path / "outer" / "xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    monkeypatch.setenv("LOCALAPPDATA", str(xdg))
    from vaf.core.platform import Platform
    data = Platform.data_dir()
    host_dir = xdg.parent
    host_dir.mkdir(parents=True)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, body in (("fonts/a.txt", b"harmless"),
                           (f"{xdg.name}/{data.name}/custom_tools/e.py", b"print('planted')")):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    payload = buf.getvalue()
    env = mgr.create(ALICE)
    real = docker.__call__

    def _docker(args, timeout=60, **kw):
        if args[0] == "exec" and "tar" in args and "-cf" in args:
            return types.SimpleNamespace(returncode=0, stdout=payload, stderr=b"")
        return real(args, timeout, **kw)

    monkeypatch.setattr(containers, "docker", _docker)
    with pytest.raises(envmod.EnvironmentRefused, match="VAF's own data"):
        mgr.copy_out(ALICE, env.id, "out", str(host_dir))
    assert [p for p in host_dir.rglob("*") if p.is_file()] == []
    # Straight into a state root is refused before anything is read from the stream.
    with pytest.raises(envmod.EnvironmentRefused, match="VAF's own data"):
        mgr.copy_out(ALICE, env.id, "out", str(data / "custom_tools"))
    assert not (data / "custom_tools").exists()


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


def test_a_docker_that_does_not_list_is_not_a_missing_environment(docker, mgr, monkeypatch):
    """get() read a failed listing as "no environment", which sent people after a wrong id.
    MUTATION: drop strict=True in get() - red."""
    env = mgr.create(ALICE)
    real = docker.__call__

    def _docker(args, timeout=60, **kw):
        if args[0] == "ps":
            return _done(1, "", "Cannot connect to the Docker daemon")
        return real(args, timeout, **kw)

    monkeypatch.setattr(containers, "docker", _docker)
    with pytest.raises(envmod.EnvironmentsUnlisted, match="did not list"):
        mgr.get(ALICE, env.id)
    assert issubclass(envmod.EnvironmentsUnlisted, EnvironmentRefused)


def test_copy_in_never_enters_a_linked_folder(docker, mgr, monkeypatch, tmp_path):
    """Before Python 3.13 rglob's ** followed a linked folder, and a file below it was no link
    itself, so it went in. MUTATION: back to rglob("*", recurse_symlinks=True), the old
    behaviour - red."""
    import io
    import tarfile
    env = mgr.create(ALICE)
    site = tmp_path / "site"
    (site / "src").mkdir(parents=True)
    (site / "src" / "app.py").write_text("print(1)")
    secret = tmp_path / "dotssh"
    secret.mkdir()
    (secret / "id_ed25519").write_text("PRIVATE")
    (site / "keys").symlink_to(secret, target_is_directory=True)
    (site / "key_link").symlink_to(secret / "id_ed25519")
    real = docker.__call__
    sent = []

    def _docker(args, timeout=60, **kw):
        if args[0] == "exec" and kw.get("input"):
            sent.append(kw["input"])
        return real(args, timeout, **kw)

    monkeypatch.setattr(containers, "docker", _docker)
    mgr.copy_in(ALICE, env.id, str(site))
    with tarfile.open(fileobj=io.BytesIO(sent[-1])) as tar:
        names = sorted(tar.getnames())
    assert names == ["site", "site/src", "site/src/app.py"]


def test_a_restored_environment_gets_its_container_back_instead_of_being_reaped(
        docker, mgr, tmp_path, monkeypatch):
    """A restore on another machine brings back the records and the volumes, never the
    containers, and the reaper cleared such a record AND its volume as an orphan: the
    restored work was deleted ten minutes after the first start. readopt() recreates the
    container from the record; a temporary environment is dropped (it would have expired);
    one whose host folder is missing here is reported. MUTATION: skip the recreate - red."""
    monkeypatch.setattr(environment_image, "ensure_image", lambda: "vaf-sandbox-env:test")
    folder = tmp_path / "site"
    folder.mkdir()
    keep = mgr.create(ALICE, kind="project", name="kept")
    temp = mgr.create(ALICE, kind="temporary")
    mounted = mgr.create(ALICE, kind="project", project_path=str(folder))
    folder.rmdir()
    docker.containers.clear()                      # the new machine: no containers

    report = mgr.readopt()

    assert report["readopted"] == [keep.id]
    assert keep.container in docker.containers
    assert report["dropped"] == [temp.id] and mgr._read_state(temp.id) is None
    assert temp.volume not in docker.volumes
    assert report["missing_folder"] == [mounted.id]
    mgr.reap_once(now=__import__("time").time() + 3600)
    assert mgr._read_state(keep.id) is not None and keep.volume in docker.volumes


@pytest.mark.parametrize("where", ["home", "root"])
def test_a_restored_record_cannot_mount_what_create_would_refuse(docker, mgr, monkeypatch, where):
    """A restored record is data from a file. Its mount is relabelled with `:z`; one naming
    the home directory or `/` must be refused like a new environment's. MUTATION: skip the
    mount checks in readopt - red."""
    import os
    from pathlib import Path
    monkeypatch.setattr(environment_image, "ensure_image", lambda: "vaf-sandbox-env:test")
    env = mgr.create(ALICE, kind="project", name="tampered")
    st = mgr._read_state(env.id)
    st["project_path"] = str(Path.home()) if where == "home" else os.path.abspath(os.sep)
    (mgr._state_dir() / f"{env.id}.json").write_text(json.dumps(st), encoding="utf-8")
    docker.containers.clear()
    report = mgr.readopt()
    assert report["failed"] == [env.id] and env.container not in docker.containers


@pytest.mark.parametrize("network, cap, expected", [
    ("bogus", "open", "none"),          # no profile at all: the narrowest, never "open"
    ("open", "registries", "registries"),
])
def test_a_restored_record_is_held_to_what_create_allows(docker, mgr, monkeypatch,
                                                         network, cap, expected):
    """A restored record is data from a file. Its id is the file's, its names are derived
    (they are what removing an environment deletes), an unknown network profile is not the
    open one `_start_new` falls through to, and network and memory are capped by today's
    limits - capped, not refused, which would hand the work to the reaper.
    MUTATION: build the container from the record's fields again - red."""
    monkeypatch.setattr(environment_image, "ensure_image", lambda: "vaf-sandbox-env:test")
    monkeypatch.setenv("VAF_SANDBOX_ENV_NETWORK_MAX", cap)
    env = mgr.create(ALICE, kind="project", name="tampered", network="none")
    st = mgr._read_state(env.id)
    st.update({"id": "someone-else", "container": "vaf-memory-db", "volume": "precious",
               "net": "bridge", "network": network, "memory_mb": 999999})
    (mgr._state_dir() / f"{env.id}.json").write_text(json.dumps(st), encoding="utf-8")
    docker.containers.clear()
    docker.containers["vaf-memory-db"] = {"state": "running", "labels": {}, "args": [],
                                          "networks": set()}
    docker.volumes["precious"] = {}

    report = mgr.readopt()

    assert report["readopted"] == [env.id]
    args = _run_args(docker, env.container)
    assert args[args.index("--memory") + 1] == f"{envmod.setting('memory_max_mb')}m"
    assert args[args.index("--network") + 1] == env.net
    assert docker.networks[env.net]["internal"] is (expected != "open")
    assert f"{env.volume}:/workspace" in args
    kept = mgr._read_state(env.id)
    assert (kept["id"], kept["container"], kept["volume"], kept["network"]) == \
        (env.id, env.container, env.volume, expected)
    assert "precious" in docker.volumes and docker.containers["vaf-memory-db"]["state"] == "running"


def test_a_record_names_only_its_own_volume_and_network_for_removal(docker, mgr, monkeypatch):
    """The reaper clears a record without a container together with the volume and network
    it names, and readopt drops a temporary one with its volume: a record naming a volume
    or network that is not this environment's must not get it deleted. MUTATION: remove
    the record's names as they are - red."""
    monkeypatch.setattr(environment_image, "ensure_image", lambda: "vaf-sandbox-env:test")
    for env_id, kind in (("deadbeef", "project"), ("cafef00d", "temporary")):
        (mgr._state_dir()).mkdir(parents=True, exist_ok=True)
        (mgr._state_dir() / f"{env_id}.json").write_text(json.dumps({
            "id": env_id, "kind": kind, "owner": "ab12cd34ef56", "created": 0,
            "volume": "precious", "net": "user-net", "project_path": "/nowhere/at/all"}),
            encoding="utf-8")
    docker.volumes.update({"precious": {}, "vaf-env-vol-cafef00d": {}})
    docker.networks["user-net"] = {"labels": {}, "internal": False, "options": []}

    report = mgr.readopt()
    mgr.reap_once(now=__import__("time").time())

    assert report["dropped"] == ["cafef00d"] and "vaf-env-vol-cafef00d" not in docker.volumes
    assert mgr._read_state("deadbeef") is None, "the orphan record itself is cleared"
    assert "precious" in docker.volumes and "user-net" in docker.networks


@pytest.mark.skipif(not os.path.isdir("/proc/self/root"), reason="needs /proc/<pid>/root (Linux)")
def test_the_mount_is_read_from_the_host_never_from_the_container(monkeypatch, tmp_path):
    """The identity comes from /proc/<pid>/root on the host; no command runs in the container
    (a planted `stat` there passed the old check). The last part is not followed and must be a
    folder. MUTATION: follow a link at the mount point - red."""
    asked = []

    def _docker(args, timeout=60, **kw):
        asked.append(args)
        return _done(0, f"{os.getpid()}\n")

    monkeypatch.setattr(containers, "docker", _docker)
    folder = tmp_path / "work"
    folder.mkdir()
    st = os.stat(folder)
    read = EnvironmentManager._mounted_identity
    assert read("c1", inside=str(folder)) == f"{st.st_dev}:{st.st_ino}"
    assert asked == [["inspect", "-f", "{{.State.Pid}}", "c1"]]
    link = tmp_path / "link"
    try:
        os.symlink(folder, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    with pytest.raises(OSError):
        read("c1", inside=str(link))
    (tmp_path / "file").write_text("x", encoding="utf-8")
    with pytest.raises(OSError):
        read("c1", inside=str(tmp_path / "file"))
    monkeypatch.setattr(containers, "docker", lambda *a, **k: _done(0, "0\n"))
    with pytest.raises(OSError, match="no running process"):
        read("c1", inside=str(folder))
