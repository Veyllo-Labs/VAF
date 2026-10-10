# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The old shared code sandbox is removed on existing installs.

Compose no longer knows the `sandbox` service, so nothing else would ever remove its
container: it has `restart: unless-stopped` and comes back with every docker start, and
`compose stop` ignores a service its file dropped. The stack start removes it, its two
networks and its volume, every time until they are gone."""
import types

from vaf.core import containers
from vaf.core import service_stack as ss


def _fake(present):
    calls = []

    def _docker(args, timeout=60, **kw):
        calls.append(list(args))
        if args[0] == "inspect":
            return types.SimpleNamespace(returncode=0 if "container" in present else 1,
                                         stdout="exited\n", stderr="")
        if args[:2] in (["network", "inspect"], ["volume", "inspect"]):
            ok = args[2] in present
            return types.SimpleNamespace(returncode=0 if ok else 1, stdout=args[2], stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    return calls, _docker


def test_everything_the_old_sandbox_left_is_removed(monkeypatch):
    calls, fake = _fake({"container", "vaf-sandbox-network", "vaf-sandbox-ephemeral",
                         "vaf_sandbox_workspace"})
    monkeypatch.setattr(containers, "docker", fake)
    said = []
    ss._remove_legacy_sandbox(said.append)
    removals = [c for c in calls if "rm" in c[:2]]
    assert removals == [["rm", "-f", "vaf-sandbox"], ["network", "rm", "vaf-sandbox-network"],
                        ["network", "rm", "vaf-sandbox-ephemeral"],
                        ["volume", "rm", "vaf_sandbox_workspace"]]
    assert said and "old shared code sandbox" in said[0]


def test_nothing_left_removes_nothing_and_says_nothing(monkeypatch):
    calls, fake = _fake(set())
    monkeypatch.setattr(containers, "docker", fake)
    said = []
    ss._remove_legacy_sandbox(said.append)
    assert not [c for c in calls if "rm" in c[:2]] and said == []


def test_it_runs_before_the_core_services_come_up():
    """Before `up`: compose would otherwise warn about the orphan, and the container
    would hold its memory through the whole start."""
    import inspect
    src = inspect.getsource(ss._ensure_service_stack)
    assert src.index("_remove_legacy_sandbox(log)") < src.index("list(CORE_SERVICES)")


def test_the_registry_no_longer_lists_it():
    assert "sandbox" not in {s.service_key for s in ss.SERVICES}
    assert "vaf-sandbox" not in {s.container_name for s in ss.SERVICES}
