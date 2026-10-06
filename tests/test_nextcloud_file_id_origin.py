# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Nextcloud sends the account's app password with every request, so a file id that is an
absolute URL may only name the account's own server.

The cloud_storage tool hands the model's `file_id` through unchanged. Before this, any id
starting with "http" became the request URL as it was: `read(file_id="https://other.example/x")`
sent the app password there in the first PROPFIND, and the destination guard let it through,
because it admits every public host."""
from types import SimpleNamespace

import pytest

from vaf.cloud.nextcloud import NextcloudProvider

OWN = "https://cloud.example.org"


class _Session:
    """Records every request; answers a download with two bytes."""

    def __init__(self):
        self.calls = []

    def _record(self, method, url, **kw):
        self.calls.append((method, url, kw.get("auth")))
        return SimpleNamespace(raise_for_status=lambda: None, content=b"",
                               iter_content=lambda chunk_size: [b"ok"])

    def request(self, method, url, **kw):
        return self._record(method, url, **kw)

    def get(self, url, **kw):
        return self._record("GET", url, **kw)

    def delete(self, url, **kw):
        return self._record("DELETE", url, **kw)


@pytest.fixture()
def nc():
    p = NextcloudProvider("alice", "nextcloud_x")
    p._server_url, p._webdav_username, p._password = OWN, "alice", "app-password"
    p._dav_base = f"{OWN}/remote.php/dav/files/alice"
    p._session = _Session()
    return p


@pytest.mark.parametrize("foreign", [
    "https://other.example/x",
    "http://cloud.example.org/remote.php/dav/files/alice/VAF/a.txt",       # another scheme
    "https://cloud.example.org:8443/remote.php/dav/files/alice/VAF/a.txt",  # another port
    "https://cloud.example.org@other.example/x",                           # another host behind a user part
    "https://cloud.example.org.other.example/x",
])
def test_an_absolute_id_on_another_server_never_gets_a_request(nc, tmp_path, foreign):
    """MUTATION: use an id that starts with "http" as the URL again."""
    with pytest.raises(ValueError, match="not on this Nextcloud server"):
        nc.get_file_metadata(foreign)
    with pytest.raises(ValueError):
        nc.download_file(foreign, tmp_path / "out.bin")
    with pytest.raises(ValueError):
        nc.delete_file(foreign)
    assert nc._session.calls == []


def test_an_absolute_id_on_the_own_server_and_a_relative_one_still_work(nc, tmp_path):
    own = f"{OWN}/remote.php/dav/files/alice/VAF/a.txt"
    nc.download_file(own, tmp_path / "a.bin")
    nc.download_file("http-notes.txt", tmp_path / "b.bin")   # a name, not a scheme
    assert [c[1] for c in nc._session.calls] == [
        own, f"{OWN}/remote.php/dav/files/alice/VAF Sync/http-notes.txt"]


def test_the_tool_answers_with_the_refusal_and_sends_nothing(nc, monkeypatch):
    import vaf.tools.cloud_storage as cs
    monkeypatch.setattr(nc, "authenticate", lambda: True)
    monkeypatch.setattr(cs, "create_cloud_provider", lambda *a, **k: nc)
    out = cs._action_read("alice", "nextcloud_x", "nextcloud", "https://other.example/x")
    assert "not on this Nextcloud server" in out
    assert nc._session.calls == []
