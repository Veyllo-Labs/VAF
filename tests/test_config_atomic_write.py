# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Atomic config writes + the cross-process lock (vaf/core/config.py).

config.json is written by several processes (server, tray, subagent children).
The old plain open("w") left a window where a concurrent reader saw a truncated
file, Config.load fell back to DEFAULTS, and the memory-crypto key loader
minted a replacement key on a live installation - orphaning every encrypted
row. These tests pin the write path that makes that impossible:
- save() writes via tmp+fsync+os.replace (readers see old or new, never partial),
- a failed replace leaves the original file byte-identical and no temp litter,
- a save that omits a PROTECTED key preserves the stored value.
"""
import json
import os

import pytest

from vaf.core.config import Config


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)
    monkeypatch.setattr(Config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(Config, "_filelock", None)  # singleton points at old path otherwise
    monkeypatch.setattr(Config, "_observers", [])
    return tmp_path / "config.json"


def test_save_produces_valid_json_and_no_temp_litter(cfg, tmp_path):
    Config.save({"provider": "local", "n_ctx": 32768})
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["provider"] == "local"
    litter = [p.name for p in tmp_path.iterdir() if p.name.startswith(".config-")]
    assert litter == [], "temp files must be replaced or unlinked"


def test_failed_replace_keeps_the_original_intact(cfg, tmp_path, monkeypatch):
    Config.save({"provider": "local"})
    before = cfg.read_bytes()

    import vaf.core.config as cfgmod

    def _boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(cfgmod.os, "replace", _boom)
    with pytest.raises(OSError):
        Config.save({"provider": "openai"})
    assert cfg.read_bytes() == before, "a failed write must never touch the original"
    litter = [p.name for p in tmp_path.iterdir() if p.name.startswith(".config-")]
    assert litter == [], "the failed temp file must be unlinked"


def test_save_without_a_protected_key_preserves_it(cfg):
    Config.save({"provider": "local", "memory_encryption_key": "S0VZS0VZ"})
    Config.save({"provider": "openai"})  # payload omits the key entirely
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["memory_encryption_key"] == "S0VZS0VZ", \
        "PROTECTED_KEYS must restore an omitted memory_encryption_key"


def test_set_nests_inside_the_lock(cfg):
    """set() -> save() acquires the same lock twice; filelock is reentrant per
    (instance, thread), so this must complete instead of deadlocking."""
    Config.set("provider", "local")
    assert json.loads(cfg.read_text(encoding="utf-8"))["provider"] == "local"
