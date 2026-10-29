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
    assert out["memory_hybrid_lexical_scan_limit"] == 2000 and applied[0] == 2

    # A deliberate custom value is not touched.
    out, _ = mig.run_config_migrations({"memory_hybrid_lexical_scan_limit": 800}, 1)
    assert out["memory_hybrid_lexical_scan_limit"] == 800

    # Absent key stays absent (fresh sparse config keeps riding DEFAULTS).
    out, _ = mig.run_config_migrations({}, 1)
    assert "memory_hybrid_lexical_scan_limit" not in out

    # Idempotent: a second pass over the migrated value changes nothing.
    out2, applied2 = mig.run_config_migrations(dict(out), mig.CONFIG_FORMAT_VERSION)
    assert applied2 == []


def test_v3_lifts_only_the_old_token_default():
    """A sign-in from another device now ends after a week without use, which only means
    something when a session can outlive a day: the old 24-hour default becomes 30 days.
    Full-config saves wrote 24 out explicitly, so only a value migration reaches existing
    installs. MUTATION: rewrite 48 too; leave the version at 2 - red."""
    assert mig.CONFIG_FORMAT_VERSION >= 3
    out, applied = mig.run_config_migrations({"local_network_jwt_expiry_hours": 24}, 2)
    assert out["local_network_jwt_expiry_hours"] == 720 and applied[0] == 3
    out, _ = mig.run_config_migrations({"local_network_jwt_expiry_hours": 48}, 2)
    assert out["local_network_jwt_expiry_hours"] == 48
    out, _ = mig.run_config_migrations({}, 2)
    assert "local_network_jwt_expiry_hours" not in out
    again, applied = mig.run_config_migrations({"local_network_jwt_expiry_hours": 720}, 2)
    assert again["local_network_jwt_expiry_hours"] == 720


def test_a_stored_day_long_lifetime_becomes_a_month_on_load(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"config_format_version": 2,
                                    "local_network_jwt_expiry_hours": 24}))
    monkeypatch.setattr(Config, "CONFIG_FILE", cfg_file)
    assert Config.get("local_network_jwt_expiry_hours") == 720
    raw = json.loads(cfg_file.read_text())
    assert raw["local_network_jwt_expiry_hours"] == 720
    assert raw["config_format_version"] == mig.CONFIG_FORMAT_VERSION


def test_v4_turns_only_the_old_default_into_auto():
    """The browser pool now sizes itself by free memory and never serves fewer than two,
    so the old fixed default 2 becomes "auto". Full-config saves wrote 2 out explicitly,
    so only a value migration reaches existing installs. Off (0), any other count and
    auto itself stay; a bool True (which equals 1, not 2) is no count to rewrite either.
    MUTATION: rewrite any integer - red; leave the version at 3 - red."""
    assert mig.CONFIG_FORMAT_VERSION == 4
    out, applied = mig.run_config_migrations({"browser_pool_max": 2}, 3)
    assert out["browser_pool_max"] == "auto" and applied == [4]
    for kept in (0, 3, 1, "auto", True):
        out, _ = mig.run_config_migrations({"browser_pool_max": kept}, 3)
        assert out["browser_pool_max"] == kept, kept
    out, _ = mig.run_config_migrations({}, 3)
    assert "browser_pool_max" not in out
    # Idempotent: a second pass over the migrated value changes nothing.
    again, _ = mig.run_config_migrations({"browser_pool_max": "auto"}, 3)
    assert again["browser_pool_max"] == "auto"


def test_a_stored_two_becomes_auto_on_load(tmp_path, monkeypatch):
    """A v3 file that holds the old default is upgraded on the first load and the
    sparse file keeps it: "auto" and version 4, no defaults written."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"config_format_version": 3, "browser_pool_max": 2}))
    monkeypatch.setattr(Config, "CONFIG_FILE", cfg_file)
    assert Config.get("browser_pool_max") == "auto"
    raw = json.loads(cfg_file.read_text())
    assert raw["browser_pool_max"] == "auto" and raw["config_format_version"] == 4
    assert "model" not in raw


def test_the_session_keys_are_registered():
    """The two session keys in DEFAULTS and in CONFIG_SCHEMA.md, the deleted 2FA switch in
    neither, and the schema's key count true. MUTATION: keep the deleted row - red."""
    import re
    from pathlib import Path
    assert Config.DEFAULTS["local_network_jwt_expiry_hours"] == 720
    assert Config.DEFAULTS["local_network_session_idle_days"] == 7
    schema = (Path(__file__).resolve().parents[1] / "docs" / "setup" / "CONFIG_SCHEMA.md"
              ).read_text(encoding="utf-8")
    assert "| `local_network_session_idle_days` | `7` |" in schema
    assert "| `local_network_jwt_expiry_hours` | `720` |" in schema
    assert "local_network_" + "require_2fa" not in schema
    count = int(re.search(r"`Config\.DEFAULTS` \((\d+) keys\)", schema).group(1))
    assert count == len(Config.DEFAULTS)


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
