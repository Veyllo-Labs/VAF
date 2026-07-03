# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Media-capture grant policy for the desktop window (macOS WKUIDelegate handler).

The decision is a pure function so it is testable off-macOS. Policy: grant ONLY
microphone capture requested by the local WebUI. The window's main frame can host
non-local pages (in-window GitHub OAuth; HuggingFace model-card preview links
navigate wherever the publisher points them), and the macOS TCC prompt fires only
once per host app - an unconditional grant would hand such pages a silent live
mic after the user's first legitimate STT use. Camera is denied because the host
bundle only declares NSMicrophoneUsageDescription (touching a TCC resource
without its usage description gets the process killed by macOS).
"""
from vaf.core.desktop_window import (
    _media_capture_decision,
    _WK_MEDIA_CAPTURE_MICROPHONE,
)

WK_CAMERA = 0
WK_CAMERA_AND_MIC = 2


def test_local_microphone_is_granted():
    assert _media_capture_decision(["127.0.0.1"], _WK_MEDIA_CAPTURE_MICROPHONE) is True
    assert _media_capture_decision(["localhost"], _WK_MEDIA_CAPTURE_MICROPHONE) is True
    assert _media_capture_decision(["::1"], _WK_MEDIA_CAPTURE_MICROPHONE) is True
    # origin + top-frame host both readable and local
    assert _media_capture_decision(["127.0.0.1", "localhost"], _WK_MEDIA_CAPTURE_MICROPHONE) is True


def test_non_local_origins_are_denied():
    """In-window OAuth / model-card navigation targets must never get capture."""
    assert _media_capture_decision(["github.com"], _WK_MEDIA_CAPTURE_MICROPHONE) is False
    assert _media_capture_decision(["evil.example"], _WK_MEDIA_CAPTURE_MICROPHONE) is False
    # Mixed: a non-local frame inside a local page (or vice versa) -> deny
    assert _media_capture_decision(["127.0.0.1", "evil.example"], _WK_MEDIA_CAPTURE_MICROPHONE) is False


def test_unreadable_origin_fails_closed():
    assert _media_capture_decision([], _WK_MEDIA_CAPTURE_MICROPHONE) is False
    assert _media_capture_decision(None or [], _WK_MEDIA_CAPTURE_MICROPHONE) is False


def test_camera_capture_is_denied_even_locally():
    """Only NSMicrophoneUsageDescription is installed - a camera grant would let a
    page trigger a TCC-unlisted capture and macOS kills the process for that."""
    assert _media_capture_decision(["127.0.0.1"], WK_CAMERA) is False
    assert _media_capture_decision(["127.0.0.1"], WK_CAMERA_AND_MIC) is False
