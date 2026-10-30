# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Two config keys, one decision: which provider a sub-agent runs on.

THE DEFECT THIS PINS. `subagent_provider` names the choice and
`subagent_use_separate_provider` gates it, and the gate defaults to False. A
name written WITHOUT the gate is therefore silently inert: nothing raises,
nothing warns, every sub-agent keeps the main provider. The terminal app's
settings row did exactly that - it wrote the name, moved its marker onto the
chosen provider, and reported success, while all six consumers went on
inheriting. The classic menu's own panel had the same hole in its display half:
it read the gate into a variable and then printed the name regardless.

MEASURED before the primitive was written: SIX places derived the pair by hand,
in two byte-identical blocks. Four spawn sites (coder, librarian,
research_agent, document_agent) turned it into `VAF_PROVIDER`; two more
(headless_runner, platform) resolved it against the main provider. That number
is what earns the primitive, and the ratchet at the bottom is what keeps it
from growing back.
"""
import types

import pytest

from vaf.core.config import Config, set_subagent_provider, subagent_provider_override

# The six that used to hand-roll it, plus the two lanes that write it.
CONSUMERS = (
    "vaf/tools/coder.py",
    "vaf/tools/librarian.py",
    "vaf/tools/research_agent.py",
    "vaf/tools/document_agent.py",
    "vaf/core/headless_runner.py",
    "vaf/core/platform.py",
)


@pytest.fixture(autouse=True)
def _config_in_memory(monkeypatch):
    """A dict store. These tests WRITE config keys; the real ~/.vaf/config.json
    must never be one of the things they touch."""
    store = {}

    def _get(cls, key, default=None):
        if key in store:
            return store[key]
        return default if default is not None else cls.DEFAULTS.get(key)

    def _set(cls, key, value):
        store[key] = value

    monkeypatch.setattr(Config, "get", classmethod(_get))
    monkeypatch.setattr(Config, "set", classmethod(_set))
    return store


# ── the primitive ───────────────────────────────────────────────────────────────────

def test_the_name_alone_is_inert(_config_in_memory):
    """The headline. This is what the terminal app did, and why nothing changed."""
    Config.set("subagent_provider", "anthropic")
    assert subagent_provider_override() is None
    assert _config_in_memory.get("subagent_use_separate_provider") is None


def test_the_setter_writes_both_halves(_config_in_memory):
    set_subagent_provider("anthropic")
    assert subagent_provider_override() == "anthropic"
    assert _config_in_memory["subagent_provider"] == "anthropic"
    assert _config_in_memory["subagent_use_separate_provider"] is True


def test_inherit_clears_the_gate_it_set(_config_in_memory):
    """Going back to inherit must not leave the gate standing - the stale name
    would come back into force the moment anything wrote the gate again."""
    set_subagent_provider("anthropic")
    set_subagent_provider("inherit")
    assert subagent_provider_override() is None
    assert _config_in_memory["subagent_use_separate_provider"] is False


@pytest.mark.parametrize("value", ["", None, "   ", "inherit"])
def test_every_way_of_saying_inherit_means_inherit(_config_in_memory, value):
    set_subagent_provider(value)
    assert subagent_provider_override() is None


def test_the_sentinel_never_leaks_as_a_provider(_config_in_memory):
    """"inherit" is a stored sentinel, not something a backend can be built on.
    The four spawn sites put this return value straight into VAF_PROVIDER."""
    Config.set("subagent_provider", "inherit")
    Config.set("subagent_use_separate_provider", True)      # gate on, name absent
    assert subagent_provider_override() is None


def test_an_unreadable_config_inherits_rather_than_breaking_a_spawn(monkeypatch):
    """This runs on the spawn path of every sub-agent. It must never raise:
    the failure mode of a throw here is a sub-agent that does not start."""
    def _boom(cls, key, default=None):
        raise OSError("config unreadable")

    monkeypatch.setattr(Config, "get", classmethod(_boom))
    assert subagent_provider_override() is None


# ── the terminal app's row ──────────────────────────────────────────────────────────

def _rows():
    from vaf.cli.tui_app.screens import SettingsScreen
    return SettingsScreen.__new__(SettingsScreen)._menu_rows("subagent_provider")


def _marked(rows):
    return [str(r[2]) for r in rows if "▍" in str(r[2])]


def test_the_marker_follows_what_subagents_actually_run_on(_config_in_memory):
    """With the name set but the gate off, sub-agents run on the MAIN provider.
    The row used to mark the name and contradict that."""
    Config.set("subagent_provider", "anthropic")            # name only, as before
    marked = _marked(_rows())
    assert len(marked) == 1 and "Inherit" in marked[0], marked


def test_the_marker_follows_the_choice_once_it_is_real(_config_in_memory):
    set_subagent_provider("anthropic")
    marked = _marked(_rows())
    assert len(marked) == 1 and "anthropic" in marked[0], marked


def _screen(rows):
    """The real screen with just enough around it to activate one row.

    `app` is a read-only property on every Textual node, so the stand-in goes
    in through a subclass rather than an assignment.
    """
    from vaf.cli.tui_app.screens import SettingsScreen

    notified = []
    fake_app = types.SimpleNamespace(
        notify=lambda msg, **kw: notified.append((msg, kw)),
        post_message=lambda msg: None,
    )

    class _Detached(SettingsScreen):
        app = property(lambda self: fake_app)

    s = _Detached.__new__(_Detached)
    s._rows = rows
    s._stack = ["main", "subagent_provider"]
    s.notified = notified
    s._rebuild = lambda: None
    return s


def test_choosing_a_provider_writes_the_pair(_config_in_memory):
    """Wiring, not stage: the row must reach the setter, or the primitive is
    tested while the screen goes on writing one key."""
    rows = _rows()
    idx = next(i for i, r in enumerate(rows) if r[0] == "subagent_provider"
               and r[1] == "local")
    _screen(rows)._activate(idx)
    assert _config_in_memory["subagent_provider"] == "local"
    assert _config_in_memory["subagent_use_separate_provider"] is True


def test_a_provider_without_an_api_key_is_refused(_config_in_memory, monkeypatch):
    """What `vaf settings` does. Storing it would spawn every sub-agent onto a
    backend that cannot build, one process away from any error message."""
    monkeypatch.setattr(Config, "get_api_key", classmethod(lambda cls, p: ""))
    rows = _rows()
    idx = next((i for i, r in enumerate(rows) if r[0] == "subagent_provider"
                and r[1] not in ("inherit", "local")), None)
    if idx is None:
        pytest.skip("no API provider in the registry to try")
    screen = _screen(rows)
    screen._activate(idx)
    assert "subagent_provider" not in _config_in_memory, "a keyless provider was stored"
    assert screen.notified and "no API key" in screen.notified[0][0]


def test_local_needs_no_key(_config_in_memory, monkeypatch):
    """The refusal must not catch the one provider that has nothing to key."""
    monkeypatch.setattr(Config, "get_api_key", classmethod(lambda cls, p: ""))
    rows = _rows()
    idx = next(i for i, r in enumerate(rows) if r[0] == "subagent_provider"
               and r[1] == "local")
    _screen(rows)._activate(idx)
    assert _config_in_memory["subagent_provider"] == "local"


# ── the ratchet (Rule 2: a guard, not prose) ────────────────────────────────────────

def test_only_the_primitive_knows_the_two_keys_are_a_pair():
    """Shrink-only. Every consumer that reads the gate by hand is a place that
    can drift back into the defect above, and there were six of them."""
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    out = subprocess.run(["git", "grep", "-l", "subagent_use_separate" + "_provider",
                          "--", "vaf/"], cwd=str(root), capture_output=True, text=True)
    files = sorted(f for f in out.stdout.split() if f)
    assert files == ["vaf/core/config.py"], (
        f"the gate key is read outside the primitive again: {files}")


@pytest.mark.parametrize("path", CONSUMERS)
def test_every_former_hand_roller_now_asks_the_primitive(path):
    """The other half of the ratchet: the key is gone from these files AND the
    call is there. Without this, deleting the derivation entirely would pass."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / path).read_text(encoding="utf-8")
    assert "subagent_provider_override" in src, f"{path} no longer resolves the override"


def test_both_writing_lanes_go_through_the_setter():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for path in ("vaf/cli/cmd/settings.py", "vaf/cli/tui_app/screens.py"):
        src = (root / path).read_text(encoding="utf-8")
        assert "set_subagent_provider" in src, f"{path} writes the pair by hand"


# ── the save paths pair the gate with the name (the web select wrote the name only) ──

from vaf.core.config import pair_subagent_provider  # noqa: E402


def test_a_new_name_brings_its_gate():
    existing = {"subagent_provider": "inherit", "subagent_use_separate_provider": False}
    out = pair_subagent_provider(existing, {"subagent_provider": "openai", "provider": "veyllo"})
    assert out == {"subagent_provider": "openai", "subagent_use_separate_provider": True,
                   "provider": "veyllo"}
    out = pair_subagent_provider({"subagent_provider": "openai", "subagent_use_separate_provider": True},
                                 {"subagent_provider": " "})
    assert out["subagent_provider"] == "inherit" and out["subagent_use_separate_provider"] is False


def test_an_unchanged_inert_name_stays_inert():
    """Every web save sends the whole form. Pairing an unchanged value would switch on a
    name an older version stored without its gate, on the next unrelated save."""
    existing = {"subagent_provider": "openai", "subagent_use_separate_provider": False}
    incoming = {"subagent_provider": "openai", "subagent_use_separate_provider": False,
                "temperature": 0.3}
    assert pair_subagent_provider(existing, incoming) is incoming
    assert pair_subagent_provider(existing, {"temperature": 0.3}) == {"temperature": 0.3}


def test_a_changed_gate_alone_is_kept_but_never_switches_on_inherit():
    existing = {"subagent_provider": "openai", "subagent_use_separate_provider": True}
    assert pair_subagent_provider(existing, {"subagent_use_separate_provider": False}) == {
        "subagent_use_separate_provider": False}
    existing = {"subagent_provider": "inherit", "subagent_use_separate_provider": False}
    assert pair_subagent_provider(existing, {"subagent_use_separate_provider": "true"}) == {
        "subagent_use_separate_provider": False}


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    import json
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"provider": "veyllo", "subagent_provider": "inherit",
                                "subagent_use_separate_provider": False}), encoding="utf-8")
    monkeypatch.setattr(Config, "CONFIG_FILE", path)
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)
    return path


def _stored_override():
    # The autouse store above backs Config.get; the save paths write the file, so the
    # primitive reads the file's contents as a mapping.
    return subagent_provider_override(Config.load())


def test_the_rest_save_pairs_the_gate(config_file):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from vaf.api import config_routes

    app = FastAPI()

    @app.middleware("http")
    async def _as_admin(request, call_next):
        request.state.user = {"username": "alice", "role": "admin", "user_scope_id": "ab12cd34"}
        return await call_next(request)

    app.include_router(config_routes.router)
    client = TestClient(app)
    assert client.patch("/api/config", json={"subagent_provider": "openai"}).status_code == 200
    assert _stored_override() == "openai"
    client.patch("/api/config", json={"subagent_provider": "inherit"})
    assert _stored_override() is None
    # An inert pair stored by an older version, then an unrelated save of the whole form.
    import json
    config_file.write_text(json.dumps({"provider": "veyllo", "subagent_provider": "openai",
                                       "subagent_use_separate_provider": False}), encoding="utf-8")
    client.patch("/api/config", json={"subagent_provider": "openai", "temperature": 0.3})
    assert _stored_override() is None


def test_the_websocket_save_pairs_the_gate(config_file, monkeypatch):
    import json
    import threading

    import jwt
    from starlette.testclient import TestClient

    import vaf.auth.crypto as crypto
    from vaf.core.config import get_local_admin_scope_id
    from vaf.core.web_server import app

    monkeypatch.setattr(crypto, "get_jwt_secret", lambda: "s" * 32)
    token = jwt.encode({"sub": "1", "user_scope_id": str(get_local_admin_scope_id()),
                        "username": "admin", "role": "admin"}, "s" * 32, algorithm="HS256")

    def saved(ws, wait=20):
        box = {}

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
        reader.join(wait)
        assert "msg" in box, f"no config_saved within {wait}s ({box.get('error')!r})"
        return box["msg"]

    with TestClient(app, client=("127.0.0.1", 40000)).websocket_connect(f"/ws?token={token}") as ws:
        ws.send_text(json.dumps({"type": "save_config", "config": {"subagent_provider": "openai"}}))
        saved(ws)
        assert _stored_override() == "openai"
        ws.send_text(json.dumps({"type": "save_config", "config": {"subagent_provider": "inherit"}}))
        saved(ws)
        assert _stored_override() is None


def test_classic_menu_label_follows_the_gate(_config_in_memory):
    from vaf.cli.cmd.settings import _subagent_menu_label

    _config_in_memory.update(subagent_provider="openai", subagent_use_separate_provider=False)
    assert _subagent_menu_label("veyllo") == "🔧 Sub-Agent Provider: VEYLLO (inherited)"
    _config_in_memory["subagent_use_separate_provider"] = True
    assert _subagent_menu_label("veyllo") == "🔧 Sub-Agent Provider: OPENAI"
