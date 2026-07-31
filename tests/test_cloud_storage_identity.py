# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`cloud_storage` acts as its CALLER, and the path it is handed is not free.

TWO DEFECTS IN ONE TOOL, on two different axes, and they are asserted separately here
because they fail separately.

IDENTITY. The tool resolved who was calling by reading `VAF_USERNAME` - an environment
variable that is set NOWHERE in the repository; the only occurrence was the read itself.
So the fallback was not the exceptional case, it was the only one, and every call resolved
to the machine owner's name for every user on every lane. `_get_cloud_accounts` branches on
exactly that name, which made its per-user arm (`cloud_config_by_user`) unreachable code: a
tenant was handed the OWNER's connected accounts, and through them the owner's cloud
credentials against Google Drive, OneDrive, Dropbox, Nextcloud and iCloud. Not a race, not
a multi-worker artefact - unconditional.

PATH. `_action_save` took a model-chosen local path, did `expanduser().resolve()` and
copied it, with `is_safe_path` appearing zero times in the module. The copy lands under
`Platform.data_dir()`, one of the four roots `GET /api/file` serves, so a file the caller
could not read could be made readable by asking this tool to "save" it. The guard that
should have caught this could not see the tool at all: its collector skipped the class
because `name` comes from a module constant, and once that was fixed the delegation check
cleared it because it imports `LibrarianTool` from a module that happens to contain the
rule.

WHY THE BOUNDARY IS NARROW rather than a `file_access` declaration on the class, which is
what every other file tool now uses: `read` and `show_in_viewer` download a cloud file into
a TEMPORARY file and hand it to `LibrarianTool._read_file`, which asks `is_safe_path`
itself. Measured - inside a tenant jail a path in the system temp directory is refused - so
a class-wide declaration would break those two actions on the tool's own scratch file,
which no tenant boundary has any business containing.
"""
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from vaf.tools.cloud_storage import CloudStorageTool

TENANT_SCOPE = "deadbeef-0000-0000-0000-000000000000"   # synthetic; never a real scope
OWNER = "owner-account"                                  # synthetic local-admin name
TENANT = "tenant-account"

OWNER_ACCOUNTS = {"accounts": [
    {"provider": "google_drive", "account_id": "owner-acct", "sync_enabled": True},
]}
TENANT_ACCOUNTS = {"tenant-account": {"accounts": [
    {"provider": "onedrive", "account_id": "tenant-acct", "sync_enabled": True},
]}}


@pytest.fixture
def cloud_config():
    """The owner's accounts and one tenant's, through the REAL config accessor."""
    from vaf.core.config import Config

    real = Config.get

    def _get(key, default=None):
        if key == "cloud_config":
            return OWNER_ACCOUNTS
        if key == "cloud_config_by_user":
            return TENANT_ACCOUNTS
        if key == "local_admin_username":
            return OWNER
        return real(key, default)

    with patch.object(Config, "get", staticmethod(_get)):
        yield


def _status(**identity):
    """Drive the TOOL, not the helper - the wiring is half of what is under test."""
    return CloudStorageTool().run(action="status", **identity)


# ── effect one: the tool acts as its caller ──────────────────────────────────

def test_a_tenant_no_longer_sees_the_owners_cloud_accounts(cloud_config):
    """THE refusal side of the identity half, and the defect this whole step exists for.

    A caller with a foreign scope and no username used to resolve to the owner's name and
    receive the owner's connected accounts. Asserted on what comes BACK rather than on the
    resolved name: the name is an implementation detail, the account list is the exposure.
    """
    out = _status(user_scope_id=TENANT_SCOPE, user_role="user")
    assert "owner-acct" not in out, (
        "a tenant is still handed the owner's connected cloud account - and with it the "
        "owner's OAuth credentials, because the credential key is derived from this identity"
    )


def test_the_per_user_branch_is_reachable_at_last(cloud_config):
    """`cloud_config_by_user` was dead code, not a fallback.

    Nothing could reach it while the identity came from an environment variable nobody
    sets, so this is the assertion that the branch exists for a reason again. Without it the
    test above would also pass for a tool that returns nothing to anybody.
    """
    out = _status(username=TENANT, user_scope_id=TENANT_SCOPE, user_role="user")
    assert "tenant-acct" in out, f"the tenant cannot reach their OWN account either: {out[:200]}"
    assert "owner-acct" not in out


def test_the_owner_still_sees_their_own(cloud_config):
    """The control. Every assertion above also holds for a tool that answers nobody."""
    out = _status(username=OWNER, user_role="admin")
    assert "owner-acct" in out, f"the machine owner lost their own accounts: {out[:200]}"


# ── effect two: the path handed in is not free ───────────────────────────────

@pytest.fixture
def tenant_home(tmp_path, monkeypatch):
    """A home with the tenant's own tree and one file outside it."""
    home = tmp_path / "home"
    own = home / "Documents" / "VAF_Projects" / "deadbeef"
    own.mkdir(parents=True)
    (own / "mine.txt").write_text("mine")
    (home / "Documents").mkdir(exist_ok=True)
    outside = home / "Documents" / "not-mine.txt"
    outside.write_text("SECRET-NOT-YOURS")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    return home, own / "mine.txt", outside


def test_a_path_outside_the_callers_tree_is_refused_and_nothing_is_copied(tenant_home, cloud_config):
    """THE refusal side of the path half, asserted on the COPY rather than on the string.

    A tool can answer "Access denied" after `shutil.copy2` has already run, and from the
    outside those two are indistinguishable. So the assertion is that the destination tree
    contains nothing - the same reason the WebSocket ownership fix asserts on the session
    file instead of the return value.
    """
    home, _mine, outside = tenant_home
    copied = []
    with patch("vaf.tools.filesystem._visible_skill_roots", return_value=[]), \
         patch.object(shutil, "copy2", lambda *a, **k: copied.append(a)):
        out = CloudStorageTool().run(
            action="save", file_path=str(outside), account_id="tenant-acct",
            username=TENANT, user_scope_id=TENANT_SCOPE, user_role="user",
        )
    assert copied == [], "the file was copied before the refusal was returned"
    assert "denied" in out.lower(), f"a foreign file was accepted for upload: {out[:200]}"


def test_the_refusal_is_not_an_existence_oracle(tenant_home, cloud_config):
    """Access is decided before existence, so a refused path cannot report whether it is
    there. Both answers have to be the same shape."""
    home, _mine, outside = tenant_home
    missing = home / "Documents" / "no-such-file.txt"
    with patch("vaf.tools.filesystem._visible_skill_roots", return_value=[]):
        present = CloudStorageTool().run(
            action="save", file_path=str(outside), account_id="tenant-acct",
            username=TENANT, user_scope_id=TENANT_SCOPE, user_role="user")
        absent = CloudStorageTool().run(
            action="save", file_path=str(missing), account_id="tenant-acct",
            username=TENANT, user_scope_id=TENANT_SCOPE, user_role="user")
    assert "not found" not in present.lower()
    assert "not found" not in absent.lower()
    assert present.split(":")[0] == absent.split(":")[0]


# ── the second untrusted door, found by refutation and not by me ─────────────

def test_retrieve_refuses_a_path_that_escapes_the_sync_folder(cloud_config):
    """`file_path` is documented as a NAME in the sync folder; pathlib disagrees.

    An absolute right operand swallows the base - `sync_dir / "/etc/shadow"` is
    `/etc/shadow` - and the copy lands in the Downloads folder, which `GET /api/file`
    serves. So this was the sharper of the two doors: `save` copies INTO a store, `retrieve`
    copies OUT to a served one. It survived the first pass of this change because the
    guard was placed where the author was already looking, and the module was then described
    as contained. An adversarial review found it one function below the fix.
    """
    for escape in ("/etc/shadow", "../../../../etc/shadow"):
        out = CloudStorageTool().run(
            action="retrieve", file_path=escape, account_id="owner-acct",
            username=OWNER, user_role="admin")
        assert "denied" in out.lower(), f"{escape!r} was accepted: {out[:160]}"


def test_the_identity_survives_the_real_dispatcher(cloud_config):
    """Drives `assign_declared_identity` instead of handing the kwargs in by hand.

    The first version of this file passed identity straight to `run()`, so the one place
    that actually DECIDES a caller's name was never exercised - and that is exactly the
    place the librarian's second dispatch path drops it. A test that supplies the answer it
    is checking measures nothing about the wiring, whatever its docstring says.
    """
    from vaf.core.tool_dispatch import assign_declared_identity

    tool = CloudStorageTool()
    args = assign_declared_identity(tool, {"action": "status"},
                                    user_scope_id=TENANT_SCOPE, username=None, user_role="user")
    assert "owner-acct" not in tool.run(**args), (
        "with the identity assigned by the dispatcher rather than by the test, a nameless "
        "tenant still reaches the owner's accounts"
    )


# ── the third instance of the same class, frozen rather than parked in prose ──
#
# `_action_save` and `_action_retrieve` are guarded. The DESTINATION side is not, and it is
# the same defect wearing a different hat: two actions write into a directory chosen with no
# identity at all. Frozen here with the measurement so it is seen from now on; repairing it
# belongs with the provider chain in cloud step B, where the scope arrives.

UNIDENTIFIED_WRITE_TARGETS = {
    "_action_download":
        "writes to Platform.downloads_dir(), which is Path.home()/'Downloads' - process "
        "global, no username and no scope. Every tenant's cloud download therefore lands in "
        "the OWNER's home, and that directory is one of the four roots GET /api/file serves, "
        "so the file becomes readable through the API as well as written to the wrong home.",
    "_action_retrieve":
        "same destination, same absence of identity. Its SOURCE side was closed in cloud "
        "step A; the destination was not, which is precisely the shape this round keeps "
        "meeting - a boundary as wide as the door somebody was standing in.",
}


@pytest.mark.parametrize("fn", sorted(UNIDENTIFIED_WRITE_TARGETS))
def test_the_unidentified_write_target_is_still_what_was_measured(fn):
    """A receipt, not a fix. If the destination gains an identity, this points at the note
    that says what it was for instead of leaving a stale claim behind."""
    import inspect

    import vaf.tools.cloud_storage as mod

    src = inspect.getsource(getattr(mod, fn))
    assert "downloads_dir()" in src, (
        f"{fn} no longer writes to the process-global downloads directory. If that was the "
        f"fix, delete this entry and say so.\n{UNIDENTIFIED_WRITE_TARGETS[fn]}"
    )
    assert "user_scope_id" not in src, (
        f"{fn} now takes a scope - if the destination is per-user, this entry is obsolete"
    )


def test_icloud_needs_no_credential_and_that_is_parked_with_a_trigger():
    """LATENT, and the trigger is named instead of a date.

    `ICloudProvider.authenticate()` returns True on the strength of a directory existing
    under the machine owner's home - no credential is consulted at all. It is not reachable
    today: the only route that creates an account posts an empty password and is rejected,
    and it hardcodes a different provider. DUE when any route can create an `icloud` entry,
    because at that moment a tenant with such an entry reads the owner's iCloud Drive.
    """
    import inspect

    from vaf.cloud.icloud import ICloudProvider

    src = inspect.getsource(ICloudProvider.authenticate)
    assert "credential" not in src.lower() and "token" not in src.lower(), (
        "iCloud now consults a credential - the parked finding above is obsolete, remove it"
    )
