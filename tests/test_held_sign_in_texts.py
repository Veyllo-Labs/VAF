# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""What a person sees when a sign-in is held or a session ended, in their own language.

The 429 of every window carried an English sentence ("Too many failed attempts. Try again in
15 minutes.", or "1440 minutes" for a second factor held for a day), and the login page, the
2FA step, the setup-code step and the step-up dialogs put it on screen as sent. A window whose
session ended elsewhere went to a bare login page. The pages now read the status and the
Retry-After themselves (web/lib/retryAfter.ts) and say it from the catalogues. Static checks:
the web UI has no unit test runner, and the build runs in the CI lane.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGIN = (ROOT / "web" / "app" / "login" / "page.tsx").read_text(encoding="utf-8")
APP = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
ACCOUNT = (ROOT / "web" / "components" / "settings" / "AccountSection.tsx").read_text(encoding="utf-8")
DASHBOARD = (ROOT / "web" / "components" / "NotificationsModal.tsx").read_text(encoding="utf-8")


def test_every_sign_in_door_says_a_hold_in_the_readers_language():
    """MUTATION: show the server's sentence for a 429 again on any of the four - red."""
    assert "setLoginError(res.status === 429 ? heldText(res)" in LOGIN
    assert "setSetupCodeError(res.status === 429 ? heldText(res)" in LOGIN
    assert "setBootstrapError(bootstrapRes.status === 429 ? heldText(bootstrapRes)" in LOGIN
    assert LOGIN.count("setTwoFAError(codeErrorText(res, data));") == 2
    assert "if (res.status === 429) return heldText(res);" in LOGIN
    assert "tCommon('tooManyAttempts', { when: retryWhen(retryAfterSeconds(res), locale) })" in LOGIN


def test_a_code_answer_is_named_for_what_it_is():
    """Held, the step ran out, used once already, or wrong."""
    for code, key in (("sign_in_expired", "signInExpired"), ("code_spent", "codeSpent")):
        assert re.search(rf"data\?\.code === '{code}'\) return tAuth\('{key}'\)", LOGIN), code


def test_a_session_that_ended_elsewhere_is_named_on_the_login_page():
    """The page gate renders the app only for an unexpired cookie, so a refusal there always
    means the session ended. MUTATION: redirect without the reason, or drop the notice - red."""
    assert APP.count("/login?reason=ended`);") == 2
    assert "get('reason') === 'ended'" in LOGIN
    assert "{tAuth('sessionEnded')}" in LOGIN


def test_the_password_change_and_the_step_up_name_the_wait():
    assert "throttled: t('errThrottled', { when })" in ACCOUNT
    assert "code_spent: t('errCodeSpent')" in ACCOUNT
    assert "res.status === 429 && !data.code ? tCommon('tooManyAttempts', { when })" in ACCOUNT
    assert DASHBOARD.count("setQError(res.status === 429 ? heldText(res)") == 2


def test_the_sentences_carry_the_wait():
    for locale in ("en", "de", "ja", "ko", "zh", "tr", "th"):
        cat = json.loads((ROOT / "web" / "messages" / f"{locale}.json").read_text(encoding="utf-8"))
        assert "{when}" in cat["common"]["tooManyAttempts"], locale
        assert "{when}" in cat["account"]["errThrottled"], locale
        for key in ("codeSpent", "sessionEnded", "signInExpired"):
            assert cat["auth"][key], (locale, key)
        assert cat["account"]["errCodeSpent"], locale


_TS = ROOT / "web" / "node_modules" / "typescript"


def _retry_when(cases):
    """The web's REAL retryWhen, transpiled with the app's own TypeScript and run in node, so
    the locale data is the one a browser has (ICU), not a copy of it here."""
    import shutil
    import subprocess

    import pytest
    if shutil.which("node") is None or not _TS.is_dir():
        pytest.skip("node or web/node_modules/typescript is not installed")
    script = (
        "const ts = require(process.argv[1]);"
        "const fs = require('fs');"
        "const src = fs.readFileSync(process.argv[2], 'utf8');"
        "const js = ts.transpileModule(src, {compilerOptions: {module: ts.ModuleKind.CommonJS,"
        " target: ts.ScriptTarget.ES2019}}).outputText;"
        "const m = {exports: {}}; new Function('module', 'exports', js)(m, m.exports);"
        "const cases = JSON.parse(process.argv[3]);"
        "process.stdout.write(JSON.stringify(cases.map(([s, l]) => m.exports.retryWhen(s, l))));"
    )
    out = subprocess.run(["node", "-e", script, str(_TS), str(ROOT / "web" / "lib" / "retryAfter.ts"),
                          json.dumps(cases)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_the_wait_reads_naturally_in_japanese_and_stays_as_cldr_writes_it_elsewhere():
    """CLDR writes the Japanese wait with a space between the number and its counter, which
    Japanese sets together; Korean's space is correct and stays, Chinese has none. MUTATION:
    drop the Japanese close-up, or close up every locale's spaces - red."""
    ja15, ja2h, ko15, zh15, en15 = _retry_when(
        [[900, "ja"], [7200, "ja"], [900, "ko"], [900, "zh"], [900, "en"]])
    assert ja15 == "15分後" and ja2h == "2時間後"
    assert ko15 == "15분 후"
    assert zh15 == "15分钟后"
    assert en15 == "in 15 minutes"


def test_the_setup_command_is_marked_as_code_and_never_broken():
    """At phone width the command broke across lines ("vaf / setup code") and nothing showed
    where it ended. MUTATION: render the subtitle as plain text again - red."""
    assert "t.rich('setupCodeSubtitle', {" in LOGIN
    assert re.search(r"code: \(chunks\) => <code className=\"[^\"]*whitespace-nowrap[^\"]*\">\{chunks\}</code>",
                     LOGIN)
    for locale in ("en", "de", "ja", "ko", "zh", "tr", "th"):
        cat = json.loads((ROOT / "web" / "messages" / f"{locale}.json").read_text(encoding="utf-8"))
        assert "<code>vaf setup code</code>" in cat["onboarding"]["setupCodeSubtitle"], locale


def test_the_ended_notice_carries_its_icon():
    """MUTATION: drop the icon - red."""
    notice = LOGIN.split("{endedNotice && (", 1)[1].split("</p>", 1)[0]
    assert "<Info " in notice and "{tAuth('sessionEnded')}" in notice
