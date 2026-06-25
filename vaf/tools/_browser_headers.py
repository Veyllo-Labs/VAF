# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Realistic, internally-consistent browser HTTP headers for the scrape/fetch paths.

A request that only sends `User-Agent` + `Accept` is itself a bot tell — real browsers
send a full, consistent header set. This returns one where the UA Chrome version matches
the `sec-ch-ua` client-hint versions (no inconsistency to flag — the same consistency
principle as the browser-agent hardening).

`Accept-Encoding` is deliberately NOT set: `requests` advertises only what it can actually
decode (gzip/deflate here — brotli isn't installed), so we avoid both a new dependency and
brotli-garbled response bodies.

Honest limit: this only fixes the HTTP *header* layer. A plain `requests` client still has a
recognisable TLS (JA3/JA4) fingerprint that a browser does not — defeating that needs a TLS
impersonation library, which we don't add. For hard targets, prefer the search APIs or route
through the (hardened) browser_agent.
"""
from __future__ import annotations

import random

# Each profile is internally consistent: the Chrome major in the UA == the sec-ch-ua version.
_CHROME = "131"
_PROFILES = [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", '"Windows"'),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", '"macOS"'),
    ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", '"Linux"'),
]
_CH_UA = f'"Google Chrome";v="{_CHROME}", "Chromium";v="{_CHROME}", "Not_A Brand";v="24"'
_ACCEPT = ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
           "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7")


def browser_headers(user_agent: str | None = None,
                    accept_language: str = "en-US,en;q=0.9",
                    referer: str | None = None) -> dict:
    """Return a full, consistent Chrome header set for a top-level document request.

    When the caller supplies its own `user_agent`, the Chromium client-hint headers are
    omitted — a custom UA may not be Chrome, and a mismatched UA <-> client-hints pair is
    itself detectable.
    """
    ua, platform = random.choice(_PROFILES)
    headers = {
        "User-Agent": user_agent or ua,
        "Accept": _ACCEPT,
        "Accept-Language": accept_language,
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin" if referer else "none",
        "Sec-Fetch-User": "?1",
    }
    if not user_agent:
        headers["sec-ch-ua"] = _CH_UA
        headers["sec-ch-ua-mobile"] = "?0"
        headers["sec-ch-ua-platform"] = platform
    if referer:
        headers["Referer"] = referer
    return headers
