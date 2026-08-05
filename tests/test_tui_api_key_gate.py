# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Switching to a provider you have no key for, and the key field that fixes it.

THE DEFECT. `apply_provider_change` wrote the provider into the config without
asking whether a key existed. The backend then failed to build, `changed` came
back zero, and the only thing the user was told was "stored; the running agent
kept its backend (restart VAF to apply)" - which is false in the one direction
that matters: the restart does not apply it, it boots straight into a provider
that cannot work. The inquirer menu has always refused instead
(`cli/cmd/settings.py`, the "No API key configured. Cannot switch" branch).

WHY THE GATE SITS ON THE BRIDGE. Three routes reach the same method - the model
overlay, the settings row that dismisses into it, and the `/model` command. A
check in the overlay would leave the other two open.

WHAT THE CLASSIC LANE'S CONTRACT ACTUALLY IS, pinned below because three of its
four clauses are easy to get backwards:
  - empty input means KEEP the stored key, not clear it;
  - a typed key is verified with a real request before the provider moves;
  - a key that fails to verify is still STORED (the request can fail on the
    network as easily as on the key) but the provider stays put;
  - an existing key switches WITHOUT a verification request - re-testing a
    working key would spend one on every switch.
"""
import threading
from types import SimpleNamespace

import pytest


def _drain(bridge, timeout=5.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not bridge._busy and bridge._queue.empty():
            time.sleep(0.05)
            if not bridge._busy and bridge._queue.empty():
                return
        time.sleep(0.02)


class _Rig:
    """A bridge with the config and the network replaced by records."""

    def __init__(self, monkeypatch, *, stored_key="", verifies=True):
        import vaf.core.agent as agent_mod
        import vaf.core.config as config_mod
        from vaf.cli.tui_app.agent_bridge import AgentBridge

        self.written = {}
        self.keys_set = []
        self.verified = []
        self.notes = []
        self.verify_thread = []

        monkeypatch.setattr(agent_mod, "reload_all_api_backends", lambda **kw: 1)
        monkeypatch.setattr(config_mod.Config, "set",
                            classmethod(lambda cls, k, v: self.written.__setitem__(k, v)))
        monkeypatch.setattr(config_mod.Config, "get_api_key",
                            classmethod(lambda cls, p: self.keys_set[-1][1]
                                        if self.keys_set else stored_key))
        monkeypatch.setattr(config_mod.Config, "set_api_key",
                            classmethod(lambda cls, p, k: self.keys_set.append((p, k))))

        def _verify(provider):
            self.verified.append(provider)
            self.verify_thread.append(threading.current_thread())
            return verifies

        monkeypatch.setattr(AgentBridge, "_api_key_verifies", staticmethod(_verify))

        events = SimpleNamespace(
            system_note=lambda t: self.notes.append(("note", t)),
            event_note=lambda t, m, s: self.notes.append((t, m)),
            presence=lambda *a, **k: None,
            context=lambda *a: None)
        self.bridge = AgentBridge(
            SimpleNamespace(get_token_usage=lambda: (1, 2),
                            set_event_sink=lambda s: None, shutdown=lambda: None),
            SimpleNamespace(id="s"), None, events,
            web_interface_getter=lambda: SimpleNamespace(resolve_gate=lambda *a: True))

    def apply(self, *args, **kwargs):
        self.bridge.apply_provider_change(*args, **kwargs)
        _drain(self.bridge)

    def close(self):
        self.bridge.shutdown()


# ── the refusal ─────────────────────────────────────────────────────────────────────

def test_a_provider_without_a_key_is_refused_and_nothing_is_written(monkeypatch):
    """The headline. Storing it poisons the NEXT start, which is the part the
    old warning got exactly backwards."""
    rig = _Rig(monkeypatch, stored_key="")
    rig.apply("anthropic")
    assert rig.written == {}, "a keyless provider reached the config"
    assert any("no API key" in m for _, m in rig.notes), rig.notes
    rig.close()


def test_local_is_never_gated(monkeypatch):
    """It has nothing to key, and a gate here would lock out the offline lane."""
    rig = _Rig(monkeypatch, stored_key="")
    rig.apply("local")
    assert rig.written.get("provider") == "local"
    assert rig.verified == []
    rig.close()


def test_a_model_change_within_a_keyed_provider_still_works(monkeypatch):
    """`apply_provider_change("", model)` carries no provider name - the gate has
    to resolve the CURRENT one instead of waving it through."""
    import vaf.core.config as config_mod

    rig = _Rig(monkeypatch, stored_key="sk-stored-key-value")
    monkeypatch.setattr(config_mod.Config, "get",
                        classmethod(lambda cls, k, d=None: "anthropic"
                                    if k == "provider" else d))
    rig.apply("anthropic", "claude-sonnet-4-6")
    assert rig.written.get("api_model_anthropic") == "claude-sonnet-4-6"
    rig.close()


# ── the verification contract ───────────────────────────────────────────────────────

def test_an_existing_key_switches_without_spending_a_request(monkeypatch):
    rig = _Rig(monkeypatch, stored_key="sk-stored-key-value")
    rig.apply("anthropic")
    assert rig.written.get("provider") == "anthropic"
    assert rig.verified == [], "a working key was re-tested; that costs a request"
    rig.close()


def test_a_typed_key_is_stored_and_verified_before_the_switch(monkeypatch):
    rig = _Rig(monkeypatch, stored_key="", verifies=True)
    rig.apply("anthropic", new_key="sk-typed-key-value")
    assert rig.keys_set == [("anthropic", "sk-typed-key-value")]
    assert rig.verified == ["anthropic"]
    assert rig.written.get("provider") == "anthropic"
    rig.close()


def test_a_key_that_fails_to_verify_is_kept_but_the_provider_stays(monkeypatch):
    """Parity with `vaf settings`, and the reason is not politeness: the request
    can fail on the network as easily as on the key, and re-typing a correct key
    because a wifi drop discarded it is worse than keeping it."""
    rig = _Rig(monkeypatch, stored_key="", verifies=False)
    rig.apply("anthropic", new_key="sk-typed-key-value")
    assert rig.keys_set == [("anthropic", "sk-typed-key-value")], "the key was discarded"
    assert "provider" not in rig.written, "an unverified provider was switched to"
    assert any("did not verify" in m for _, m in rig.notes), rig.notes
    rig.close()


def test_the_verification_runs_off_the_ui_thread(monkeypatch):
    """`test_connection` performs a real chat completion. On the UI thread the
    whole app freezes for as long as the provider takes to answer."""
    rig = _Rig(monkeypatch, stored_key="")
    rig.apply("anthropic", new_key="sk-typed-key-value")
    assert rig.verify_thread, "the verification never ran"
    assert rig.verify_thread[0] is not threading.main_thread()
    rig.close()


def test_the_key_never_reaches_a_note(monkeypatch):
    """Notes become transcript lines, and the transcript is written to the
    session file. The classic lane reported only a character count."""
    secret = "sk-typed-key-value"
    rig = _Rig(monkeypatch, stored_key="", verifies=False)
    rig.apply("anthropic", new_key=secret)
    joined = " ".join(f"{t} {m}" for t, m in rig.notes)
    assert secret not in joined, joined
    rig.close()


# ── the overlay ─────────────────────────────────────────────────────────────────────

def _ask(current: str, keys: list, *, typed: str = None):
    """Push the field, answer it, and return what it dismissed with.

    `asyncio.run` around an inner coroutine, the way the app smoke test drives
    the pilot - this repo has no pytest-asyncio and adding one for two tests
    would be a dependency for a shape that already works.
    """
    import asyncio

    from textual.app import App, ComposeResult
    from textual.widgets import Input, Static

    from vaf.cli.tui_app.screens import ApiKeyScreen

    box = {"result": "unset", "masked": None, "hint": ""}

    class _Host(App):
        def compose(self) -> ComposeResult:
            yield Static("host")

    async def _drive():
        app = _Host()
        async with app.run_test(size=(90, 24)) as pilot:
            app.push_screen(ApiKeyScreen("anthropic", current),
                            lambda v: box.__setitem__("result", v))
            await pilot.pause()
            # `app.query_one` searches the DEFAULT screen; the field lives on
            # the pushed modal.
            field = app.screen.query_one("#apikey-input", Input)
            box["masked"] = field.password
            if typed is not None:
                field.value = typed
            for key in keys:
                await pilot.press(key)
            await pilot.pause()
            # Only while the field is still up. Guarding on the screen TYPE
            # rather than catching: a swallowed AttributeError here would read
            # as "the hint never changed", the exact opposite of the question.
            # (`.content`, not `.renderable` - Static dropped the latter.)
            if isinstance(app.screen, ApiKeyScreen):
                box["hint"] = str(app.screen.query_one("#apikey-hint").content)

    asyncio.run(_drive())
    return box


def test_the_key_field_is_masked():
    assert _ask("", [])["masked"] is True, "the key was echoed to the screen"


def test_a_typed_key_comes_back_as_itself():
    assert _ask("", ["enter"], typed="sk-typed-key-value")["result"] == \
        "sk-typed-key-value"


def test_empty_means_keep_and_escape_means_cancel():
    """Two different answers, and one of them must not switch the provider.
    Collapsing them into a single falsy value is the mistake this pins."""
    assert _ask("sk-stored-key-value", ["enter"])["result"] == ""
    assert _ask("sk-stored-key-value", ["escape"])["result"] is None


def test_a_short_paste_is_refused_without_closing_the_field():
    """A truncated paste is the common accident. Dismissing on it would store
    the fragment and then fail verification for the wrong reason."""
    box = _ask("", ["enter"], typed="sk-short")
    assert box["result"] == "unset", "a short key was accepted"
    assert "8 characters" in box["hint"], box["hint"]


# ── the route to the field ──────────────────────────────────────────────────────────

def test_the_model_overlay_offers_a_way_to_replace_a_stored_key():
    """Without it, the only route to the field is picking a provider that has
    NO key - which a user with a wrong or expired one can never reach."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "vaf" / "cli" / "tui_app"
           / "screens.py").read_text(encoding="utf-8")
    assert 'Binding("k", "set_key"' in src
    assert "def action_set_key" in src


def test_the_app_asks_for_a_key_instead_of_only_refusing():
    """Wiring: the bridge refuses either way, so this is the difference between
    being refused and being helped."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "vaf" / "cli" / "tui_app"
           / "app.py").read_text(encoding="utf-8")
    tail = src.split("def action_model", 1)[1][:900]
    assert "ApiKeyScreen(provider, stored)" in tail
    assert "new_key=key" in tail
    assert "if key is None" in tail, "a cancelled field must change nothing"
