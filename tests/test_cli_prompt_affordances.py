# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""History, inline suggestions and completion - one core, both lanes.

The classic prompt had all three and the full-screen lane had none. Rebuilding
them for one lane would have meant a second history format, a second candidate
list, and a suggester reached through a private method. So the logic moved into
`vaf/cli/history.py` and `vaf/cli/completion.py`, and the classic completer
became a ~12-line adapter over the same core (182 lines deleted).

One measured defect fixed on the way, which belonged to the classic lane and
the web server as much as to the app: `learn()` wrote the whole learned corpus
synchronously on every submitted line - 140 ms on a real install (2.5 MB,
48k prefixes), measured with this repo's own file.
"""
import json
import os
import tempfile
import time
from pathlib import Path

import pytest

from vaf.cli.completion import Candidate, complete, quick_paths


# ── the shared completion core ──────────────────────────────────────────────────────

def test_slash_completes_from_the_command_registry():
    """Not a private list: the completer physically cannot offer a word the
    dispatcher does not run, which is the drift that started all of this."""
    from vaf.cli.commands import COMMANDS

    words = {c.insert for c in complete("/")}
    assert words == {c.word for c in COMMANDS}


def test_prefix_matches_win_and_fuzzy_is_only_a_typo_fallback():
    """`/set` offering `exit` and `listen` buries the one real match."""
    assert [c.insert for c in complete("/set")] == ["settings"]
    assert [c.insert for c in complete("/restor")] == ["restore"]     # typo
    assert complete("/zzzzz") == []


def test_a_fully_typed_command_offers_nothing():
    """Otherwise the open menu swallows the Enter meant to SEND the command -
    `/settings` could never be submitted at all."""
    assert complete("/settings") == []
    assert complete("/sessions") == []


def test_at_lists_folders_and_files_with_sizes(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "note.txt").write_bytes(b"x" * 2048)

    cands = complete(f"@{tmp_path}{os.sep}")
    by_name = {c.insert.rstrip(os.sep): c for c in cands}

    assert "sub" in by_name and by_name["sub"].meta == "Folder"
    assert by_name["sub"].insert.endswith(os.sep), "a folder must keep going"
    assert "note.txt" in by_name and by_name["note.txt"].meta == "2KB"


def test_at_filters_by_what_was_typed(tmp_path):
    (tmp_path / "alpha.txt").write_text("a")
    (tmp_path / "beta.txt").write_text("b")

    names = {c.insert for c in complete(f"@{tmp_path}{os.sep}al")}
    assert names == {"alpha.txt"}


def test_hidden_files_appear_only_when_asked(tmp_path):
    (tmp_path / ".secret").write_text("s")
    (tmp_path / "plain.txt").write_text("p")

    assert {c.insert for c in complete(f"@{tmp_path}{os.sep}")} == {"plain.txt"}
    assert ".secret" in {c.insert for c in complete(f"@{tmp_path}{os.sep}.")}


def test_quick_paths_cover_the_usual_places():
    paths = quick_paths()
    for key in ("~", "desktop", "downloads", "documents", "home", "."):
        assert key in paths


def test_a_plain_message_completes_nothing():
    """This is a chat prompt first: completion must not fire on prose."""
    assert complete("what is the weather") == []
    assert complete("") == []


def test_the_completion_core_stays_off_the_heavy_import_graph():
    import subprocess
    import sys

    probe = (
        "import sys\n"
        "import vaf.cli.completion, vaf.cli.history\n"
        "assert 'textual' not in sys.modules\n"
        "print('clean')\n"
    )
    result = subprocess.run([sys.executable, "-c", probe],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


# ── the classic completer is now an adapter ─────────────────────────────────────────

def test_the_classic_completer_delegates_to_the_shared_core():
    """If it kept its own body, the two lanes would drift apart again - and the
    ~180 lines this round deleted would grow back."""
    import inspect

    import vaf.cli.tui as tui_mod

    src = inspect.getsource(tui_mod)
    assert src.count("from vaf.cli.completion import complete") >= 2, (
        "a completer stopped using the shared core")
    for gone in ("self.quick_paths = {", "PathCompleter(expanduser=True)",
                 "display_meta=\"Quick Path\""):
        assert gone not in src, f"the hand-rolled completer came back: {gone!r}"


# ── history, shared with the classic lane ───────────────────────────────────────────

def test_history_round_trips_through_the_shared_file(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    import importlib

    import vaf.cli.history as hist
    importlib.reload(hist)

    assert hist.load_history() == []
    assert not hist.history_file().exists(), "reading must not create the file"

    hist.append_history("first")
    hist.append_history("second")
    assert hist.load_history() == ["second", "first"], "newest must come first"


def test_history_is_readable_by_prompt_toolkits_own_reader(monkeypatch, tmp_path):
    """The point of sharing: the classic lane opens the same file with the same
    class, so a private format here would split the history in two."""
    monkeypatch.setenv("HOME", str(tmp_path))
    import importlib

    import vaf.cli.history as hist
    importlib.reload(hist)

    hist.append_history("written by the app lane")

    from prompt_toolkit.history import FileHistory
    entries = list(FileHistory(str(hist.history_file())).load_history_strings())
    assert "written by the app lane" in entries


def test_blank_lines_are_not_recorded(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    import importlib

    import vaf.cli.history as hist
    importlib.reload(hist)

    hist.append_history("   ")
    hist.append_history("")
    assert hist.load_history() == []


# ── the autosuggest, and the 140 ms it used to cost ─────────────────────────────────

def _suggester(tmp_path, corpus=None):
    from vaf.cli.autosuggest import SmartAutoSuggest

    store = Path(tmp_path) / "autosuggest.json"
    if corpus:
        store.write_text(json.dumps(corpus))
    return SmartAutoSuggest(history_file=store)


def test_learning_does_not_block_the_caller(tmp_path):
    """The measured defect: `learn()` wrote 2.5 MB synchronously on every
    submitted line. In a full-screen app that is a visible freeze on Enter."""
    smart = _suggester(tmp_path)
    smart.learned_phrases = {f"word{i}": {"next": i} for i in range(20000)}

    slow = []

    def _slow_write():
        slow.append(True)
        time.sleep(0.3)

    smart._write_learned = _slow_write

    started = time.perf_counter()
    smart.learn("this sentence teaches something new")
    elapsed = time.perf_counter() - started

    assert elapsed < 0.05, f"learn() blocked for {elapsed*1000:.0f} ms"


def test_the_learned_corpus_still_reaches_disk(tmp_path):
    from vaf.cli.autosuggest import SmartAutoSuggest

    smart = _suggester(tmp_path)
    smart.SAVE_DEBOUNCE_SECONDS = 0.05
    smart.learn("alpha beta gamma")

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not smart.history_file.exists():
        time.sleep(0.05)

    assert smart.history_file.exists(), "the debounced save never landed"
    stored = json.loads(smart.history_file.read_text())
    assert stored.get("alpha", {}).get("beta") == 1


def test_flush_writes_immediately(tmp_path):
    smart = _suggester(tmp_path)
    smart.SAVE_DEBOUNCE_SECONDS = 3600      # would never fire on its own
    smart.learn("alpha beta gamma")
    assert not smart.history_file.exists()

    smart.flush()
    assert smart.history_file.exists(), "a clean shutdown must not lose learning"


def test_a_half_written_corpus_can_never_be_read(tmp_path):
    """Written to a temp file and renamed: a crash mid-write must not leave
    invalid JSON where the next start expects a corpus."""
    import inspect

    from vaf.cli.autosuggest import SmartAutoSuggest

    src = inspect.getsource(SmartAutoSuggest._write_learned)
    assert ".replace(" in src and "with_suffix" in src


def test_suggest_is_public_and_lane_agnostic(tmp_path):
    """Both lanes need it: prompt_toolkit wants a Suggestion object, the app
    wants a plain string. Without this one of them reaches into a private."""
    smart = _suggester(tmp_path, corpus={"hallo": {"welt": 5}})
    assert smart.suggest("hallo ") == "welt"
    assert smart.suggest("a") is None            # too short to guess
    assert smart.suggest("") is None


def test_the_prompt_toolkit_shape_still_works(tmp_path):
    from prompt_toolkit.document import Document

    smart = _suggester(tmp_path, corpus={"hallo": {"welt": 5}})
    found = smart.get_suggestion(None, Document("hallo ", cursor_position=6))
    assert found is not None and found.text == "welt"
