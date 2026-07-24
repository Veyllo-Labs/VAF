# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""P4.3: native /api/mail account endpoints (list/test/add/verify/patch/delete)
built on the email_accounts SSOT + the P4.2 calendar-safe delete. Endpoints are
called directly (async) with the SSOT helpers mocked."""
import asyncio

import pytest
from fastapi import HTTPException

import vaf.api.mail_routes as mr
import vaf.core.email_accounts as ea

_USER = {"username": "admin", "user_scope_id": "s"}


def _v2(monkeypatch, on=True):
    monkeypatch.setattr(mr.Config, "get",
                        staticmethod(lambda k, d=None: on if k == "mail_engine_v2_enabled" else d))


def test_accounts_list_endpoint(monkeypatch):
    _v2(monkeypatch)
    monkeypatch.setattr(ea, "list_mail_accounts", lambda u, user_scope_id=None: [
        {"account_id": "a@x", "email": "a@x", "provider": "imap", "label": "Work", "imap_ready": True}])
    out = asyncio.run(mr.accounts(_USER))
    assert out["accounts"][0]["email"] == "a@x" and out["accounts"][0]["imap_ready"] is True


def test_accounts_test_reports_failure_with_hint(monkeypatch):
    _v2(monkeypatch)
    monkeypatch.setattr(ea, "test_imap_login", lambda *a, **k: (False, "auth failed", "use an app password"))
    out = asyncio.run(mr.accounts_test({"email": "a@x", "password": "pw"}, _USER))
    assert out["ok"] is False and out["hint"] == "use an app password"


def test_accounts_delete_uses_calendar_safe_cascade(monkeypatch):
    _v2(monkeypatch)
    seen = {}
    monkeypatch.setattr(ea, "delete_mail_account",
                        lambda aid, username=None, cred_username=None, user_scope_id=None:
                        seen.update(aid=aid) or {"ok": True, "kept_for_calendar": True})
    out = asyncio.run(mr.accounts_delete("g@x", _USER))
    assert out["ok"] and out["kept_for_calendar"] is True and seen["aid"] == "g@x"


def test_accounts_endpoint_requires_v2(monkeypatch):
    _v2(monkeypatch, on=False)
    with pytest.raises(HTTPException) as e:
        asyncio.run(mr.accounts(_USER))
    assert e.value.status_code == 404


def test_set_category_route_relabels_learns_and_backfills(monkeypatch):
    # P5.3/P5.4: relabel route is local-only (v2 flag only, no write flag); learns
    # a sender rule + backfills (updated count passthrough); 404 on a missing pk.
    _v2(monkeypatch)
    monkeypatch.setattr(mr, "_scope_of", lambda u: "s")

    class _Svc:
        def __init__(self, scope):
            self.scope = scope
        def relabel_and_learn(self, pk, cat, username=None):
            return {"category": "social", "updated": 3} if pk == 7 else None

    monkeypatch.setattr("vaf.mail.service.MailService", _Svc)
    out = asyncio.run(mr.set_message_category(7, {"category": "Social"}, _USER))
    assert out == {"ok": True, "category": "social", "updated": 3}
    with pytest.raises(HTTPException) as e:
        asyncio.run(mr.set_message_category(8, {"category": "social"}, _USER))
    assert e.value.status_code == 404


def test_apply_sender_rules_route_returns_updated_count(monkeypatch):
    # P5.4: standalone backfill endpoint, local classification, v2-gated.
    _v2(monkeypatch)
    monkeypatch.setattr(mr, "_scope_of", lambda u: "s")

    class _Svc:
        def __init__(self, scope):
            pass
        def apply_sender_rules_backfill(self, username=None):
            return 4

    monkeypatch.setattr("vaf.mail.service.MailService", _Svc)
    assert asyncio.run(mr.apply_sender_rules(_USER)) == {"ok": True, "updated": 4}
