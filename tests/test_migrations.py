# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import json

import vaf.core.migrations as mig
from vaf.core.config import Config


def test_run_config_migrations_applies_pending(monkeypatch):
    def v1_to_v2(cfg):
        cfg = dict(cfg)
        cfg.setdefault("new_key", "def")
        return cfg

    monkeypatch.setattr(mig, "CONFIG_MIGRATIONS", [(2, v1_to_v2)])
    out, applied = mig.run_config_migrations({"a": 1}, 1)
    assert out["new_key"] == "def" and applied == [2]

    out2, applied2 = mig.run_config_migrations({"a": 1}, 2)  # already at v2
    assert "new_key" not in out2 and applied2 == []


def test_load_upgrades_sparse_v1_file_to_current_version(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"provider": "deepseek"}))
    monkeypatch.setattr(Config, "CONFIG_FILE", cfg_file)

    loaded = Config.load()
    assert loaded["provider"] == "deepseek"
    assert loaded["config_format_version"] == mig.CONFIG_FORMAT_VERSION
    # Persisted sparse: the version stamp is added, defaults are NOT written.
    raw = json.loads(cfg_file.read_text())
    assert raw["provider"] == "deepseek"
    assert raw["config_format_version"] == mig.CONFIG_FORMAT_VERSION
    assert "model" not in raw


def test_v2_lifts_only_the_old_lexical_scan_default():
    """The old cap (400) predates chunk encryption and silently truncated the
    lexical lane on larger stores; full-config saves wrote it out explicitly,
    so only a value migration reaches existing installs.

    MUTATION: revert the 400->2000 rewrite in _v2_lift_lexical_scan_cap and
    the first assertion goes red."""
    out, applied = mig.run_config_migrations(
        {"memory_hybrid_lexical_scan_limit": 400}, 1)
    assert out["memory_hybrid_lexical_scan_limit"] == 2000 and applied == [2]

    # A deliberate custom value is not touched.
    out, _ = mig.run_config_migrations({"memory_hybrid_lexical_scan_limit": 800}, 1)
    assert out["memory_hybrid_lexical_scan_limit"] == 800

    # Absent key stays absent (fresh sparse config keeps riding DEFAULTS).
    out, _ = mig.run_config_migrations({}, 1)
    assert "memory_hybrid_lexical_scan_limit" not in out

    # Idempotent: a second pass over the migrated value changes nothing.
    out2, applied2 = mig.run_config_migrations(dict(out), 2)
    assert applied2 == []


def test_config_load_runs_and_persists_migration(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"config_format_version": 1, "provider": "local"}))
    monkeypatch.setattr(Config, "CONFIG_FILE", cfg_file)

    def v1_to_v2(cfg):
        cfg = dict(cfg)
        cfg.setdefault("migrated_flag", True)
        return cfg

    monkeypatch.setattr(mig, "CONFIG_FORMAT_VERSION", 2)
    monkeypatch.setattr(mig, "CONFIG_MIGRATIONS", [(2, v1_to_v2)])

    loaded = Config.load()
    assert loaded["migrated_flag"] is True
    assert loaded["config_format_version"] == 2

    # Persisted against the sparse file (not bloated with all defaults).
    raw = json.loads(cfg_file.read_text())
    assert raw["config_format_version"] == 2 and raw.get("migrated_flag") is True
    assert "model" not in raw  # defaults were NOT written

    # Second load is idempotent (file already at v2).
    loaded2 = Config.load()
    assert loaded2["config_format_version"] == 2
