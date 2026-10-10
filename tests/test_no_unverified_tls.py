# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""No request in vaf/ turns certificate verification off.

The last one was webfetch: on any TLS error it retried with ``verify=False`` and cached
the answer as the page. That is a silent downgrade on exactly the failure an interception
produces, and the cache then served the intercepted page to later calls. A certificate that
does not verify is an answer to report, not an obstacle to route around.

The one unverified handshake that remains is deliberate and not a request: the A2A trust
bootstrap (vaf/core/a2a/trust.py) fetches a peer's certificate chain WITHOUT trusting it,
to compare it against a fingerprint that arrived by another route. It is allowlisted by
path and must keep its ``CERT_NONE`` inside that one function.

The second is the FTP lane's pinned mode (vaf/core/ftp.py, `connect`): a server whose
certificate no authority vouches for is reached without that check, and its certificate's
SHA-256 fingerprint is compared with the one remembered at the first connection BEFORE the
password is sent; a different one is refused. The first connection itself is the person's to
confirm (the ssh lane's trust-on-first-use, a named boundary there). The authority check
always runs first; the pinned mode is never a fallback for a server confirmed through an
authority.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "vaf"

_UNVERIFIED = re.compile(r"verify\s*=\s*False|_create_unverified_context|CERT_NONE")
_ALLOWED = {Path("core/a2a/trust.py"): 1,   # the fingerprint-checked bootstrap, once
            Path("core/ftp.py"): 1}         # the pinned FTPS server, compared before login


def test_no_request_disables_certificate_verification():
    found = {}
    for path in ROOT.rglob("*.py"):
        text = path.read_bytes().decode("utf-8", errors="replace")
        hits = [ln for ln in text.splitlines()
                if _UNVERIFIED.search(ln) and not ln.lstrip().startswith("#")]
        if hits:
            found[path.relative_to(ROOT)] = hits
    unexpected = {p: h for p, h in found.items() if len(h) > _ALLOWED.get(p, 0)}
    assert not unexpected, f"certificate verification switched off: {unexpected}"
