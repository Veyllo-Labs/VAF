# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Every Microsoft sign-in VAF starts uses the identity platform's v2.0 endpoints.

OneDrive's authorize URL read `/common/oauth2/v2/authorize`, a path Microsoft does not
serve, next to a correct `/oauth2/v2.0/token`: "Connect OneDrive" could only fail at the
sign-in redirect, and no test looked at the URL. MUTATION: put the old OneDrive URL back -
both tests go red.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_MS = re.compile(r"https://login\.microsoftonline\.com/[^\s\"'`]+")


def test_the_provider_tables_use_v2_0():
    from vaf.cloud.oauth_cloud import CLOUD_PROVIDERS
    from vaf.core.oauth_pkce import PROVIDERS
    urls = [conf[key] for table in (CLOUD_PROVIDERS, PROVIDERS) for conf in table.values()
            for key in ("auth_url", "token_url") if "login.microsoftonline.com" in conf.get(key, "")]
    assert len(urls) >= 6, urls        # OneDrive, Outlook and Microsoft 365: authorize and token
    for url in urls:
        assert re.search(r"/oauth2/v2\.0/(authorize|token)$", url), url


def test_no_microsoft_oauth_url_in_the_tree_skips_v2_0():
    found = []
    for base in ("vaf", "web"):
        for path in sorted((ROOT / base).rglob("*")):
            if path.suffix not in (".py", ".ts", ".tsx") or "node_modules" in path.parts:
                continue
            for url in _MS.findall(path.read_bytes().decode("utf-8", "replace")):
                if "/oauth2/" in url:
                    found.append(url)
                    assert "/oauth2/v2.0/" in url, f"{path.relative_to(ROOT).as_posix()}: {url}"
    assert found, "the scan found no Microsoft OAuth URL at all"
