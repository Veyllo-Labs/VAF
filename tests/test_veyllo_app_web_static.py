# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The Veyllo App window, its Connections card and its rows under Account, pinned at the source
(web/ has no JS test runner).

The pairing states, the refusals, the relay's problems, its states and the kinds of an admin
action's failure each live in three places: the server (vaf/core/app_link, the routes), the
window (which code it words) and the catalogues (the words). A code added on the server and
forgotten in the window would reach the person as a bare code or as nothing; these guards say
so first, in the same way as the certificate section's reason codes.
"""
import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
WIZARD = WEB / "components" / "connections" / "VeylloAppSetupWizard.tsx"
PANEL = WEB / "components" / "connections" / "ConnectionsPanel.tsx"
SESSIONS = WEB / "components" / "settings" / "SessionsSection.tsx"
MODAL = WEB / "components" / "SettingsModal.tsx"
CSS = WEB / "app" / "globals.css"
LOCALES = ("en", "de", "ja", "ko", "th", "tr", "zh")


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _catalog(locale: str) -> dict:
    return json.loads((WEB / "messages" / f"{locale}.json").read_text(encoding="utf-8"))


def _window_keys(locale: str = "en") -> dict:
    return _catalog(locale)["settings"]["veylloApp"]


def _const_block(source: str, name: str) -> str:
    match = re.search(rf"const {name}\b[^=]*=\s*([\[{{][\s\S]*?[\]}}])(?: as const)?;", source)
    assert match, f"{name} moved or was renamed"
    return match.group(1)


def _between(source: str, start: str, end: str) -> str:
    i = source.index(start)
    return source[i:source.index(end, i + len(start))]


# ── the card ────────────────────────────────────────────────────────────────────

def test_the_card_opens_the_window_and_has_no_toggle():
    """The card, its gear and Connect open the window; there is no on/off switch and no
    disconnect for the Veyllo App (access ends per device). MUTATION: drop the card's own
    gear branch, so the configured card falls into the toggle-and-disconnect branch - red."""
    src = _src(PANEL)
    row = _between(src, "id: 'veyllo_app'", "},")
    assert "configKey: 'veyllo_app_config'" in row and "available: true" in row
    assert "icon: Smartphone" in row and "iconColor: 'bg-teal-700'" in row
    # The three ways in: the card (openApp), the gear of a paired card, Connect of an unpaired one.
    open_app = _between(src, "const openApp = () => {", "};")
    assert "if (app.id === 'veyllo_app') onOpenVeylloAppWizard?.();" in open_app
    gear = _between(src, "app.id === 'veyllo_app' && configured ? (", ") : configured ? (")
    assert "onClick={openApp}" in gear
    assert "handleToggleConnection" not in gear and "handleDisconnect" not in gear
    connect = _between(src, "if (app.id === 'whatsapp') onOpenWhatsAppWizard?.();", "if (app.id === 'email')")
    assert "if (app.id === 'veyllo_app') onOpenVeylloAppWizard?.();" in connect
    # Its branch comes before the generic configured branch, which holds the toggle.
    assert src.index("app.id === 'veyllo_app' && configured ? (") < src.index(") : configured ? (")
    for handler in ("const handleToggleConnection", "const handleDisconnect"):
        body = _between(src, handler, "\n    };\n")
        assert "veyllo_app" not in body, handler
    # The description and the status line are translated.
    assert "t('veylloApp.description')" in src
    assert "'veylloApp.cardLineOn' : 'veylloApp.cardLineOff'" in src
    for locale in LOCALES:
        block = _catalog(locale)["settings"]["connectionsPanel"]["veylloApp"]
        assert set(block) == {"description", "cardLineOn", "cardLineOff"}, locale
    assert "onOpenVeylloAppWizard={() => setShowVeylloAppWizard(true)}" in _src(MODAL)


# ── step one ────────────────────────────────────────────────────────────────────

def test_step_one_says_what_the_frd_asks():
    """What the app can do, what Veyllo sees, the cost of each way, the app's name for VAF and
    that the app's agent uses VAF as a tool with the privacy policy beside it. MUTATION:
    delete the agentUsesVaf line from the JSX - red."""
    src = _src(WIZARD)
    explain = _between(src, "const explain = (", "const qr = (")
    for key in ("canChat", "canReceive", "canTasks", "appName", "agentUsesVaf", "privacyLink",
                "seesRelay", "seesDirect", "costSentence", "showQr"):
        assert f"t('{key}')" in explain, key
        for locale in LOCALES:
            assert _window_keys(locale).get(key), (locale, key)
    # A plain link to the policy, opened in a new tab; VAF never fetches it.
    assert "const PRIVACY_URL = 'https://veyllo.app/legal/privacy';" in src
    link = _between(explain, "<a href={PRIVACY_URL}", "</a>")
    assert 'target="_blank"' in link and 'rel="noopener noreferrer"' in link
    assert src.count("PRIVACY_URL") == 2, "the policy URL is only linked, never fetched"
    # The names stay literal in every language.
    for locale in LOCALES:
        keys = _window_keys(locale)
        assert "Main-Agent (VAF)" in keys["appName"] and "Veyllo" in keys["agentUsesVaf"], locale
        assert keys["wayRelay"] == "Veyllo Relay" and keys["title"] == "Veyllo App", locale


def test_every_refusal_has_a_sentence():
    """Every code the window and QR routes can refuse with is worded, in all seven catalogues.
    MUTATION: drop a refusal key from REFUSAL_KEYS - red."""
    from vaf.api import app_link_routes, auth_routes
    tree = ast.parse(Path(auth_routes.__file__).read_text(encoding="utf-8"))
    func = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "own_second_factor_refusal")
    twofa = {v.value for d in ast.walk(func) if isinstance(d, ast.Dict)
             for k, v in zip(d.keys, d.values)
             if isinstance(k, ast.Constant) and k.value == "code" and isinstance(v, ast.Constant)}
    assert twofa == {"twofa_not_set_up", "twofa_unreadable", "code_spent", "wrong_code"}, twofa
    server = set(app_link_routes._REFUSALS) | {"bad_mode"} | twofa
    block = _const_block(_src(WIZARD), "REFUSAL_KEYS")
    entries = dict(re.findall(r"(\w+): \{ ns: '(?:own|account)', key: '([\w.]+)' \}", block))
    assert set(entries) == server
    own = {k for k, v in entries.items() if v.startswith("refusals.")}
    for locale in LOCALES:
        cat = _catalog(locale)
        assert set(cat["settings"]["veylloApp"]["refusals"]) == own, locale
        for code, key in entries.items():
            node = cat["settings"]["veylloApp"] if key.startswith("refusals.") else cat["account"]
            for part in key.split("."):
                node = node[part]
            assert isinstance(node, str) and node, (locale, code)
        assert cat["settings"]["veylloApp"]["refusalNoDirectAdmin"], locale


# ── step two ────────────────────────────────────────────────────────────────────

def test_the_qr_never_leaves_this_machine():
    """The QR is the server's own PNG: one image, a data URL. That no web image anywhere is
    fetched from a third-party renderer is the repo-wide guard in test_qr_drawn_on_this_machine.py.
    MUTATION: another image source - red."""
    src = _src(WIZARD)
    images = re.findall(r"<img\b[^>]*>", src, re.S)
    assert len(images) == 1, images
    assert "src={'data:image/png;base64,' + qrPng}" in images[0]
    assert "setQrPng(String(data.qr_png || ''))" in src
    # The countdown is the server's own clock, counted from the answer.
    assert "(Number(data.expires_at) - Number(data.server_time)) * 1000" in src


def test_relay_off_means_local_only():
    """The radiogroup only exists while the relay is allowed; off shows the admin's sentence.
    With no way at all there is no code field. MUTATION: render the radiogroup
    unconditionally - red."""
    src = _src(WIZARD)
    assert "const relayAllowed = status?.relay.enabled !== false;" in src
    assert "const impossible = !!status && !relayAllowed && !directAvailable;" in src
    qr = _between(src, "const qr = (", "const confirmView")
    allowed = _between(qr, "{relayAllowed ? (", "{t('relayOffByAdmin')}")
    assert "<WaySwitch" in allowed
    assert qr.count("<WaySwitch") == 1
    explain = _between(src, "const explain = (", "const qr = (")
    gate = explain.index(") : impossible ? (")
    assert explain.index("t('impossible')") > gate
    assert explain.index("<form") > explain.index(") : (", gate), "the code field shows when pairing is impossible"
    # The first QR is relay only when the relay is allowed.
    assert "await issue(relayAllowed ? 'relay' : 'local');" in src


def test_every_pairing_state_has_a_view():
    """Every state the server can answer has a view and a sentence. MUTATION: drop
    used_elsewhere from PAIRING_VIEWS - red."""
    from vaf.core.app_link import pairing, relay
    states = set()
    tree = ast.parse(Path(pairing.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "state" \
                        and isinstance(node.value, ast.Constant):
                    states.add(node.value.value)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_finish" \
                and isinstance(node.args[1], ast.Constant):
            states.add(node.args[1].value)
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == "state" and isinstance(v, ast.Constant):
                    states.add(v.value)
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "state":
            states.add(node.value.value)
        if isinstance(node, ast.FunctionDef) and node.name == "confirm":
            states |= {r.value.value for r in ast.walk(node)
                       if isinstance(r, ast.Return) and isinstance(r.value, ast.Constant)}
    rtree = ast.parse(Path(relay.__file__).read_text(encoding="utf-8"))
    states |= {n.args[1].value for n in ast.walk(rtree) if isinstance(n, ast.Call)
               and getattr(n.func, "attr", "") == "mark_relay_outcome"}
    assert {"none", "issued", "claimed", "awaiting_confirm", "confirming", "paired", "denied",
            "expired", "used_elsewhere", "failed"} <= states
    block = _const_block(_src(WIZARD), "PAIRING_VIEWS")
    views = set(re.findall(r"^\s*(\w+): '\w+',$", block, re.M))
    assert views == states
    for locale in LOCALES:
        assert set(_window_keys(locale)["pairingStates"]) == states, locale


def test_relay_errors_states_and_reasons_are_worded():
    """The relay's problems for a waiting pairing, the connector's states and the kinds of an
    admin action's failure: each list equals the server's and the catalogues'. MUTATION: a
    relay state without a word - red."""
    from vaf.core.app_link import relay
    src = _src(WIZARD)
    rtree = ast.parse(Path(relay.__file__).read_text(encoding="utf-8"))
    errors = set()
    for node in ast.walk(rtree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", "")
            if name == "set_relay_error" and isinstance(node.args[0], ast.Constant):
                errors.add(node.args[0].value)
            if name == "mark_relay_outcome" and isinstance(node.args[2], ast.Constant):
                errors.add(node.args[2].value)
    assert errors == set(re.findall(r"'(\w+)'", _const_block(src, "RELAY_ERRORS")))
    help_states = set(re.findall(r"'(\w+)'", _const_block(src, "RELAY_STATE_HELP")))
    assert help_states <= set(relay.STATES)
    reasons = set(re.findall(r"'(\w+)'", _between(src, "const failureText", "};")))
    assert set(relay.FAILURE_REASONS) <= reasons
    for locale in LOCALES:
        keys = _window_keys(locale)
        assert set(keys["relayErrors"]) == errors, locale
        assert set(keys["relayStates"]) == set(relay.STATES), locale
        assert set(keys["relayStateHelp"]) == help_states, locale
        assert set(keys["failureReasons"]) == set(relay.FAILURE_REASONS), locale


# ── "Is this your device?" ──────────────────────────────────────────────────────

def test_the_confirm_shows_the_fingerprint_as_sent():
    """The fingerprint is shown exactly as the server sent it, and Yes and No answer the
    confirmation. MUTATION: `.toLowerCase()` on the fingerprint; Yes posting accept:false - red."""
    src = _src(WIZARD)
    view = _between(src, "const confirmView", "const endText")
    assert "{pairing.device.fingerprint}</p>" in view
    for field in ("pairing.device.model", "platform: pairing.device.platform", "version: pairing.device.app_version"):
        assert field in view, field
    yes = _between(view, "onClick={() => void answer(true)}", "</button>")
    no = _between(view, "onClick={() => void answer(false)}", "</button>")
    assert "t('confirmYes')" in yes and "t('confirmNo')" in no
    answer = _between(src, "const answer = async (accept: boolean)", "\n    };\n")
    assert "api('/pairings/current/confirm')" in answer and "JSON.stringify({ accept })" in answer


# ── the poll and the close ──────────────────────────────────────────────────────

def test_the_poll_and_the_close():
    """The state is asked every 2 s only while open, visible and still running; closing
    withdraws a live QR and refuses a waiting phone. MUTATION: drop the visibility guard;
    drop the DELETE - red."""
    src = _src(WIZARD)
    running = set(re.findall(r"'(\w+)'", _between(src, "const RUNNING", ";")))
    assert running == {"issued", "claimed", "awaiting_confirm", "confirming"}
    poll = _between(src, "// The poll:", "}, [isOpen, pairing.state, applyPairing]);")
    assert "if (!isOpen || !RUNNING.has(pairing.state)) return;" in poll
    assert "if (document.visibilityState !== 'visible') return;" in poll
    assert re.search(r"\}, 2000\);", poll)
    leave = _between(src, "const leave = useCallback(() => {", "}, []);")
    withdraw = _between(leave, "if (state === 'issued' || state === 'claimed') {", "} else if")
    assert "withdrawCurrent();" in withdraw
    helper = _between(src, "const withdrawCurrent = () => {", "};")
    assert "api('/pairings/current'), { method: 'DELETE'" in helper
    deny = _between(leave, "} else if (state === 'awaiting_confirm') {", "setPairing(")
    assert "api('/pairings/current/confirm')" in deny and "JSON.stringify({ accept: false })" in deny
    # The QR and the device leave the page's state with the window.
    assert "setQrPng(null);" in leave and "setPairing({ state: 'none' });" in leave
    close = _between(src, "const close = () => {", "};")
    assert "leave();" in close and "onClose();" in close
    # X, Escape and the backdrop all go through close; so does a close from outside.
    assert "useEscapeLayer({ active: isOpen, level: 60, onEscape: close });" in src
    assert "onClick={close}>" in src and "return () => leave();" in src


def test_a_code_nobody_shows_is_withdrawn():
    """Two ways a code stays live on the server while no screen shows it: a refused new code
    (the server keeps the earlier one), and an answer that arrives after the window closed.
    Both withdraw it; an answer overtaken by a newer issue() does not, since the newer code
    replaces it on the server. MUTATION: drop the DELETE in the refusal branch; drop the
    one in the overtaken branch; withdraw also when a newer issue() started - red."""
    src = _src(WIZARD)
    issue = _between(src, "const issue = useCallback(async (mode: Way) => {", "}, [refusalText, t]);")
    assert "issueSeq.current = seq;" in issue
    overtaken = _between(issue, "if (seq !== pollSeq.current) {", "return;")
    assert "if (res.ok && issueSeq.current === seq) withdrawCurrent();" in overtaken
    refused = _between(issue, "if (!res.ok) {", "if (data.code === 'window_closed') {")
    assert "withdrawCurrent();" in refused


def test_the_switch_is_the_d14_switch():
    """Roles, keys and the exact motion of the way switch, mirrored for right-to-left and
    still for reduced motion. MUTATION: drop the reduced-motion twin; change 1.25 - red."""
    src = _src(WIZARD)
    switch = _between(src, "function WaySwitch(", "export default function")
    for needle in ('role="radiogroup"', 'role="radio"', "aria-checked={checked}", "aria-label={groupLabel}",
                   "tabIndex={checked ? 0 : -1}", "ArrowRight", "ArrowLeft", "Home:", "End:",
                   "vaf-way-thumb", "data-way={value}", "start-1"):
        assert needle in switch, needle
    css = _src(CSS)
    assert ".vaf-way-thumb { transition: transform .45s cubic-bezier(.3,1.25,.4,1); }" in css
    assert '.vaf-way-thumb[data-way="local"] { transform: translateX(100%); }' in css
    assert '[dir="rtl"] .vaf-way-thumb[data-way="local"] { transform: translateX(-100%); }' in css
    enter = _between(css, "@keyframes vafWayEnter {", "}\n}")
    assert "translateY(6px)" in enter and "blur(6px)" in enter
    assert ".vaf-way-enter { animation: vafWayEnter .5s cubic-bezier(.22,1,.36,1) both; }" in css
    reduced = css[css.index(".vaf-way-enter { animation: vafWayEnter .5s"):]
    reduced = _between(reduced, "@media (prefers-reduced-motion: reduce) {", "\n}\n")
    assert ".vaf-way-thumb { transition: none; }" in reduced
    twin = _between(reduced, "@keyframes vafWayEnter", "}\n")
    assert "opacity" in twin and "transform" not in twin and "filter" not in twin
    assert ".vaf-way-enter { animation: vafWayEnter .2s linear both; }" in reduced
    # The QR and its explanation fade in again on every switch.
    assert 'key={way} className="vaf-way-enter' in src


def test_no_coloured_icons_in_the_pairing_view():
    """Black and white only: success and failure are said by the text. MUTATION:
    `text-green-600` on the paired icon - red."""
    src = _src(WIZARD)
    assert not re.search(r"\btext-(?:green|teal|blue|emerald|sky|indigo|amber)-\d+", src)
    assert not re.search(r"\bbg-(?:green|teal|blue|emerald|sky|indigo|amber)-\d+", src)


# ── Account and the admin section ───────────────────────────────────────────────

def test_account_lists_and_removes_app_devices():
    """The account's Veyllo Apps are rows of "Signed-in devices", removed after a question;
    the sessions and the devices load and reload together. MUTATION: drop the reload after
    signing out the other devices - red."""
    src = _src(SESSIONS)
    load = _between(src, "const load = useCallback(async () => {", "}, [apiBase]);")
    assert "/api/app-link/devices`" in load and "/api/auth/sessions`" in load
    assert "const appDevices = await apps;" in load and "setAppRows(appDevices);" in load
    remove = _between(src, "const removeApp = async", "\n    };\n")
    assert "/api/app-link/devices/${encodeURIComponent(row.id)}`" in remove and "method: 'DELETE'" in remove
    assert "void load();" in remove
    others = _between(src, "const endOthers = async () => {", "\n    };\n")
    assert "void load();" in others and "/api/auth/sessions/end-others" in others
    assert "confirm(" not in others, "the house dialog asks, not the browser's"
    assert "title={t('removeAppTitle')}" in src and "onConfirm={() => void endOthers()}" in src
    row = _between(src, "{appRows.map(row => (", "</li>")
    for needle in ("t('veylloAppBadge')", "t('wayRelay')", "t('wayDirect')", "t('connectedNow')",
                   "t('appLastConnected'", "t('appPaired'", "t('appFingerprint'", "setAsking(row)"):
        assert needle in row, needle
    # Unix seconds become a date before the shared formatter.
    assert "new Date(sec * 1000)" in src
    # Sign out every other device shows with an app device even when no other session is open.
    assert "const others = rows.some(r => !r.current) || appRows.length > 0;" in src


def test_sign_out_others_says_it_removes_the_app():
    """MUTATION: the old confirm text in one locale - red."""
    for locale in LOCALES:
        account = _catalog(locale)["account"]
        assert "Veyllo App" in account["confirmSignOutOthers"], locale
        assert "90" in account["devicesIntro"] and "Veyllo App" in account["devicesIntro"], locale


def test_admin_section_is_admins_only():
    """The relay switch, rotate, dissolve and every account's devices render for an admin
    only. MUTATION: render the section for everyone - red."""
    src = _src(WIZARD)
    assert "{isAdmin && view === 'explain' && <RelayAdminSection" in src
    assert src.count("<RelayAdminSection") == 1
    assert "isAdmin={currentUser?.role === 'admin'}" in _src(MODAL)
    section = src[src.index("function RelayAdminSection("):]
    assert "api('/devices?all=1')" in section and "api('/relay')" in section
    assert "onClick={() => setAsking('rotate')}" in section
    assert "open={asking === 'rotate'}" in section and "onConfirm={() => void rotate()}" in section
    rotate = _between(section, "const rotate = async", "\n    };\n")
    assert "api('/relay/rotate')" in rotate
    dissolve = _between(section, "const dissolve = async", "\n    };\n")
    assert "api('/relay/dissolve')" in dissolve and "JSON.stringify({ code: code.trim() })" in dissolve
    assert "<Switch on={relay.enabled}" in section and "{row.username}" in section


def test_the_window_promises_what_each_way_delivers():
    """Push reaches a phone only when it paired through the Veyllo relay (no relay member, no
    notify), a task for the phone costs half a cent per direction, 2FA is set up at sign-in or
    after an admin's reset, and the switch that gives the phone a direct address is named as the
    Local Network tab names it. MUTATION: the old Local text without the push sentence in one
    locale; the cost sentence without the task price - red."""
    for locale in LOCALES:
        cat = _catalog(locale)
        keys = cat["settings"]["veylloApp"]
        assert "veyllo relay" in keys["canReceive"].lower(), locale
        assert "Veyllo Relay" in keys["explainLocal"], locale
        for key in ("costSentence", "explainRelay"):
            assert any(w in keys[key] for w in ("half", "halbe", "0.5", "yarım")), (locale, key)
        hosting = cat["settings"]["localNetwork"]["enableHosting"]
        for text in (keys["impossibleAdmin"], keys["refusalNoDirectAdmin"], keys["refusals"]["no_direct_path"]):
            assert hosting in text, (locale, text)
        assert "2FA" in keys["refusals"]["twofa_not_set_up"], locale
    en = _catalog("en")["settings"]["veylloApp"]
    assert "User Management" in en["refusals"]["twofa_not_set_up"]
    assert "(으)로" not in _catalog("ko")["settings"]["veylloApp"]["appName"]
    assert "Wähle Lokal" in _catalog("de")["settings"]["veylloApp"]["relayErrors"]["relay_unavailable"]


def test_no_dot_ends_a_device_line_on_a_phone():
    """Below md the fingerprint takes a line of its own and the separator before it goes, in the
    Account rows and in the admin list. MUTATION: show the dot below md again - red."""
    for path in (SESSIONS, WIZARD):
        src = _src(path)
        assert "<span className=\"max-md:hidden\">{' · '}</span>" in src, path.name
        assert "font-mono whitespace-nowrap max-md:block\">" in src, path.name
