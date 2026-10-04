# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The web file routes hold an account without admin rights to the rule its agent tools obey.

The routes served and stored files under four roots - the machine owner's Documents and
Downloads, VAF's data directory and the output directory - for every signed-in account. The
data directory holds every account's stores, and the save routes asked no identity at all.
An account's tools were confined to its own project tree the whole time, so the same account
was refused a file by its agent and handed it by the web UI. One decision now,
`_allowed_file_path`, and for a non-admin account it asks `vaf.jail_allows`: the account's
own tree, its rooms' shared folders and, for reading, its visible skills. An admin keeps
the four roots.
"""
import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import Request

TENANT = "ab12cd34-0000-0000-0000-000000000000"


def _request(scope, role="user", path="/api/file"):
    req = Request({"type": "http", "method": "GET", "path": path, "headers": []})
    if scope is not None:
        req.state.user = {"user_id": "7", "username": "alice", "role": role, "user_scope_id": scope}
    return req


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A scratch home with the paths the cases need. Both home variables: Path.home() reads
    USERPROFILE on Windows and HOME elsewhere."""
    import vaf.tools.filesystem as fs
    from vaf.core.platform import Platform

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    fs._shared_room_roots_cache.clear()
    projects = tmp_path / "Documents" / "VAF_Projects"
    paths = {
        "own": projects / "ab12cd34" / "chat1" / "notes.md",
        "other": projects / "ffff0000" / "chat9" / "report.md",
        "room": projects / "ffff0000" / "room-folder" / "shared.md",
        "owner_docs": tmp_path / "Documents" / "taxes.md",
        "downloads": tmp_path / "Downloads" / "invoice.md",
        "data_dir": Platform.data_dir() / "contacts.json",
    }
    for p in paths.values():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    return paths


def _refused(fn, *a, **k):
    with pytest.raises(HTTPException) as refused:
        fn(*a, **k)
    return refused.value.status_code


# -- reading -------------------------------------------------------------------------------


def test_a_non_admin_account_reads_only_what_its_tools_may_read(tree):
    """MUTATION: drop the jail question from _allowed_file_path and the owner's documents,
    downloads and the data directory are served to every account again."""
    from vaf.core.web_server import _allowed_file_path
    req = _request(TENANT)
    assert _allowed_file_path(str(tree["own"]), req) == tree["own"].resolve()
    for name in ("other", "owner_docs", "downloads", "data_dir"):
        assert _refused(_allowed_file_path, str(tree[name]), req) == 403, name


def test_a_member_reads_the_shared_folder_of_its_room(tree, monkeypatch):
    import vaf.tools.filesystem as fs
    from vaf.core.web_server import _allowed_file_path
    monkeypatch.setattr(fs, "_shared_room_roots", lambda scope: [str(tree["room"].parent)])
    assert _allowed_file_path(str(tree["room"]), _request(TENANT)) == tree["room"].resolve()


def test_a_refusal_does_not_say_whether_the_file_exists(tree):
    """MUTATION: check existence first again and a missing file answers 404 where an existing
    one answers 403 - any account could map the owner's disk one name at a time."""
    from vaf.core.web_server import _allowed_file_path
    req = _request(TENANT)
    missing_elsewhere = tree["owner_docs"].with_name("does-not-exist.md")
    assert _refused(_allowed_file_path, str(missing_elsewhere), req) == 403
    missing_own = tree["own"].with_name("does-not-exist.md")
    assert _refused(_allowed_file_path, str(missing_own), req) == 404


def test_an_admin_and_the_tokenless_desktop_keep_the_four_roots(tree):
    from vaf.core.web_server import _allowed_file_path
    for req in (_request(TENANT, role="admin"), _request(None)):
        for name in ("owner_docs", "downloads", "data_dir", "other"):
            assert _allowed_file_path(str(tree[name]), req) == tree[name].resolve(), name


def test_an_account_without_a_scope_is_refused(tree):
    """A token without the scope claim is not the local admin here. The shared identity
    helper fills a missing scope with the local admin's, which would make such a token an
    admin; the route reads the identity as it was authenticated instead."""
    from vaf.core.web_server import _allowed_file_path
    assert _refused(_allowed_file_path, str(tree["owner_docs"]), _request("")) == 403


def test_the_image_description_asks_the_same_decision(tree):
    """It used to look for the file before it asked, like the file route did."""
    from vaf.core import web_server as ws

    class _Req:
        state = _request(TENANT).state

        async def json(self):
            return {"sessionId": "chat1", "path": str(tree["owner_docs"].with_name("nope.png"))}

    with pytest.raises(HTTPException) as refused:
        asyncio.run(ws.describe_image(_Req()))
    assert refused.value.status_code == 403


# -- writing -------------------------------------------------------------------------------


def test_a_non_admin_account_saves_only_into_its_own_tree(tree):
    """MUTATION: let save_file build its own roots list again (no identity) and a tenant writes
    into the data directory, where custom_tools/*.py is loaded as code."""
    from vaf.core import web_server as ws
    from vaf.core.platform import Platform
    req = _request(TENANT, path="/api/file/save")
    tool = Platform.data_dir() / "custom_tools" / "planted.py"
    for target in (tool, tree["other"], tree["owner_docs"]):
        with pytest.raises(HTTPException) as refused:
            asyncio.run(ws.save_file(ws.FileSaveRequest(path=str(target), content="y"), req))
        assert refused.value.status_code == 403, target
    assert not tool.exists()
    assert tree["other"].read_text(encoding="utf-8") == "x"

    fresh = tree["own"].with_name("fresh.md")
    asyncio.run(ws.save_file(ws.FileSaveRequest(path=str(fresh), content="y"), req))
    assert fresh.read_text(encoding="utf-8") == "y"


def test_an_admin_still_saves_under_the_four_roots(tree):
    from vaf.core import web_server as ws
    target = tree["owner_docs"].with_name("admin-note.md")
    asyncio.run(ws.save_file(ws.FileSaveRequest(path=str(target), content="z"),
                             _request(TENANT, role="admin", path="/api/file/save")))
    assert target.read_text(encoding="utf-8") == "z"


@pytest.mark.parametrize("route,body", [
    ("save_file_as_docx_native", lambda p: {"path": p + ".docx", "document": {}}),
    ("save_file_as_xlsx", lambda p: {"path": p + ".xlsx", "content": "<table></table>"}),
    ("save_file_as_pptx", lambda p: {"path": p + ".pptx", "content": "<h2>x</h2>"}),
])
def test_every_office_save_route_refuses_before_it_writes(tree, route, body):
    """Refused before the office library is even imported, so this holds without it."""
    from vaf.core import web_server as ws
    fn = getattr(ws, route)
    model = fn.__annotations__["body"]
    model = getattr(ws, model) if isinstance(model, str) else model
    target = str(tree["owner_docs"].with_name("office"))
    with pytest.raises(HTTPException) as refused:
        asyncio.run(fn(model(**body(target)), _request(TENANT, path="/api/file/save")))
    assert refused.value.status_code == 403


# -- the internal event lane ----------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/workflow/update", "/api/subagent/stream"])
def test_the_internal_event_routes_are_for_the_local_process_only(path):
    """Sub-agent processes and `vaf workflow` post here over loopback without a token, which is
    the local admin. An account's token reached them too: the viewer branch read any path into
    a session and file_created repointed a session's project folder, for any session."""
    from vaf.core import web_server as ws
    route = next(r for r in ws.app.routes if getattr(r, "path", "") == path)
    assert any(d.call is ws._require_admin_caller for d in route.dependant.dependencies), path


def test_the_internal_event_gate_admits_the_local_process_and_refuses_an_account():
    from vaf.core.web_server import _require_admin_caller
    _require_admin_caller(_request(None))                       # tokenless loopback: local admin
    _require_admin_caller(_request(TENANT, role="admin"))
    assert _refused(_require_admin_caller, _request(TENANT)) == 403


# -- the editor draft ------------------------------------------------------------------------


def test_the_editor_draft_lands_where_the_account_may_read_and_save_it(tree, monkeypatch):
    """It was written to the data directory so the file routes could reach it - which only
    worked because they reached everything. Now in the chat's workspace, in a folder the
    workspace window lists: no path component is hidden, so it stays findable after the chat
    is deleted too."""
    import vaf.core.web_interface as wi
    from vaf.core.headless_runner import _maybe_open_draft_in_editor
    from vaf.tools.filesystem import jail_allows
    opened = []
    monkeypatch.setattr(wi, "notify_document_created", lambda sid, path, title=None, **k: opened.append(path))
    _maybe_open_draft_in_editor("chat1", "Schreib mir einen Text", "Lorem ipsum. " * 30, "web",
                                user_scope_id=TENANT)
    assert len(opened) == 1
    draft = opened[0]
    assert draft.replace("\\", "/").endswith("VAF_Projects/ab12cd34/chat1/drafts/entwurf.md")
    from pathlib import Path
    workspace = tree["own"].parents[1] / "chat1"
    assert not any(part.startswith(".") for part in Path(draft).resolve().relative_to(workspace.resolve()).parts)
    for mode in ("read", "write"):
        assert jail_allows(draft, user_scope_id=TENANT, user_role="user", mode=mode)


# -- the desktop bridge ----------------------------------------------------------------------


def test_the_desktop_save_bridge_refuses_when_it_cannot_check(tree, monkeypatch):
    """It fell through to the save dialog when the roots could not be resolved."""
    from vaf.core import desktop_window as dw
    from vaf.core.platform import Platform

    asked = []

    class _Window:
        def create_file_dialog(self, *a, **k):
            asked.append(a)
            return None

    def boom():
        raise RuntimeError("no roots")

    monkeypatch.setattr(dw, "_window", _Window())
    monkeypatch.setattr(dw, "_webview", type("W", (), {"SAVE_DIALOG": 1}))
    monkeypatch.setattr(Platform, "served_file_roots", staticmethod(boom))
    assert dw.save_file_as(str(tree["owner_docs"])) == {"ok": False, "error": "forbidden"}
    assert asked == []


# -- the model folder ------------------------------------------------------------------------


def test_downloading_a_model_is_an_admin_decision():
    """The WebSocket command filled the machine's model folder from any repository a connection
    named, with no role check. MUTATION: drop the gate and the source check below fails."""
    import inspect

    from vaf.core import web_server as ws
    from vaf.core.web_interface import WebInterfaceManager

    src = inspect.getsource(ws)
    branch = src[src.index('elif type == "download_model":'):]
    branch = branch[:branch.index('elif type == "cancel_model_download":')]
    assert branch.index("manager.connection_is_admin(websocket)") < branch.index("elif not repo_id:")

    mgr = WebInterfaceManager()
    user, admin = object(), object()
    try:
        mgr.set_connection_user(user, TENANT, "alice", "user")
        mgr.set_connection_user(admin, TENANT, "bob", "admin")
        assert mgr.connection_is_admin(user) is False
        assert mgr.connection_is_admin(admin) is True
    finally:
        for key in (user, admin):
            mgr.connection_users.pop(key, None)
            mgr.connection_usernames.pop(key, None)
            mgr.connection_roles.pop(key, None)
