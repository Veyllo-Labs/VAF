# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The one block of the system prompt that grew with usage and nothing bounded.

`known_facts` is a retrieved summary of facts about the person, written by the memory
compaction and injected into EVERY system prompt. Measured 2026-09-03 on a real store: it
stood at 9,471 characters, more than a third of the whole system message, and had grown
38 percent in 20 days with no ceiling anywhere on its path. Seven of the twelve injected
blocks were already bounded; this was not one of them.

The ceiling is applied at BOTH ends on purpose. At the writer, so the file stops growing.
At the reader, so a cache written before the ceiling existed is bounded on the very next
turn rather than on the next refresh - a door only protects what is written after it, the
same reason the room's vote deadline is read defensively as well as composed defensively.
"""
import pytest

from vaf.core.config import Config
from vaf.memory.rag import profile_cache_ceiling, trim_profile_summary


@pytest.fixture()
def ceiling(monkeypatch):
    """A small ceiling, so the arithmetic is readable rather than inferred."""
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: 200 if key == "memory_profile_cache_chars" else default))
    return 200


def test_a_summary_within_the_ceiling_is_untouched(ceiling):
    text = "name: Alice\ncity: Berlin\nprefers: short answers"
    assert trim_profile_summary(text) == text


def test_a_summary_over_the_ceiling_is_cut_and_says_so(ceiling):
    """MUTATION: return the text unchanged, or cut without the note.

    The note is what tells the model it is reading a part rather than the whole. Without
    it the agent cannot tell "these are all the facts" from "these are the newest ones",
    and it will answer as though the rest does not exist.
    """
    text = "\n".join(f"fact {i}: something worth remembering about the person" for i in range(20))
    assert len(text) > ceiling
    out = trim_profile_summary(text)

    assert len(out) <= ceiling, "the cut result is still over the ceiling"
    assert "capped" in out, "nothing says the summary was cut"
    assert out.startswith("fact 0:"), "the cut took from the wrong end"


def test_the_cut_lands_on_a_line_boundary(ceiling):
    """MUTATION: cut at the character limit.

    Every line is one retrieved fact. A cut through the middle of one hands the model
    half a sentence with no way to tell that it is half.
    """
    text = "\n".join(f"fact {i}: {'x' * 40}" for i in range(20))
    body = trim_profile_summary(text).split("\n[older facts")[0]
    for line in body.split("\n"):
        assert line in text.split("\n"), f"a line was cut through: {line!r}"


def test_a_single_line_longer_than_the_ceiling_is_still_bounded(ceiling):
    """The line rule must not defeat the ceiling: one enormous line has no boundary to
    cut on, and returning it whole would be the ceiling not applying at all."""
    out = trim_profile_summary("one: " + "y" * 5000)
    assert len(out) <= ceiling


def test_zero_turns_the_ceiling_off(monkeypatch):
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: 0 if key == "memory_profile_cache_chars" else default))
    text = "\n".join(f"fact {i}" for i in range(500))
    assert profile_cache_ceiling() == 0
    assert trim_profile_summary(text) == text


def test_an_unreadable_setting_falls_back_rather_than_raising(monkeypatch):
    """This runs on the path that builds every prompt. A bad config value must not be
    the reason a turn fails."""
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: "lots" if key == "memory_profile_cache_chars" else default))
    assert profile_cache_ceiling() == 4000


def test_both_ends_apply_the_ceiling():
    """MUTATION: cap at the writer only.

    The writer alone leaves every cache written before the ceiling existed injecting its
    full size until the next compaction happens to refresh it, which may be days.
    """
    from pathlib import Path

    rag = (Path(__file__).resolve().parents[1] / "vaf" / "memory" / "rag.py").read_text(encoding="utf-8")
    prompt = (Path(__file__).resolve().parents[1] / "vaf" / "core" / "system_prompt.py").read_text(encoding="utf-8")
    assert "trim_profile_summary(summary or \"\")" in rag, "the writer does not cap"
    assert "trim_profile_summary(" in prompt, "the reader does not cap"


def test_the_key_is_admin_only_and_documented():
    """A key that changes what every prompt carries is not a per-user preference, and the
    schema doc's count line is a guard of its own."""
    from pathlib import Path

    assert "memory_profile_cache_chars" in Config.DEFAULTS
    assert Config.is_global_config_key("memory_profile_cache_chars"), "a user could shrink it"
    doc = (Path(__file__).resolve().parents[1] / "docs" / "setup" / "CONFIG_SCHEMA.md").read_text(encoding="utf-8")
    assert "memory_profile_cache_chars" in doc
