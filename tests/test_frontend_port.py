# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The Web UI's port has ONE reader: ``vaf.network.binding.frontend_port``.

Five places read ``VAF_WEB_UI_PORT`` with a fallback of 3000 while nothing in the tree ever
set it, so a frontend that had moved to 3001 (because 3000 was taken) got every OAuth return
sent to a port nothing listened on. The frontend already records the port it really bound;
this pins that the record wins, that the config answers while no frontend runs, and that no
sixth hand copy of the env read appears.
"""
import re
from pathlib import Path

import pytest

from vaf.core.config import Config
from vaf.core.frontend_manager import FrontendManager
from vaf.network import binding

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def port_file(tmp_path, monkeypatch):
    path = tmp_path / "web_port"
    monkeypatch.setattr(FrontendManager, "get_port_file", lambda self: str(path))
    monkeypatch.delenv("VAF_WEB_UI_PORT", raising=False)
    return path


def _config(monkeypatch, value):
    real = Config.get

    def fake(key, default=None):
        if key == "local_network_port_frontend":
            return value
        return real(key, default)

    monkeypatch.setattr(Config, "get", staticmethod(fake))


def test_the_port_the_frontend_really_bound_wins(port_file, monkeypatch):
    _config(monkeypatch, 3000)
    port_file.write_text("3001")
    assert binding.frontend_port() == 3001


def test_the_config_answers_while_no_frontend_runs(port_file, monkeypatch):
    _config(monkeypatch, 3100)
    assert binding.frontend_port() == 3100


def test_a_broken_record_falls_back_instead_of_raising(port_file, monkeypatch):
    _config(monkeypatch, "abc")
    port_file.write_text("not a port")
    assert binding.frontend_port() == 3000


def test_the_env_override_still_wins(port_file, monkeypatch):
    port_file.write_text("3001")
    monkeypatch.setenv("VAF_WEB_UI_PORT", "4000")
    assert binding.frontend_port() == 4000
    monkeypatch.setenv("VAF_WEB_UI_PORT", "junk")
    assert binding.frontend_port() == 3001


def test_the_oauth_return_address_follows_the_real_port(port_file, monkeypatch):
    from vaf.network import oauth_redirect

    real = Config.get

    def fake(key, default=None):
        if key in ("local_network_enabled", "local_network_tls_enabled"):
            return False
        return real(key, default)

    monkeypatch.setattr(Config, "get", staticmethod(fake))
    port_file.write_text("3001")
    assert oauth_redirect.frontend_base_url() == "http://localhost:3001"


def test_no_hand_copy_of_the_env_read_is_left():
    """The five copies were the defect; a sixth would be the same defect again."""
    readers = []
    for path in (REPO / "vaf").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"environ(?:\.get\(|\[)\s*[\"']VAF_WEB_UI_PORT", text):
            readers.append(path.relative_to(REPO).as_posix())
    assert readers == ["vaf/network/binding.py"]
