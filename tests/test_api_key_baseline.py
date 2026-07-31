# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""What a provider API key looks like on disk, and who can reach it. Frozen before the merge.

Provider keys are the most valuable secrets VAF holds, and they are the one class it stores
WORSE than the rest: `~/.vaf/config.json`, base64, next to an encrypted store that already
holds mail, GitHub and cloud credentials. The module says so itself at both ends -
"Base64 encoded for basic obfuscation - NOT encryption!" beside the defaults, and "Securely
store API key" in the docstring twenty lines further down. Two labels, one mechanism.

THIS FILE EXISTS BECAUSE THE MERGE IS NOT REVIEWABLE BY READING. The estate is base64 in
every installed copy of VAF and reaches users through `vaf update`; a mistake here does not
cost test lines, it costs every user their API keys. So the current behaviour is MEASURED and
frozen first, and the merged resolver has to reproduce it exactly.

THREE RESOLUTION PATHS TODAY, which is one more than it looks like from either end:

  product    `Config.get_api_key(p)` - a CLASSMETHOD reading `cls.load()`, i.e. the FILE
  embedder   `Agent(config={...})` -> `_config_overrides` -> `APIBackendManager(api_key=RAW)`
  direct     anything constructing `APIBackendManager` itself

The second never touches the first. Measured consequence, and the reason this is a contract
breach rather than an inconvenience: the embedder's key reaches ONE of thirteen consumers.
The failover chain and model discovery call `Config.get_api_key` unconditionally, even when
`self._embedded` is true - so for an embedder that chain is structurally dead, not weak.
"""
import base64

import pytest

# Synthetic throughout; never a real key. Shapes copied from the real providers so the
# base64 forms below are the ones a real installation actually carries.
SYNTHETIC = {
    "openai": "sk-proj-ABC123",
    "anthropic": "sk-ant-api03-XYZ",
    "google": "AIzaSyD-1234567890",
    "veyllo": "vaf_live_secret",
}

# MEASURED 2026-07-31 from the live encoder, not computed here: a test that recomputes the
# expectation with the same call it is checking agrees with any answer. These are the exact
# strings a user's config.json holds today.
ON_DISK = {
    "openai": "c2stcHJvai1BQkMxMjM=",
    "anthropic": "c2stYW50LWFwaTAzLVhZWg==",
    "google": "QUl6YVN5RC0xMjM0NTY3ODkw",
    "veyllo": "dmFmX2xpdmVfc2VjcmV0",
}


@pytest.fixture
def clean_config(tmp_path, monkeypatch):
    """A config file of our own, established rather than hoped for.

    The first version of this fixture set HOME and the XDG names and was silently useless:
    `Config.CONFIG_FILE` is a CLASS ATTRIBUTE computed at IMPORT time from
    `Path.home() / ".vaf"`, so changing the environment afterwards moves nothing. It showed
    up as one test reading the previous test's key - a fixture that finds its precondition
    instead of creating it, which is the failure mode this repository has already written
    down. The attribute is what decides, so the attribute is what gets pointed somewhere else.
    """
    from vaf.core.config import Config

    app_dir = tmp_path / "vaf"
    app_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Config, "APP_DIR", app_dir)
    monkeypatch.setattr(Config, "CONFIG_FILE", app_dir / "config.json")
    yield


# ── what the file holds ──────────────────────────────────────────────────────

@pytest.mark.parametrize("provider", sorted(SYNTHETIC))
def test_the_on_disk_form_is_unchanged(provider, clean_config):
    """One character different is not a refactor, it is a user who has to find their key
    again. The literal is frozen, not recomputed."""
    from vaf.core.config import Config

    Config.set_api_key(provider, SYNTHETIC[provider])
    assert Config.load().get(f"api_key_{provider}") == ON_DISK[provider]


@pytest.mark.parametrize("provider", sorted(SYNTHETIC))
def test_the_round_trip_returns_exactly_what_went_in(provider, clean_config):
    from vaf.core.config import Config

    Config.set_api_key(provider, SYNTHETIC[provider])
    assert Config.get_api_key(provider) == SYNTHETIC[provider]


def test_a_plaintext_estate_entry_still_reads(clean_config):
    """The `try/except` fallback, which is the half that must survive the merge.

    Keys written before the encoder existed sit in the file unencoded. They are indexed by
    the same name, so one key has two encodings and only an exception tells them apart. That
    is two truths under one name - and it is exactly why the merge has to keep reading both
    rather than deciding which one it likes.
    """
    from vaf.core.config import Config

    for raw in ("sk-proj-PLAINTEXT", "sk-ant-api03-PLAIN", "AIzaSyPLAIN"):
        cfg = Config.load()
        cfg["api_key_openai"] = raw
        Config.save(cfg)
        assert Config.get_api_key("openai") == raw


def test_an_unset_key_is_the_empty_string(clean_config):
    """The control, and the reason the hard-error change is user-visible: `""` is what every
    one of the thirteen consumers reads as "not configured"."""
    from vaf.core.config import Config

    assert Config.get_api_key("openai") == ""


def test_the_double_encoding_hazard_is_recorded_not_repaired(clean_config):
    """LATENT, not live, and frozen so the merge does not inherit it silently.

    `get_api_key` decodes base64 and falls back to plaintext on failure, so a stored value
    that IS valid base64 and valid UTF-8 is destroyed rather than returned. No real key shape
    reaches that state - measured across the `sk-`, `sk-ant-`, `gsk_` and `AIza` forms, all
    of which fail to decode and therefore survive. The hazard is the shape, not today's data.
    """
    from vaf.core.config import Config

    for shape in SYNTHETIC.values():
        with pytest.raises(Exception):
            base64.b64decode(shape.encode()).decode()

    cfg = Config.load()
    cfg["api_key_openai"] = "dGVzdA=="        # valid base64 for "test"
    Config.save(cfg)
    assert Config.get_api_key("openai") == "test", (
        "the double-encoding hazard changed shape; the merge must not inherit it by accident"
    )


# ── who can reach a key, and who cannot ──────────────────────────────────────

def _get_api_key_callers():
    """GENERATED, never typed. The identity baseline caught a hand-written list inventing
    nine tools and dropping five; the same discipline applies to a list that decides which
    consumers a migration has to keep working."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "vaf"
    out = set()
    for f in sorted(root.rglob("*.py")):
        src = f.read_bytes().decode()
        for i, line in enumerate(src.split("\n"), 1):
            if re.search(r"\bget_api_key\s*\(", line) and "def get_api_key" not in line:
                out.add(f"{f.relative_to(root.parent)}:{i}")
    return out


# Measured 2026-07-31. Thirteen call sites; the embedder's key reaches exactly one of them
# (`api_backend.py:864`, and only through the separate RAW path below).
CALLERS = {
    "vaf/api/voice_routes.py:82",
    "vaf/cli/cmd/settings.py:718",
    "vaf/cli/cmd/settings.py:913",
    "vaf/core/api_backend.py:864",
    "vaf/core/api_backend.py:958",
    "vaf/core/api_backend.py:961",
    "vaf/core/api_backend.py:1364",
    "vaf/core/headless_runner.py:919",
    "vaf/core/speech_api.py:113",
    "vaf/core/voice_agent.py:580",
    "vaf/core/voice_agent.py:587",
    "vaf/tools/coder.py:3185",
    "vaf/whare_wananga/teacher.py:132",
}


def test_the_set_of_consumers_is_what_was_measured():
    """A new consumer is a new place that must keep working after the merge, and a vanished
    one is a lane that quietly stopped asking. Frozen by NAME rather than by count - the
    count was the thing that went wrong three times in the round that produced this file."""
    live = _get_api_key_callers()
    assert live == CALLERS, f"the consumer set moved: {sorted(live ^ CALLERS)}"


def test_the_embedders_key_never_reaches_the_shared_reader():
    """THE contract breach, pinned as a fact rather than argued.

    `Config.get_api_key` is a classmethod over `cls.load()`, so a config dict handed to
    `Agent(...)` cannot influence it. The embedder's key travels a separate, raw path into
    one constructor argument. Everything else - failover, model discovery, voice, speech,
    the coder - reads the file.
    """
    import inspect

    from vaf.core.config import Config

    src = inspect.getsource(Config.get_api_key)
    assert "cls.get(" in src, "the reader no longer goes through the class-level config"
    assert "self" not in src.split("(", 1)[1].split(")", 1)[0], (
        "get_api_key gained instance state; if it can see an embedder's config now, this "
        "whole baseline needs re-measuring rather than adjusting"
    )


def test_the_reload_lock_is_the_only_thing_protecting_an_embedded_key():
    """Frozen because the merge DELETES it, and it is a guard rather than ballast.

    `reload_api_backend` re-applies the provider and key from the LIVE on-disk config. For an
    embedded agent whose key exists only in memory, this check is the sole reason a provider
    switch does not silently replace it with whatever the file holds. It may only disappear
    once precedence does its job - caller config beating the file - and that has to be
    asserted on the refusing side, not assumed.
    """
    import inspect

    from vaf.core.agent import Agent

    src = inspect.getsource(Agent.reload_api_backend)
    assert "_config_overrides" in src, (
        "the embedded-mode guard is gone from reload_api_backend. If precedence replaced it, "
        "there must be a test proving a reload cannot overwrite a caller-supplied key - "
        "without one this is a regression with a green suite."
    )
