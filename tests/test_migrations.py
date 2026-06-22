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


def test_v1_load_is_noop_and_adds_version(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"provider": "deepseek"}))
    monkeypatch.setattr(Config, "CONFIG_FILE", cfg_file)

    loaded = Config.load()
    assert loaded["provider"] == "deepseek"
    assert loaded["config_format_version"] == 1
    # No migration ran, so the sparse file is untouched.
    assert json.loads(cfg_file.read_text()) == {"provider": "deepseek"}


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
