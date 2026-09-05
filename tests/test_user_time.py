# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Unit tests for vaf.core.user_time — the user-timezone single-source-of-truth helpers.

Network-free and user-store-free: identities are passed in as dicts, so no ~/.vaf access.
"""
from datetime import datetime, date

from vaf.core import user_time as ut


def test_resolve_timezone_set_invalid_unset():
    assert ut.resolve_user_timezone(identity={"timezone": "Europe/Berlin"}) is not None
    assert ut.resolve_user_timezone(identity={"timezone": ""}) is None       # Server default
    assert ut.resolve_user_timezone(identity={}) is None                     # missing
    assert ut.resolve_user_timezone(identity={"timezone": "Not/AZone"}) is None  # invalid -> None


def test_user_now_aware_vs_naive():
    aware = ut.user_now(identity={"timezone": "Asia/Tokyo"})
    assert aware.tzinfo is not None, "tz set -> aware datetime in the user's zone"
    naive = ut.user_now(identity={})
    assert naive.tzinfo is None, "Server default -> naive server-local (byte-identical to datetime.now())"


def test_user_today_is_a_date():
    assert isinstance(ut.user_today(identity={"timezone": "America/New_York"}), date)


def test_date_time_format_defaults_and_overrides():
    assert ut.user_date_time_format({}, "de") == "%d.%m.%Y %H:%M:%S"
    assert ut.user_date_time_format({}, "en") == "%Y-%m-%d %H:%M:%S"
    assert ut.user_date_time_format({"date_format": "mm/dd/yyyy"}, "de").startswith("%m/%d/%Y")
    assert ut.user_date_time_format({"time_format": "12h"}, "en").endswith("%I:%M:%S {ampm}")


def test_format_user_datetime_fixed():
    dt = datetime(2026, 6, 29, 14, 5, 9)
    assert ut.format_user_datetime(dt, identity={}, language="de") == "29.06.2026 14:05:09"
    assert ut.format_user_datetime(dt, identity={}, language="en") == "2026-06-29 14:05:09"
    assert ut.format_user_datetime(dt, identity={"time_format": "12h"}, language="en") == "2026-06-29 02:05:09 PM"


def test_weekday_name_localized():
    dt = datetime(2026, 6, 29)  # a Monday
    assert ut.user_weekday_name(dt, "de") == "Montag"
    assert ut.user_weekday_name(dt, "en") == "Monday"


def test_parse_user_datetime_reads_every_notation_in_the_users_zone():
    from datetime import timezone, timedelta
    berlin = {"timezone": "Europe/Berlin"}
    dt, all_day = ut.parse_user_datetime("2026-03-01T14:00", identity=berlin)
    assert not all_day and dt.tzinfo is not None and dt.utcoffset() == timedelta(hours=1) and (dt.hour, dt.minute) == (14, 0)
    dt2, _ = ut.parse_user_datetime("2026-03-01 14:00:30", identity=berlin)
    assert dt2.second == 30 and dt2.tzinfo is not None
    dt3, _ = ut.parse_user_datetime("2026-03-01T13:00:00Z", identity=berlin)               # an offset is taken as given
    assert dt3.utcoffset() == timedelta(0) and dt3 == datetime(2026, 3, 1, 13, 0, tzinfo=timezone.utc)
    dt4, all_day4 = ut.parse_user_datetime("2026-03-05", identity=berlin)
    assert all_day4 and (dt4.hour, dt4.minute) == (0, 0) and dt4.tzinfo is not None
    now = datetime(2026, 3, 1, 8, 0, tzinfo=ut.ZoneInfo("Europe/Berlin"))
    dt5, all_day5 = ut.parse_user_datetime("09:30", identity=berlin, now=now)                # the reminder grammar: today
    assert not all_day5 and (dt5.year, dt5.month, dt5.day, dt5.hour, dt5.minute) == (2026, 3, 1, 9, 30)
    # Server default: naive results, the module's contract.
    dt6, _ = ut.parse_user_datetime("2026-03-01T14:00", identity={})
    assert dt6.tzinfo is None and dt6.hour == 14
    assert ut.parse_user_datetime("", identity=berlin) is None
    assert ut.parse_user_datetime("next tuesday", identity=berlin) is None
