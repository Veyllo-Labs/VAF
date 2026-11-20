# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Connections and the WhatsApp window read the answers of the switch, start and refresh calls.

THE DEFECT, measured on the code before this change: the Connections toggle saved the whole
`whatsapp_config` block and then called `POST /start` or `/stop`, read neither status and set
the badge optimistically, so a 400 was silent and an admin's switch-off stopped every account's
WhatsApp; the disconnect called the same whole-bridge `/stop` before its reset; the window's
"Start WhatsApp" restarted every account's connection for an admin, and Refresh read neither
`sync-chats` nor its start. An admin's restart notice stayed until the next restart, because
the payload said nothing about the accounts it named.

THE RULE: the toggle, the banner's Start and Refresh call `PUT /api/whatsapp/switch` (the
caller's own WhatsApp) and show a refusal (`refusalText`: the server's `detail`, else the
fallback in the person's language); the notice clears itself once `restartNoticeResolved` says
every account it names runs again.

The two helpers run in node (skipped without node or web/node_modules/typescript); the
components are pinned by source slices.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
TS = WEB / "node_modules" / "typescript"
PANEL = WEB / "components" / "connections" / "ConnectionsPanel.tsx"
DASHBOARD = WEB / "components" / "connections" / "WhatsAppDashboard.tsx"


def _run_ts(module: str, cases):
    if shutil.which("node") is None or not TS.is_dir():
        pytest.skip("node or web/node_modules/typescript is not installed")
    script = (
        "const ts = require(process.argv[1]);"
        "const fs = require('fs');"
        "const src = fs.readFileSync(process.argv[2], 'utf8');"
        "const js = ts.transpileModule(src, {compilerOptions: {module: ts.ModuleKind.CommonJS,"
        " target: ts.ScriptTarget.ES2019}}).outputText;"
        "const m = {exports: {}}; new Function('module', 'exports', js)(m, m.exports);"
        "const cases = JSON.parse(process.argv[3]);"
        "process.stdout.write(JSON.stringify(cases.map(([fn, args]) => m.exports[fn](...args))));"
    )
    out = subprocess.run(["node", "-e", script, str(TS), str(WEB / "lib" / module), json.dumps(cases)],
                         capture_output=True, text=True, check=True, timeout=60).stdout
    return json.loads(out)


def _src(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _between(text: str, start: str, end: str, after: int = 0) -> str:
    i = text.index(start, after)
    return text[i:text.index(end, i)]


# -- the helpers -----------------------------------------------------------------------------------

def test_refusal_text():
    """MUTATION: `refusalText` returns a list `detail` (a 422's validation errors)."""
    fallback = "WhatsApp could not be switched. Try again."
    cases = [
        ["refusalText", [True, {"detail": "ignored"}, fallback]],
        ["refusalText", [False, {"detail": "No account has WhatsApp linked and switched on."}, fallback]],
        ["refusalText", [False, {"detail": ""}, fallback]],
        ["refusalText", [False, {"detail": "   "}, fallback]],
        ["refusalText", [False, {"detail": [{"loc": ["body", "enabled"], "msg": "Input should be a valid boolean"}]}, fallback]],
        ["refusalText", [False, None, fallback]],
        ["refusalText", [False, "Internal Server Error", fallback]],
        ["refusalText", [False, {}, fallback]],
    ]
    assert _run_ts("apiRefusal.ts", cases) == [
        None, "No account has WhatsApp linked and switched on.", fallback, fallback, fallback, fallback, fallback,
        fallback,
    ]


def test_restart_notice_resolves():
    """MUTATION: treat an absent account as unresolved; resolve on a null map."""
    cases = [
        ["restartNoticeResolved", [["alice", "bob"], {"admin": True, "alice": True, "bob": True}]],
        ["restartNoticeResolved", [["alice", "bob"], {"admin": True, "alice": True, "bob": False}]],
        ["restartNoticeResolved", [["alice"], {"admin": True}]],
        ["restartNoticeResolved", [["alice"], None]],
        ["restartNoticeResolved", [["constructor"], {"admin": True}]],
        ["restartNoticeResolved", [[], {}]],
    ]
    assert _run_ts("whatsappRestartNotice.ts", cases) == [True, False, True, False, True, True]


# -- AC10: the toggle and the disconnect -----------------------------------------------------------

def test_the_toggle_and_the_disconnect_act_on_the_callers_own_whatsapp():
    """MUTATION: the toggle back to the PATCH plus start/stop; the disconnect's `/stop` back."""
    src = _src(PANEL)
    handler = _between(src, "const handleToggleConnection", "const handleDisconnect")
    toggle = handler[handler.index("if (appId === 'whatsapp')"):]
    for needle in ("api('api/whatsapp/switch')", "method: 'PUT'", "JSON.stringify({ enabled })",
                   "refusalText(res.ok, json, t('whatsappSwitchFailed'))", "setWhatsappSwitchError(refused)",
                   "await checkConnectionStatus()", "enabled: whatsappEnabled"):
        assert needle in toggle, needle
    for gone in ("api/whatsapp/stop", "api/whatsapp/start", "api('api/config')", "'PATCH'"):
        assert gone not in toggle, gone
    assert "setWhatsappSwitchError(null)" in toggle.split("fetch(", 1)[0], "cleared on the next toggle"

    disconnect = _between(src, "const handleDisconnect", "if (appId === 'email')")
    whatsapp = disconnect[disconnect.index("if (appId === 'whatsapp')"):]
    assert "api/whatsapp/qr/reset" in whatsapp
    assert "api/whatsapp/stop" not in whatsapp and "api/whatsapp/start" not in whatsapp

    # The refusal's line under the card, and the status re-read that answers the stored switch.
    assert "{app.id === 'whatsapp' && whatsappSwitchError && (" in src
    assert 'role="alert">{whatsappSwitchError}</p>' in src
    status = _between(src, "const checkConnectionStatus", "const startTelegramPairing")
    assert "whatsappEnabled = status.enabled" in status and "return { whatsappEnabled };" in status


# -- AC11: the window's Start and Refresh ------------------------------------------------------

def test_the_dashboard_starts_its_own_whatsapp_and_reads_the_answers():
    """MUTATION: the banner's Start back to `handleRestartBridge`; Refresh back to
    `POST /start` without reading the answer; drop the read of `sync-chats`."""
    src = _src(DASHBOARD)
    start_own = _between(src, "const startOwn = ", "const handleRefresh")
    assert "api('api/whatsapp/switch')" in start_own and "method: 'PUT'" in start_own
    assert "JSON.stringify({ enabled: true })" in start_own

    refresh = _between(src, "const handleRefresh", "const handleStartOwn")
    assert "api/whatsapp/start" not in refresh
    assert "await startOwn()" in refresh
    assert "refusalText(sync.ok," in refresh and "refusalText(start.ok," in refresh
    assert refresh.count("t('refreshFailed')") == 3 and refresh.count("setNote(") == 3

    handler = _between(src, "const handleStartOwn", "const saveWhatsAppConfig")
    assert "await startOwn()" in handler
    assert "refusalText(res.ok, await res.json().catch(() => null), t('startFailed'))" in handler
    assert "setRestartError(refused)" in handler and "setRestartError(t('startFailed'))" in handler

    stopped = _between(src, "const stoppedBanner", "const banner = ")
    assert "onClick={handleStartOwn}" in stopped
    assert "handleRestartBridge" not in stopped
    # The gear's Restart is the admin's restart of every account, unchanged.
    settings = _between(src, "const settingsContent", "</SettingsCard>")
    assert "onClick={handleRestartBridge}" in settings


# -- AC12: the notice clears itself ---------------------------------------------------------------

def test_the_restart_notice_clears_itself():
    """MUTATION: drop the clear; drop the quiet re-check."""
    src = _src(DASHBOARD)
    assert "restartNoticeResolved(restartNoticeNames, data?.running_by_account ?? null)" in src
    effect = _between(src, "if (restartNotice && restartNoticeResolved(", "}, [data, restartNotice, restartNoticeNames]);")
    assert "setRestartNotice(null)" in effect and "setRestartNoticeNames([])" in effect
    timer = _between(src, "if (!isOpen || !restartNotice) return;", "}, [isOpen, restartNotice, fetchDashboard]);")
    assert "setInterval(() => { fetchDashboard({ quiet: true }); }, 60000)" in timer
    assert "clearInterval(timer)" in timer
    fetch = _between(src, "const fetchDashboard = useCallback(", "}, [t]);")
    assert "if (!quiet) {" in fetch and "if (!quiet) setLoadFailed(true)" in fetch
    assert "if (!quiet) setLoading(false)" in fetch
    assert "running_by_account:" in fetch
    restart = _between(src, "const handleRestartBridge", "const handleRelink")
    assert "setRestartNoticeNames(notStarted)" in restart
    assert "setRestartNoticeNames([])" in restart.split("try {", 1)[0]


# -- AC14: the new keys ----------------------------------------------------------------------------

KEYS = (("connectionsPanel", "whatsappSwitchFailed"), ("whatsappDashboard", "startFailed"),
        ("whatsappDashboard", "refreshFailed"))


def test_the_new_keys_exist_everywhere():
    catalogues = sorted((WEB / "messages").glob("*.json"))
    assert len(catalogues) == 7
    for path in catalogues:
        settings = json.loads(path.read_text(encoding="utf-8"))["settings"]
        for section, key in KEYS:
            value = settings[section][key]
            assert isinstance(value, str) and value.strip(), (path.name, key)
            assert "\u2014" not in value and "{" not in value, (path.name, key)
    src = _src(PANEL) + _src(DASHBOARD)
    for _, key in KEYS:
        assert f"t('{key}')" in src, key
