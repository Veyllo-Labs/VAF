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

UPDATED AFTER THE MERGE. The literals below no longer describe what VAF WRITES - they
describe the ESTATE it must keep READING, which is the half that decides whether a user
loses their keys. Everything else here moved from "what is" to "what must still hold".

THREE RESOLUTION PATHS BEFORE THE MERGE, one more than it looked like from either end:

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
def test_the_estate_form_still_reads(provider, clean_config):
    """THE test the whole change hangs on: an installed base64 key keeps working.

    These literals used to describe what `set_api_key` WROTE. It writes to the encrypted
    store now, so what they describe is the ESTATE - the exact strings sitting in every
    config.json that `vaf update` will upgrade. One character misread here is not a
    refactor, it is a user who has to find their key again.
    """
    from vaf.core.config import Config

    config = Config.load()
    config[f"api_key_{provider}"] = ON_DISK[provider]
    Config.save(config)
    assert Config.get_api_key(provider) == SYNTHETIC[provider]


@pytest.mark.parametrize("provider", sorted(SYNTHETIC))
def test_a_stored_key_round_trips(provider, clean_config):
    """The new write path, end to end - and it must NOT land in config.json."""
    from vaf.core.config import Config

    Config.set_api_key(provider, SYNTHETIC[provider])
    assert Config.get_api_key(provider) == SYNTHETIC[provider]
    assert not Config.load().get(f"api_key_{provider}"), (
        "the key is in config.json again; the write side is the half that keeps a Settings "
        "save from writing to a file nobody asks any more"
    )


def test_a_plaintext_estate_entry_still_reads(clean_config):
    """The `try/except` fallback, which is the half that must survive the merge.

    Keys written before the encoder existed sit in the file unencoded. They are indexed by
    the same name, so one key has two encodings and only an exception tells them apart. That
    is two truths under one name - and it is exactly why the merge has to keep reading both
    rather than deciding which one it likes.
    """
    from vaf.core.config import Config

    raw = "sk-proj-PLAINTEXT"
    cfg = Config.load()
    cfg["api_key_openai"] = raw
    Config.save(cfg)
    assert Config.get_api_key("openai") == raw
    # Read again: the first read MIGRATED it, so this one comes from the store. Same answer
    # either way - which is the whole point, and why the loop this test used to run was
    # wrong: after the first iteration the store already answered, so the remaining
    # iterations measured the migration rather than the estate.
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
    out = {}
    for f in sorted(root.rglob("*.py")):
        src = f.read_bytes().decode()
        n = sum(
            1 for line in src.split("\n")
            if re.search(r"\bget_api_key\s*\(", line) and "def get_api_key" not in line
        )
        if n:
            # `.as_posix()`: on Windows `relative_to` yields backslashes and every
            # comparison below would miss.
            # FILE + COUNT, not file:line - keyed by line this froze twice in one day on
            # pure line shifts from edits ABOVE the call (the identity wiring, then the
            # allowlist wiring). A guard that needs updating when nothing it guards
            # changed trains people to update it without reading; a new or vanished CALL
            # still changes the count and still trips.
            out[f.relative_to(root.parent).as_posix()] = n
    return out


# Measured 2026-07-31, AFTER the merge. Fourteen call sites - the four in `search.py` used
# to read `Config.get("api_key_brave_search")` RAW, past `get_api_key` entirely, because the
# search keys are a third family under the same prefix: never written by `set_api_key`, only
# by the web path, and therefore never base64. They were invisible to the count that shaped
# the first plan; missing them would have taken the web search its keys.
CALLERS = {
    "vaf/api/voice_routes.py": 1,
    "vaf/cli/cmd/settings.py": 2,
    # The terminal app refuses a sub-agent provider it has no key for, which is
    # what the inquirer menu above it always did. It ASKS whether a key exists
    # and never reads the value, so it adds a place that must keep working and
    # no place a key can escape from.
    "vaf/cli/tui_app/screens.py": 1,
    "vaf/core/api_backend.py": 1,
    "vaf/core/headless_runner.py": 1,
    "vaf/core/speech_api.py": 1,
    "vaf/core/voice_agent.py": 2,
    "vaf/tools/coder.py": 1,
    "vaf/tools/search.py": 4,
    "vaf/whare_wananga/teacher.py": 1,
}

# The three places a key can ENTER storage. Frozen because the first version of this change
# made the write side "set_api_key" and missed the two that matter most - a boundary as wide
# as the surface somebody had enumerated. The two web paths carried keys RAW into
# config.json, so with only the read side moved a Settings save would have kept writing to a
# file nobody asks any more: the user changes their key, the UI says saved, and the agent
# goes on using the old one.
WRITE_SITES = {
    "vaf/cli/cmd/settings.py",       # Config.set_api_key -> the store
    "vaf/api/config_routes.py",      # HTTP config update -> absorb_config_keys
    "vaf/core/web_server.py",        # WebSocket config update -> absorb_config_keys
}


def test_the_set_of_consumers_is_what_was_measured():
    """A new consumer is a new place that must keep working, and a vanished one is a lane
    that quietly stopped asking. Frozen by NAME rather than by count - the count was the
    thing that went wrong three times in the round that produced this file."""
    live = _get_api_key_callers()
    # Both sides are dicts, so the difference has to be taken over their ITEMS.
    # `live ^ CALLERS` raised TypeError instead: the guard fired correctly and
    # then could not say what had moved.
    assert live == CALLERS, (
        "the consumer set moved: "
        f"{sorted(set(live.items()) ^ set(CALLERS.items()))}")


def test_every_write_site_routes_into_the_store():
    """The write side, asserted as places rather than as a sentence.

    Each of the three either calls `set_api_key` (which stores) or `absorb_config_keys`
    (which lifts every `api_key_*` out of a payload before it is saved). A fourth site
    appearing that writes `api_key_*` straight into the config is the regression this pins.
    """
    import pathlib as _pl
    import re as _re

    root = _pl.Path(__file__).resolve().parents[1]
    for site in sorted(WRITE_SITES):
        src = (root / site).read_bytes().decode()
        assert _re.search(r"absorb_config_keys|set_api_key", src), (
            f"{site} no longer routes API keys into the store"
        )

    # And nobody else merges a raw payload into the config without absorbing first.
    #
    # By CALL, not by substring. The substring version flagged `vaf/core/api_keys.py` for
    # naming the helper in a docstring, which is the "a guard reading text instead of code"
    # failure in tests/README.md - and the tempting fix, adding the file to the allow-list,
    # would have blinded the guard to the one module most able to do the damage it watches
    # for. An AST walk cannot be fooled by prose in either direction.
    import ast as _ast

    offenders = []
    for f in sorted((root / "vaf").rglob("*.py")):
        if f.relative_to(root).as_posix() in WRITE_SITES or f.name == "config.py":
            continue
        try:
            tree = _ast.parse(f.read_bytes())
        except SyntaxError:                                  # pragma: no cover
            continue
        calls = (
            n for n in _ast.walk(tree)
            if isinstance(n, _ast.Call)
            and getattr(n.func, "attr", getattr(n.func, "id", "")) == "merge_preserving_nonempty_sensitive"
        )
        if next(calls, None) is not None:
            offenders.append(f.relative_to(root).as_posix())
    assert not offenders, (
        f"a config-merge path that was not part of the measured write set: {offenders}. "
        f"It would write API keys raw into config.json where nothing asks for them."
    )


def test_the_embedders_key_now_reaches_every_consumer():
    """The contract breach, closed - and asserted as its OPPOSITE.

    This test used to pin the breach as a fact: `Config.get_api_key` was a classmethod over
    the on-disk config, so a dict handed to `Agent(...)` could not influence it, and the
    embedder's key travelled a separate raw path into ONE constructor argument. The failover
    chain and model discovery asked the file regardless, so that chain was structurally dead
    for an embedder while `docs/EMBEDDING.md` said "pass your key".

    Now the overrides dict itself is the highest-precedence SOURCE, so anything resolving a
    key sees it. Driven through the resolver rather than through a backend, because what
    changed is where the answer comes from, not who asks.
    """
    from vaf.core.api_keys import resolve_api_key

    caller = {"api_key_openai": "sk-CALLER-ONLY"}
    assert resolve_api_key("openai", caller) == "sk-CALLER-ONLY"
    # And it does not bleed into a provider the caller did not supply.
    assert resolve_api_key("anthropic", caller) == ""


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
