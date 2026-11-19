# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Only an admin chooses which provider and model see images and drive the browser.

THE DEFECT. `vision_provider`, `vision_model`, `browser_agent_provider` and
`browser_agent_model` matched no admin-only prefix and no entry, so a settings save by any
signed-in account (PATCH /api/config, the WebSocket `save_config`) stored them: measured,
a non-admin pointed both lanes at a provider of their choice, on the instance's key for
that provider, and since there is one config file, for every account. The `vision_`
budgets (output bound, downscale edge, re-encode quality, inline image mode) were
writable the same way.

THE RULE is the key name: the `vision_` and `browser_agent_` prefixes are in
`Config.GLOBAL_CONFIG_KEY_PREFIXES`, and the one save step both web lanes run
(`Config.split_connection_toggles`) drops them from a non-admin's body. A class guard
ties every key that picks a provider or model for a job to that set.

The second half: the warnings that quote such a stored value (`note_foreign_model`, the
CLI's `_reset_notice`) stay one line whatever the value holds, and the once-set behind
the warning keeps digests, not values. Synthetic identities and a scratch config only.
"""
import ast
import json
import random
import threading
from types import SimpleNamespace

import pytest

import vaf.core.model_catalog as mc
import vaf.core.provider_registry as pr
from vaf.core.config import MODEL_ID_KEYS, MODEL_PROVIDER_KEYS, Config

TENANT_SCOPE = "ab12cd34-0000-4000-8000-000000000001"
ADMIN_SCOPE = "ab12cd34-0000-4000-8000-0000000000aa"

LANE_KEYS = ("vision_provider", "vision_model", "browser_agent_provider", "browser_agent_model")
ALL_FAMILY_KEYS = LANE_KEYS + ("vision_local_mmproj", "vision_mode",
                               "vision_description_max_tokens", "vision_image_max_edge",
                               "vision_image_jpeg_quality")

# What an admin chose: models of the named providers, so the foreign-lane reset leaves
# them alone.
ADMIN_CHOICE = {"provider": "openai", "vision_provider": "google",
                "vision_model": "gemini-2.5-flash", "browser_agent_provider": "openai",
                "browser_agent_model": "gpt-5.6-terra", "tts_auto_speak": False}
HOSTILE = {"vision_provider": "openrouter", "vision_model": "openrouter/auto",
           "browser_agent_provider": "openrouter", "browser_agent_model": "openrouter/auto",
           "vision_mode": "inline_multimodal", "vision_description_max_tokens": 999999,
           "tts_auto_speak": True}

# The control characters the oracle refuses: C0, DEL, C1, the line and paragraph separators.
_BAD = frozenset([*map(chr, range(0x00, 0x20)), "\x7f", *map(chr, range(0x80, 0xA0)),
                  "\u2028", "\u2029"])


def _clean(text: str) -> bool:
    return not (set(text) & _BAD)


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    """A process that has reported and logged nothing yet; what would be printed or
    logged is recorded instead."""
    monkeypatch.setattr(pr, "_foreign_noted", set())
    monkeypatch.setattr(Config, "_lane_reset_logged", set())
    monkeypatch.setattr(Config, "_migration_logged", set())
    rec = SimpleNamespace(events=[], log=[])
    import vaf.cli.ui as ui_mod
    monkeypatch.setattr(ui_mod.UI, "event", staticmethod(lambda *a, **kw: rec.events.append(a)))
    monkeypatch.setattr("vaf.core.log_helper.append_domain_log",
                        lambda *a, **kw: rec.log.append(a))
    monkeypatch.setattr(mc, "_today", lambda: __import__("datetime").date(2027, 6, 1))
    return rec


@pytest.fixture
def stored(tmp_path, monkeypatch):
    """Write a synthetic config file and point Config at it; returns a reader of the
    raw file."""
    from vaf.core import migrations

    path = tmp_path / "config.json"
    monkeypatch.setattr(Config, "CONFIG_FILE", path)
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)
    monkeypatch.setattr(Config, "_filelock", None)

    def write(cfg: dict):
        path.write_text(json.dumps({"config_format_version": migrations.CONFIG_FORMAT_VERSION,
                                    **cfg}), encoding="utf-8")

    def raw() -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    return SimpleNamespace(write=write, raw=raw, path=path)


def _patch(body: dict, user: dict) -> dict:
    import asyncio

    from vaf.api import config_routes
    request = SimpleNamespace(state=SimpleNamespace(user=user))
    return asyncio.run(config_routes.patch_config(dict(body), request, user))


def _ws_save(monkeypatch, *, role: str, scope: str, body: dict) -> dict:
    """One `save_config` over a real `/ws` connection with a signed token; the answer."""
    import jwt
    from starlette.testclient import TestClient

    import vaf.auth.crypto as crypto
    from vaf.core.web_server import app

    monkeypatch.setattr(crypto, "get_jwt_secret", lambda: "s" * 32)
    token = jwt.encode({"sub": "7", "user_scope_id": scope, "username": "alice",
                        "role": role}, "s" * 32, algorithm="HS256")
    box = {}
    with TestClient(app, client=("127.0.0.1", 40000)).websocket_connect(f"/ws?token={token}") as ws:
        ws.send_text(json.dumps({"type": "save_config", "config": body}))

        def pump():
            try:
                for _ in range(100):
                    msg = ws.receive_json()
                    if msg.get("type") == "config_saved":
                        box["msg"] = msg
                        return
            except Exception as e:  # noqa: BLE001 - reported by the assert below
                box["error"] = e

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        reader.join(20)
    assert "msg" in box, f"no config_saved ({box.get('error')!r})"
    return box["msg"]


# ── AC1: the key names ────────────────────────────────────────────────────────

def test_lane_keys_are_admin_only():
    """MUTATIONS: drop `"vision_"` or `"browser_agent_"` from the prefixes; keep the
    literal `vision_local_mmproj` entry (the last assertion)."""
    for key in ALL_FAMILY_KEYS:
        assert Config.is_global_config_key(key), f"{key} is writable by any signed-in account"
        assert key not in Config.filter_for_non_admin({key: "x", "theme": "dark"}), key
    assert Config.filter_for_non_admin({k: "x" for k in ALL_FAMILY_KEYS}) == {}
    # The prefix covers it; a literal entry next to it is a second copy of one decision.
    assert "vision_local_mmproj" not in Config.GLOBAL_CONFIG_KEYS


def test_the_schema_doc_says_so():
    """The schema rows announce the rule (and `tests/test_config_admin_only_claims.py` then
    holds each claim to the code). MUTATION: remove "Admin-only" from one of the rows."""
    import pathlib
    import re

    doc = (pathlib.Path(__file__).resolve().parent.parent / "docs" / "setup"
           / "CONFIG_SCHEMA.md").read_text(encoding="utf-8")
    for key in ALL_FAMILY_KEYS:
        rows = re.findall(r"^\|\s*`" + re.escape(key) + r"`\s*\|(.*)$", doc, re.M)
        assert len(rows) == 1, key
        assert re.search(r"admin[- ]only", rows[0], re.I), f"the `{key}` row does not say admin-only"


# ── AC2: the class ────────────────────────────────────────────────────────────

def test_every_key_that_picks_a_provider_or_model_is_admin_only():
    """Every role setting a settings screen can change, and every key that names a model
    or the provider a model id belongs to, decides what runs on the instance's API keys
    for every account. MUTATION: drop either prefix."""
    from vaf.core.model_roles import ROLE_CONFIG_KEYS

    keys = set(ROLE_CONFIG_KEYS) | set(MODEL_ID_KEYS) | set(MODEL_PROVIDER_KEYS.values())
    # A source that silently became empty would make this pass on nothing.
    assert len(keys) >= 30, sorted(keys)
    assert set(LANE_KEYS) <= keys
    open_keys = sorted(k for k in keys if not Config.is_global_config_key(k))
    assert open_keys == [], f"writable by any signed-in account: {open_keys}"


# ── AC3: a non-admin PATCH ────────────────────────────────────────────────────

def test_non_admin_patch_keeps_the_lane_keys(stored):
    """MUTATIONS: drop either prefix; make `filter_for_non_admin` return the body as is."""
    stored.write(ADMIN_CHOICE)
    answer = _patch(HOSTILE, {"role": "user", "user_scope_id": TENANT_SCOPE,
                              "username": "alice"})
    raw = stored.raw()
    for key in LANE_KEYS:
        assert raw[key] == ADMIN_CHOICE[key], key
        assert answer[key] == ADMIN_CHOICE[key], key
    assert raw.get("vision_mode", Config.DEFAULTS["vision_mode"]) == Config.DEFAULTS["vision_mode"]
    assert raw.get("vision_description_max_tokens") != 999999
    # The account's own setting in the same body is saved: the filter drops keys, not saves.
    assert raw["tts_auto_speak"] is True


# ── AC4: a non-admin WebSocket save ───────────────────────────────────────────

def test_non_admin_websocket_save_keeps_the_lane_keys(stored, monkeypatch):
    """MUTATIONS: drop either prefix; pass `is_admin=True` in the WebSocket lane."""
    from vaf.core.config import get_local_admin_scope_id

    assert TENANT_SCOPE != str(get_local_admin_scope_id())
    stored.write(ADMIN_CHOICE)
    msg = _ws_save(monkeypatch, role="user", scope=TENANT_SCOPE, body=dict(HOSTILE))
    assert msg.get("status") == "success", msg
    raw = stored.raw()
    for key in LANE_KEYS:
        assert raw[key] == ADMIN_CHOICE[key], key
    assert raw["tts_auto_speak"] is True


# ── AC5: an admin still chooses ───────────────────────────────────────────────

_ADMIN_BODY = {"vision_provider": "openai", "vision_model": "gpt-5.6-terra",
               "browser_agent_provider": "openai", "browser_agent_model": "gpt-5.6-terra"}


def test_admin_patch_still_stores_the_lane_keys(stored):
    """MUTATION: drop the lane keys from every body in `split_connection_toggles`."""
    stored.write({"provider": "openai"})
    answer = _patch(_ADMIN_BODY, {"role": "admin", "user_scope_id": ADMIN_SCOPE,
                                  "username": "admin"})
    raw = stored.raw()
    for key, value in _ADMIN_BODY.items():
        assert raw[key] == value, key
        assert answer[key] == value, key


def test_admin_websocket_save_still_stores_the_lane_keys(stored, monkeypatch):
    """MUTATION: drop the lane keys from every body in `split_connection_toggles`."""
    stored.write({"provider": "openai"})
    msg = _ws_save(monkeypatch, role="admin", scope=ADMIN_SCOPE, body=dict(_ADMIN_BODY))
    assert msg.get("status") == "success", msg
    raw = stored.raw()
    for key, value in _ADMIN_BODY.items():
        assert raw[key] == value, key


# ── AC6: what is stored stays ─────────────────────────────────────────────────

def test_stored_lane_values_survive_load(stored):
    """The gate is on writes only: the file records no writer, so a value stored before
    it is read as before. MUTATION: reset the four keys in `Config.load`."""
    stored.write(ADMIN_CHOICE)
    loaded = Config.load()
    raw = stored.raw()
    for key in LANE_KEYS:
        assert loaded[key] == ADMIN_CHOICE[key], key
        assert raw[key] == ADMIN_CHOICE[key], key


# ── AC7: the warning stays one line ───────────────────────────────────────────

def test_foreign_model_note_is_one_line(_quiet):
    """MUTATIONS: quote the model as `'{model}'` without repr; drop `one_line` in
    `provider_label_line`."""
    model = "x\n\rFORGED\x1b]0;T\x07\x85\u2028\u2029\x7fy"
    assert pr.note_foreign_model("deepseek", model, "browser_agent_model") is True
    assert len(_quiet.log) == 1 and len(_quiet.events) == 1
    line = str(_quiet.log[0][1])
    shown = str(_quiet.events[0][1])
    for text in (line, shown):
        assert _clean(text), repr(text)
        assert repr(model) in text
    assert line.startswith("[FOREIGN_MODEL] browser_agent_model 'x\\n\\rFORGED\\x1b")

    # An unknown provider is its own label: a line break in it reads as a space.
    assert pr.note_foreign_model("evil\nprov", "m", "browser_agent_model") is True
    shown = str(_quiet.events[-1][1])
    assert _clean(shown), repr(shown)
    assert "is not one of evil prov's models" in shown


def test_ordinary_foreign_model_note_reads_as_before(_quiet):
    pr.note_foreign_model("deepseek", "veyllo-chat", "browser_agent_model")
    assert str(_quiet.events[-1][1]).startswith(
        "browser_agent_model 'veyllo-chat' is not one of DeepSeek's models and is ignored; "
        "DeepSeek's own model is used instead.")


# ── AC8: the once-set keeps digests ───────────────────────────────────────────

def test_foreign_noted_keeps_digests(_quiet):
    """MUTATIONS: key the set on the raw model; key it on the raw provider."""
    provider = "p" * 100_000
    models = [f"m{i}-" + "x" * 100_000 for i in range(3)]
    for model in models:
        assert pr.note_foreign_model(provider, model, "browser_agent_model") is True
    assert len(pr._foreign_noted) == 3
    held = sum(len(str(part)) for entry in pr._foreign_noted for part in entry)
    assert held < 1000, held
    assert pr.note_foreign_model(provider, models[1], "browser_agent_model") is False
    # The line quotes a clipped value and a clipped label, not the whole ones.
    assert all(len(str(a[1])) < 1000 for a in _quiet.log)


# ── AC9: the CLI's reset notice ───────────────────────────────────────────────

@pytest.fixture
def notice(monkeypatch):
    import vaf.core.model_roles as roles
    from vaf.cli.cmd import models
    monkeypatch.setattr(roles, "resolve_role",
                        lambda *a, **kw: SimpleNamespace(model="gpt-5.6-terra"))
    return models._reset_notice


def test_reset_notice_is_one_line(notice):
    """MUTATIONS: drop `!r` in `_reset_notice`; use `provider_label` for its label."""
    text = notice("browser_agent_model", "a\nb\x1bc", "openai")
    assert not any(ord(ch) < 0x20 for ch in text), repr(text)
    assert "browser_agent_model 'a\\nb\\x1bc' is not one of OpenAI's models" in text
    text = notice("browser_agent_model", "m", "evil\nprov\x1b")
    assert _clean(text), repr(text)
    assert "is not one of evil prov 's models" in text
    # An ordinary id reads as before.
    assert notice("browser_agent_model", "gpt-5.6-terra", "deepseek").startswith(
        "browser_agent_model 'gpt-5.6-terra' is not one of DeepSeek's models and was reset; "
        "DeepSeek's own model (gpt-5.6-terra) is used now.")


# ── Fuzz: both texts over hostile values ──────────────────────────────────────

_ALPHABET = (
    [chr(c) for c in range(0x00, 0x20)] + ["\x7f"] + [chr(c) for c in range(0x80, 0xA0)]
    + ["\u2028", "\u2029", "\u202e", "\u202d", "\u2066", "\u200f", "\x1b[2J", "\x1b]0;T\x07",
       "\x1b[", "\x9b", "'", '"', "\\", "\\x1b", "\\n"]
    + list("abcXYZ019 -_/.:") + ["\U0001F600", "\U00010348", "\u00e9"]
)


def _draw(rng: random.Random) -> str:
    size = rng.choice((0, 1, 5, 40, 199, 200, 201, 600, rng.randint(0, 5000)))
    return "".join(rng.choice(_ALPHABET) for _ in range(size))


def _repr_span(msg: str, source: str, label: str, tail: str) -> str:
    """The repr'd value: between `<source> ` and the last ` is not one of <label>'s models<tail>`."""
    assert msg.startswith(source + " "), msg[:80]
    end = msg.rindex(f" is not one of {label}'s models{tail}")
    return msg[len(source) + 1:end]


def fuzz_once(rng: random.Random, notice, rec) -> list:
    """One draw through both texts; the oracle's complaints (empty when it holds). ``rec``
    is the recorder `_quiet` installs in place of the terminal and the backend log."""
    from vaf.core.tool_dispatch import clip_middle

    bad = []
    provider, model = _draw(rng), _draw(rng)
    clipped = clip_middle(model, 200, marker="... ({left_out} chars left out) ...")
    label = pr.provider_label_line(provider)
    if not _clean(label) or len(label) > 300:
        bad.append(("label", provider, model))

    pr._foreign_noted.clear()
    rec.events.clear()
    rec.log.clear()
    pr.note_foreign_model(provider, model, "browser_agent_model")
    if len(rec.events) != 1 or len(rec.log) != 1:
        return [("not one terminal line and one log line", provider, model)]
    texts = [str(rec.events[0][1]), str(rec.log[0][1])[len("[FOREIGN_MODEL] "):]]
    for text in texts:
        if not _clean(text):
            bad.append(("control character", provider, model))
        span = _repr_span(text, "browser_agent_model", label, " and is ignored")
        if ast.literal_eval(span) != clipped:
            bad.append(("repr span", provider, model))
    if any(sum(len(str(p)) for p in entry) >= 200 for entry in pr._foreign_noted):
        bad.append(("once-set entry", provider, model))

    text = notice("browser_agent_model", model, provider)
    if not _clean(text):
        bad.append(("reset notice control character", provider, model))
    if ast.literal_eval(_repr_span(text, "browser_agent_model", label, " and was reset")) != clipped:
        bad.append(("reset notice repr span", provider, model))
    return bad


def test_fuzz_both_texts_stay_one_line(notice, _quiet):
    """300 seeded draws (the larger run happens outside the suite with the same function).
    MUTATIONS: drop `!r` in either text; drop `one_line` on either label."""
    rng = random.Random(162)
    bad = []
    for _ in range(300):
        bad += fuzz_once(rng, notice, _quiet)
    assert bad == [], bad[:3]
