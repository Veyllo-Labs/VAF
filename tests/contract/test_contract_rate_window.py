# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The embedder contract for `vaf.RateWindow` (docs/EMBEDDING.md, "Bounding what one person
may send").

A stranger serving several people bounds each of them from the FACADE alone: no Agent, no
config, no room.
"""
import inspect
import os
import subprocess
import sys
import textwrap

import vaf


def test_the_name_resolves_from_the_facade():
    from vaf import RateWindow
    import vaf.core.rate_window as rw  # the re-export source named in EMBEDDING.md
    assert RateWindow is rw.RateWindow
    assert "RateWindow" in vaf.__all__


def test_the_documented_signatures():
    from vaf import RateWindow
    init = inspect.signature(RateWindow.__init__)
    assert list(init.parameters)[:3] == ["self", "window_s", "limit"]
    assert init.parameters["limit"].default is None
    assert init.parameters["clock"].kind is inspect.Parameter.KEYWORD_ONLY
    assert init.parameters["max_keys"].kind is inspect.Parameter.KEYWORD_ONLY
    take = inspect.signature(RateWindow.take)
    assert list(take.parameters)[:3] == ["self", "key", "weight"]
    assert take.parameters["weight"].default == 1
    for name in ("limit", "now"):
        assert take.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
    assert list(inspect.signature(RateWindow.refund).parameters) == ["self", "key", "weight"]
    for method in ("used", "forget", "clear"):
        assert callable(getattr(RateWindow, method))


def test_import_vaf_does_not_load_the_window(tmp_path):
    """Lazy like every facade name. MUTATION: import rate_window eagerly in vaf/__init__.py -
    red."""
    script = textwrap.dedent(
        """
        import sys
        import vaf
        assert "vaf.core.rate_window" not in sys.modules, "import vaf loaded the window"
        from vaf import RateWindow
        assert "vaf.core.rate_window" in sys.modules
        """
    )
    result = subprocess.run([sys.executable, "-c", script], cwd=str(tmp_path),
                            env=dict(os.environ), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def test_a_stranger_bounds_each_person():
    """Three lines a minute per person: alice's fourth is refused whole, bob is not touched,
    a line that was charged and then not stored is given back, and the minute passes."""
    clock = {"now": 0.0}
    lines = vaf.RateWindow(60, 3, clock=lambda: clock["now"])
    assert [lines.take("alice") for _ in range(4)] == [True, True, True, False]
    assert lines.take("bob")
    assert lines.refund("alice", 1) and lines.take("alice")
    assert not lines.take("alice")
    clock["now"] = 60.5
    assert lines.take("alice")

    budget = vaf.RateWindow(3600, clock=lambda: clock["now"])
    assert budget.take("alice", 900, limit=1000)
    assert not budget.take("alice", 200, limit=1000), "all or nothing"
    assert budget.used("alice") == 900
