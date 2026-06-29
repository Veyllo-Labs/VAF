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
    assert ut.user_date_time_format({"time_format": "12h"}, "en").endswith("%I:%M:%S %p")


def test_format_user_datetime_fixed():
    dt = datetime(2026, 6, 29, 14, 5, 9)
    assert ut.format_user_datetime(dt, identity={}, language="de") == "29.06.2026 14:05:09"
    assert ut.format_user_datetime(dt, identity={}, language="en") == "2026-06-29 14:05:09"
    assert ut.format_user_datetime(dt, identity={"time_format": "12h"}, language="en") == "2026-06-29 02:05:09 PM"


def test_weekday_name_localized():
    dt = datetime(2026, 6, 29)  # a Monday
    assert ut.user_weekday_name(dt, "de") == "Montag"
    assert ut.user_weekday_name(dt, "en") == "Monday"
