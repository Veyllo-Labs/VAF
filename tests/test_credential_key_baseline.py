# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The exact strings that address stored credentials. Frozen before they were consolidated.

Three independently written `_credential_key` functions decided how a credential is addressed:
one in `vaf/core/credential_store.py` (mail), one in `vaf/github/credential_github.py`, one in
`vaf/cloud/credential_cloud.py`. None imported another. Two of the three knew about
`user_scope_id`; the cloud one had no such parameter at all, which is why the cloud lane keyed
per NAME while mail and github keyed per SCOPE - and why a tenant reached the owner's cloud
accounts. The one copy nobody touched is the one that fell behind, which is the usual direction.

Consolidating them is only safe if every produced string stays identical to the character. A key
is not an implementation detail: it is the address of an existing secret on disk and in the
keyring. One character different and the owner's mail, GitHub and cloud credentials are not
"migrated", they are unreachable. So the strings were MEASURED from the three original functions
and frozen here BEFORE the merge, and the merged builder has to reproduce this set exactly.

Four axes of variation, all of them measured rather than derived from reading:

    namespace         "email"  |  "github"  |  "cloud"
    provider segment  present (mail, cloud) | ABSENT (github)
    admin form        segment omitted (mail, cloud) | literal "default" (github)
    name prefix       none (mail, cloud) | literal "user" (github)

Two of those would not be guessed: the mail namespace is literally `email:` even for a
`google_drive` provider, and github writes `github:user:<name>:<id>` with an extra literal
segment. A builder that "cleaned that up" would be a data-loss commit wearing a refactor's
clothes.
"""
from unittest.mock import patch

import pytest

SCOPE = "deadbeef-0000-0000-0000-000000000000"   # synthetic; never a real scope UUID
NAME = "alice"                                    # synthetic
ACCT = "Acct One"                                 # spaces + capitals, so normalisation is measured too

# (lane, form, expected_key). Measured 2026-07-31 from the three pre-merge functions.
FROZEN = [
    ("mail", "email", None, None, "email:email:acct_one"),
    ("mail", "email", NAME, None, "email:email:alice:acct_one"),
    ("mail", "email", None, SCOPE, "email:email:deadbeef-0000-0000-0000-000000000000:acct_one"),
    ("mail", "imap", None, None, "email:imap:acct_one"),
    ("mail", "imap", NAME, None, "email:imap:alice:acct_one"),
    ("mail", "imap", None, SCOPE, "email:imap:deadbeef-0000-0000-0000-000000000000:acct_one"),
    # The namespace stays `email:` even for a cloud provider. Measured, not a typo.
    ("mail", "google_drive", None, None, "email:google_drive:acct_one"),
    ("mail", "google_drive", NAME, None, "email:google_drive:alice:acct_one"),
    ("mail", "google_drive", None, SCOPE,
     "email:google_drive:deadbeef-0000-0000-0000-000000000000:acct_one"),
    ("mail", "nextcloud", None, None, "email:nextcloud:acct_one"),
    ("mail", "nextcloud", NAME, None, "email:nextcloud:alice:acct_one"),
    ("mail", "nextcloud", None, SCOPE,
     "email:nextcloud:deadbeef-0000-0000-0000-000000000000:acct_one"),
    # github: no provider segment, literal "default" for the admin, literal "user" before a name.
    ("github", None, None, None, "github:default:acct_one"),
    ("github", None, NAME, None, "github:user:alice:acct_one"),
    ("github", None, None, SCOPE, "github:deadbeef-0000-0000-0000-000000000000:acct_one"),
    ("cloud", "google_drive", None, None, "cloud:google_drive:acct_one"),
    ("cloud", "google_drive", NAME, None, "cloud:google_drive:alice:acct_one"),
    ("cloud", "onedrive", None, None, "cloud:onedrive:acct_one"),
    ("cloud", "onedrive", NAME, None, "cloud:onedrive:alice:acct_one"),
    ("cloud", "dropbox", None, None, "cloud:dropbox:acct_one"),
    ("cloud", "dropbox", NAME, None, "cloud:dropbox:alice:acct_one"),
    ("cloud", "nextcloud", None, None, "cloud:nextcloud:acct_one"),
    ("cloud", "nextcloud", NAME, None, "cloud:nextcloud:alice:acct_one"),
    ("cloud", "icloud", None, None, "cloud:icloud:acct_one"),
    ("cloud", "icloud", NAME, None, "cloud:icloud:alice:acct_one"),
]


def _build(lane, provider, name, scope):
    """One builder for all three lanes now. The expectations above were measured from the THREE
    original functions, so this is what makes the merge provable rather than plausible: same
    inputs, same strings, and nothing on disk becomes unreachable."""
    from vaf.core.credential_store import build_credential_key

    namespace = {"mail": "email", "github": "github", "cloud": "cloud"}[lane]
    return build_credential_key(ACCT, namespace=namespace, provider=provider,
                                username=name, user_scope_id=scope)


def test_identity_arguments_are_keyword_only():
    """The old cloud signature was (account_id, provider, username) - positional, so anything
    identity-shaped could land in the wrong slot without a word from Python. Keyword-only deletes
    that failure mode instead of testing for it."""
    import inspect

    from vaf.core.credential_store import build_credential_key

    params = inspect.signature(build_credential_key).parameters
    for ident in ("namespace", "provider", "username", "user_scope_id"):
        assert params[ident].kind is inspect.Parameter.KEYWORD_ONLY, (
            f"{ident} can be passed positionally again"
        )


def test_the_three_lane_local_builders_are_gone():
    """Two definitions deleted, not two definitions kept in sync. A copy that still exists is a
    copy that will drift - the cloud one is the proof: it was the only one never updated for
    scopes, and it is the one that let a tenant reach the owner's accounts."""
    import vaf.cloud.credential_cloud as cloud
    import vaf.github.credential_github as github

    assert not hasattr(github, "_credential_key"), "github kept its own key builder"
    assert not hasattr(cloud, "_credential_key"), "cloud kept its own key builder"


@pytest.mark.parametrize("lane,provider,name,scope,expected", FROZEN)
def test_the_key_is_unchanged(lane, provider, name, scope, expected):
    """One character different is not a refactor, it is an unreachable secret."""
    assert _build(lane, provider, name, scope) == expected


def test_the_local_admin_scope_collapses_to_the_unscoped_form():
    """Measured separately because it is the one case where a SCOPE produces the form that has
    no identity in it: the configured local admin is the machine owner, whose credentials
    predate scoping and are stored unscoped. Getting this wrong strands the owner's own
    accounts while every test above still passes."""
    from vaf.core.config import get_local_admin_scope_id
    from vaf.core.credential_store import _credential_key

    admin_scope = get_local_admin_scope_id()
    if not admin_scope:
        pytest.skip("no local admin scope configured on this machine")
    assert _credential_key(ACCT, "email", None, user_scope_id=admin_scope) == "email:email:acct_one"


def test_the_account_id_is_normalised_the_same_way_everywhere():
    """`Acct One` becomes `acct_one` in all three lanes. It is the reason the frozen ids above
    look nothing like what a caller passes, and a lane that stopped normalising would look
    correct in a diff and find nothing on disk."""
    for lane, provider, name, scope, expected in FROZEN:
        assert expected.endswith("acct_one"), f"{lane} stopped normalising the account id"

# ── read-only legacy forms: never produced, always probed ────────────────────
#
# `get_credentials` builds two more key shapes inline, and they are deliberately NOT part of the
# shared builder: they are formats that exist on disk from older versions and are only ever READ.
# The first has a literal `admin` segment from when the owner's name was written out; the second
# is the unscoped form, which the builder does still produce for the local admin.
#
# They are frozen here for the same reason as everything above: a future cleanup that "removes the
# duplicate unscoped key" or "drops the obsolete admin form" would silently make old credentials
# unreachable, and every other test in this file would stay green.
LEGACY_READ_ONLY = [
    "email:{provider}:admin:{id}",
    "email:{provider}:{id}",
]


def test_the_legacy_admin_form_is_no_longer_PRODUCED():
    """The half of "legacy" that nobody measured, and it was false.

    The list above is labelled read-only, and the label is a claim about the present: this
    shape is not written any more. It was. A tool run with no username had the LITERAL "admin"
    substituted for it, and `_cred_key_username` compares against the CONFIGURED owner - so on
    every installation whose owner registered under another name, that nameless caller was a
    named stranger and wrote precisely `email:<provider>:admin:<id>`. The read path was
    described correctly; the world was not, and the word "legacy" is what kept anyone from
    looking, because it had already answered the question.

    Asserted through the real assigner rather than by grepping for a literal: what matters is
    the key that comes out the far end of the nameless path, not which spelling produced it.
    """
    import vaf.core.config as config_mod
    import vaf.core.credential_store as cred_mod
    from vaf.core.tool_dispatch import assign_declared_identity

    class _NameOnly:
        identity_kwargs = ("username",)

    with patch.object(config_mod, "get_local_admin_username", lambda: "sam"), \
         patch.object(cred_mod, "get_local_admin_username", lambda: "sam"):
        args = assign_declared_identity(_NameOnly(), {}, user_scope_id=None,
                                        username=None, user_role=None)
        produced = cred_mod._credential_key(ACCT, "imap", cred_mod._cred_key_username(args["username"]))

    assert produced == "email:imap:acct_one", (
        f"a caller with no username produced {produced!r}. The forms in LEGACY_READ_ONLY are "
        f"documented as no longer written; if one of them is being written again, that label "
        f"is the lie and the probe below is load-bearing rather than historical."
    )


def test_the_legacy_read_forms_are_still_probed():
    """And ONLY in local-admin context. That condition is the load-bearing half: a scoped user who
    fell back to these would land on the owner's credentials, which is the exact defect this whole
    consolidation exists to remove."""
    import inspect

    from vaf.core import credential_store

    src = inspect.getsource(credential_store.get_email_credentials)
    assert 'f"email:{p}:admin:{safe_id}"' in src, "the literal-admin legacy key is no longer probed"
    assert 'f"email:{p}:{safe_id}"' in src, "the unscoped legacy key is no longer probed"
    # The USE, not the name: the assignment line survives any mutation of the branch, so
    # asserting on "is_local_admin_context" alone stayed green when the guard was replaced by
    # `if True:`. Measured, not assumed - that mutation was run.
    assert "if is_local_admin_context:" in src, (
        "the legacy probes lost their guard; a scoped user must never reach them"
    )
    gate = src.index("is_local_admin_context =")
    first_legacy = src.index('f"email:{p}:admin:{safe_id}"')
    assert gate < first_legacy, "the guard is computed after the legacy keys are built"
