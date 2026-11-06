# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A home of the test's own for the app link: configuration, ssl directory, device store,
a fresh task queue, the security events captured in memory, and the Veyllo App lane on for
two accounts."""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

import pytest

from tests.cert_factory import isolated_home

SCOPE_A = "0a0b0c0d-0000-4000-8000-0000000000a1"
SCOPE_B = "0a0b0c0d-0000-4000-8000-0000000000b2"


@dataclass
class AppLinkHome:
    root: Any
    ssl_dir: Any
    events: List[Tuple[str, Dict[str, Any]]] = field(default_factory=list)

    def lane(self, scope: str, on: bool) -> None:
        from vaf.core.config import Config
        with Config._locked():
            cfg = Config.load()
            by_scope = dict(cfg.get("connection_enabled_by_scope") or {})
            mine = dict(by_scope.get(scope) or {})
            mine["veyllo_app"] = on
            by_scope[scope] = mine
            cfg["connection_enabled_by_scope"] = by_scope
            Config.save(cfg)

    def set_block(self, **values) -> None:
        from vaf.core.config import Config
        with Config._locked():
            cfg = Config.load()
            block = dict(cfg.get("veyllo_app_config") or {})
            block.update(values)
            cfg["veyllo_app_config"] = block
            Config.save(cfg)

    def kinds(self) -> List[str]:
        return [kind for kind, _ in self.events]


@pytest.fixture
def app_link_home(tmp_path, monkeypatch):
    from vaf.core import security_events
    from vaf.core.app_link import pki
    from vaf.core.platform import Platform
    from vaf.core.task_queue import TaskQueue

    ssl_dir = isolated_home(tmp_path, monkeypatch, veyllo_app_config={
        "enabled": True, "relay_enabled": True, "relay_api_base": "https://relay.test",
        "relay_instance_id": "", "device_idle_days": 90},
        connection_enabled_by_scope={SCOPE_A: {"veyllo_app": True},
                                     SCOPE_B: {"veyllo_app": True}})
    vaf_dir = tmp_path / "vafdir"
    vaf_dir.mkdir()
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: vaf_dir))
    data_dir = tmp_path / "datadir"
    data_dir.mkdir()
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: data_dir))
    home = AppLinkHome(root=tmp_path, ssl_dir=ssl_dir)

    def _capture(kind, **fields):
        home.events.append((kind, fields))

    monkeypatch.setattr(security_events, "log_security_event", _capture)
    TaskQueue._instance = None  # type: ignore[attr-defined]
    pki.reset_context()
    yield home
    pki.reset_context()
    TaskQueue._instance = None  # type: ignore[attr-defined]
