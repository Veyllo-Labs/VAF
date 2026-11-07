# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""iCloud Drive is the machine owner's alone, and only "VAF Sync" inside it.

The provider reads the iCloud folder of the OS user that runs VAF, with no credential in
between, and its file ids are absolute paths. So two things must hold, and each is asserted
here on its own: an instance exists only for the local admin (name AND scope), and every id
and path it is handed stays inside "VAF Sync" on its RESOLVED path.

macOS is faked at the provider's own platform check (`vaf.cloud.icloud.sys`): setting
`sys.platform` globally breaks urllib's lazy import on Linux. Every folder lies under
tmp_path; the identities are synthetic.
"""
import hashlib
import os
import types
from pathlib import Path

import pytest

from vaf.core.path_jail import PathEscape

OWNER = "owner-account"
OWNER_SCOPE = "ab12cd34-0000-4000-8000-0000000000aa"
TENANT = "tenant-account"
TENANT_SCOPE = "ab12cd34-0000-4000-8000-000000000001"
SECOND_ADMIN = "second-admin"
SECOND_ADMIN_SCOPE = "ab12cd34-0000-4000-8000-000000000002"

SECRET = b"TOPSECRET"
OWNER_DRIVE = b"OWNER-ICLOUD"
IN_ROOT = b"IN-ROOT-CONTENT"


def _link(link: Path, target: Path) -> Path:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create symlinks")
    return link


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """A fake iCloud Drive with "VAF Sync", a file next to it, a file outside the iCloud
    tree, and the owner's identity through the real config accessor."""
    from vaf.cloud import icloud
    from vaf.core.config import Config

    home = tmp_path / "home"
    home.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    for var in ("XDG_DATA_HOME", "LOCALAPPDATA"):
        monkeypatch.setenv(var, str(tmp_path / "xdg-data"))
    for var in ("XDG_CONFIG_HOME", "APPDATA"):
        monkeypatch.setenv(var, str(tmp_path / "xdg-config"))

    base = tmp_path / "iCloud"
    root = base / "VAF Sync"
    root.mkdir(parents=True)
    (root / "in.txt").write_bytes(IN_ROOT)
    (base / "owner.txt").write_bytes(OWNER_DRIVE)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(SECRET)

    monkeypatch.setattr(icloud, "sys", types.SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(icloud, "ICLOUD_BASE", base)

    store = {
        "local_admin_username": OWNER,
        "local_admin_scope_id": OWNER_SCOPE,
        "cloud_config": {"accounts": []},
        "cloud_config_by_user": {},
    }
    real = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: store[key] if key in store else real(key, default)))
    return types.SimpleNamespace(tmp=tmp_path, home=home, base=base, root=root,
                                 outside=outside, store=store, icloud=icloud)


def _owner(lab):
    prov = lab.icloud.ICloudProvider(OWNER, "icloud", user_scope_id=OWNER_SCOPE)
    assert prov.authenticate()
    return prov


def _snapshot(folder: Path) -> dict:
    return {p.relative_to(folder).as_posix(): (p.read_bytes() if p.is_file() else None)
            for p in sorted(folder.rglob("*"))}


# -- AC1: an id outside "VAF Sync" is refused --------------------------------------------

def _outside_id(lab, case):
    if case == "absolute, outside the iCloud tree":
        return str(lab.outside / "secret.txt"), SECRET
    if case == "the owner's iCloud Drive next to VAF Sync":
        return str(lab.base / "owner.txt"), OWNER_DRIVE
    if case == "walks up out of VAF Sync":
        return str(lab.root) + "/../owner.txt", OWNER_DRIVE
    if case == "a link inside VAF Sync that points out":
        _link(lab.root / "link.txt", lab.outside / "secret.txt")
        return str(lab.root / "link.txt"), SECRET
    if case == "a different case of VAF Sync":
        variant = lab.base / "vaf sync"
        variant.mkdir(exist_ok=True)
        if not (variant / "in.txt").exists():
            (variant / "in.txt").write_bytes(IN_ROOT)
        return str(variant / "in.txt"), IN_ROOT
    raise AssertionError(case)


@pytest.mark.parametrize("case", [
    "absolute, outside the iCloud tree",
    "the owner's iCloud Drive next to VAF Sync",
    "walks up out of VAF Sync",
    "a link inside VAF Sync that points out",
    "a different case of VAF Sync",
])
def test_ids_outside_vaf_sync_are_refused(lab, case):
    """MUTATION: get_file_metadata back to `Path(file_id)` - red (outside metadata).
    MUTATION: `_in_root` without its contained_path call (lexical only) - the link and the
    walk-up cases red."""
    prov = _owner(lab)
    file_id, body = _outside_id(lab, case)
    assert prov.get_file_metadata(file_id) is None
    dest = lab.tmp / "dest.bin"
    with pytest.raises(PathEscape):
        prov.download_file(file_id, dest)
    assert not dest.exists()


def test_in_root_id_still_works(lab):
    prov = _owner(lab)
    listed = {m.name: m for m in prov.list_files("/")}
    meta = listed["in.txt"]
    assert meta.path == "/in.txt"
    again = prov.get_file_metadata(meta.file_id)
    assert again is not None and again.file_id == meta.file_id
    dest = lab.tmp / "copy.txt"
    assert prov.download_file(meta.file_id, dest) == dest
    assert dest.read_bytes() == IN_ROOT


# -- AC2: the resolved spelling of the root ----------------------------------------------

def test_resolved_spelling_of_the_root_is_accepted(lab, monkeypatch):
    """A home under a linked parent (/var -> /private/var on macOS): an id spelled through
    the resolved root names the same file. MUTATION: compare only against the unresolved
    root in `_relative` - red."""
    real = lab.tmp / "real-icloud"
    (real / "VAF Sync").mkdir(parents=True)
    (real / "VAF Sync" / "in.txt").write_bytes(IN_ROOT)
    linked = _link(lab.tmp / "linked-icloud", real)
    monkeypatch.setattr(lab.icloud, "ICLOUD_BASE", linked)
    prov = _owner(lab)
    resolved_id = str(real.resolve() / "VAF Sync" / "in.txt")
    meta = prov.get_file_metadata(resolved_id)
    assert meta is not None and meta.path == "/in.txt"
    assert meta.file_id == str(linked / "VAF Sync" / "in.txt")
    dest = lab.tmp / "copy.txt"
    prov.download_file(resolved_id, dest)
    assert dest.read_bytes() == IN_ROOT


# -- AC3: delete -------------------------------------------------------------------------

def test_delete_never_leaves_the_root(lab):
    """MUTATION: back to the lexical `relative_to` check - red (the owner's file next to
    VAF Sync is deleted)."""
    prov = _owner(lab)
    assert prov.delete_file(str(lab.root) + "/../owner.txt") is False
    assert (lab.base / "owner.txt").read_bytes() == OWNER_DRIVE
    assert prov.delete_file(str(lab.root)) is False
    assert lab.root.is_dir()
    assert prov.delete_file(str(lab.root / "in.txt")) is True
    assert not (lab.root / "in.txt").exists()
    _link(lab.root / "link.txt", lab.outside / "secret.txt")
    assert prov.delete_file(str(lab.root / "link.txt")) is False
    assert (lab.outside / "secret.txt").read_bytes() == SECRET


# -- AC4: upload -------------------------------------------------------------------------

def test_upload_destination_stays_inside(lab):
    """MUTATION: back to `sync_root / remote_path.strip("/")` - red (`../x` lands next to
    VAF Sync)."""
    prov = _owner(lab)
    src = lab.tmp / "local.txt"
    src.write_bytes(b"local body")
    before = _snapshot(lab.base)
    for remote in ("../x.txt", str(lab.tmp / "abs.txt"), "", "/"):
        with pytest.raises(PathEscape):
            prov.upload_file(src, remote)
    assert _snapshot(lab.base) == before
    assert not (lab.tmp / "abs.txt").exists()
    assert prov.upload_file(src, "plain.txt").path == "/plain.txt"
    assert prov.upload_file(src, "sub/name.txt").path == "/sub/name.txt"
    assert (lab.root / "sub" / "name.txt").read_bytes() == b"local body"


# -- AC5: list ---------------------------------------------------------------------------

def test_listing_hides_links_out_and_refuses_walking_up(lab):
    """MUTATION: drop the child-escape skip in list_files - red (the outside file's hash is
    listed)."""
    prov = _owner(lab)
    _link(lab.root / "link.txt", lab.outside / "secret.txt")
    with pytest.raises(PathEscape):
        prov.list_files("../")
    listed = prov.list_files("/")
    assert {m.name for m in listed} == {"in.txt"}
    secret_hash = hashlib.sha256(SECRET).hexdigest()
    assert all(m.content_hash != secret_hash for m in listed)


@pytest.mark.skipif(os.name == "nt", reason="Windows drops a trailing space from a name")
def test_a_name_with_trailing_whitespace_is_checked_as_it_is_opened(lab):
    """`contained_path` trims what it checks: "in.txt " (a link out) was checked as the
    in-root "in.txt" and then opened as itself. MUTATION: drop the whitespace refusal in
    `_in_root` - red (the outside content is read and its hash listed)."""
    prov = _owner(lab)
    _link(lab.root / "in.txt ", lab.outside / "secret.txt")
    spaced = str(lab.root / "in.txt ")
    assert prov.get_file_metadata(spaced) is None
    dest = lab.tmp / "dest.bin"
    with pytest.raises(PathEscape):
        prov.download_file(spaced, dest)
    assert not dest.exists()
    listed = prov.list_files("/")
    assert {m.name for m in listed} == {"in.txt"}
    secret_hash = hashlib.sha256(SECRET).hexdigest()
    assert all(m.content_hash != secret_hash for m in listed)
    assert prov.delete_file(spaced) is False
    assert (lab.root / "in.txt ").is_symlink()


# -- AC6: who gets a provider ------------------------------------------------------------

@pytest.mark.parametrize("username,scope", [
    (TENANT, TENANT_SCOPE),
    (TENANT, None),
    (OWNER, TENANT_SCOPE),
    (SECOND_ADMIN, SECOND_ADMIN_SCOPE),
    ("", None),
])
def test_only_the_machine_owner_gets_an_icloud_provider(lab, username, scope):
    """MUTATION: drop the name half of `_is_machine_owner` - (TENANT, None) red.
    MUTATION: drop the scope half - (OWNER, TENANT_SCOPE) red."""
    from vaf.cloud.base import create_cloud_provider
    with pytest.raises(lab.icloud.ICloudOwnerOnly) as err:
        create_cloud_provider("icloud", username, "icloud", user_scope_id=scope)
    assert isinstance(err.value, ValueError)
    assert "belongs to the computer owner's account" in str(err.value)


@pytest.mark.parametrize("username,scope", [
    (OWNER, OWNER_SCOPE), (OWNER, None), (OWNER.upper(), OWNER_SCOPE), (f" {OWNER} ", None),
])
def test_the_machine_owner_gets_an_icloud_provider(lab, username, scope):
    from vaf.cloud.base import create_cloud_provider
    prov = create_cloud_provider("icloud", username, "icloud", user_scope_id=scope)
    assert prov.authenticate()


# -- AC7: every tool action of a tenant --------------------------------------------------

ACTIONS = ("save", "list", "retrieve", "status", "browse", "download", "read", "search",
           "search_all", "show_in_viewer")


def test_tenant_tool_actions_never_reach_icloud(lab, monkeypatch):
    """A tenant with an `icloud` entry in their own accounts: no action returns or copies a
    file of the host's iCloud Drive. MUTATION: remove the gate in `ICloudProvider.__init__`
    - red (read returns the in-root content)."""
    import vaf.core.session as session_mod
    from vaf.tools.cloud_storage import CloudStorageTool

    lab.store["cloud_config_by_user"] = {TENANT: {"accounts": [
        {"provider": "icloud", "account_id": "icloud", "sync_enabled": True,
         "user_scope_id": TENANT_SCOPE}]}}
    projects = lab.home / "Documents" / "VAF_Projects" / "ab12cd34"
    projects.mkdir(parents=True)
    monkeypatch.setattr(session_mod, "get_user_projects_root", lambda scope: projects)
    tenant_file = projects / "mine.txt"
    tenant_file.write_bytes(b"tenant body")

    saved = []

    class _Sessions:
        def load(self, sid):
            return types.SimpleNamespace(runtime_state={"sidebar_documents": []})

        def save(self, session, **kw):
            saved.append(session)

    import vaf.core.subagent_ipc as ipc
    monkeypatch.setattr(ipc, "get_current_session_id", lambda: "chat1")
    monkeypatch.setattr(session_mod, "SessionManager", _Sessions)

    icloud_before = _snapshot(lab.base)
    file_id = str(lab.root / "in.txt")
    identity = {"username": TENANT, "user_scope_id": TENANT_SCOPE, "user_role": "user"}
    for action in ACTIONS:
        out = CloudStorageTool().run(
            action=action, provider="icloud", file_id=file_id, query="in",
            file_path=str(tenant_file) if action == "save" else "in.txt", **identity)
        assert IN_ROOT.decode() not in out, (action, out)
        if action in ("read", "show_in_viewer", "download"):
            assert "belongs to the computer owner's account" in out, (action, out)
        if action == "search_all":
            assert "provider=icloud" not in out, out
    assert _snapshot(lab.base) == icloud_before
    downloads = projects / "Downloads"
    assert not downloads.exists() or not any(downloads.iterdir())
    assert saved == []


# -- AC8: the background sync worker -----------------------------------------------------

def test_sync_worker_does_not_sync_icloud_for_a_tenant(lab):
    """MUTATION: remove the gate in `ICloudProvider.__init__` - red (the tenant's file is
    uploaded into the owner's iCloud Drive)."""
    from vaf.cloud import sync_worker
    local = sync_worker._get_user_sync_dir(TENANT, "icloud")
    (local / "tenant.txt").write_bytes(b"tenant body")
    icloud_before = _snapshot(lab.base)
    local_before = _snapshot(local)
    with pytest.raises(lab.icloud.ICloudOwnerOnly):
        sync_worker._run_sync_for_account(TENANT, "icloud", "icloud", TENANT_SCOPE)
    assert _snapshot(lab.base) == icloud_before
    assert _snapshot(local) == local_before
