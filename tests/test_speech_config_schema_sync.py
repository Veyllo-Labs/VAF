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
    keys = [k for k in Config.DEFAULTS if k.startswith(("speech_", "stt_", "tts_"))]
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
