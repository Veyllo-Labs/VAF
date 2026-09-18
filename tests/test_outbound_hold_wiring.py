# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The hold is wired into the lanes that must know about it, and into no others.

The decision lives in one pure function (tests/test_outbound_hold.py pins it). What this file
pins is the wiring, because every one of these is a place where a hold that exists on paper
would do nothing, or would fire where nobody can answer it:

- the chat seam, which is the only dispatcher stage that sees the final recipient argument,
  the assigned identity AND the chat source;
- the three mail tools, which must pass the flag into the one send funnel;
- the card, which must appear from a signal rather than a timer;
- the CLI, because a headless install has the same two questions and no card.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT = (ROOT / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")
PAGE = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")


def _region(source: str, start: str, end: str) -> str:
    assert start in source and end in source, (start, end)
    return source.split(start, 1)[1].split(end, 1)[0]


# ---- the framework seam -------------------------------------------------------

def test_the_seam_is_the_chat_dispatcher_stage():
    """MUTATION: move the hold into the tools, or into execute_tool before the source is known.

    `_chat_session_plumbing` is the before_dispatch hook: it runs after the arguments are
    repaired and the identity assigned, it is wired only for the chat lane, and a string it
    returns becomes the tool result without dispatching - which is exactly "this is a draft
    now". The workflow engine and the automation lane use the same funnel with no chat source,
    so a hold anywhere lower would park what nobody can approve.
    """
    region = _region(AGENT, "def _chat_session_plumbing", "def _announce_held_send")
    assert "outbound_hold.holds_outward_send(" in region
    assert 'source=str(getattr(self, "_current_chat_source"' in region
    assert "outbound_hold.MAIL_HOLD_TOOLS" in region
    # The scheduled-turn exception has to REACH the decision. Both ends were pinned (the
    # runner sets the flag, the pure function honours it) and the wire between them was not,
    # so deleting this one kwarg parked every reminder the person scheduled and left the
    # suite green.
    assert 'unattended=bool(getattr(self, "_unattended_turn", False))' in region
    assert 'tool_args["hold"] = True' in region
    assert "outbound_hold.park_messenger_call(" in region
    assert "outbound_hold.held_result(" in region


def test_the_hold_can_never_lose_a_send_to_its_own_failure():
    """A guard that parks messages must fail OPEN: if anything in it raises, the send goes as
    it did before, and the reason is in the backend log rather than in a lost message."""
    region = _region(AGENT, "def _chat_session_plumbing", "def _announce_held_send")
    assert "except Exception as _hold_exc:" in region
    assert "[OUTBOUND_HOLD]" in region


def test_the_card_is_told_by_the_result_marker():
    """MUTATION: announce from the tool name instead of the marker.

    The two lanes park in different stores and neither id belongs in a chat event, so the
    signal is "something is waiting" and the card fetches it. Keyed on the marker, one line
    covers mail and messenger alike.
    """
    assert "self._announce_held_send(result)" in AGENT
    region = _region(AGENT, "def _announce_held_send", "def _clear_last_assistant_ui")
    assert "HELD_PREFIX" in region and '"outbound_held"' in region


def test_the_signal_fires_for_a_held_result_and_for_nothing_else(monkeypatch):
    """The behaviour behind the guard above, driven rather than read.

    MUTATION: invert the marker test in `_announce_held_send` (`if str(result).startswith(...)`
    -> `if not ...`) and every literal the source guard greps stays in place while the card
    never appears. The method needs nothing of an Agent but the session id, so a stand-in
    object is the whole harness.
    """
    from types import SimpleNamespace

    import vaf.core.web_interface as wi
    from vaf.core.agent import Agent
    from vaf.core.outbound_hold import HELD_PREFIX

    pushes, unread = [], []
    fake = SimpleNamespace(
        _push_session_update=lambda sid, payload: pushes.append((sid, payload)),
        emit_session_unread=lambda sid: unread.append(sid),
    )
    monkeypatch.setattr(wi, "get_web_interface", lambda: fake)
    agent = SimpleNamespace(current_session_id="green123456")

    Agent._announce_held_send(agent, f"{HELD_PREFIX} A WhatsApp message to +49...")
    assert pushes == [("green123456", {"type": "outbound_held"})]
    assert unread == ["green123456"], "the chat list's red dot for a person who moved on"

    pushes.clear(), unread.clear()
    Agent._announce_held_send(agent, "Message sent via WhatsApp.")
    Agent._announce_held_send(agent, "")
    assert pushes == [] and unread == [], "an ordinary result announces nothing"


# ---- the mail tools -----------------------------------------------------------

def test_every_mail_send_tool_passes_the_flag_into_the_one_funnel():
    """MUTATION: leave one of the three out.

    send_mail, reply_mail and forward_mail are three separate immediate sends on the same
    funnel. A round that parks two of them leaves the third door open, which is the shape
    every registry incident in this repo has.
    """
    from vaf.core.outbound_hold import MAIL_HOLD_TOOLS

    files = {"send_mail": "vaf/tools/send_mail.py", "reply_mail": "vaf/tools/reply_mail.py",
             "forward_mail": "vaf/tools/manage_mail.py"}
    assert set(MAIL_HOLD_TOOLS) == set(files), MAIL_HOLD_TOOLS
    for name, rel in files.items():
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert 'hold = bool(kwargs.get("hold", False))' in src, name
        assert "hold=hold" in src, name
        assert "from vaf.core.outbound_hold import held_result" in src, name


# ---- the harness --------------------------------------------------------------

def test_the_card_is_mounted_and_fed_by_signals_not_by_a_timer():
    """MUTATION: poll the outbox on an interval.

    The store announces a parked draft exactly as it announces a message, so the card rides
    `outbound_held` and `inbox_changed`. A timer would be a third refresh policy in a file
    whose interval count is itself guarded.
    """
    assert "import HeldSendCard from '@/components/outbox/HeldSendCard';" in PAGE
    assert "<HeldSendCard apiBase={getApiBase()} version={heldVersion}" in PAGE
    assert "data.type === 'outbound_held'" in PAGE
    # IN the conversation, not over the header: the card is the agent's own output waiting for
    # a word: it belongs to that one answer in that one chat, while a banner over the header
    # would read as a system alert about the whole app. It is the last row of the chat's own
    # list, in a bot row wrapper, so it lines up under the answer that produced it.
    row = PAGE.split("<HeldSendCard", 1)[0][-1200:]
    # The bot row's geometry, all three parts: the row, the 85 percent block, and the avatar
    # gutter as a spacer. The row centers its child, so a card without the block starts left of
    # the whole column, and one without the spacer starts under the avatar instead of under the
    # text (both measured live, both looked wrong in exactly that way).
    assert "flex gap-4 pt-4 vaf-msg-row" in row
    assert 'w-full max-w-[85%] max-md:max-w-full flex gap-4' in row
    assert '<div className="w-9 shrink-0" aria-hidden="true" />' in row
    assert PAGE.index("<HeldSendCard") < PAGE.index("<div ref={scrollRef} />")
    card = (ROOT / "web" / "components" / "outbox" / "HeldSendCard.tsx").read_text(encoding="utf-8")
    # No POLLING. The one interval in the file drives the reading pause's own countdown, and it
    # must not be a refresh in disguise: the listing is fetched on the signal and on a chat
    # change, never on a clock.
    assert card.count("setInterval(") == 1
    tick = card.split("setInterval(", 1)[1][:260]
    assert "setNow(t)" in tick and "load()" not in tick
    # And it stops itself twice over: no card, no timer, and once the last reading pause has
    # run out the tick clears itself. Without the second stop a card sitting on screen
    # re-rendered the whole chat four times a second for as long as it was there.
    assert "if (!rows.length) return;" in card
    assert "if (stopAt <= stamp) return;" in card
    assert "if (t >= stopAt && tickRef.current) { clearInterval(tickRef.current); tickRef.current = null; }" in tick
    assert "useTranslations('outbox')" in card


def test_the_card_has_no_hardcoded_copy():
    """Every string the person reads comes from the catalogues, in all seven languages."""
    card = (ROOT / "web" / "components" / "outbox" / "HeldSendCard.tsx").read_text(encoding="utf-8")
    keys = {"title", "to", "noRecipient", "send", "discard", "more", "failed", "countdown"}
    for key in keys:
        assert f"t('{key}'" in card, key
    for lang in ("de", "en", "tr", "zh", "ja", "ko", "th"):
        block = json.loads((ROOT / "web" / "messages" / f"{lang}.json").read_text(encoding="utf-8"))["outbox"]
        assert set(block) == keys, (lang, set(block) ^ keys)


# ---- the CLI ------------------------------------------------------------------

def test_the_cli_prints_and_decides(monkeypatch, tmp_path):
    """A headless install has the same two questions and no card. Unlike `vaf inbox`, this
    group writes: a draft nobody can reach is a message lost to the terminal."""
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from vaf.core import channel_message_store as store
    from vaf.core.platform import Platform

    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    import vaf.core.config as cfg_mod
    monkeypatch.setattr(cfg_mod.Config, "get", classmethod(lambda cls, key, default=None: default))
    monkeypatch.setattr(store, "_local_admin", lambda: "admin")
    monkeypatch.setattr(store, "_local_admin_scope_id", lambda: "admin-scope")
    import vaf.core.web_interface as wi
    monkeypatch.setattr(wi, "notify_inbox_changed", lambda scope: None)
    store._reset_announce_state()

    import vaf.cli.cmd.outbox as cmd
    monkeypatch.setattr(cmd, "_identity", lambda: ("alice", "scope-1"))
    from vaf.core.outbound_hold import park_messenger_call
    entry_id = park_messenger_call("send_whatsapp", {"to_phone": "+49170", "message": "Hallo"},
                                   username="alice", user_scope_id="scope-1")

    runner = CliRunner()
    out = runner.invoke(cmd.app, ["list", "--json"])
    assert out.exit_code == 0
    rows = [json.loads(line) for line in out.output.splitlines() if line.strip().startswith("{")]
    assert len(rows) == 1 and rows[0]["id"] == entry_id and rows[0]["kind"] == "call"

    dropped = runner.invoke(cmd.app, ["discard", "call", str(entry_id)])
    assert dropped.exit_code == 0
    again = runner.invoke(cmd.app, ["discard", "call", str(entry_id)])
    assert again.exit_code == 1, "a draft can only be dropped once"

    unknown = runner.invoke(cmd.app, ["send", "pigeon", "1"])
    assert unknown.exit_code == 1

    # And the verb that WRITES: a second draft is sent through the same tool the card uses,
    # the row settles, and the list is empty afterwards. Without this the group's whole point
    # (a terminal can decide, not only look) rested on its failure branch.
    second = park_messenger_call("send_whatsapp", {"to_phone": "+49170", "message": "Zweite"},
                                 username="alice", user_scope_id="scope-1")
    sent = []
    import vaf.core.outbound_hold as oh
    monkeypatch.setattr(oh, "resolve_tool", lambda name: SimpleNamespace(
        run=lambda **kw: sent.append(kw) or "Message sent via WhatsApp."))
    ok = runner.invoke(cmd.app, ["send", "call", str(second)])
    assert ok.exit_code == 0, ok.output
    assert len(sent) == 1 and sent[0]["to_phone"] == "+49170"
    assert store.held_send(second, "alice", "scope-1")["state"] == "sent"
    empty = runner.invoke(cmd.app, ["list", "--json"])
    assert [line for line in empty.output.splitlines() if line.strip().startswith("{")] == []


def test_the_outbox_group_sits_behind_the_terminal_door():
    """It prints messages, so it follows `vaf inbox` and `vaf session` through the same door."""
    main = (ROOT / "vaf" / "main.py").read_text(encoding="utf-8")
    region = _region(main, 'app.add_typer(outbox.app, name="outbox"', ")")
    assert "callback=_terminal_door" in region


# ---- the seam, driven rather than read ----------------------------------------

def _bare_agent(source="web", tmp=None, monkeypatch=None):
    """The chat seam with only the attributes it touches: no model, no tools, no session."""
    from vaf.core.agent import Agent
    from vaf.core.platform import Platform
    if tmp is not None and monkeypatch is not None:
        monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp / "data"))
    a = Agent.__new__(Agent)
    a._current_chat_source = source
    a._current_username = "alice"
    a._current_user_scope_id = "scope-1"
    a.current_session_id = "s1"
    a._front_office_mode = False
    a.main_persistence = None
    a.tools = {}
    a._active_tools = []
    a.history = []
    return a


def test_the_seam_holds_what_it_should_and_nothing_else(tmp_path, monkeypatch):
    """Driven end to end through the real hook, because a source check cannot see whether the
    decision actually fires. The mail branch sets the flag and lets the tool run; the WhatsApp
    branch answers with the held marker and never dispatches; Telegram and a background source
    pass straight through."""
    import vaf.core.web_interface as wi
    monkeypatch.setattr(wi, "notify_inbox_changed", lambda scope: None)
    from vaf.core import channel_message_store as store
    store._reset_announce_state()

    agent = _bare_agent(tmp=tmp_path, monkeypatch=monkeypatch)
    mail_args = {"to": "uwe@example.com", "subject": "Angebot", "body": "Text"}
    assert agent._chat_session_plumbing("send_mail", mail_args) is None
    assert mail_args["hold"] is True

    from vaf.core.outbound_hold import HELD_PREFIX, pending
    wa_args = {"to_phone": "+491700000000", "message": "Hallo", "_agent": agent}
    held = agent._chat_session_plumbing("send_whatsapp", wa_args)
    assert held and held.startswith(HELD_PREFIX)
    rows = pending("alice", "scope-1")
    assert len(rows) == 1 and rows[0]["recipient"] == "+491700000000"

    tg_args = {"message": "Hallo"}
    assert agent._chat_session_plumbing("send_telegram", tg_args) is None
    assert "hold" not in tg_args

    background = _bare_agent(source="automation", tmp=tmp_path, monkeypatch=monkeypatch)
    bg_args = {"to": "uwe@example.com", "subject": "s", "body": "b"}
    assert background._chat_session_plumbing("send_mail", bg_args) is None
    assert "hold" not in bg_args

    # A turn the PERSON scheduled fires later under the source of the chat it belongs to, so
    # the source alone would park a reminder nobody is sitting in front of. The runner marks
    # such a turn and the seam has to pass that mark on.
    # MUTATION: drop the `unattended=` kwarg from the seam and both assertions go red.
    timer = _bare_agent(tmp=tmp_path, monkeypatch=monkeypatch)
    timer._unattended_turn = True
    timer_args = {"to": "uwe@example.com", "subject": "s", "body": "b"}
    assert timer._chat_session_plumbing("send_mail", timer_args) is None
    assert "hold" not in timer_args, "a scheduled turn sends, it does not wait for a click"
    timer_wa = {"to_phone": "+491700000000", "message": "Reminder"}
    assert timer._chat_session_plumbing("send_whatsapp", timer_wa) is None


def test_a_live_object_in_the_arguments_cannot_defeat_the_hold(tmp_path, monkeypatch):
    """MUTATION: park `args` as they arrive.

    The dispatcher injects `_agent` (the Agent itself) into every send call so send_whatsapp
    can see a Front Office turn. Measured: json.dumps then raises, the guard fails open, and
    the message goes out - a hold that silently does not hold. Underscore keys and anything
    unserializable are dropped, which is also right: the approval is the person's own act, not
    a Front Office turn.
    """
    import vaf.core.web_interface as wi
    monkeypatch.setattr(wi, "notify_inbox_changed", lambda scope: None)
    from vaf.core import channel_message_store as store
    from vaf.core.outbound_hold import park_messenger_call, storable_args
    store._reset_announce_state()
    monkeypatch.setattr((__import__("vaf.core.platform", fromlist=["Platform"])).Platform,
                        "data_dir", staticmethod(lambda: tmp_path / "data"))

    agent = _bare_agent(tmp=tmp_path, monkeypatch=monkeypatch)
    kept = storable_args({"to_phone": "+1", "message": "x", "_agent": agent,
                          "callback": lambda: None, "voice_lang": "de"})
    assert kept == {"to_phone": "+1", "message": "x", "voice_lang": "de"}

    entry_id = park_messenger_call("send_whatsapp",
                                   {"to_phone": "+1", "message": "x", "_agent": agent},
                                   username="alice", user_scope_id="scope-1")
    row = store.held_send(entry_id, "alice", "scope-1")
    assert json.loads(row["args"]) == {"to_phone": "+1", "message": "x"}


def test_the_other_chat_gets_the_red_dot():
    """MUTATION: drop the `emit_session_unread` from the announcement.

    The card lives in the chat the draft belongs to, so a person who moved to another
    conversation would have nothing to look at. The chat list's own unread dot is the existing
    answer (a background reply already uses it) and the browser ignores it for the chat it is
    showing, so this lights up exactly the "somewhere else" case.
    """
    region = _region(AGENT, "def _announce_held_send", "def _clear_last_assistant_ui")
    assert "emit_session_unread" in region
    assert '"outbound_held"' in region


def test_the_card_asks_only_for_its_own_chat():
    """MUTATION: fetch /api/outbox without the session.

    Without the filter the card shows every waiting draft of the person, so a message being
    written in one chat appears in the next one they open - the one thing this must never do.
    """
    card = (ROOT / "web" / "components" / "outbox" / "HeldSendCard.tsx").read_text(encoding="utf-8")
    assert "api/outbox?session_id=" in card
    assert "if (!sessionId) { setRows([]); return; }" in card
    assert "sessionId={currentSessionId || ''}" in PAGE


def test_the_send_button_is_the_house_white_and_locked_until_it_is_read():
    """MUTATION: enable the send button at once, or animate the card's own border.

    The pause sits BEFORE the click, not after it: the point is that nobody fires off a message
    they have not read, and a person who wants it gone can still discard it at any moment. The
    light tone is spelled out because the bare white token folds to the dark surface in dark
    mode. And the rim that marks the waiting card breathes in opacity on an element of its own,
    because animating the card's border or shadow repaints the card every frame (the measured
    GPU leak the repaint rule was written for).
    """
    card = (ROOT / "web" / "components" / "outbox" / "HeldSendCard.tsx").read_text(encoding="utf-8")
    assert "dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-[#f5f5f5]" in card
    assert "dark:bg-white" not in card
    assert "const SEND_DELAY_SECONDS = 3;" in card
    # The lane belongs in the busy key: mail op ids and parked-call ids are two sequences, so
    # a bare number would disable a mail draft's buttons while a call of the same id is sending.
    assert "disabled={busy === `${r.kind}-${r.id}` || locked > 0}" in card
    assert "setBusy(`${row.kind}-${row.id}`)" in card
    assert "t('countdown', { seconds: locked })" in card
    # Discard is never locked: throwing away something unread costs nothing.
    assert 'disabled={busy === `${r.kind}-${r.id}`} onClick={() => act(r, \'discard\')}' in card
    assert 'className="vaf-draft-rim pointer-events-none absolute inset-0 rounded-2xl"' in card

    css = (ROOT / "web" / "app" / "globals.css").read_text(encoding="utf-8")
    rim = css.split("@keyframes vafDraftRim", 1)[1][:200]
    assert "opacity" in rim and "transform" not in rim
    assert css.count("@keyframes vafDraftRim") == 2, "the reduced-motion answer is missing"
    # And it is visible on BOTH themes. A white ring is the one value that cannot be: on the
    # light card (bg-gray-50) it disappears, and the rim is the only signal that the card wants
    # reading before the button opens.
    light = css.split(".vaf-draft-rim {", 1)[1].split("}", 1)[0]
    assert "rgba(17,24,39" in light, "the light theme needs dark ink"
    assert ".dark .vaf-draft-rim {" in css and "rgba(255,255,255,.35)" in css.split(".dark .vaf-draft-rim {", 1)[1][:200]


def test_the_card_carries_no_colour_of_its_own():
    """MUTATION: bring the amber back.

    The card is the agent's output in the conversation, not a warning strip, so it wears the
    same neutral surface and border tokens as the rest of the theme. An amber card reads as an
    alert about the app, and the one thing on it that does signal is the rim, which breathes
    and then stops.
    """
    card = (ROOT / "web" / "components" / "outbox" / "HeldSendCard.tsx").read_text(encoding="utf-8")
    assert "amber" not in card
    assert "dark:bg-[#1f1f1f]" in card and "dark:border-[#2e2e2e]" in card
