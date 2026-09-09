# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The HTML document editor's sheet effect must attach its listeners on every run.

The effect that writes the editable sheet into the iframe re-runs on every content
change, and React runs the previous run's cleanup first, which removes the body's
listeners. A run that only consumes the "this change came from the sheet" flag and
returns early therefore leaves the body without its input listener: from the second
keystroke on, nothing typed reaches the state that Save, Download and the agent
context read (live incident, reproduced with the effect pattern in a real browser).
The fix attaches the listeners after the flag is consumed, on every run. This pins
that shape, because the early return is the natural thing to write back in.
"""
import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "web" / "components" / "DocumentEditor.tsx"


def _sheet_effect():
    src = _SRC.read_text(encoding="utf-8")
    head = "// Update iframe content when content changes from outside"
    tail = "}, [content, isOpen, updateSelectionFormat]);"
    assert head in src and tail in src, "the sheet effect moved; point this guard at its new home"
    return src.split(head, 1)[1].split(tail, 1)[0]


def test_the_input_listener_is_attached_after_the_flag_is_consumed():
    effect = _sheet_effect()
    flag_reset = effect.find("contentFromIframeRef.current = false;")
    listener = effect.find("doc.body.addEventListener('input', captureContent);")
    assert flag_reset != -1 and listener != -1, "the flag reset or the input listener left the sheet effect"
    assert flag_reset < listener, "the flag must be consumed before the listeners are attached, on the same run"
    between = effect[flag_reset:listener]
    # Any return statement, standalone or inline (`if (x) return;`), not only one on its own line.
    assert not re.search(r"\breturn\s*[;}]", between), (
        "an early return between consuming the flag and attaching the input listener "
        "drops the listener after the first keystroke"
    )


def test_the_cleanup_removes_what_every_run_attaches():
    effect = _sheet_effect()
    assert "doc.body.removeEventListener('input', captureContent);" in effect, \
        "the cleanup no longer removes the input listener, so every run would stack one more"
