# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The local model row: a live weight swap instead of an honest dead end.

The row was `("later", ...)` with a restart pointer, because a swap used to
need a full agent rebuild that wiped the history. The engine primitive
`reload_local_model` removed that reason, so the TUI half is thin on purpose:
the submenu lists `models/*.gguf` with the active file marked, and a pick
hands the name to the bridge, which writes the config and swaps on the LANE -
the new weights block while they load, and a running turn refuses the swap.

With a cloud provider only the config moves (the classic contract): the pick
names the file the local provider serves NEXT time.
"""
import threading
import time
from types import SimpleNamespace

import pytest

from vaf.cli.tui_app.agent_bridge import AgentBridge


def _wait(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


class _Events:
    def __init__(self, events):
        self._events = events

    def __getattr__(self, name):
        def _rec(*args):
            self._events.append((name, *args))
        return _rec


def _bridge(monkeypatch, *, provider="local", reload_result=True,
            models_dir="/nonexistent", config=None):
    import vaf.core.config as config_mod

    written = {}
    values = dict(config or {})
    monkeypatch.setattr(config_mod.Config, "set",
                        classmethod(lambda cls, k, v: written.__setitem__(k, v)))
    monkeypatch.setattr(config_mod.Config, "get",
                        classmethod(lambda cls, k, d=None: values.get(k, d)))

    reloads = []

    def _reload():
        reloads.append(threading.current_thread())
        if isinstance(reload_result, Exception):
            raise reload_result
        return reload_result

    events = []
    agent = SimpleNamespace(
        provider=provider,
        models_dir=models_dir,
        reload_local_model=_reload,
        get_token_usage=lambda: (1, 2),
        set_event_sink=lambda s: None,
        shutdown=lambda: None,
    )
    b = AgentBridge(agent, SimpleNamespace(id="green123456"), None,
                    _Events(events),
                    web_interface_getter=lambda: SimpleNamespace(
                        resolve_gate=lambda *a: True))
    return b, events, written, reloads


def test_the_swap_runs_on_the_lane_and_reports(monkeypatch):
    b, events, written, reloads = _bridge(monkeypatch)
    b.apply_local_model("qwen-test.gguf")
    assert _wait(lambda: any(e[0] == "system_note" and "switched to" in e[1]
                             for e in events)), events
    assert written.get("model") == "qwen-test.gguf"
    assert reloads and reloads[0] is not threading.main_thread(), (
        "the blocking weight load ran on the caller's thread")
    b.shutdown()


def test_a_running_turn_refuses_and_writes_nothing(monkeypatch):
    b, events, written, reloads = _bridge(monkeypatch)
    b._busy = True
    b.apply_local_model("qwen-test.gguf")
    assert any(e[0] == "event_note" and "not while a turn" in e[2]
               for e in events), events
    time.sleep(0.1)
    assert written == {} and reloads == []
    b._busy = False
    b.shutdown()


def test_a_cloud_provider_only_moves_the_config(monkeypatch):
    b, events, written, reloads = _bridge(monkeypatch, provider="veyllo")
    b.apply_local_model("qwen-test.gguf")
    assert _wait(lambda: any(e[0] == "system_note" and
                             "applies when the provider is local" in e[1]
                             for e in events)), events
    assert written.get("model") == "qwen-test.gguf"
    assert reloads == [], "a cloud agent has no weights to swap"
    b.shutdown()


def test_a_failed_swap_is_named_not_hidden(monkeypatch):
    b, events, written, reloads = _bridge(monkeypatch, reload_result=False)
    b.apply_local_model("qwen-test.gguf")
    assert _wait(lambda: any(e[0] == "event_note" and "could not switch" in e[2]
                             for e in events)), events
    b.shutdown()


def test_a_crashing_swap_does_not_kill_the_lane(monkeypatch):
    b, events, written, reloads = _bridge(
        monkeypatch, reload_result=RuntimeError("kaputt"))
    b.apply_local_model("qwen-test.gguf")
    assert _wait(lambda: any(e[0] == "event_note" and "switch failed" in e[2]
                             for e in events)), events
    done = []
    b._submit(lambda: done.append(True))
    assert _wait(lambda: bool(done)), "the lane died with the exception"
    b.shutdown()


@pytest.mark.parametrize("stored,expected", [
    ("qwen-test.gguf", "qwen-test.gguf"),
    ("Some/Repo/qwen-test.gguf", "qwen-test.gguf"),
    ("qwen-test", "qwen-test.gguf"),
])
def test_the_active_marker_normalizes_like_the_classic_menu(
        monkeypatch, tmp_path, stored, expected):
    (tmp_path / "qwen-test.gguf").write_bytes(b"x")
    (tmp_path / "other.gguf").write_bytes(b"x")
    b, events, written, reloads = _bridge(
        monkeypatch, models_dir=str(tmp_path), config={"model": stored})
    files, current = b.list_local_models()
    assert files == ["other.gguf", "qwen-test.gguf"]
    assert current == expected
    b.shutdown()


def test_a_missing_models_dir_is_an_empty_list(monkeypatch):
    b, events, written, reloads = _bridge(monkeypatch, models_dir="/nonexistent")
    files, current = b.list_local_models()
    assert files == []
    b.shutdown()


# ── the screen half ─────────────────────────────────────────────────────────────────

def _screen(bridge):
    from vaf.cli.tui_app.screens import SettingsScreen

    fake_app = SimpleNamespace(_bridge=bridge, notified=[], posted=[])
    fake_app.notify = lambda msg, **kw: fake_app.notified.append(msg)
    fake_app.post_message = lambda m: fake_app.posted.append(m)

    class _S(SettingsScreen):
        app = property(lambda s: fake_app)

    s = _S.__new__(_S)
    s._cfg = lambda key, default=None: default
    s._refresh_labels = lambda: None
    s._stack = ["main", "local_model"]
    return s, fake_app


def test_the_submenu_marks_the_active_file():
    bridge = SimpleNamespace(
        list_local_models=lambda: (["a.gguf", "b.gguf"], "b.gguf"))
    s, _ = _screen(bridge)
    rows = s._menu_rows("local_model")
    kinds = [(k, a) for k, a, _ in rows]
    assert ("local_model", "a.gguf") in kinds and ("local_model", "b.gguf") in kinds
    marked = [label for k, a, label in rows if a == "b.gguf"]
    unmarked = [label for k, a, label in rows if a == "a.gguf"]
    assert "▍" in marked[0] and "▍" not in unmarked[0]


def test_an_empty_models_dir_is_an_honest_note():
    bridge = SimpleNamespace(list_local_models=lambda: ([], ""))
    s, _ = _screen(bridge)
    rows = s._menu_rows("local_model")
    assert rows[0][0] == "note" and "no models" in rows[0][2]


def test_a_pick_hands_the_file_to_the_bridge():
    picked = []
    bridge = SimpleNamespace(
        list_local_models=lambda: (["a.gguf"], ""),
        apply_local_model=lambda f: picked.append(f))
    s, fake_app = _screen(bridge)
    s._rows = [("local_model", "a.gguf", "")]
    s._activate(0)
    assert picked == ["a.gguf"]
    assert fake_app.notified and "a.gguf" in fake_app.notified[0]
