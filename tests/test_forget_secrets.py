# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A credential the person hands over in the chat is stored, and then it is gone from the chat.

The person writes "FTP: Passwort hunter2-..." and the agent calls store_credential. The value
must end up in the person's credential store and NOWHERE the chat is kept: not in the history
the next request is built from, not in the saved chat (the runner writes the person's message
there when the turn ends), not in the stored intent, the channel store, the word corpus or the
terminal history, and never in a log line or the event stream. And the chat must stay valid:
nothing is deleted, so every tool call keeps its answer.

Driven through the REAL Agent.chat_step with only the model replaced.

MUTATION: drop the _forget_declared_secrets call after a tool and the history test goes red;
drop the registry read in SessionManager.save and the saved-chat test goes red; drop the
masking of the timeline or the tool_update emit and the log test goes red; drop the replay
cache from the turn-boundary scrub and the replay test goes red.
"""
import json

import pytest

import vaf.core.forget_secrets as fs
from vaf.core import user_secrets as us
from vaf.core.platform import Platform

SESSION = "green123456"
VALUE = "hunter2-geheim-2026"
REQUEST = f"Mein FTP-Passwort ist {VALUE}, speicher es bitte"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path / "vaf"))
    monkeypatch.setattr(us, "_stores", {})
    monkeypatch.setattr(fs, "_by_session", {})
    # The log scrub reads the day's logs: these tests' own, never the machine's.
    import vaf.core.log_helper as log_helper
    (tmp_path / "logs").mkdir()
    monkeypatch.setattr(log_helper, "get_app_log_dir", lambda: tmp_path / "logs")
    # Only the sinks a test adds itself: a lane registered in this process (the web server's
    # reload, the Telegram bridge) must not act on a test's chat.
    monkeypatch.setattr(fs, "_listeners", [])


@pytest.fixture
def agent():
    from vaf.core.agent import Agent
    a = Agent(register_signals=False, run_kind="chat",
              config_overrides={"provider": "openai", "api_key_openai": "sk-test"})
    a.current_session_id = SESSION
    a._current_chat_source = "web"
    a.init_chat()
    a._bind_session_persistence(SESSION)
    a._active_tools = None
    return a


class _Model:
    """Round 1 stores the value the person wrote; round 2 answers."""

    def __init__(self, name="FTP_PASS", secret=VALUE):
        self.args = {"name": name, "secret": secret}
        self.main, self.seen = 0, []

    def __call__(self, messages=None, **kw):
        if not kw.get("tools"):
            yield "ok"
            return
        self.main += 1
        self.seen.append(messages)
        if self.main > 1:
            yield "Gespeichert."
            return
        yield "Ich speichere es."
        call = {"index": 0, "id": "c1", "type": "function",
                "function": {"name": "store_credential", "arguments": json.dumps(self.args)}}
        yield json.dumps({"tool_calls": [call]})
        yield json.dumps({"finish_reason": "tool_calls"})


def _turn(agent, model, text=REQUEST):
    agent.api_backend.chat_completion = model
    agent.chat_step(user_input=text, stream_callback=lambda t: None)


def _blob(obj):
    return json.dumps(obj, ensure_ascii=False, default=str)


# ── the agent ───────────────────────────────────────────────────────────────────

def test_a_stored_credential_leaves_the_history_and_the_chat_stays_valid(agent):
    _turn(agent, _Model())
    assert us.env_for("$VAF_SECRET_FTP_PASS", username="admin") == {"VAF_SECRET_FTP_PASS": VALUE}, \
        "it is stored, for the person the chat belongs to"
    assert VALUE not in _blob(agent.history), "gone from what the next request is built from"
    user = next(m for m in agent.history if m.get("role") == "user")
    assert user["content"].endswith("[VAF_SECRET_FTP_PASS], speicher es bitte"), user["content"]
    roles = [m["role"] for m in agent.history if m["role"] != "system"]
    assert roles == ["user", "assistant", "tool", "assistant"], "nothing was removed"
    sent = agent._prepare_messages([dict(m) for m in agent.history])
    call = next(m for m in sent if m.get("tool_calls"))
    assert call["tool_calls"][0]["id"] == "c1" and any(m.get("tool_call_id") == "c1" for m in sent), \
        "the call keeps its answer"


def test_a_failed_store_forgets_nothing(agent):
    _turn(agent, _Model(name="123"))
    assert VALUE in _blob(agent.history), "the value must stay until it is stored somewhere"
    assert fs.session_env(SESSION) == {}


def test_a_store_that_cannot_open_says_what_not_to_do(agent, monkeypatch):
    """Measured live: the store was unreadable and the model wrote the password into a plaintext
    file in the workspace as its own fallback."""
    def broken(*a, **k):
        raise RuntimeError("user_secrets: key material is unavailable")
    monkeypatch.setattr(us, "set_secret", broken)
    result = agent.tools["store_credential"].run(name="FTP_PASS", secret=VALUE)
    assert result.startswith("Error:") and "NOT stored" in result and "no file" in result
    assert VALUE not in result


def test_the_stored_intent_forgets_it(agent):
    _turn(agent, _Model())
    assert VALUE not in (agent.main_persistence.get_user_intent() or "")
    assert "[VAF_SECRET_FTP_PASS]" in agent.main_persistence.get_user_intent()


def test_the_argument_never_reaches_a_log_or_the_event_stream(agent, monkeypatch):
    seen = []
    import vaf.core.agent as agent_mod
    import vaf.core.log_helper as log_helper
    monkeypatch.setattr(agent_mod, "log_timeline_event", lambda *a, **k: seen.append(_blob(k)))
    monkeypatch.setattr(log_helper, "log_tool_use", lambda *a, **k: seen.append(_blob(k)))
    from vaf.core.web_interface import get_web_interface
    wi = get_web_interface()
    monkeypatch.setattr(wi, "emit_tool_update", lambda *a, **k: seen.append(_blob([a, k])))
    _turn(agent, _Model())
    assert seen, "the setup is real: the call was recorded"
    assert not [s for s in seen if VALUE in s], [s for s in seen if VALUE in s]


# ── the saved chat ──────────────────────────────────────────────────────────────

def test_the_saved_chat_never_carries_it(agent, tmp_path):
    """The runner writes the person's message when the turn ends - after the tool ran."""
    from vaf.core import data_files
    from vaf.core.session import Session, SessionManager
    _turn(agent, _Model())
    mgr = SessionManager(storage_dir=str(tmp_path / "sessions"))
    session = Session(id=SESSION, name="FTP")
    session.add_message("user", REQUEST)
    session.add_message("assistant", "Gespeichert.")
    path = mgr.save(session)
    text = data_files.read_bytes(path).decode("utf-8")
    assert VALUE not in text and "[VAF_SECRET_FTP_PASS]" in text
    other = Session(id="red654321", name="other")
    other.add_message("user", f"unrelated {VALUE}")
    assert VALUE in data_files.read_bytes(mgr.save(other)).decode("utf-8"), \
        "only the chat it was handed over in is rewritten"


# ── Anthropic's signed blocks ───────────────────────────────────────────────────

def test_the_replay_blocks_stay_during_the_turn_and_go_after(agent):
    history = [{"role": "user", "content": REQUEST},
               {"role": "assistant", "content": "", "tool_calls": [
                   {"id": "c1", "function": {"name": "store_credential",
                                             "arguments": json.dumps({"secret": VALUE})}}],
                "_anthropic_blocks": [{"type": "thinking", "thinking": VALUE, "signature": "s"}]}]
    env = {"VAF_SECRET_FTP_PASS": VALUE}
    fs.scrub_messages(history, env, drop_replay_cache=False)
    assert VALUE not in history[0]["content"] and VALUE not in _blob(history[1]["tool_calls"])
    assert history[1]["_anthropic_blocks"][0]["thinking"] == VALUE, \
        "a signed block is never edited while its turn runs"
    fs.remember(SESSION, env)
    fs.forget_in_history(history, SESSION)
    assert "_anthropic_blocks" not in history[1], "gone once the turn is over"
    assert VALUE not in _blob(history)


def test_signed_replay_blocks_stay_for_their_turn_and_go_with_the_next(agent, monkeypatch):
    """Anthropic with thinking: the round after the tool must still replay the signed blocks of
    the call (a missing or edited one is refused); the next turn starts without them."""
    tool = agent.tools["store_credential"]
    real_run = tool.run

    def run_with_blocks(**kwargs):
        call = next(m for m in reversed(agent.history) if m.get("tool_calls"))
        call["_anthropic_blocks"] = [{"type": "thinking", "thinking": f"pw {VALUE}", "signature": "s"}]
        return real_run(**kwargs)

    monkeypatch.setattr(tool, "run", run_with_blocks)
    model = _Model()
    _turn(agent, model)
    replayed = [m for m in model.seen[1] if m.get("_anthropic_blocks")]
    assert replayed and VALUE in _blob(replayed), "kept verbatim for the round after the tool"
    monkeypatch.setattr(tool, "run", real_run)
    nxt = _Model()
    _turn(agent, nxt, text="Danke")
    assert VALUE not in _blob(nxt.seen[0]), "the next turn's first request is clean"
    assert VALUE not in _blob(agent.history)


def test_the_memory_compaction_never_learns_it(agent):
    from vaf.memory.rag import _build_compaction_conversation_excerpt
    _turn(agent, _Model())
    agent.history.append({"role": "assistant", "content": f"Dein Passwort {VALUE} ist gespeichert."})
    assert VALUE not in _build_compaction_conversation_excerpt(agent)


# ── the other sinks ─────────────────────────────────────────────────────────────

def test_the_last_interaction_preview_forgets_it():
    """The runner records the turn's preview AFTER the turn - after the store call."""
    from vaf.core.last_interaction import get_last_interaction, update_last_interaction
    fs.remember(SESSION, {"VAF_SECRET_FTP_PASS": VALUE})
    update_last_interaction(user_scope_id=None, source="web", preview=REQUEST[:80], session_id=SESSION)
    assert VALUE not in json.dumps(get_last_interaction(None), default=str)


def test_the_debug_logs_of_the_day_forget_it(monkeypatch, tmp_path):
    """Measured live: the memory search logged the person's message, value included."""
    import vaf.core.log_helper as log_helper
    from datetime import date
    logs = log_helper.get_app_log_dir()
    today = date.today().isoformat()
    (logs / f"rag_{today}.log").write_text(f"SEARCH query='Passwort {VALUE}'\n", encoding="utf-8")
    (logs / f"tray_debug_{today}.log").write_text(f"voice {VALUE}\n", encoding="utf-8")
    fs.forget({"VAF_SECRET_FTP_PASS": VALUE}, session_id=SESSION)
    assert VALUE not in (logs / f"rag_{today}.log").read_text(encoding="utf-8")
    assert VALUE in (logs / f"tray_debug_{today}.log").read_text(encoding="utf-8"), \
        "a log a handler holds open is left alone"


def test_the_channel_store_forgets_it(tmp_path):
    from vaf.core import channel_message_store as store
    store.append_message(username="admin", chat_id="chat1", message_id="m1", direction="in",
                         body=f"hier: {VALUE}", ts=1.0, channel="telegram")
    fs.forget({"VAF_SECRET_FTP_PASS": VALUE}, session_id=SESSION, username="admin")
    bodies = [r["body"] for r in store.get_chat_messages("admin", "chat1", channel="telegram")]
    assert bodies == ["hier: [VAF_SECRET_FTP_PASS]"], bodies


def test_the_word_corpus_and_the_terminal_history_forget_it(monkeypatch, tmp_path):
    import vaf.cli.autosuggest as auto_mod
    import vaf.cli.history as hist
    monkeypatch.setattr(auto_mod, "_per_account", {})
    monkeypatch.setattr(hist, "history_file", lambda: tmp_path / "history")
    fs.add_listener(auto_mod._forget_listener)
    fs.add_listener(hist._forget_listener)
    from vaf.core.config import get_local_admin_scope_id
    owner = get_local_admin_scope_id()
    corpus = auto_mod.autosuggest_for(owner)
    corpus.learn(f"mein passwort ist {VALUE} danke")
    hist.append_history(REQUEST)
    fs.forget({"VAF_SECRET_FTP_PASS": VALUE}, session_id=SESSION, user_scope_id=owner)
    assert VALUE.lower() not in _blob({k: dict(v) for k, v in corpus.learned_phrases.items()})
    assert VALUE not in (tmp_path / "history").read_text(encoding="utf-8")


def test_telegram_deletes_the_message_that_carried_it(monkeypatch):
    import vaf.api.telegram_bridge as tg
    deleted = []
    monkeypatch.setattr(tg, "_recent_inbound", {})
    monkeypatch.setattr(tg, "_delete_telegram_message", lambda chat, mid: deleted.append((chat, mid)) or True)
    fs.add_listener(tg._forget_listener)
    tg._remember_inbound("telegram_42", "42", 7, "Server ist ftp.example.org")
    tg._remember_inbound("telegram_42", "42", 8, f"Passwort: {VALUE}")
    fs.forget({"VAF_SECRET_FTP_PASS": VALUE}, session_id="telegram_42")
    assert deleted == [("42", "8")], "only the message that carried it"


def test_the_web_chat_reloads_after_the_scrubbed_save():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    assert "_add_forget_listener(_refresh_after_forget)" in src
    assert '"type": "context_checkpoint"' in src.split("def _refresh_after_forget", 1)[1][:1500]


def test_the_model_learns_how_to_store_one():
    note = us.prompt_note(can_store=True)
    assert "store_credential" in note
    without = us.prompt_note()
    assert "store_credential" not in without and "never ask for it in the chat" in without \
        and "Settings" in without, "without the tool the chat is still not the way"


def test_a_value_too_short_to_remove_is_not_stored(agent):
    result = agent.tools["store_credential"].run(name="PIN", secret="123", username="admin")
    assert result.startswith("Error:") and "NOT stored" in result and "Settings" in result
    assert us.names(username="admin") == [], "refused before anything was stored"
