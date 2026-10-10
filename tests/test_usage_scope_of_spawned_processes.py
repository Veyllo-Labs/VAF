# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Model calls are booked to the account they were made for, also in a sub-agent process.

The ledger read only the label a caller set (vaf.core.cost.usage_context). A sub-agent child
sets none, so a live coder run for a second account was booked to the machine owner: in the
totals, the Usage view and the budget cap. A spawned child carries its account as
VAF_USER_SCOPE_ID; a workflow process takes it from the chat and runs it through
WorkflowEngine.execute."""
import pytest

from vaf.core import cost as cost_mod


@pytest.fixture
def ledger(monkeypatch):
    booked = []
    monkeypatch.setattr(cost_mod, "record_spend", lambda scope, est, **kw: booked.append(scope) or 0.0)
    import vaf.core.log_helper as log_helper
    monkeypatch.setattr(log_helper, "append_usage_log", lambda *a, **k: None)
    return booked


def _call(**kw):
    cost_mod.record_call("veyllo", "veyllo-chat", 100, 10, lane="coder", **kw)


def test_a_spawned_child_books_to_the_account_it_was_spawned_for(ledger, monkeypatch):
    """MUTATION: drop _spawned_for from record_call - red: booked to nobody, read as the owner."""
    monkeypatch.setenv("VAF_USER_SCOPE_ID", "scope-bob")
    _call()
    assert ledger[-1] == "scope-bob"


def test_a_label_the_caller_set_still_wins(ledger, monkeypatch):
    monkeypatch.setenv("VAF_USER_SCOPE_ID", "scope-bob")
    with cost_mod.usage_context(scope="scope-alice"):
        _call()
    assert ledger[-1] == "scope-alice"
    _call(user_scope_id=None)                      # an explicit word stays the caller's
    assert ledger[-1] is None


def test_the_main_process_books_an_unlabelled_call_to_nobody(ledger, monkeypatch):
    monkeypatch.delenv("VAF_USER_SCOPE_ID", raising=False)
    _call()
    assert ledger[-1] is None


def test_a_workflow_run_books_to_the_account_it_runs_for(ledger, monkeypatch):
    """MUTATION: drop @_books_to_the_runs_account from WorkflowEngine.execute - red."""
    monkeypatch.delenv("VAF_USER_SCOPE_ID", raising=False)
    from vaf.workflows import engine as eng
    wrapper_code = eng._books_to_the_runs_account(lambda self: None).__code__
    assert eng.WorkflowEngine.execute.__code__ is wrapper_code

    before = cost_mod._SCOPE.get()

    class _Run:
        user_scope_id = "scope-carol"

        @eng._books_to_the_runs_account
        def execute(self):
            _call()
            return "done"

    assert _Run().execute() == "done"
    assert ledger[-1] == "scope-carol"
    assert cost_mod._SCOPE.get() == before          # the outer label comes back
