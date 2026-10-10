# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Contract: sandbox environments (docs/EMBEDDING.md, "Sandbox environments").

Offline: no docker is reached. What is pinned is what an application's code calls -
the names on the facade, the parameters, the kinds and network profiles, the refusal
type - not how the manager talks to docker."""
import inspect

import vaf


def _params(fn):
    return list(inspect.signature(fn).parameters)


def test_the_manager_is_on_the_facade_and_one_per_process():
    assert "EnvironmentManager" in vaf.__all__ and "get_environment_manager" in vaf.__all__
    assert vaf.get_environment_manager() is vaf.get_environment_manager()
    assert isinstance(vaf.get_environment_manager(), vaf.EnvironmentManager)


def test_kinds_and_network_profiles():
    from vaf.core.environments import KINDS, NETWORKS
    assert KINDS == ("temporary", "project")
    assert NETWORKS == ("none", "registries", "open")


def test_refusals_are_one_exception_type():
    from vaf.core.environments import EnvironmentRefused
    assert issubclass(EnvironmentRefused, Exception)


def test_the_documented_signatures():
    m = vaf.EnvironmentManager
    assert _params(m.create)[:2] == ["self", "owner_scope"]
    for kw in ("kind", "name", "project_path", "network", "memory_mb", "wait_for_image"):
        assert kw in _params(m.create), kw
    assert _params(m.get)[:3] == ["self", "owner_scope", "env_id"] and "admin" in _params(m.get)
    assert _params(m.exec)[:4] == ["self", "owner_scope", "env_id", "command"]
    assert "timeout" in _params(m.exec)
    for name in ("read_file", "write_file", "list_files", "copy_in", "copy_out",
                 "stop", "delete"):
        assert _params(getattr(m, name))[:3] == ["self", "owner_scope", "env_id"], name
    assert "admin" in _params(m.delete) and "admin" in _params(m.stop)
    assert _params(m.list)[:2] == ["self", "owner_scope"] and "everyone" in _params(m.list)
    for name in ("scratch_for", "start_reaper", "stop_all_at_quit", "prune"):
        assert callable(getattr(m, name)), name
