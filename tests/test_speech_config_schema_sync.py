# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Speech config drift guards + permission/redaction pins (CLAUDE.md Rule 2).

Guards:
- every speech-related DEFAULTS key has a row in docs/setup/CONFIG_SCHEMA.md,
- the schema's "(N keys)" count line matches len(Config.DEFAULTS) exactly,
- ElevenLabs stays OUT of PROVIDER_MODELS (audio-only vendor; adding it would
  poison the LLM provider UI and the coder endpoint map),
- the new voice provider keys are admin-write-only and api_key_elevenlabs is
  read-redacted for non-admins.
"""
import re
from pathlib import Path

from vaf.core.config import Config, PROVIDER_MODELS

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCHEMA = _REPO_ROOT / "docs" / "setup" / "CONFIG_SCHEMA.md"


def test_every_speech_default_has_a_schema_row():
    doc = _SCHEMA.read_text(encoding="utf-8")
    # voice_agent_ = the live-call LLM lane; documented in the same schema
    # (and admin-gated like the speech provider keys, see below).
    keys = [k for k in Config.DEFAULTS
            if k.startswith(("speech_", "stt_", "tts_", "voice_agent_"))]
    keys.append("api_key_elevenlabs")
    missing = [k for k in keys if f"`{k}`" not in doc]
    assert not missing, f"CONFIG_SCHEMA.md is missing rows for: {missing}"


def test_key_count_line_matches_defaults():
    doc = _SCHEMA.read_text(encoding="utf-8")
    m = re.search(r"\((\d+) keys\)", doc)
    assert m, "CONFIG_SCHEMA.md no longer contains the '(N keys)' count line"
    assert int(m.group(1)) == len(Config.DEFAULTS), (
        f"CONFIG_SCHEMA.md says {m.group(1)} keys but Config.DEFAULTS has "
        f"{len(Config.DEFAULTS)} - update the count line"
    )


def test_elevenlabs_is_not_an_llm_provider():
    assert "elevenlabs" not in PROVIDER_MODELS


def test_speech_provider_keys_admin_write_only():
    for key in (
        "speech_tts_provider", "speech_tts_api_model", "speech_tts_api_voice",
        "speech_stt_provider", "speech_stt_api_model",
        "speech_stt_enabled", "stt_enabled",
        # Voice-agent LLM lane: a LAN user must never redirect the call's
        # inference or burn the admin's API quota.
        "voice_agent_provider", "voice_agent_model",
    ):
        assert Config.is_global_config_key(key), f"{key} must be admin-write-only"
        assert Config.filter_for_non_admin({key: "x"}) == {}, f"{key} escapes filter_for_non_admin"


def test_tts_auto_speak_stays_user_writable():
    # Deliberate: playback preference; billing exposure is bounded by the
    # admin-gated enable/provider keys.
    assert not Config.is_global_config_key("tts_auto_speak")


def test_api_key_elevenlabs_is_secret():
    assert Config.is_secret_config_key("api_key_elevenlabs")
    assert Config.is_global_config_key("api_key_elevenlabs")


def test_config_for_user_redacts_key_keeps_provider():
    cfg = {"api_key_elevenlabs": "el-key", "speech_tts_provider": "elevenlabs"}
    filtered = {k: v for k, v in cfg.items() if not Config.is_secret_config_key(k)}
    assert "api_key_elevenlabs" not in filtered
    assert filtered.get("speech_tts_provider") == "elevenlabs"


# ── Veyllo STT default-on-key-add (owner product decision) ────────────────────

def test_veyllo_stt_default_seeded_on_key_add():
    """First Veyllo key added + no STT provider chosen -> default speech_stt_provider=veyllo."""
    merged = {"api_key_veyllo": "enc", "speech_stt_provider": ""}
    Config.apply_veyllo_stt_default({"api_key_veyllo": ""}, merged)
    assert merged["speech_stt_provider"] == "veyllo"


def test_veyllo_stt_default_respects_explicit_cloud_choice():
    merged = {"api_key_veyllo": "enc", "speech_stt_provider": "openai"}
    Config.apply_veyllo_stt_default({"api_key_veyllo": ""}, merged)
    assert merged["speech_stt_provider"] == "openai"  # explicit choice wins


def test_veyllo_stt_default_not_on_key_rotation():
    """Key already present is not an absent->present transition -> never re-seed
    (so an explicit local pick, which writes '', is not overwritten later)."""
    merged = {"api_key_veyllo": "new-enc", "speech_stt_provider": ""}
    Config.apply_veyllo_stt_default({"api_key_veyllo": "old-enc"}, merged)
    assert merged["speech_stt_provider"] == ""


def test_veyllo_stt_default_not_without_key():
    merged = {"api_key_veyllo": "", "speech_stt_provider": ""}
    Config.apply_veyllo_stt_default({"api_key_veyllo": ""}, merged)
    assert merged["speech_stt_provider"] == ""


def test_veyllo_stt_default_idempotent_when_already_veyllo():
    merged = {"api_key_veyllo": "enc", "speech_stt_provider": "veyllo"}
    Config.apply_veyllo_stt_default({"api_key_veyllo": ""}, merged)
    assert merged["speech_stt_provider"] == "veyllo"


def test_veyllo_stt_default_blocked_by_explicit_local_whisper():
    """local_whisper stores provider='' but engine='local' (non-default) - an
    unambiguous explicit local opt-out that must NOT be flipped to the metered cloud."""
    merged = {"api_key_veyllo": "enc", "speech_stt_provider": "", "speech_stt_engine": "local"}
    Config.apply_veyllo_stt_default({"api_key_veyllo": ""}, merged)
    assert merged["speech_stt_provider"] == ""


def test_veyllo_stt_default_seeds_pristine_local_docker():
    """local_docker / unset both leave the default engine 'docker' -> seeded."""
    merged = {"api_key_veyllo": "enc", "speech_stt_provider": "", "speech_stt_engine": "docker"}
    Config.apply_veyllo_stt_default({"api_key_veyllo": ""}, merged)
    assert merged["speech_stt_provider"] == "veyllo"


def test_config_save_applies_veyllo_stt_default_centrally(tmp_path, monkeypatch):
    """The seed runs inside Config.save, so EVERY write path is covered - including
    the CLI's set_api_key -> set -> save, not just the two web endpoints."""
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)
    monkeypatch.setattr(Config, "CONFIG_FILE", tmp_path / "config.json")
    # Baseline: no Veyllo key, no STT provider chosen (pristine local).
    Config.save({"provider": "local", "speech_stt_provider": "", "speech_stt_engine": "docker"})
    assert (Config.load().get("speech_stt_provider") or "") == ""
    # CLI-style key add funnels through set() -> save(); the central seed must fire.
    Config.set_api_key("veyllo", "vaf_live_secret")
    assert Config.load().get("speech_stt_provider") == "veyllo"


def test_config_save_seed_respects_explicit_local_across_key_add(tmp_path, monkeypatch):
    """An explicit local_whisper opt-out made BEFORE the key is added survives a
    later key add through Config.save (the exact regression the review caught)."""
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)
    monkeypatch.setattr(Config, "CONFIG_FILE", tmp_path / "config.json")
    Config.save({"provider": "local", "speech_stt_provider": "", "speech_stt_engine": "local"})
    Config.set_api_key("veyllo", "vaf_live_secret")
    assert (Config.load().get("speech_stt_provider") or "") == ""  # local opt-out preserved
