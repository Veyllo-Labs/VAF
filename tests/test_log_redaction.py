# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Access-log secret redaction: the /ws?token=<jwt> handshake leaked a live
session token into uvicorn's access log (terminal + tray_debug). The filter
must mask it - and other secret query params - without touching benign URLs,
and the uvicorn log_config wiring must be a valid dictConfig."""
import logging
import logging.config

import pytest

from vaf.core.log_helper import (
    RedactTokenFilter,
    _SECRET_QS_RE,
    redacted_uvicorn_log_config,
)

_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJNZXJ0In0.SMBDXSqQhIX6NAsz02m4Wq0GBytodmj"


def test_masks_ws_token_line():
    line = f'WebSocket /ws?token={_JWT} [accepted]'
    assert _SECRET_QS_RE.sub(r"\1***", line) == "WebSocket /ws?token=*** [accepted]"


@pytest.mark.parametrize("url,expected", [
    (f"/ws?token={_JWT}", "/ws?token=***"),
    ("/x?access_token=abc.def", "/x?access_token=***"),
    ("/x?api_key=SECRET&q=hi", "/x?api_key=***&q=hi"),
    ("/x?password=hunter2", "/x?password=***"),
    ("/x?q=hello&page=2", "/x?q=hello&page=2"),      # benign untouched
    ("/api/security/overview", "/api/security/overview"),
])
def test_secret_params_masked_others_untouched(url, expected):
    assert _SECRET_QS_RE.sub(r"\1***", url) == expected


def test_filter_scrubs_record_args_like_uvicorn_access():
    # uvicorn access records carry the request line in record.args
    rec = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", f"/ws?token={_JWT}", "1.1", 200), None,
    )
    assert RedactTokenFilter().filter(rec) is True
    rendered = rec.msg % rec.args
    assert _JWT not in rendered and "token=***" in rendered


def test_filter_scrubs_msg_and_survives_non_string_args():
    rec = logging.LogRecord("x", logging.INFO, "", 0, f"connect /ws?token={_JWT}", (1, 2, 3), None)
    RedactTokenFilter().filter(rec)
    assert "token=***" in str(rec.msg) and _JWT not in str(rec.msg)


def test_log_config_is_valid_dictconfig_with_filter_wired():
    cfg = redacted_uvicorn_log_config()
    assert "redact_secrets" in cfg["filters"]
    for name in ("access", "default"):
        assert "redact_secrets" in cfg["handlers"][name]["filters"]
    logging.config.dictConfig(cfg)  # raises if malformed
