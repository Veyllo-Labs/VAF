# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The calendar provider client as a sync source: pages through a window, normalises both
providers into one shape (all-day, cancelled, series instance, updated, link), raises
AuthError on a dead token only, writes wall-clock times in the named zone, and resolves a
named account exactly or not at all. No network: requests is faked."""
from types import SimpleNamespace

import pytest

from vaf.core import calendar_client as cc


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(cc, "get_valid_access_token", lambda *a, **k: "tok")


def _fake_requests(monkeypatch, handler):
    calls = []

    def _get(url, params=None, headers=None, timeout=None):
        calls.append(("GET", url, params, headers, None))
        return handler("GET", url, params, None)

    def _post(url, json=None, headers=None, timeout=None):
        calls.append(("POST", url, None, headers, json))
        return handler("POST", url, None, json)

    def _patch(url, json=None, headers=None, timeout=None):
        calls.append(("PATCH", url, None, headers, json))
        return handler("PATCH", url, None, json)

    def _delete(url, headers=None, timeout=None):
        calls.append(("DELETE", url, None, headers, None))
        return handler("DELETE", url, None, None)

    monkeypatch.setattr(cc, "requests", SimpleNamespace(get=_get, post=_post, patch=_patch, delete=_delete))
    return calls


# ── paging and normalisation ─────────────────────────────────────────────────────

GOOGLE_PAGE_1 = {
    "items": [
        {"id": "g1", "iCalUID": "g1@google.com", "summary": "Standup", "status": "confirmed", "location": "Room 1",
         "start": {"dateTime": "2026-03-02T09:00:00+01:00", "timeZone": "Europe/Berlin"},
         "end": {"dateTime": "2026-03-02T09:30:00+01:00", "timeZone": "Europe/Berlin"},
         "updated": "2026-03-01T10:00:00.123Z", "recurringEventId": "series1", "htmlLink": "https://cal.example/g1", "etag": "\"e1\""},
        {"id": "g2", "summary": "Holiday", "status": "confirmed",
         "start": {"date": "2026-03-05"}, "end": {"date": "2026-03-06"}, "updated": "2026-02-01T00:00:00Z", "htmlLink": "https://cal.example/g2"},
    ],
    "nextPageToken": "p2",
}
GOOGLE_PAGE_2 = {"items": [{"id": "g3", "status": "cancelled", "start": {"dateTime": "2026-03-07T10:00:00Z"}, "end": {"dateTime": "2026-03-07T11:00:00Z"}, "updated": "2026-03-06T00:00:00Z"}]}


def test_google_list_pages_and_normalises(token, monkeypatch):
    def handler(method, url, params, body):
        assert method == "GET" and params["singleEvents"] is True and params["showDeleted"] is True
        return _Resp(200, GOOGLE_PAGE_2 if params.get("pageToken") == "p2" else GOOGLE_PAGE_1)
    calls = _fake_requests(monkeypatch, handler)
    events = cc.list_events("gmail", "me@example.com", "scope", "2026-03-01T00:00:00Z", "2026-03-31T00:00:00Z", max_results=2)
    assert len(calls) == 2 and calls[0][2]["maxResults"] == 2
    assert [e["id"] for e in events] == ["g1", "g2", "g3"]
    g1, g2, g3 = events
    assert g1["ical_uid"] == "g1@google.com" and g1["location"] == "Room 1" and g1["tz"] == "Europe/Berlin"
    assert g1["all_day"] is False and g1["status"] == "confirmed" and g1["recurring_event_id"] == "series1"
    assert g1["link"] == "https://cal.example/g1" and g1["htmlLink"] == g1["link"] and g1["etag"] == "\"e1\""
    assert abs(g1["updated"] - 1772359200.123) < 0.01                                     # 2026-03-01T10:00:00.123Z
    assert g2["all_day"] is True and g2["start"] == "2026-03-05" and g2["end"] == "2026-03-06"
    assert g3["status"] == "cancelled" and g3["summary"] == "(no title)"


MS_PAGE_1 = {
    "value": [
        {"id": "m1", "iCalUId": "m1@outlook", "subject": "Review", "isAllDay": False, "isCancelled": False,
         "start": {"dateTime": "2026-03-02T08:00:00.0000000", "timeZone": "UTC"}, "end": {"dateTime": "2026-03-02T09:00:00.0000000", "timeZone": "UTC"},
         "lastModifiedDateTime": "2026-03-01T10:00:00.0000000Z", "seriesMasterId": "sm1", "webLink": "https://outlook.example/m1",
         "location": {"displayName": "Teams"}, "bodyPreview": "agenda", "body": {"contentType": "html", "content": "<p>agenda</p>"}, "@odata.etag": "W/\"x\""},
    ],
    "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/calendar/calendarView?$skip=1",
}
MS_PAGE_2 = {"value": [{"id": "m2", "subject": "Offsite", "isAllDay": True, "isCancelled": True,
                        "start": {"dateTime": "2026-03-09T00:00:00.0000000", "timeZone": "UTC"}, "end": {"dateTime": "2026-03-10T00:00:00.0000000", "timeZone": "UTC"},
                        "lastModifiedDateTime": "2026-03-01T00:00:00Z"}]}


def test_microsoft_list_follows_next_link_and_reads_all_day(token, monkeypatch):
    def handler(method, url, params, body):
        if "$skip=1" in url:
            assert params is None                                                     # the next link carries its own query
            return _Resp(200, MS_PAGE_2)
        assert params["$top"] == 250 and "calendarView" in url
        return _Resp(200, MS_PAGE_1)
    calls = _fake_requests(monkeypatch, handler)
    events = cc.list_events("microsoft", "me@outlook.example", "scope", "2026-03-01T00:00:00Z", "2026-03-31T00:00:00Z")
    assert len(calls) == 2 and calls[0][3]["Prefer"] == 'outlook.timezone="UTC"'
    m1, m2 = events
    assert m1["summary"] == "Review" and m1["location"] == "Teams" and m1["description"] == "agenda"
    assert m1["recurring_event_id"] == "sm1" and m1["link"] == "https://outlook.example/m1" and m1["webLink"] == m1["link"]
    assert m1["tz"] == "UTC" and m1["updated"] == 1772359200.0 and m1["etag"] == "W/\"x\""
    assert m2["all_day"] is True and m2["start"] == "2026-03-09" and m2["end"] == "2026-03-10" and m2["status"] == "cancelled"


def test_a_dead_token_raises_auth_error_and_other_failures_stay_quiet(token, monkeypatch):
    _fake_requests(monkeypatch, lambda *a: _Resp(401, text="expired"))
    with pytest.raises(cc.AuthError):
        cc.list_events("gmail", "me@example.com", "scope", "2026-03-01T00:00:00Z", "2026-03-31T00:00:00Z")
    with pytest.raises(cc.AuthError):
        cc.create_event("microsoft", "me@outlook.example", "scope", "x", "2026-03-01T10:00:00Z", "2026-03-01T11:00:00Z")
    with pytest.raises(cc.AuthError):
        cc.delete_event("gmail", "me@example.com", "scope", "g1")
    _fake_requests(monkeypatch, lambda *a: _Resp(500, text="boom"))
    assert cc.list_events("gmail", "me@example.com", "scope", "2026-03-01T00:00:00Z", "2026-03-31T00:00:00Z") == []
    assert cc.create_event("gmail", "me@example.com", "scope", "x", "2026-03-01T10:00:00Z", "2026-03-01T11:00:00Z") is None
    assert cc.delete_event("microsoft", "me@outlook.example", "scope", "m1") is False


def test_without_a_token_nothing_is_called(monkeypatch):
    monkeypatch.setattr(cc, "get_valid_access_token", lambda *a, **k: None)
    calls = _fake_requests(monkeypatch, lambda *a: _Resp(200, {}))
    assert cc.list_events("gmail", "a", "s", "2026-03-01T00:00:00Z", "2026-03-02T00:00:00Z") == []
    assert cc.create_event("gmail", "a", "s", "x", "2026-03-01T10:00:00Z", "2026-03-01T11:00:00Z") is None
    assert calls == []


# ── writes carry the zone ────────────────────────────────────────────────────────

def test_writes_send_the_named_zone_and_all_day_dates(token, monkeypatch):
    def handler(method, url, params, body):
        return _Resp(200, {"id": "new", "start": body.get("start", {}), "end": body.get("end", {}), "summary": body.get("summary") or body.get("subject")})
    calls = _fake_requests(monkeypatch, handler)
    cc.create_event("gmail", "me@example.com", "scope", "Timed", "2026-03-02T14:00:00", "2026-03-02T15:00:00", tz="Europe/Berlin", location="Cafe")
    g_body = calls[-1][4]
    assert g_body["start"] == {"dateTime": "2026-03-02T14:00:00", "timeZone": "Europe/Berlin"} and g_body["location"] == "Cafe"
    cc.create_event("gmail", "me@example.com", "scope", "Day", "2026-03-05", "2026-03-06", all_day=True)
    assert calls[-1][4]["start"] == {"date": "2026-03-05"} and calls[-1][4]["end"] == {"date": "2026-03-06"}
    # Graph wants wall-clock time in the zone: an aware UTC instant is converted, a naive one is taken as given.
    cc.create_event("microsoft", "me@outlook.example", "scope", "Timed", "2026-03-02T13:00:00+00:00", "2026-03-02T14:00:00Z", tz="Europe/Berlin", location="Cafe")
    m_body = calls[-1][4]
    assert m_body["start"] == {"dateTime": "2026-03-02T14:00:00", "timeZone": "Europe/Berlin"}
    assert m_body["end"] == {"dateTime": "2026-03-02T15:00:00", "timeZone": "Europe/Berlin"}
    assert m_body["location"] == {"displayName": "Cafe"} and "isAllDay" not in m_body
    cc.create_event("microsoft", "me@outlook.example", "scope", "Day", "2026-03-05", "2026-03-06", all_day=True)
    assert calls[-1][4]["isAllDay"] is True and calls[-1][4]["start"]["dateTime"] == "2026-03-05T00:00:00"
    # Legacy behaviour without a zone: a naive time is stamped UTC.
    cc.create_event("gmail", "me@example.com", "scope", "Legacy", "2026-03-02T14:00:00", "2026-03-02T15:00:00")
    assert calls[-1][4]["start"] == {"dateTime": "2026-03-02T14:00:00Z", "timeZone": "UTC"}
    cc.update_event("microsoft", "me@outlook.example", "scope", "m1", start="2026-03-02T09:00:00", end="2026-03-02T10:00:00", tz="Asia/Tokyo", location="Desk")
    assert calls[-1][0] == "PATCH" and calls[-1][4]["start"]["timeZone"] == "Asia/Tokyo" and calls[-1][4]["isAllDay"] is False


def test_delete_treats_already_gone_as_done(token, monkeypatch):
    _fake_requests(monkeypatch, lambda *a: _Resp(404))
    assert cc.delete_event("gmail", "me@example.com", "scope", "gone") is True
    assert cc.delete_event("microsoft", "me@outlook.example", "scope", "gone") is True


# ── account resolution ───────────────────────────────────────────────────────────

def test_a_named_account_must_match(monkeypatch):
    cfg = {"accounts": [
        {"account_id": "a@example.com", "provider": "gmail", "email": "a@example.com"},
        {"account_id": "b@outlook.example", "provider": "microsoft", "email": "b@outlook.example", "mail_enabled": False},
        {"account_id": "c@example.com", "provider": "imap", "email": "c@example.com"},
        {"account_id": "d@example.com", "provider": "gmail", "enabled": False},
    ]}
    monkeypatch.setattr(cc, "_get_email_config", lambda *a, **k: cfg)
    accounts = cc.get_calendar_accounts("alice", "scope")
    assert [a["account_id"] for a in accounts] == ["a@example.com", "b@outlook.example"]     # imap and disabled are not calendars
    assert cc.resolve_calendar_account()["account_id"] == "a@example.com"
    assert cc.resolve_calendar_account(provider="microsoft")["account_id"] == "b@outlook.example"
    assert cc.resolve_calendar_account(account_id="B@Outlook.example")["account_id"] == "b@outlook.example"
    assert cc.resolve_calendar_account(account_id="nobody@example.com") is None
    assert cc.resolve_calendar_account(provider="caldav") is None


def test_iso_helpers_read_provider_timestamps():
    assert cc._to_unix("2026-03-01T10:00:00Z") == 1772359200.0
    assert cc._to_unix("2026-03-01T10:00:00.1234567Z") == pytest.approx(1772359200.123457, abs=1e-5)
    assert cc._to_unix("2026-03-01T11:00:00+01:00") == 1772359200.0
    assert cc._to_unix("") is None and cc._to_unix("not a date") is None
    assert cc._wall_clock("2026-03-02T13:00:00Z", "Europe/Berlin") == "2026-03-02T14:00:00"
    assert cc._wall_clock("2026-03-02T13:00:00", "Europe/Berlin") == "2026-03-02T13:00:00"
    assert cc._ensure_rfc3339("2026-03-02T13:00:00", "Europe/Berlin") == "2026-03-02T13:00:00"
    assert cc._ensure_rfc3339("2026-03-02T13:00:00") == "2026-03-02T13:00:00Z"
    assert cc._ensure_rfc3339("2026-03-02") == "2026-03-02T00:00:00Z"


# ── strict mode for the sync engine ───────────────────────────────────────────────

def test_strict_listing_raises_on_a_failed_page_and_on_a_missing_token(monkeypatch, token):
    """The sync engine reads "not returned" as "deleted", so it must know a list was
    complete: a failed page is a ProviderError, no token an AuthError. The tools keep the
    quiet default and get the items that arrived."""
    def handler(method, url, params, body):
        if params and params.get("pageToken") == "p2":
            return _Resp(503, {}, "unavailable")
        return _Resp(200, {"items": [{"id": "g1", "summary": "A", "status": "confirmed",
                                      "start": {"dateTime": "2026-03-02T09:00:00Z"},
                                      "end": {"dateTime": "2026-03-02T10:00:00Z"}}],
                           "nextPageToken": "p2"})
    _fake_requests(monkeypatch, handler)
    quiet = cc.list_events("gmail", "a@gmail.example", "scope", "2026-03-01T00:00:00Z", "2026-03-31T00:00:00Z")
    assert [e["id"] for e in quiet] == ["g1"]                                    # partial, as before
    with pytest.raises(cc.ProviderError):
        cc.list_events("gmail", "a@gmail.example", "scope", "2026-03-01T00:00:00Z", "2026-03-31T00:00:00Z", strict=True)

    def ms_handler(method, url, params, body):
        if url.endswith("next"):
            raise ConnectionError("reset")
        return _Resp(200, {"value": [{"id": "m1", "subject": "B", "isAllDay": False,
                                      "start": {"dateTime": "2026-03-02T09:00:00.0000000", "timeZone": "UTC"},
                                      "end": {"dateTime": "2026-03-02T10:00:00.0000000", "timeZone": "UTC"}}],
                           "@odata.nextLink": "https://graph.example/next"})
    _fake_requests(monkeypatch, ms_handler)
    assert [e["id"] for e in cc.list_events("microsoft", "m@x", "scope", "2026-03-01T00:00:00Z", "2026-03-31T00:00:00Z")] == ["m1"]
    with pytest.raises(cc.ProviderError):
        cc.list_events("microsoft", "m@x", "scope", "2026-03-01T00:00:00Z", "2026-03-31T00:00:00Z", strict=True)

    monkeypatch.setattr(cc, "get_valid_access_token", lambda *a, **k: None)
    assert cc.list_events("gmail", "a@gmail.example", "scope", "2026-03-01T00:00:00Z", "2026-03-31T00:00:00Z") == []
    with pytest.raises(cc.AuthError):
        cc.list_events("gmail", "a@gmail.example", "scope", "2026-03-01T00:00:00Z", "2026-03-31T00:00:00Z", strict=True)
    monkeypatch.setattr(cc, "get_valid_access_token", lambda *a, **k: "tok")
    with pytest.raises(cc.ProviderError):
        cc.list_events("caldav", "c@x", "scope", "2026-03-01T00:00:00Z", "2026-03-31T00:00:00Z", strict=True)
