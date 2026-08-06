# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One command list, and the guard that keeps it one.

The measurement that produced `vaf/cli/commands.py`: the same list lived in
five places and had already drifted twice - the classic dispatcher handled
`restart` without recognising it as a bare word (so typing it sent the word to
the model), and the completer offered `model`, `history` and `export`, which no
dispatcher handled (so accepting the completion produced "Unknown command").

A drifting command list fails in the two worst ways available: a word that is
offered and ignored costs a turn and pollutes the session, and a word that is
handled but not caught is silently sent to the model.
"""
import pytest

from vaf.cli.commands import COMMANDS, LANES, bare_words, lookup, parse, suggest


# ── the registry itself ─────────────────────────────────────────────────────────────

def test_every_word_resolves_to_exactly_one_command():
    seen = {}
    for cmd in COMMANDS:
        for word in cmd.words:
            assert word not in seen, (
                f"{word!r} is claimed by both {seen.get(word)} and {cmd.word}")
            seen[word] = cmd.word
            assert lookup(word) is cmd
            assert lookup(word.upper()) is cmd, "lookup must be case-insensitive"


def test_lanes_are_declared_from_the_known_set():
    for cmd in COMMANDS:
        assert cmd.lane in LANES, f"{cmd.word} has lane {cmd.lane!r}"


def test_speech_stop_runs_off_the_lane():
    """The one command that must act WHILE a turn runs: the agent lane is
    exactly the thread busy producing the speech the user wants stopped."""
    assert lookup("halt").lane == "now"
    for alias in ("stop", "quiet", "stfu"):
        assert lookup(alias).lane == "now"


def test_destructive_commands_ask_first():
    for word in ("clear", "undo", "restart"):
        assert lookup(word).confirm, f"/{word} runs without asking"


# ── parsing ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("word", sorted(bare_words()))
def test_every_bare_word_is_recognised_alone(word):
    parsed = parse(word)
    assert parsed.command is not None, f"{word!r} would be sent to the model"
    assert not parsed.is_message


def test_a_slash_form_never_reaches_the_model():
    parsed = parse("/frobnicate")
    assert parsed.command is None
    assert parsed.unknown_word == "frobnicate"
    assert not parsed.is_message, "an unknown /command must not become a message"


def test_an_unknown_word_suggests_the_closest_real_one():
    assert "restore" in suggest("restor")
    assert suggest("zzzzzzz") == []


def test_arguments_survive_with_their_original_case():
    """Session ids are not lowercase. The old parser lowercased the whole line
    and then took only the first word - both halves were wrong."""
    parsed = parse("session AbC123XyZ")
    assert parsed.command.word == "session"
    assert parsed.args == ("AbC123XyZ",)


def test_theme_takes_a_name():
    parsed = parse("theme dark")
    assert parsed.command.word == "theme"
    assert parsed.args == ("dark",)


def test_a_sentence_that_starts_with_a_command_word_stays_a_sentence():
    """`clear` routes; `clear the table for dinner` is a message. Commands
    without an argument spec only match as a lone word."""
    for line in ("clear the table for dinner", "help me write a letter",
                 "tools are expensive"):
        assert parse(line).is_message, f"{line!r} was swallowed as a command"


def test_the_slash_form_still_routes_with_arguments():
    parsed = parse("/theme nord")
    assert parsed.command.word == "theme"
    assert parsed.args == ("nord",)


def test_blank_input_is_not_a_command():
    for line in ("", "   ", "/", "/   "):
        assert parse(line).command is None
        assert not parse(line).unknown_word


# ── the guard: no sixth copy ────────────────────────────────────────────────────────

def test_the_classic_lane_catch_set_is_the_registry():
    """`vaf/cli/cmd/run.py` had its own 18-word set. It must now BE the
    registry, or a word added here goes to the model over there."""
    import inspect

    import vaf.cli.cmd.run as run_mod

    src = inspect.getsource(run_mod)
    assert "KNOWN_COMMANDS = bare_words()" in src
    assert '"stfu"}' not in src, "the hand-written set came back"


def test_the_classic_completer_offers_only_words_that_route():
    """It used to offer `model`, `history` and `export` - none of which the
    dispatcher handled. Accepting such a completion produced an error.

    The guarantee is now structural rather than a shared list: the completer
    delegates to `vaf/cli/completion.py`, whose command half is built from
    COMMANDS, so a phantom word has nowhere to come from."""
    import inspect

    import vaf.cli.tui as tui_mod
    from vaf.cli.completion import complete

    src = inspect.getsource(tui_mod)
    assert "'export'" not in src, "a phantom command came back into the completer"

    offered = {c.insert for c in complete("/")}
    assert offered == {c.word for c in COMMANDS}
    # `export` was the canonical phantom: offered while nothing routed it.
    # It ROUTES now (registry entry, app handler, classic branch), so being
    # offered is the guarantee working, not the phantom returning.
    assert "export" in offered


def test_the_app_palette_and_help_are_derived():
    from vaf.cli.tui_app.screens import HelpScreen, PaletteScreen

    palette = {word for word, _ in PaletteScreen.entries()}
    registry = {f"/{c.word}" for c in COMMANDS if c.palette}
    assert palette == registry

    help_rows = {row[0].split()[0] for row in HelpScreen.command_rows()}
    assert help_rows == {f"/{c.word}" for c in COMMANDS}


def test_the_app_wires_every_registry_command():
    """A word in the registry with no handler would look available in the
    palette and do nothing when chosen."""
    from vaf.cli.tui_app.app import VafApp

    app = VafApp.__new__(VafApp)
    app._bridge = None
    handlers = set(VafApp._handlers(app))
    missing = {c.word for c in COMMANDS} - handlers
    assert not missing, f"registry commands with no handler: {sorted(missing)}"


def test_the_registry_stays_off_the_heavy_import_graph():
    """Both lanes import this at module level; pulling textual or the agent in
    here would tax every `vaf` invocation."""
    import subprocess
    import sys

    probe = (
        "import sys\n"
        "import vaf.cli.commands, vaf.cli.tool_catalog\n"
        "for mod in ('textual', 'prompt_toolkit', 'vaf.core.agent'):\n"
        "    assert mod not in sys.modules, mod\n"
        "print('clean')\n"
    )
    result = subprocess.run([sys.executable, "-c", probe],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


# ── the tool catalog ────────────────────────────────────────────────────────────────

def test_the_tool_policy_lives_in_one_place():
    """Hidden tools and coder-only rows are POLICY. A copy of a policy constant
    drifts silently, which is why the settings menu now imports it."""
    import inspect

    import vaf.cli.cmd.settings as settings_mod

    src = inspect.getsource(settings_mod.show_tools_menu)
    assert "describe_tools" in src
    assert "TOOLS_HIDDEN_FROM_CLI = frozenset" not in src
    assert "CODER_SUBAGENT_TOOLS = [" not in src


def test_describe_tools_hides_internals_and_marks_coder_only():
    from types import SimpleNamespace

    from vaf.cli.tool_catalog import describe_tools

    class _Tool:
        description = "does a thing"

    agent = SimpleNamespace(tools={"update_intent": _Tool(), "bash": _Tool(),
                                   "write_file": _Tool()})
    rows = describe_tools(agent)
    names = [r.name for r in rows]

    assert "update_intent" not in names, "an internal tool was advertised"
    assert names.count("bash") == 1, "a main-agent tool was listed twice"
    assert any(r.name == "codesearch" and r.coder_only for r in rows)


def test_describe_tools_truncates_long_descriptions():
    from types import SimpleNamespace

    from vaf.cli.tool_catalog import DESCRIPTION_CHARS, describe_tools

    class _Tool:
        description = "x" * 400

    rows = describe_tools(SimpleNamespace(tools={"noisy": _Tool()}))
    row = [r for r in rows if r.name == "noisy"][0]
    assert len(row.description) <= DESCRIPTION_CHARS + 3


def test_the_classic_lane_handles_every_word_it_catches():
    """The drift that motivated this round, now impossible in both directions:
    a word in the catch set that no branch handles produces "Unknown command"
    instead of reaching the model, and a handled word outside the set is sent
    to the model instead of running. Both were live before this change."""
    import re

    src = open("vaf/cli/cmd/run.py", encoding="utf-8").read()
    start = src.find('if user_input.lower() in ("s", "settings")')
    assert start > 0, "the modern lane's word handlers moved - re-anchor this guard"
    lane = src[start:src.find("def _run_classic")]

    handled = set()
    for match in re.finditer(
            r'(?:user_input\.lower\(\)|cmd) (?:==|in) \(?([^)\n:]+)\)?:', lane):
        handled |= set(re.findall(r'["\']([a-z?]+)["\']', match.group(1)))

    missing = sorted(bare_words() - handled)
    assert not missing, (
        f"the classic lane catches these but handles none of them: {missing}")
