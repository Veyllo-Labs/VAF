# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""P5.1: MailService.annotate_visibility re-surfaces the phishing/suspicious
signal in the v2 reader. The agent tools HIDE those mails; the human UI must warn
about them instead (the safety layer MailDashboard had). Verifies the field shim
(v2 from_addr/snippet -> scorer from/body_snippet) and that a spam-category row is
flagged while a benign row stays clean. annotate_visibility never touches the store
so no tmp store is needed."""
import vaf.tools.mail_utils as mu
from vaf.mail.service import MailService


def _svc():
    return MailService.__new__(MailService)  # annotate_visibility uses no store


def test_annotate_visibility_flags_spam_and_shims_v2_fields(monkeypatch):
    # deterministic policy: filter on, threshold 3, no trusted domains
    monkeypatch.setattr(mu, "_phishing_filter_policy", lambda: (True, 3, set()))
    rows = [
        {"thread_id": 1, "from_addr": "Bank <alerts@bank.example>",
         "subject": "Statement", "snippet": "your monthly statement", "category": "spam"},
        {"thread_id": 2, "from_addr": "Alice <alice@example.com>",
         "subject": "Lunch?", "snippet": "see you at noon", "category": "primary"},
    ]
    out = _svc().annotate_visibility(rows)

    # mutates in place, same objects back
    assert out is rows
    # spam category scores 10 -> above threshold -> flagged with a reason
    assert rows[0]["suspicious_for_agent"] is True
    assert "provider_spam_category" in rows[0]["suspicious_reasons"]
    # benign row stays clean
    assert rows[1]["suspicious_for_agent"] is False
    assert rows[1]["suspicious_reasons"] == []


def test_annotate_visibility_reads_legacy_field_names_too(monkeypatch):
    # rows already in scorer shape (from/body_snippet) must work unchanged
    monkeypatch.setattr(mu, "_phishing_filter_policy", lambda: (True, 3, set()))
    rows = [{"from": "x@spammy.example", "subject": "hi",
             "body_snippet": "hello", "category": "junk"}]
    out = _svc().annotate_visibility(rows)
    assert out[0]["suspicious_for_agent"] is True


def test_annotate_visibility_disabled_policy_clears_flags(monkeypatch):
    monkeypatch.setattr(mu, "_phishing_filter_policy", lambda: (False, 3, set()))
    rows = [{"from_addr": "x@x", "subject": "s", "snippet": "b", "category": "spam"}]
    out = _svc().annotate_visibility(rows)
    assert out[0]["suspicious_for_agent"] is False
    assert out[0]["suspicious_reasons"] == []


def test_annotate_visibility_empty_list_is_noop():
    assert _svc().annotate_visibility([]) == []
