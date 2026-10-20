# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Where "create a Veyllo API key" leads: the dashboard on veyllo.app, in the UI's language.

The first-run wizard and Settings each linked to the start page of veyllo.app, where the
key is not made. Both now take the one address from web/lib/veyllo.ts."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


def test_the_wizard_and_settings_link_the_dashboard():
    """MUTATION: put 'https://veyllo.app' back as the wizard's link - red."""
    for rel in ("app/login/page.tsx", "components/SettingsModal.tsx"):
        source = (WEB / rel).read_text(encoding="utf-8")
        assert "veylloDashboardUrl(" in source, rel
        assert not re.search(r"""['"]https://veyllo\.app/?['"]""", source), rel


def test_the_address_is_the_localized_dashboard():
    helper = (WEB / "lib" / "veyllo.ts").read_text(encoding="utf-8")
    assert "`https://veyllo.app/${locale}/dashboard`" in helper
    assert "'https://veyllo.app/dashboard'" in helper, "English has no prefix there"
