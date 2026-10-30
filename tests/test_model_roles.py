# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf/core/model_roles.py`: which provider and model does each job use.

THE PROPERTY THIS FILE PINS: the overview cannot lie. `resolve_role` is the one
decision, and every backend entry point (the vision lane, the voice call, the thinking
run, the sub-agent spawn, the browser bridge, OCR, speech, the failover chain) returns
exactly the provider and model of its row, over a matrix of main providers, role
settings, API-key presence and install profiles. Before the resolver, the same rule was
decided in eight places by hand (four for the thinking runs alone), and a settings
overview would have been a ninth copy.

MUTATIONS, each turns the named test red:
- follow the main provider for vision without `model_supports_vision` ->
  test_expected_rows[deepseek-follow] and test_no_silent_vision_fallback;
- treat `inherit` as a provider name for thinking -> test_expected_rows[thinking-inherit];
- drop the gate check for sub-agents -> test_expected_rows[subagent-name-without-gate];
- put the old body back into `voice_agent._resolve_backend` without the key check ->
  test_backend_agrees_with_overview;
- let `_build_failover_chain` skip `failover_links` (append the backup without its key
  check) -> test_backend_agrees_with_overview;
- re-add `Config.get("thinking_provider")` in thinking_mode -> test_no_hand_reads_of_the_role_keys;
- return the first keyed vision-capable provider when main cannot see ->
  test_no_silent_vision_fallback;
- put the hand tuple back into `inference_profile.conflicts` ->
  test_conflicts_reads_the_role_list.
"""
import itertools
import subprocess
import sys
import types
from pathlib import Path

import pytest

from vaf.core import model_roles as mr
from vaf.core.config import Config

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def machine(monkeypatch):
    """The installation's configuration, its API keys and the Tesseract binary, all in
    the test. The local server probe is refused, which the registry reads as capable,
    and nothing reaches the network."""
    state = {"cfg": {}, "keys": set(), "tesseract": False}
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: {**cls.DEFAULTS, **state["cfg"]}))
    import vaf.core.api_keys as api_keys
    monkeypatch.setattr(api_keys, "resolve_api_key",
                        lambda provider, caller_config=None: "sk-x" if provider in state["keys"] else "")

    def _version():
        if not state["tesseract"]:
            raise RuntimeError("no tesseract")
        return "5.0"
    monkeypatch.setitem(sys.modules, "pytesseract",
                        types.SimpleNamespace(get_tesseract_version=_version))
    import requests

    def _no_network(*a, **k):
        raise requests.ConnectionError("no network in tests")
    monkeypatch.setattr(requests, "get", _no_network)
    return state


def _row(role, provider, model, source, reason="", also=()):
    spec = {r.name: r for r in mr.ROLES}[role]
    return {"role": role, "provider": provider, "model": model, "source": source,
            "reason": reason, "setting": spec.setting, "tab": spec.tab, "also": list(also)}


def _default(provider):
    return Config.get_default_model(provider)


# ── criterion 1: the rows ────────────────────────────────────────────────────────────

EXPECTED = {
    # name: (config, keys, tesseract, role, expected row)
    "deepseek-follow": (
        {"provider": "deepseek", "api_model_deepseek": "deepseek-v4-pro"}, {"deepseek"}, False,
        "vision", _row("vision", None, None, "unavailable", "main_cannot_see")),
    "anthropic-follow": (
        {"provider": "anthropic", "api_model_anthropic": "claude-sonnet-4-6"}, {"anthropic"}, False,
        "vision", _row("vision", "anthropic", "claude-sonnet-4-6", "follows_main")),
    "vision-set": (
        {"provider": "deepseek", "vision_provider": "google"}, {"deepseek", "google"}, False,
        "vision", _row("vision", "google", "set-later", "set")),
    "chat-no-key": (
        {"provider": "veyllo"}, set(), False,
        "chat", _row("chat", "veyllo", "default", "main", "no_api_key")),
    "chat-local": (
        {"provider": "local", "model": "qwen.gguf"}, set(), False,
        "chat", _row("chat", "local", "qwen.gguf", "main")),
    "chat-local-api-profile": (
        {"provider": "local", "model": "qwen.gguf", "inference_profile": "api"}, set(), False,
        "chat", _row("chat", "local", "qwen.gguf", "main", "no_local_models")),
    "ocr-tesseract": (
        {"provider": "deepseek"}, {"deepseek"}, True,
        "ocr", _row("ocr", None, "tesseract", "local_engine")),
    "ocr-via-vision": (
        {"provider": "anthropic", "api_model_anthropic": "claude-sonnet-4-6"}, {"anthropic"}, False,
        "ocr", _row("ocr", "anthropic", "claude-sonnet-4-6", "via_vision")),
    "ocr-nothing": (
        {"provider": "deepseek"}, {"deepseek"}, False,
        "ocr", _row("ocr", None, None, "unavailable", "ocr_no_engine")),
    "browser-follow-blind": (
        {"provider": "deepseek", "api_model_deepseek": "deepseek-v4-pro"}, {"deepseek"}, False,
        "browser_agent", _row("browser_agent", "deepseek", "deepseek-v4-pro", "follows_main",
                              "browser_dom_only")),
    "browser-follow-described": (
        {"provider": "deepseek", "api_model_deepseek": "deepseek-v4-pro", "vision_provider": "google"},
        {"deepseek", "google"}, False,
        "browser_agent", _row("browser_agent", "deepseek", "deepseek-v4-pro", "follows_main",
                              "browser_described")),
    "browser-set": (
        {"provider": "deepseek", "browser_agent_provider": "anthropic",
         "browser_agent_model": "claude-sonnet-4-6"}, {"deepseek", "anthropic"}, False,
        "browser_agent", _row("browser_agent", "anthropic", "claude-sonnet-4-6", "set")),
    "subagent-follow": (
        {"provider": "veyllo", "api_model_veyllo": "veyllo-chat"}, {"veyllo"}, False,
        "subagents", _row("subagents", "veyllo", "veyllo-chat", "follows_main")),
    "subagent-set": (
        {"provider": "veyllo", "subagent_provider": "openai", "subagent_use_separate_provider": True,
         "api_model_openai": "gpt-5.6-terra"}, {"veyllo", "openai"}, False,
        "subagents", _row("subagents", "openai", "gpt-5.6-terra", "set")),
    "subagent-name-without-gate": (
        {"provider": "veyllo", "api_model_veyllo": "veyllo-chat", "subagent_provider": "openai",
         "subagent_use_separate_provider": False}, {"veyllo", "openai"}, False,
        "subagents", _row("subagents", "veyllo", "veyllo-chat", "follows_main", "choice_inactive")),
    "thinking-inherit": (
        {"provider": "deepseek", "thinking_provider": "inherit", "api_model_deepseek": "deepseek-v4-pro"},
        {"deepseek"}, False,
        "thinking", _row("thinking", "deepseek", "deepseek-v4-pro", "follows_main")),
    "thinking-api-model-ignored": (
        {"provider": "deepseek", "thinking_provider": "OpenAI", "thinking_model": "gpt-mini",
         "api_model_openai": "gpt-5.6-terra"}, {"deepseek", "openai"}, False,
        "thinking", _row("thinking", "openai", "gpt-5.6-terra", "set", "thinking_model_ignored")),
    "thinking-local-model": (
        {"provider": "openai", "thinking_provider": "local", "thinking_model": "small.gguf"},
        {"openai"}, False,
        "thinking", _row("thinking", "local", "small.gguf", "set")),
    "voice-follow-no-key": (
        {"provider": "openai"}, set(), False,
        "voice_call", _row("voice_call", None, None, "unavailable", "no_api_key")),
    "voice-follow-local": (
        {"provider": "local"}, set(), False,
        "voice_call", _row("voice_call", "local", None, "follows_main")),
    "voice-vllm-api-main": (
        {"provider": "openai", "voice_agent_provider": "local", "local_engine": "vllm"}, {"openai"}, False,
        "voice_call", _row("voice_call", None, None, "unavailable", "vllm_voice_api_main")),
    "stt-off": (
        {"provider": "openai"}, {"openai"}, False,
        "speech_to_text", _row("speech_to_text", None, None, "off")),
    "stt-set": (
        {"provider": "deepseek", "speech_stt_enabled": True, "speech_stt_provider": "openai"},
        {"deepseek", "openai"}, False,
        "speech_to_text", _row("speech_to_text", "openai", "whisper-1", "set")),
    "stt-on-does-not-follow-main": (
        {"provider": "openai", "speech_stt_enabled": True}, {"openai"}, False,
        "speech_to_text", _row("speech_to_text", None, "docker", "local_engine")),
    "stt-unusable-under-api": (
        {"provider": "openai", "stt_enabled": True, "speech_stt_provider": "elevenlabs",
         "inference_profile": "api"}, {"openai"}, False,
        "speech_to_text", _row("speech_to_text", None, None, "unavailable", "speech_provider_unusable")),
    "tts-no-local-models": (
        {"provider": "openai", "speech_tts_enabled": True, "inference_profile": "api"}, {"openai"}, False,
        "text_to_speech", _row("text_to_speech", None, None, "unavailable", "no_local_models")),
    "tts-system-under-api": (
        {"provider": "openai", "speech_tts_enabled": True, "speech_tts_engine": "system",
         "inference_profile": "api"}, {"openai"}, False,
        "text_to_speech", _row("text_to_speech", None, "system", "local_engine")),
    "failover-off": (
        {"provider": "openai"}, {"openai"}, False,
        "failover", _row("failover", None, None, "off")),
    "failover-balanced": (
        {"provider": "openai", "failover_level": "balanced", "failover_backup_provider": "anthropic",
         "failover_backup_model": "claude-haiku-4-5"}, {"openai", "anthropic"}, False,
        "failover", _row("failover", "anthropic", "claude-haiku-4-5", "set", also=("local",))),
    "failover-backup-without-key": (
        {"provider": "openai", "failover_level": "balanced", "failover_backup_provider": "anthropic",
         "inference_profile": "api"}, {"openai"}, False,
        "failover", _row("failover", None, None, "unavailable", "failover_no_link")),
    "failover-local-main": (
        {"provider": "local", "failover_level": "basic"}, set(), False,
        "failover", _row("failover", None, None, "unavailable", "failover_main_local")),
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_expected_rows(machine, name):
    """Criterion 1, the full row for the named states. Both readings agree: the machine
    configuration (config=None) and the same values handed in as a mapping."""
    cfg, keys, tesseract, role, want = EXPECTED[name]
    want = dict(want)
    if want["model"] == "default":
        want["model"] = _default(want["provider"])
    if want["model"] == "set-later":
        from vaf.core.provider_registry import default_vision_model
        want["model"] = default_vision_model(want["provider"])
    machine.update(cfg=dict(cfg), keys=set(keys), tesseract=tesseract)
    assert mr.resolve_role(role).as_dict() == want
    assert mr.resolve_role(role, {**Config.DEFAULTS, **cfg}).as_dict() == want


def test_a_mapping_is_read_instead_of_the_machine(machine):
    """The draft preview: every lane that takes `config` reads the mapping, not the
    machine's file (only the API keys stay the machine's)."""
    machine.update(cfg={"provider": "deepseek"}, keys={"deepseek", "openai", "anthropic"})
    draft = {**Config.DEFAULTS, "provider": "anthropic", "api_model_anthropic": "claude-sonnet-4-6",
             "ocr_engine": "tesseract", "speech_stt_enabled": True, "speech_stt_provider": "openai",
             "speech_stt_api_model": "gpt-4o-transcribe", "speech_tts_enabled": True,
             "speech_tts_engine": "system", "voice_agent_provider": "local",
             "voice_agent_model": "voice.gguf"}
    rows = {a.role: a.as_dict() for a in mr.resolve_roles(draft)}
    assert rows["vision"] == _row("vision", "anthropic", "claude-sonnet-4-6", "follows_main")
    # The draft's explicit tesseract (absent here) is refused, not run on the vision model.
    assert rows["ocr"] == _row("ocr", None, None, "unavailable", "ocr_no_engine")
    assert rows["speech_to_text"] == _row("speech_to_text", "openai", "gpt-4o-transcribe", "set")
    assert rows["text_to_speech"] == _row("text_to_speech", None, "system", "local_engine")
    assert rows["voice_call"] == _row("voice_call", "local", "voice.gguf", "set")
    # The machine itself is unchanged.
    assert mr.resolve_role("vision").source == "unavailable"


def test_no_silent_vision_fallback(machine):
    """With DeepSeek as the main provider and vision on follow, nothing else reads the
    images, however many vision-capable providers have a key (Decision 3)."""
    from vaf.core.vision_infer import select_vision_backend
    machine.update(cfg={"provider": "deepseek", "api_model_deepseek": "deepseek-v4-pro"},
                   keys={"deepseek", "openai", "anthropic", "google", "veyllo", "openrouter"})
    row = mr.resolve_role("vision")
    assert (row.source, row.reason, row.provider) == ("unavailable", "main_cannot_see", None)
    assert select_vision_backend() == (None, None)


def test_the_role_list_is_one_list():
    names = [r.name for r in mr.ROLES]
    assert names == ["chat", "vision", "ocr", "browser_agent", "subagents", "thinking",
                     "voice_call", "speech_to_text", "text_to_speech", "failover"]
    assert set(names) == set(mr._RESOLVERS)
    assert set(mr.REASON_TEXT) >= {"no_api_key", "main_cannot_see", "ocr_no_engine",
                                   "browser_described", "browser_dom_only", "choice_inactive",
                                   "thinking_model_ignored", "vllm_voice_api_main",
                                   "no_local_models", "speech_provider_unusable",
                                   "failover_no_link", "config_unreadable"}


def test_an_unreadable_config_is_a_row_not_an_exception(monkeypatch):
    """The resolver sits on spawn and turn paths."""
    def boom(cls, *a, **k):
        raise OSError("config.json unreadable")
    monkeypatch.setattr(Config, "get", classmethod(boom))
    monkeypatch.setattr(Config, "load", classmethod(boom))
    for spec in mr.ROLES:
        row = mr.resolve_role(spec.name)
        assert (row.source, row.reason) == ("unavailable", "config_unreadable"), spec.name
    with pytest.raises(KeyError):
        mr.resolve_role("no_such_role")


# ── criterion 2: the overview cannot lie ─────────────────────────────────────────────

class _Bridge:
    def __init__(self, model=None, provider_name=None, session_id=None):
        self.model, self.provider = model, provider_name


class _Manager:
    def __init__(self, provider, config=None, caller_config=None):
        self.provider_name = provider


_ROLE_VARIANTS = [
    {},
    {"vision_provider": "google", "vision_model": "gemini-2.5-flash"},
    {"vision_provider": "openai"},
    {"browser_agent_provider": "anthropic"},
    {"browser_agent_model": "gpt-5.6-terra"},
    {"subagent_provider": "openai", "subagent_use_separate_provider": True},
    {"subagent_provider": "openai", "subagent_use_separate_provider": False},
    {"thinking_provider": "openai", "thinking_model": "gpt-mini"},
    {"thinking_provider": "local"},
    {"thinking_provider": " INHERIT "},
    {"voice_agent_provider": "openai", "voice_agent_model": "gpt-mini"},
    {"voice_agent_provider": "local"},
    {"voice_agent_provider": "local", "local_engine": "vllm"},
    {"ocr_engine": "vision"},
    {"ocr_engine": "tesseract"},
    {"speech_stt_enabled": True, "speech_stt_provider": "openai"},
    {"speech_stt_enabled": True, "speech_stt_provider": "nope"},
    {"speech_tts_enabled": True, "speech_tts_provider": "elevenlabs"},
    {"speech_tts_enabled": True, "speech_tts_engine": "chatterbox"},
    {"failover_level": "balanced", "failover_backup_provider": "anthropic"},
    {"failover_level": "maximum", "failover_backup_provider": "deepseek"},
    {"failover_level": "basic"},
]
_MAINS = [("deepseek", "deepseek-v4-pro"), ("anthropic", "claude-sonnet-4-6"),
          ("veyllo", "veyllo-chat"), ("local", "qwen.gguf")]
_MATRIX = list(itertools.product(_MAINS, range(len(_ROLE_VARIANTS)), (True, False), ("local", "api")))


def _config_for(main, variant, keyed, profile):
    provider, model = main
    cfg = {"provider": provider, "inference_profile": profile, **_ROLE_VARIANTS[variant]}
    if provider == "local":
        cfg["model"] = model
    else:
        cfg[f"api_model_{provider}"] = model
    keys = {"deepseek", "anthropic", "veyllo", "openai", "google", "elevenlabs"} if keyed else set()
    return cfg, keys


def _entry_points(monkeypatch):
    from vaf.core import pdf_extract, speech_api, thinking_mode, vision_infer, voice_agent
    from vaf.core import api_backend as ab
    from vaf.core.config import subagent_provider_override
    import vaf.tools.browser_agent as ba

    monkeypatch.setattr(ba, "VAFLLMBridge", _Bridge)

    def failover():
        real = ab.APIBackendManager
        mgr = real.__new__(real)
        mgr.provider_name, mgr.config, mgr.caller_config = Config.get("provider"), None, None
        monkeypatch.setattr(ab, "APIBackendManager", _Manager)
        try:
            return [m.provider_name for m, _ in real._build_failover_chain(mgr, "m")[1:]]
        finally:
            monkeypatch.setattr(ab, "APIBackendManager", real)

    def bridge():
        b = ba._build_vaf_bridge()
        return b.provider, b.model

    return {
        "vision": vision_infer.select_vision_backend,
        "voice_call": voice_agent._resolve_backend,
        "thinking": thinking_mode.resolve_thinking_provider,
        "subagents": subagent_provider_override,
        "browser_agent": bridge,
        "ocr": lambda: pdf_extract.resolve_ocr_engine()[0],
        "speech_to_text": speech_api.select_stt_backend,
        "text_to_speech": lambda: speech_api.select_tts_backend()[:2],
        "failover": failover,
    }


def _expected_from_row(role, row):
    pm = (row.provider, row.model) if row.provider else (None, None)
    if role in ("vision", "voice_call", "browser_agent"):
        return pm
    if role == "thinking":
        return row.provider
    if role == "subagents":
        return row.provider if row.source == "set" else None
    if role == "ocr":
        return {"local_engine": "tesseract", "via_vision": "vision"}.get(row.source)
    if role in ("speech_to_text", "text_to_speech"):
        return pm if row.source == "set" else (None, None)
    if role == "failover":
        if row.provider is None:
            return []
        return [row.provider, *row.also]
    raise AssertionError(role)


@pytest.mark.parametrize("main,variant,keyed,profile", _MATRIX)
def test_backend_agrees_with_overview(machine, monkeypatch, main, variant, keyed, profile):
    """Criterion 2: each backend entry point returns exactly its row, and the overview
    (resolve_roles, the API and `vaf providers`) is that same row."""
    cfg, keys = _config_for(main, variant, keyed, profile)
    machine.update(cfg=cfg, keys=keys, tesseract=variant % 2 == 0)
    rows = {a.role: a for a in mr.resolve_roles()}
    entry = _entry_points(monkeypatch)
    for role, call in entry.items():
        if role == "failover" and main[0] == "local":
            continue  # no request on the local model goes through the chain
        if role == "speech_to_text" and not (cfg.get("speech_stt_enabled") or cfg.get("stt_enabled")):
            continue  # off: the provider choice is not asked
        if role == "text_to_speech" and not cfg.get("speech_tts_enabled"):
            continue
        assert call() == _expected_from_row(role, rows[role]), (role, cfg, keys)
        assert mr.resolve_role(role).as_dict() == rows[role].as_dict(), role


# ── criterion 3: one reader of the role keys ─────────────────────────────────────────

_ROLE_PROVIDER_KEYS = ("thinking_provider", "vision_provider", "browser_agent_provider",
                       "voice_agent_provider")
# Rules over the stored values, writers and the save path, not resolvers.
_EXEMPT = {
    "vaf/core/model_roles.py",
    "vaf/core/config.py",
    "vaf/core/inference_profile.py",   # conflicts: reads ROLES (test below)
    "vaf/core/local_engine.py",        # conflicts: the vLLM rules
    "vaf/core/web_server.py",          # the voice GGUF prefetch on save
    "vaf/api/config_routes.py",
    "vaf/cli/cmd/setup.py",            # onboarding WRITES vision_provider
}


def test_no_hand_reads_of_the_role_keys():
    """Shrink-only. A second reader of a role key is a second answer to "who does this
    job", the thing the overview exists to make impossible."""
    pattern = "|".join(f'"{k}"' for k in _ROLE_PROVIDER_KEYS)
    out = subprocess.run(["git", "grep", "-n", "-E", pattern, "--", "vaf/"],
                         cwd=str(ROOT), capture_output=True, text=True)
    hits = [line for line in out.stdout.splitlines()
            if line and line.split(":", 1)[0] not in _EXEMPT]
    assert hits == [], "a role key is read outside model_roles again:\n" + "\n".join(hits)
    src = (ROOT / "vaf" / "core" / "thinking_mode.py").read_text(encoding="utf-8")
    assert src.count("resolve_role(") + src.count("resolve_thinking_provider()") >= 4


def test_conflicts_reads_the_role_list():
    """Criterion 10: no hand-written tuple of role keys, and the role list carries the
    labels the sentences use."""
    import inspect
    from vaf.core import inference_profile as ip
    body = inspect.getsource(ip.conflicts)
    assert "ROLES" in body
    assert '"vision_provider"' not in body and '"subagent_provider"' not in body
    labels = {r.setting: r.local_conflict_label for r in mr.ROLES if r.local_conflict_label}
    assert labels == {"vision_provider": "image understanding",
                      "voice_agent_provider": "the voice call",
                      "browser_agent_provider": "the browser agent",
                      "thinking_provider": "the thinking runs",
                      "subagent_provider": "the sub-agents"}
    out = ip.conflicts({"inference_profile": "api", "provider": "openai",
                        "thinking_provider": "local", "subagent_provider": "local"})
    assert 'thinking_provider is "local": the thinking runs would need a local model.' in out
    assert 'subagent_provider is "local": the sub-agents would need a local model.' in out


# ── criterion 12: the facade boundary ────────────────────────────────────────────────

def test_the_resolver_stays_off_the_facade():
    import vaf
    assert not any("role" in name.lower() for name in vaf.__all__)
    doc = (ROOT / "docs" / "EMBEDDING.md").read_text(encoding="utf-8")
    assert "model_roles" in doc
