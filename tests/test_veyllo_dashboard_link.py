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


def test_only_a_ui_locale_gets_a_prefix():
    """The helper asks the UI's own locale list, so a browser tag cannot become a path.
    MUTATION: drop `isSupportedLocale(locale)` from the condition - red."""
    helper = (WEB / "lib" / "veyllo.ts").read_text(encoding="utf-8")
    assert "import { isSupportedLocale } from './languages';" in helper
    assert "locale !== 'en' && isSupportedLocale(locale)" in helper


_TS = WEB / "node_modules" / "typescript"


def _dashboard_urls(locales):
    """The REAL veylloDashboardUrl and isSupportedLocale, transpiled with the app's own
    TypeScript and run in node (the pattern of tests/test_ask_user_chat.py)."""
    import json
    import shutil
    import subprocess
    import pytest
    if shutil.which("node") is None or not _TS.is_dir():
        pytest.skip("node or web/node_modules/typescript is not installed")
    script = (
        "const ts = require(process.argv[1]); const fs = require('fs'); const path = require('path');"
        "const load = (file, req) => {"
        " const js = ts.transpileModule(fs.readFileSync(file, 'utf8'), {compilerOptions:"
        " {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2019}}).outputText;"
        " const m = {exports: {}}; new Function('module', 'exports', 'require', js)(m, m.exports, req);"
        " return m.exports; };"
        "const lib = process.argv[2];"
        "const languages = load(path.join(lib, 'languages.ts'), require);"
        "const veyllo = load(path.join(lib, 'veyllo.ts'), (id) => {"
        " if (id === './languages') return languages; throw new Error('unexpected import ' + id); });"
        "const locales = JSON.parse(process.argv[3]);"
        "process.stdout.write(JSON.stringify(locales.map((l) => veyllo.veylloDashboardUrl(l))));"
    )
    out = subprocess.run(["node", "-e", script, str(_TS), str(WEB / "lib"), json.dumps(locales)],
                         capture_output=True, text=True, check=True).stdout
    return dict(zip(locales, json.loads(out)))


def test_the_url_for_every_locale():
    """Every UI locale keeps its localized dashboard, English has no prefix, anything else
    falls back to English. MUTATION: drop `isSupportedLocale(locale)` and 'en-US' leads to
    https://veyllo.app/en-US/dashboard - red."""
    codes = re.findall(r"code: '([a-z]+)'", (WEB / "lib" / "languages.ts").read_text(encoding="utf-8"))
    assert "de" in codes and "en" in codes
    odd = ["en-US", "de-DE", "EN", "", "xx", "../login"]
    urls = _dashboard_urls(codes + odd)
    for code in codes:
        want = "https://veyllo.app/dashboard" if code == "en" else f"https://veyllo.app/{code}/dashboard"
        assert urls[code] == want, code
    for locale in odd:
        assert urls[locale] == "https://veyllo.app/dashboard", locale
