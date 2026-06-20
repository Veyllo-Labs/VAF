"""The thinking run must read the local admin's REAL data scope.

A background thinking run normalizes the local admin to None for idle-tracking, but the user's
actual data (automation notes/todos, RAG, sessions) lives under their real local_admin_scope_id —
where the Web UI / main agent write. _run_thinking_for_user now resolves None ->
get_local_admin_scope_id() for all data reads, so the run reads the same store the user sees.

The fix is safe only because _key() maps that real scope back to "default", keeping the
thinking-mode bookkeeping (locks/cooldown/declined/...) unchanged. These tests pin that invariant.
"""
from vaf.core.thinking_mode import _key
from vaf.core.config import get_local_admin_scope_id


def test_local_admin_real_scope_collapses_to_default_key():
    # After the fix, None is resolved to the real local_admin_scope_id for DATA reads. _key() must
    # collapse BOTH None and that real scope to the same "default" bookkeeping key, so the resolution
    # does not change locks/cooldown/etc.
    admin = get_local_admin_scope_id()
    assert admin and admin != "default"
    assert _key(None) == "default"
    assert _key(admin) == "default"


def test_other_user_scope_is_not_collapsed():
    # A real (non-local-admin) user keeps their own bookkeeping key — the resolution only touches None.
    other = "11111111-2222-3333-4444-555555555555"
    assert _key(other) == other
