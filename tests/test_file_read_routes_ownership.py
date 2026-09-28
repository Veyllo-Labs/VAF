# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A per-account project file is readable by its account, an admin or a room member - through
EVERY read route, not only the one that happened to carry the check.

GET /api/file checked ``VAF_Projects/<uid8>`` ownership; the two converters under it
(``/as-html``, ``/docx-model``) checked the four roots only, so any account read another's
project documents through them. POST /api/image/describe carried its own copy of the rule and
missed the shared-room exception. And GET /api/download checked no owner at all and served
.html/.svg inline on the app's origin; it had no caller and is gone. One decision now:
``_allowed_file_path`` asks ``_project_path_allowed``.
"""
import pytest
from fastapi import HTTPException
from starlette.requests import Request

OTHER = "ffff0000-0000-0000-0000-000000000000"
OWNER = "ab12cd34-0000-0000-0000-000000000000"


def _request(scope_id: str | None, role: str = "user") -> Request:
    req = Request({"type": "http", "method": "GET", "path": "/api/file/as-html", "headers": []})
    if scope_id is not None:
        req.state.user = {"user_id": "1", "username": "alice", "role": role, "user_scope_id": scope_id}
    return req


@pytest.fixture
def project_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "Documents" / "VAF_Projects" / "ab12cd34" / "report.docx"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"PK")
    return target


def test_another_account_is_refused(project_file):
    from vaf.core.web_server import _allowed_file_path
    with pytest.raises(HTTPException) as refused:
        _allowed_file_path(str(project_file), _request(OTHER))
    assert refused.value.status_code == 403


def test_the_owning_account_and_an_admin_may_read(project_file):
    from vaf.core.web_server import _allowed_file_path
    assert _allowed_file_path(str(project_file), _request(OWNER)) == project_file.resolve()
    assert _allowed_file_path(str(project_file), _request(OTHER, role="admin")) == project_file.resolve()


def test_a_member_of_the_room_whose_folder_it_is_may_read(project_file, monkeypatch):
    """The exception only /api/file knew: a room's shared folder lives in its creator's tree."""
    import vaf.tools.filesystem as fs
    from vaf.core.web_server import _allowed_file_path
    monkeypatch.setattr(fs, "_shared_room_roots", lambda scope: [str(project_file.parent)])
    assert _allowed_file_path(str(project_file), _request(OTHER)) == project_file.resolve()


def test_a_file_outside_every_root_is_refused(tmp_path, monkeypatch):
    from vaf.core.web_server import _allowed_file_path
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x")
    with pytest.raises(HTTPException) as refused:
        _allowed_file_path(str(outside), _request(OWNER))
    assert refused.value.status_code == 403


def test_the_download_route_is_gone():
    from vaf.core.web_server import app
    assert not [r for r in app.routes if getattr(r, "path", "") == "/api/download"]


def test_a_stale_download_link_does_not_strand_the_desktop_window():
    """Links to the removed route survive in old chats. The desktop window pulls localhost
    links into itself, and it has no back button: a file endpoint's bare-JSON answer (here the
    404) must go to the system browser, as it did while the route existed."""
    from vaf.core.desktop_window import _INTERCEPT_JS
    check = _INTERCEPT_JS.split("function _isFileEndpoint(u) {", 1)[1].split("}", 1)[0]
    assert "u.pathname === '/api/file'" in check
    assert "u.pathname === '/api/download'" in check
