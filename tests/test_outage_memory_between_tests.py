# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""What the conftest fixture `_no_remembered_outage` forgets between two tests, and what not.

The tests come in pairs that run in file order: the first leaves a remembered outage on the
process-wide instance, the second reads what the next test sees. Nothing is dialed; the
memories are marked by hand.

- The local API's memory is forgotten: a later test that expects a push to dial gets one.
- The auth store's memory is kept: forgetting it around every test made every later
  directory lookup dial the closed database port again (266 refused connects to it in the
  whole suite became 1894). A kept "down" changes no answer there, because the suite has
  no store, and a test that fakes one calls `invalidate_permissions_cache()`, which forgets.
"""
import pytest

_LEFT = {}
# A key no lookup uses, so the remembered entry cannot answer for a real database url.
_STORE_KEY = "postgresql://outage-memory-pair.invalid/vaf"


def test_a_leaves_the_local_api_marked_down():
    from vaf.core import web_interface

    key = web_interface.internal_api_base()
    web_interface.LOCAL_API_DOWN.failed(key)
    assert web_interface.LOCAL_API_DOWN.is_down(key)
    _LEFT["api"] = key


def test_b_the_next_test_sees_the_local_api_up():
    """MUTATION: drop the LOCAL_API_DOWN forget from the fixture - red."""
    from vaf.core import web_interface

    key = _LEFT.pop("api", None) or web_interface.internal_api_base()
    assert not web_interface.LOCAL_API_DOWN.is_down(key)


def test_c_leaves_the_auth_store_marked_down():
    from vaf.auth import permissions

    permissions._store_down.failed(_STORE_KEY)
    _LEFT["store"] = True


def test_d_the_next_test_still_sees_the_auth_store_down():
    """MUTATION: forget the auth store's memory in the fixture as well - red."""
    from vaf.auth import permissions

    if not _LEFT.pop("store", False):
        pytest.skip("runs only after test_c_leaves_the_auth_store_marked_down")
    try:
        # 30 s pause; the previous test ran a moment ago.
        assert permissions._store_down.is_down(_STORE_KEY)
    finally:
        permissions._store_down.reached(_STORE_KEY)
