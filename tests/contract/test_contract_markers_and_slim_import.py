# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Contract: `vaf.markers` values and the cheap slim import.

Pins docs/EMBEDDING.md "Special return values: vaf.markers" and the
"import vaf stays cheap" promise. Marker VALUES are frozen public API: the
engine communicates handled failures as strings in the normal return
channel, and embedder code compares against these constants - changing a
value silently breaks every such comparison, so a change here is a
breaking change by definition.
"""
import os
import subprocess
import sys
import textwrap

import vaf


# Name -> value, both frozen. The values are what embedder return-channel
# checks (startswith / substring) actually match against.
FROZEN_MARKERS = {
    "SYSTEM_LOG_ONLY": "[SYSTEM_LOG_ONLY]",
    "GENERATION_STOPPED": "[Generation stopped by user]",
    "LOOP_PROTECTION": "[LOOP_PROTECTION]",
    "ASYNC_ACK": "[ASYNC_ACK]",
    "TOOL_CONFIRMATION_REQUIRED": "requires confirmation",
}


def test_the_five_marker_constants_carry_their_frozen_values():
    """The documented usage is a direct string comparison against the
    engine's return channel; the exact literal is therefore the contract,
    not an implementation detail."""
    for name, value in FROZEN_MARKERS.items():
        assert getattr(vaf.markers, name) == value, f"markers.{name} changed value"


def test_markers_all_is_exactly_the_five_documented_names():
    """A dropped name breaks `from vaf.markers import X`; an added one is a
    new promise that needs its own pin and EMBEDDING.md section first."""
    assert set(vaf.markers.__all__) == set(FROZEN_MARKERS)
    assert len(vaf.markers.__all__) == len(FROZEN_MARKERS)


def test_markers_is_reachable_through_the_facade():
    """Documented import style is `from vaf import markers`: the name must
    be in __all__ and the facade attribute must be the real module, so both
    access paths can never drift apart."""
    assert "markers" in vaf.__all__
    assert vaf.markers.SYSTEM_LOG_ONLY == "[SYSTEM_LOG_ONLY]"
    import vaf.markers as markers_module  # the documented public submodule itself

    assert vaf.markers is markers_module


def test_slim_import_stays_cheap_and_agent_loads_lazily(tmp_path):
    """EMBEDDING.md promises `import vaf` does not load the engine: version
    and markers are usable on the slim base, and only first access to
    `vaf.Agent` pays for the core. A fresh subprocess is the only honest
    probe (this test process imported vaf long ago), and cwd=tmp_path
    ensures no vaf/ source tree shadows the installed package under test.
    """
    script = textwrap.dedent(
        """
        import sys

        import vaf

        assert "vaf.core.agent" not in sys.modules, "import vaf loaded the engine"
        version = vaf.__version__

        import vaf.markers

        assert "vaf.core.agent" not in sys.modules, "vaf.markers loaded the engine"
        assert vaf.markers.SYSTEM_LOG_ONLY == "[SYSTEM_LOG_ONLY]"

        from vaf import Agent

        assert "vaf.core.agent" in sys.modules, "vaf.Agent did not load the engine"
        print(version)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"slim-import probe failed:\n{result.stderr}"
    # The probe prints the version it saw on the slim base; it must be the
    # same string this process got, proving __version__ needs no engine.
    assert vaf.__version__ in result.stdout
