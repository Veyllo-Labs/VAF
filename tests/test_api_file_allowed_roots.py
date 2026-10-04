# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Which roots the web file routes serve from and save into, and which they must never.

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
THREE roots because its docstring listed three. There are four - the VAF output directory was
not mentioned in it. A test written against that reading would have been born wrong.

The four are the whole answer for an admin only. Every other account is held to its own file
jail on top of them (pinned below, behaviour in test_file_routes_account_jail.py), and the
list is spelled once, in `Platform.served_file_roots`: the routes carried four copies of it
and the desktop's save bridge a fifth.
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
    """The one file decision (`_allowed_file_path`) as source, up to the next top-level line.
    Every read and save route calls it, so its roots are the roots of all of them."""
    import vaf.core.web_server as ws

    src = Path(inspect.getfile(ws)).read_bytes().decode()
    body = src[src.index("def _allowed_file_path("):]
    return body[:re.search(r"\n(?=\S)", body[1:]).start() + 1]


def _allowlist_source() -> str:
    """The list `Platform.served_file_roots` returns, as source."""
    import vaf.core.platform as plat

    src = Path(inspect.getfile(plat)).read_bytes().decode()
    fn = src[src.index("def served_file_roots("):]
    fn = fn[:fn.index("@staticmethod")]
    block = re.search(r"return\s*\[(.*?)\]", fn, re.S)
    assert block, "Platform.served_file_roots no longer returns a list literal"
    return block.group(1)


def _route_body(src: str, route: str) -> str:
    body = src[src.index(route):]
    return body[:body.index("\n@app.", 1)]


def test_every_read_route_asks_the_one_decision():
    """/api/file, /api/file/as-html and /api/file/docx-model: the converters used to check the
    roots only, so any account read another's project files through them."""
    import vaf.core.web_server as ws

    src = Path(inspect.getfile(ws)).read_bytes().decode()
    for route in ('@app.get("/api/file")', '@app.get("/api/file/as-html")', '@app.get("/api/file/docx-model")',
                  '@app.post("/api/image/describe")'):
        assert "_allowed_file_path(path, request)" in _route_body(src, route), route
    assert '@app.get("/api/download")' not in src, (
        "/api/download is back: it served .html/.svg inline on the app's origin and checked no owner")


def test_every_save_route_asks_the_one_decision_in_write_mode():
    """The save routes built their own roots list and asked no identity at all: any
    account could overwrite another's files, or write into the data directory, whose
    custom_tools/ folder is loaded as code."""
    import vaf.core.web_server as ws

    src = Path(inspect.getfile(ws)).read_bytes().decode()
    helper = src[src.index("def _allowed_save_path("):]
    helper = helper[:helper.index("\n@app.", 1)]
    assert '_allowed_file_path(path_str, request, mode="write", must_exist=False)' in helper
    for route, needle in (
        ('@app.post("/api/file/save")', '_allowed_file_path(body.path, request, mode="write", must_exist=False)'),
        ('@app.post("/api/file/save-docx-native")', '_allowed_save_path(body.path, ".docx", request)'),
        ('@app.post("/api/file/save-xlsx")', '_allowed_save_path(body.path, ".xlsx", request)'),
        ('@app.post("/api/file/save-pptx")', '_allowed_save_path(body.path, ".pptx", request)'),
    ):
        assert needle in _route_body(src, route), route


def test_the_four_roots_are_spelled_once():
    """One list for every file route and the desktop bridge. A copy of it is how the save
    routes kept serving the four roots to everyone after the read routes stopped."""
    vaf_dir = Path(__file__).resolve().parents[1] / "vaf"
    spelled = [p.relative_to(vaf_dir).as_posix() for p in vaf_dir.rglob("*.py")
               if "get_vaf_output_dir().resolve()" in p.read_bytes().decode("utf-8", "replace")]
    assert spelled == ["core/platform.py"], spelled
    assert "Platform.served_file_roots()" in _decision_source()


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


def test_a_non_admin_account_is_held_to_its_jail_on_top_of_the_roots():
    """The four roots hold every account's data - the owner's documents, other accounts'
    project trees under them, the data directory with every store - so the roots alone are an
    admin's answer. Everyone else gets a SECOND refusal: the file jail its own tools obey. And
    the disk is looked at only after both, or "not found" versus "refused" tells any account
    which files exist."""
    body = _decision_source()
    assert body.count("403") >= 2, "the account refusal disappeared; only the roots remain"
    assert "caller_is_admin(request)" in body, "the admin answer is not the shared definition"
    jail = body.index("jail_allows(")
    assert jail > body.index("served_file_roots()"), "the jail must come on top of the roots"
    assert body.index("is_file()") > jail, "existence is checked before the caller is refused"


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
