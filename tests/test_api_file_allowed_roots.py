# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Which roots `GET /api/file` will serve from, and which it must never.

This endpoint is the only access decision behind two tools that make none of their own:
`document_editor` hands it a path, and the Web UI fetches through it. That is a deliberate
design - a tool that never reads a file should not carry a second answer to "may this be
read", and the viewer next door was a bug precisely because it had two answers and honoured
neither. But it means the safety of those tools lives HERE, in a file nobody editing them
would think to open.

So the allowlist is pinned, and pinned by NAME rather than by count: `Platform.home()` is the
entry that would turn `document_editor` from harmless into a leak, because a path like
`~/.ssh/id_rsa` is refused today only by not being under any of the four roots.

The count matters too, and it is easy to get wrong: a first reading of this endpoint saw
THREE roots because the docstring one line above lists three. There are four - the VAF output
directory is not mentioned in it. A test written against that reading would have been born
wrong.
"""
import inspect
import re
from pathlib import Path

import pytest

EXPECTED_ROOTS = (
    "Platform.documents_dir()",
    "Platform.downloads_dir()",
    "Platform.data_dir()",
    "Platform.get_vaf_output_dir()",
)

# Anything here would make an ordinary home path fetchable, which is what the two path-passing
# tools rely on NOT being the case.
FORBIDDEN_ROOTS = ("Platform.home()", "Path.home()", "expanduser", "vaf_dir()")


def _decision_source() -> str:
    """The one read decision (`_allowed_file_path`) as source. /api/file and the two
    converters under it all call it, so its roots are the roots of all three (pinned below)."""
    import vaf.core.web_server as ws

    src = Path(inspect.getfile(ws)).read_bytes().decode()
    start = src.index("def _allowed_file_path(")
    return src[start:start + 3000].split("\n@app.", 1)[0]


def _allowlist_source() -> str:
    """The `allowed_roots` literal from the decision, as source."""
    block = re.search(r"allowed_roots\s*=\s*\[(.*?)\]", _decision_source(), re.S)
    assert block, "_allowed_file_path no longer builds an allowed_roots list"
    return block.group(1)


def test_every_read_route_asks_the_one_decision():
    """/api/file, /api/file/as-html and /api/file/docx-model: the converters used to check the
    roots only, so any account read another's project files through them."""
    import vaf.core.web_server as ws

    src = Path(inspect.getfile(ws)).read_bytes().decode()
    for route in ('@app.get("/api/file")', '@app.get("/api/file/as-html")', '@app.get("/api/file/docx-model")'):
        body = src[src.index(route):]
        body = body[:body.index("\n@app.", 1)]
        assert "_allowed_file_path(path, request)" in body, route
    assert '@app.get("/api/download")' not in src, (
        "/api/download is back: it served .html/.svg inline on the app's origin and checked no owner")


def test_the_allowlist_is_exactly_these_four_roots():
    """Four, not three. A new entry is a security decision and belongs in a diff."""
    found = re.findall(r"(Platform\.\w+\(\))", _allowlist_source())
    assert tuple(found) == EXPECTED_ROOTS, (
        "the roots /api/file serves from changed. Each one is fetchable by anything that can "
        f"hand the Web UI a path, so this is a security decision: {found}"
    )


@pytest.mark.parametrize("forbidden", FORBIDDEN_ROOTS)
def test_the_home_directory_is_not_a_root(forbidden):
    """THE entry that must never appear. `document_editor` passes an arbitrary path without
    checking it, on the grounds that this endpoint refuses anything outside the four roots.
    Add the home directory and that reasoning silently stops holding - the tool would happily
    hand over `~/.ssh/id_rsa` and the endpoint would serve it."""
    assert forbidden not in _allowlist_source(), (
        f"{forbidden} became an allowed root; document_editor's lack of its own check was "
        "justified by exactly this not being the case"
    )


def test_a_path_outside_every_root_is_refused():
    """The rule itself, not its spelling: the check is `is_relative_to` against the four,
    with a 403 otherwise."""
    src = _allowlist_source()
    body = _decision_source()
    assert "is_relative_to" in body, "the containment check changed shape"
    assert "403" in body, "a path outside every root no longer yields 403"
    assert src.count("Platform.") == 4


def test_project_ownership_is_checked_on_top_of_the_roots():
    """`VAF_Projects/<uid8>` lives UNDER documents, so the allowlist alone would let any
    tenant fetch another's generated files. The second check is what prevents that, and it
    is separate from the first."""
    import vaf.core.web_server as ws

    whole = Path(inspect.getfile(ws)).read_bytes().decode()
    body = _decision_source()
    # What is pinned is that a SECOND refusal exists after the roots, and that the function
    # it asks consults an identity about the project folder.
    assert body.count("403") >= 2, "the ownership refusal disappeared; only the roots remain"
    assert "_project_path_allowed(request, target)" in body, "the ownership check is not asked"
    gate = whole[whole.index("def _project_path_allowed("):]
    gate = gate[:gate.index("\ndef ", 1)]
    # Not a substring check on the folder name: "VAF_Projects_DISABLED" contains
    # "VAF_Projects", so renaming the guard out of existence would pass.
    assert re.search(r'VAF_Projects["\'/\s)]', gate), "the project-folder guard is gone"
    assert re.search(r"user_scope_id|is_admin_identity", gate), (
        "the ownership check no longer consults an identity"
    )


def test_the_two_path_passing_tools_still_carry_no_check_of_their_own():
    """The other half of the arrangement, stated so it cannot drift silently in either
    direction. `document_editor` deliberately has no check because this endpoint has one; if
    someone adds one there, this test should be updated deliberately rather than left as a
    stale justification in a comment."""
    import vaf.tools.document_viewer as dv

    src = Path(inspect.getfile(dv)).read_bytes().decode()
    editor = src[src.index("class DocumentEditorTool"):]
    # Cut at the next class DEFINITION, at the start of a line. Searching for the bare word
    # finds it inside the class's own prose first and slices the body away - which made this
    # test report a missing comment that was there.
    nxt = editor.find("\nclass ", 1)
    editor = editor[:nxt] if nxt != -1 else editor
    assert "is_safe_path" not in editor, (
        "document_editor gained its own check - good, but then the comment pointing at "
        "/api/file is now a second answer to the same question; reconcile them"
    )
    # A bare "/api/file" would pass on the unrelated mention in the honest return message a
    # few lines below. Pinned on the justification itself.
    assert "NO access check here" in editor and "deliberate" in editor, (
        "the comment explaining WHY document_editor carries no check is gone - without it the "
        "next reader sees an unchecked path-passing tool and either adds a redundant check or "
        "assumes one exists somewhere"
    )
