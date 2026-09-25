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
CARD = (ROOT / "web" / "components" / "outbox" / "HeldSendCard.tsx").read_text(encoding="utf-8")
HOOK = (ROOT / "web" / "components" / "outbox" / "useChatDrafts.ts").read_text(encoding="utf-8")
REFS = (ROOT / "web" / "components" / "outbox" / "draftRefs.ts").read_text(encoding="utf-8")


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
    never appears. The method needs nothing of an Agent but the session id and the turn-end
    slot it fills, so a stand-in object is the whole harness.
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
    agent._close_turn = Agent._close_turn.__get__(agent)

    Agent._announce_held_send(agent, f"{HELD_PREFIX} A WhatsApp message to +49...")
    assert pushes == [("green123456", {"type": "outbound_held"})]
    assert agent._turn_closing[1] is False, "the draft ends the turn with its hidden sentence"
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
    """MUTATION: poll the outbox on an interval, or mount the card only at the chat's end.

    The store announces a parked draft exactly as it announces a message, so the listing rides
    `outbound_held` and `inbox_changed`. A timer would be a third refresh policy in a file whose
    interval count is itself guarded.
    """
    assert "import { useChatDrafts } from '@/components/outbox/useChatDrafts';" in PAGE
    assert "useChatDrafts(getApiBase(), currentSessionId || '', heldVersion)" in PAGE
    assert "data.type === 'outbound_held'" in PAGE
    assert "setInterval(" not in HOOK, "the listing is fetched on the signal, never on a clock"
    assert "useEffect(() => { void load(); }, [load, version]);" in HOOK
    # UNDER THE TURN THAT WROTE IT: the grouped turn renders its drafts in its own column, and a
    # tool row that has no rail renders its draft right under the tool window. Only a WAITING
    # draft whose turn is not on screen falls back to the chat's last row.
    assert "<TurnDrafts rows={draftsOfTools(turnTl!.actions.filter(a => a.kind === 'tool').map(a => a.msg))}" in PAGE
    assert "<TurnDrafts rows={draftsOfTools([msg])} indent={false}" in PAGE
    assert "return chatDrafts.rows.filter(r => isWaitingDraft(r) && !placed.has(r.ref));" in PAGE
    assert PAGE.index("<UnplacedDrafts") < PAGE.index("<div ref={scrollRef} />")
    assert "<HeldSendCard" not in PAGE, "the one card at the end of the chat is gone"
    # The fallback brings the bot row's geometry with it and renders NOTHING when nothing is
    # unplaced: a wrapper in page.tsx would be an empty padded row after every conversation.
    unplaced = CARD.split("export function UnplacedDrafts", 1)[1]
    assert "if (!rows.length) return null;" in unplaced
    assert unplaced.index("if (!rows.length) return null;") < unplaced.index("vaf-msg-row")
    assert 'w-full max-w-[85%] max-md:max-w-full flex gap-4' in unplaced
    # The reading pause's own tick is the card's only interval, and it stops itself.
    assert CARD.count("setInterval(") == 1
    tick = CARD.split("setInterval(", 1)[1][:200]
    assert "setNow(at)" in tick and "if (at >= unlockAt) clearInterval(tick);" in tick
    assert "if (Date.now() >= unlockAt) return;" in CARD
    assert CARD.count("useTranslations('outbox')") >= 2

def test_the_card_has_no_hardcoded_copy():
    """Every string the person reads comes from the catalogues, in all seven languages."""
    keys = {"titleMail", "titleWhatsapp", "titleOther", "notSent", "edited", "noRecipient",
            "subjectPlaceholder", "editHint", "showMore", "showLess", "revert", "emptyText",
            "send", "discard", "countdown", "more", "failed", "ambiguous", "cc", "bcc",
            "attachments", "sent", "sending", "discarded", "replaced"}
    for key in keys:
        assert f"t('{key}'" in CARD, key
    for lang in ("de", "en", "tr", "zh", "ja", "ko", "th"):
        cat = json.loads((ROOT / "web" / "messages" / f"{lang}.json").read_text(encoding="utf-8"))
        assert set(cat["outbox"]) == keys, (lang, set(cat["outbox"]) ^ keys)
        assert cat["main"].get("wakeDraft"), lang
    # The separator between the state and the title is the catalogue's (zh writes a full-width
    # colon), and a title is one sentence per channel, never "{channel} an {recipient}" for
    # the channels that exist: the words around a channel name are grammar in ko and th.
    assert "tCommon('labelSeparator')" in CARD
    assert "t('titleMail', { recipient })" in CARD and "t('titleWhatsapp', { recipient })" in CARD
    assert "tMain('wakeDraft')" in PAGE

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

    # What came from a message is data, never markup: a body the model wrote may contain
    # "[bold]" or "[/red]", and Rich would style the table on the first and raise on the
    # second. MUTATION: drop the `escape()` calls and this invoke exits non-zero (MarkupError)
    # or the literal brackets vanish from the output.
    third = park_messenger_call("send_whatsapp", {"to_phone": "+49[/red]170", "message": "see [bold]this[/bold] and [/red]",
                                                  "file_path": "/home/user/out/[red]note.ogg"},
                                username="alice", user_scope_id="scope-1")
    shown = runner.invoke(cmd.app, ["list"])
    assert shown.exit_code == 0, shown.output
    assert "[bold]this[/bold]" in shown.output and "[/red]" in shown.output
    assert store.held_send(third, "alice", "scope-1")["state"] == "held"
    # The file that would leave with it is on the row by name, escaped like everything else.
    assert "files:" in shown.output and "[red]note.ogg" in shown.output and "/home/user" not in shown.output


def test_the_cli_edits_through_the_same_function(monkeypatch, tmp_path):
    """The terminal can change a draft's words like the card can, through `revise_draft`, and
    a send from here wakes nothing: this process has no queue anybody drains (the NAMED
    BOUNDARY in vaf/cli/cmd/outbox.py). MUTATION: pass wake=True in the CLI and the wake
    assertion goes red."""
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from vaf.core import channel_message_store as store
    from vaf.core.platform import Platform

    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    import vaf.core.config as cfg_mod
    monkeypatch.setattr(cfg_mod.Config, "get", classmethod(lambda cls, key, default=None: default))
    import vaf.core.web_interface as wi
    monkeypatch.setattr(wi, "notify_inbox_changed", lambda scope: None)
    store._reset_announce_state()
    import vaf.cli.cmd.outbox as cmd
    monkeypatch.setattr(cmd, "_identity", lambda: ("alice", "scope-1"))
    import vaf.core.task_queue as tq
    woken = []
    monkeypatch.setattr(tq, "enqueue_wake_turn", lambda **kw: woken.append(kw))
    from vaf.core.outbound_hold import park_messenger_call
    entry_id = park_messenger_call("send_whatsapp", {"to_phone": "+49170", "message": "Hallo"},
                                   username="alice", user_scope_id="scope-1", session_id="chat-a")
    runner = CliRunner()
    assert runner.invoke(cmd.app, ["edit", "call", str(entry_id)]).exit_code == 1, "nothing to change"
    assert runner.invoke(cmd.app, ["edit", "call", str(entry_id), "--text", "  "]).exit_code == 1
    out = runner.invoke(cmd.app, ["edit", "call", str(entry_id), "--text", "Hallo Uwe"])
    assert out.exit_code == 0, out.output
    assert store.held_send(entry_id, "alice", "scope-1")["preview"] == "Hallo Uwe"
    sent = []
    import vaf.core.outbound_hold as oh
    monkeypatch.setattr(oh, "resolve_tool", lambda name: SimpleNamespace(
        run=lambda **kw: sent.append(kw) or "Message sent via WhatsApp."))
    assert runner.invoke(cmd.app, ["send", "call", str(entry_id)]).exit_code == 0
    assert sent[0]["message"] == "Hallo Uwe" and woken == []
    assert runner.invoke(cmd.app, ["edit", "call", str(entry_id), "--text", "zu spät"]).exit_code == 1


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
    """MUTATION: fetch /api/outbox without the session, or keep a late answer.

    Without the filter the cards show every draft of the person, so a message being written in
    one chat appears in the next one they open - the one thing this must never do.
    """
    assert "api/outbox?session_id=${encodeURIComponent(asked)}&settled=true" in HOOK
    assert "if (!sessionId) { setRows([]); return; }" in HOOK
    # A listing that answers AFTER the person switched chats is dropped: the request remembers
    # which chat it asked for, and the answer is compared against the chat on screen before it
    # becomes rows.
    assert "const asked = sessionId;" in HOOK
    assert "if (sessionRef.current !== asked) return;" in HOOK
    assert "sessionRef.current = sessionId;" in HOOK

def test_the_send_button_is_the_house_white_and_locked_until_it_is_read():
    """MUTATION: enable the send button at once, or animate the card's own border.

    The pause sits BEFORE the click, not after it: the point is that nobody fires off a message
    they have not read, and a person who wants it gone can still discard it at any moment. The
    light tone is spelled out because the bare white token folds to the dark surface in dark
    mode. And the rim that marks the waiting card breathes in opacity on an element of its own,
    because animating the card's border or shadow repaints the card every frame (the measured
    GPU leak the repaint rule was written for).
    """
    assert "dark:bg-[#e6e6e6] dark:text-[#181818] dark:hover:bg-[#f5f5f5]" in CARD
    assert "dark:bg-white" not in CARD
    assert "const SEND_DELAY_SECONDS = 3;" in CARD
    assert "disabled={busy || locked > 0}" in CARD
    assert "t('countdown', { seconds: locked })" in CARD
    # The pause belongs to the DRAFT, not to one mount of the card: the card moves from the
    # chat's last row into its turn when the tool result arrives, and a pause that restarted
    # there would lock the button again for a text the person has been reading.
    assert "const FIRST_SEEN = new Map<string, number>();" in CARD
    assert "FIRST_SEEN.get(row.ref) ?? Date.now()" in CARD
    # Discard is never locked: throwing away something unread costs nothing.
    assert "<button type=\"button\" disabled={busy} onMouseDown={e => e.preventDefault()} onClick={() => act('discard')}" in CARD
    assert 'className="vaf-draft-rim pointer-events-none absolute inset-0 rounded-2xl"' in CARD
    # A draft whose send was interrupted offers no Send button and no editing: it may have
    # arrived, and the click that would repeat it is the one thing this card must not hand out.
    assert "{row.state !== 'ambiguous' && (" in CARD and "t('ambiguous')" in CARD
    assert "const editable = row.state !== 'ambiguous';" in CARD
    # Every address and every file: approving is approving what leaves.
    assert "t('cc', { recipients: row.cc })" in CARD and "t('bcc', { recipients: row.bcc })" in CARD
    assert "t('attachments', { names: row.attachments.join(', ') })" in CARD
    # The failure is said ONCE: the row's own line yields to the note set by the failed click.
    assert "{row.state === 'failed' && !note && (" in CARD

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


def test_the_card_lines_up_with_the_tool_windows():
    """MUTATION: drop the rail offset from TurnDrafts or from UnplacedDrafts.

    Reported from the live app: the card started left of the tool windows above it. Inside a
    turn's actions rail every tool window is indented by `pl-[26px]` (TurnActionsTimeline),
    and the card sat in the answer column without it, 26 pixels to the left - measured in a
    render of these class strings, 148 against 174. The fallback row takes the same offset,
    so a card that moves from the chat's last row into its turn does not jump sideways.
    """
    timeline = (ROOT / "web" / "components" / "TurnActionsTimeline.tsx").read_text(encoding="utf-8")
    assert '<div className="relative pl-[26px]">' in timeline, "the rail's offset moved; move the card's with it"
    turn = CARD.split("export function TurnDrafts", 1)[1].split("export function UnplacedDrafts", 1)[0]
    assert "indent && 'pl-[26px] max-md:pl-0'" in turn
    unplaced = CARD.split("export function UnplacedDrafts", 1)[1]
    assert "flex flex-col gap-3 flex-1 min-w-0 pl-[26px] max-md:pl-0" in unplaced


def test_the_card_is_editable_and_what_is_typed_is_what_leaves():
    """MUTATION: send without saving the field first, or let Send take focus before the click.

    Clicking into the text turns it into a field; leaving the field saves it (PATCH), and Send
    saves whatever is still in the field before it sends, so the person never sends the
    agent's version while their own is on screen. The buttons keep the field's focus
    (mousedown is prevented), so the click is the one place that decides.
    """
    assert "method: 'PATCH'" in CARD and "`${apiBase}/api/outbox/${row.kind}/${row.id}`" in CARD
    send = CARD.split("const act = async", 1)[1].split("const named", 1)[0]
    assert send.index("await save()") < send.index("/send"), "saved before it is sent"
    assert "if (saving.current && !(await saving.current)) return;" in send
    assert CARD.count("onMouseDown={e => e.preventDefault()}") == 3
    assert "if (e.key === 'Escape') { e.preventDefault(); revert(); }" in CARD
    assert "setNote(t('emptyText'))" in CARD
    # A long text is folded until asked for, and clicking into it unfolds it.
    assert "line-clamp-[8]" in CARD and "setUnfolded(true); setEditing('body');" in CARD


def test_the_wire_literals_match_on_both_sides():
    """MUTATION: rename one of the three literals on one side only.

    The history holds TEXT, so a reloaded chat has nothing but these to find a draft's turn,
    to hide the turn's closing sentence and to draw the wake row.
    """
    from vaf.core import outbound_hold
    assert f'export const DRAFT_TURN_END = "{outbound_hold.TURN_ENDS_AT_DRAFT}";' in REFS
    assert f"export const DRAFT_WAKE_PREFIX = '{outbound_hold.DRAFT_WAKE_PREFIX}';" in REFS
    assert "const CREATED = /^NOT SENT YET\\. Draft (mail|call):(\\d+)/;" in REFS
    assert outbound_hold.HELD_PREFIX == "NOT SENT YET."
    result = outbound_hold.held_result("send_whatsapp", {"to_phone": "+1"}, entry_id=3)
    import re
    assert re.match(r"^NOT SENT YET\. Draft (mail|call):(\d+)", result)
    # The page hides exactly the closing sentence and draws the wake row by the prefix.
    assert "const cleanAnswer = isBot ? (isDraftTurnEnd(answer) ? '' : withoutOptions(stripToolCallsJSON(answer), turnAsks)) : answer;" in PAGE
    assert "msg.kind === 'draft' || _wakeContent.startsWith(DRAFT_WAKE_PREFIX)" in PAGE
    assert "return String(content ?? '').trim() === DRAFT_TURN_END;" in REFS


def test_the_runner_shows_a_draft_turn_as_the_card():
    """MUTATION: let the fixed sentence fall into the ordinary branch.

    An empty-looking answer there becomes "No response was produced", the sentence would be
    spoken by the browser's auto-TTS and opened in the document editor, and the session would
    store the pre-draft stream instead of the sentence the browser hides.
    """
    runner = (ROOT / "vaf" / "core" / "headless_runner.py").read_text(encoding="utf-8")
    assert "_turn_ends_at_draft = response_text.strip() == _DRAFT_END" in runner
    branch = runner.split("elif _turn_ends_at_draft:", 1)[1][:900]
    assert "final_text = response_text" in branch and "emit_agent_message(" in branch
    assert "or _turn_ends_at_draft) else str(final_text)" in runner, "nothing to speak"
    assert "and not _turn_ends_at_draft and not _turn_closed):" in runner, "no document editor for the sentence"
    assert "_assistant_response = response_text" in runner.split("if _turn_ends_at_draft:", 2)[-1][:400]

def test_the_card_carries_no_colour_of_its_own():
    """MUTATION: bring the amber back.

    The card is the agent's output in the conversation, not a warning strip, so its SURFACE and
    BORDER wear the same neutral tokens as the rest of the theme. An amber card reads as an
    alert about the app, and the one thing on it that does signal is the rim, which breathes
    and then stops. A single coloured LINE is a different thing: a draft whose send failed says
    so in red, one that may already have gone out in amber, and both are facts about that one
    draft rather than a colour the card wears.
    """
    card = (ROOT / "web" / "components" / "outbox" / "HeldSendCard.tsx").read_text(encoding="utf-8")
    for token in ("bg-amber", "border-amber", "dark:bg-amber", "dark:border-amber"):
        assert token not in card, token
    assert "dark:bg-[#1f1f1f]" in card and "dark:border-[#2e2e2e]" in card


def test_the_runner_keeps_the_draft_note_with_its_turn_and_its_chat():
    """MUTATION: drop the reset before the turn, or store the note after the user message.

    The note sits BEFORE the input in the agent's history, where the turn-context persistence
    (which starts after the user message) cannot see it, so the runner stores it there itself.
    And the agent serves every chat: a turn that ends before chat_step writes the note must
    not leave the previous chat's note behind for this chat's file.
    """
    runner = (ROOT / "vaf" / "core" / "headless_runner.py").read_text(encoding="utf-8")
    call = runner.index("response = agent.chat_step(")
    assert "agent._turn_decision_note = None" in runner[call - 600:call]
    store_at = runner.index('_draft_note = getattr(agent, "_turn_decision_note", None)')
    user_at = runner.index('session.add_message(role="user", content=_user_input.strip(),')
    assert store_at < user_at
    assert 'session.add_message(role="system", content=str(_draft_note))' in runner[store_at:user_at]
    # Stored whether or not the input repeats the last stored user message: a repeated input
    # is not stored again, the note is new either way. MUTATION: move the note back inside
    # the `last_user_msg != ...` branch - red.
    dedupe_at = runner.index("if last_user_msg != _user_input.strip():")
    assert store_at < dedupe_at, "the note is stored before the repeated-input branch, not inside it"


def test_the_card_never_hides_an_edit_and_never_keeps_a_blank():
    """MUTATION: show the agent's text as soon as the field closes, or keep an empty edit.

    Between leaving the field and the save landing (or after a failed save) the card shows the
    person's words, because those are what Send sends. A field left empty brings the saved
    words back with the reason. The wake row strips its prefix only when it is there.
    """
    assert "const text = (editing || body !== saved.current.body) ? body : (row.preview || '');" in CARD
    blur = CARD.split("const onEditBlur", 1)[1].split("const revert", 1)[0]
    assert "if (!current.current.body.trim()) {" in blur
    assert blur.index("revert();") < blur.index("setNote(t('emptyText'));")
    assert "(_draftLine.startsWith(DRAFT_WAKE_PREFIX) ? _draftLine.slice(DRAFT_WAKE_PREFIX.length) : _draftLine).trim()" in PAGE
