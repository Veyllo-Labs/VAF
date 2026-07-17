# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Rule-2 guard for vaf/markers.py: the public constants must stay in sync
with the literals the engine actually emits. Renaming a string in agent.py
without updating the marker (or vice versa) fails here instead of silently
breaking every embedder's error handling."""
from pathlib import Path

from vaf import markers

AGENT_SRC = (Path(__file__).resolve().parents[1] / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")


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
        assert literal in AGENT_SRC, (
            f"markers.{name} = {literal!r} no longer appears in vaf/core/agent.py - "
            "the engine string was renamed; update vaf/markers.py (public constant, "
            "announce in CHANGELOG) or restore the engine literal."
        )


def test_marker_values_are_pinned():
    # The values themselves are public API: changing one is a breaking change.
    assert markers.SYSTEM_LOG_ONLY == "[SYSTEM_LOG_ONLY]"
    assert markers.GENERATION_STOPPED == "[Generation stopped by user]"
    assert markers.LOOP_PROTECTION == "[LOOP_PROTECTION]"
    assert markers.ASYNC_ACK == "[ASYNC_ACK]"
    assert markers.TOOL_CONFIRMATION_REQUIRED == "requires confirmation"
