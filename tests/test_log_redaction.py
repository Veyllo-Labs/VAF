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


def _uvicorn_logger_state():
    names = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")
    return {n: (logging.getLogger(n).handlers[:], logging.getLogger(n).level,
                logging.getLogger(n).propagate, logging.getLogger(n).disabled) for n in names}


def _restore(saved):
    for n, (handlers, level, propagate, disabled) in saved.items():
        lg = logging.getLogger(n)
        lg.handlers[:] = handlers
        lg.setLevel(level)
        lg.propagate, lg.disabled = propagate, disabled


def _ws_line_after(configs):
    """Configure uvicorn servers in this order (each Config runs dictConfig on the
    process-wide uvicorn loggers), then log the WebSocket accept line uvicorn writes."""
    import io

    import uvicorn

    stream = io.StringIO()
    for log_config in configs:
        kwargs = {"log_config": log_config} if log_config is not None else {}
        uvicorn.Config(lambda scope, receive, send: None, log_level="info", **kwargs)
    for name in ("uvicorn", "uvicorn.error"):
        for handler in logging.getLogger(name).handlers:
            handler.setStream(stream)
    logging.getLogger("uvicorn.error").info('%s - "WebSocket %s" [accepted]', "127.0.0.1:1", f"/ws?token={_JWT}")
    return stream.getvalue()


def test_one_server_without_the_redacting_config_unmasks_them_all():
    """Measured live: the HTTPS proxy built its Config with uvicorn's defaults, and when it
    started after the main port the desktop's token was written in full. Whichever server
    configures the shared loggers last decides - so every one has to carry the filter."""
    saved = _uvicorn_logger_state()
    try:
        leaked = _ws_line_after([redacted_uvicorn_log_config(), None])
        assert _JWT in leaked, "the mechanism: a default Config configured last strips the mask"
        masked = _ws_line_after([redacted_uvicorn_log_config(), redacted_uvicorn_log_config()])
        assert _JWT not in masked and "token=***" in masked
    finally:
        _restore(saved)
