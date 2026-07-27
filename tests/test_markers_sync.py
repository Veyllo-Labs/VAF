# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Rule-2 guard for vaf/markers.py: the public constants must stay in sync
with the literals the engine actually emits. Renaming a string in agent.py
without updating the marker (or vice versa) fails here instead of silently
breaking every embedder's error handling.

The engine is now more than one file - the dispatch pipeline moved into
``vaf/core/tool_dispatch.py`` - so the search covers both. That widening is not
cosmetic: when the confirmation string moved, this guard stayed green purely
because the terminal PROMPT in agent.py happens to contain the same words, while
the literal actually returned to the model had left the file. A guard that passes
for the wrong reason is worse than one that fails, so the confirmation marker also
gets a targeted check against the module that returns it."""
from pathlib import Path

from vaf import markers

_ROOT = Path(__file__).resolve().parents[1] / "vaf" / "core"
AGENT_SRC = (_ROOT / "agent.py").read_text(encoding="utf-8")
DISPATCH_SRC = (_ROOT / "tool_dispatch.py").read_text(encoding="utf-8")
ENGINE_SRC = AGENT_SRC + "\n" + DISPATCH_SRC


def test_markers_are_imported_via_the_facade():
    import vaf

    assert "markers" in vaf.__all__
    assert vaf.markers is markers
    assert set(markers.__all__) == {
        "SYSTEM_LOG_ONLY",
        "GENERATION_STOPPED",
        "LOOP_PROTECTION",
        "ASYNC_ACK",
        "TOOL_CONFIRMATION_REQUIRED",
    }


def test_every_marker_literal_exists_in_the_engine_source():
    for name in markers.__all__:
        literal = getattr(markers, name)
        assert isinstance(literal, str) and literal
        assert literal in ENGINE_SRC, (
            f"markers.{name} = {literal!r} no longer appears in the engine source "
            "(vaf/core/agent.py + vaf/core/tool_dispatch.py) - the engine string was "
            "renamed; update vaf/markers.py (public constant, announce in CHANGELOG) or "
            "restore the engine literal."
        )


def test_the_confirmation_marker_is_checked_where_it_is_actually_returned():
    """Targeted, because the general check above went green for the wrong reason once.

    ``TOOL_CONFIRMATION_REQUIRED`` is what an embedder matches on to tell "refused, needs a
    human" apart from a real answer. The string it must match is the one RETURNED by the
    gate, not the wording of the terminal prompt - and after the gate moved out of agent.py
    those two lived in different files while reading identically. Anchoring on the returning
    module keeps the guard honest as the pipeline keeps moving."""
    assert markers.TOOL_CONFIRMATION_REQUIRED in DISPATCH_SRC, (
        "the confirmation gate no longer returns the public marker string from "
        "vaf/core/tool_dispatch.py - every embedder's refusal handling matches on it"
    )
    assert "return (f\"[ERROR] Tool" in DISPATCH_SRC, (
        "the refusal is no longer a returned string; embedders are promised a string "
        "rather than an exception (docs/EMBEDDING.md, 'Gated tools never hang or raise')"
    )


def test_marker_values_are_pinned():
    # The values themselves are public API: changing one is a breaking change.
    assert markers.SYSTEM_LOG_ONLY == "[SYSTEM_LOG_ONLY]"
    assert markers.GENERATION_STOPPED == "[Generation stopped by user]"
    assert markers.LOOP_PROTECTION == "[LOOP_PROTECTION]"
    assert markers.ASYNC_ACK == "[ASYNC_ACK]"
    assert markers.TOOL_CONFIRMATION_REQUIRED == "requires confirmation"
