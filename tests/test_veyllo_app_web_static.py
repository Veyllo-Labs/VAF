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
SHELL = WEB / "components" / "connections" / "SetupWizardShell.tsx"
DISCORD = WEB / "components" / "connections" / "DiscordSetupWizard.tsx"
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


def _key(keys: dict, dotted: str) -> str:
    """A catalogue value by its dotted path inside settings.veylloApp."""
    node = keys
    for part in dotted.split("."):
        node = node[part]
    return node


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


def test_the_veyllo_app_card_comes_first_under_communication():
    """Nothing sorts the cards at render time, so CONNECTION_APPS order is screen order. The
    Veyllo App stands first, the other messengers keep theirs; the full order is pinned because
    Discord is hidden from non-admins, so "before Discord" alone would not say what they see.
    MUTATIONS: move veyllo_app back below whatsapp; swap discord and telegram; sort by name in
    getAppsByCategory - each red."""
    src = _src(PANEL)
    apps = _between(src, "export const CONNECTION_APPS: ConnectionApp[] = [", "\n];")
    # Lazy across the fields between id and category: an entry may carry a comment there.
    pairs = re.findall(r"id: '([a-z_]+)'[\s\S]*?category: '([a-z_]+)'", apps)
    communication = [app_id for app_id, category in pairs if category == "communication"]
    assert communication == [
        "veyllo_app", "discord", "telegram", "slack", "signal", "whatsapp",
        "email", "microsoft_teams", "matrix", "irc",
    ]
    for helper in ("const getAppsByCategory = ", "const getFilteredAppsByCategory = "):
        body = _between(src, helper, "\n    };\n")
        assert ".sort(" not in body, helper


# ── step one ────────────────────────────────────────────────────────────────────

def test_step_one_says_what_the_frd_asks():
    """What the app can do, what Veyllo sees, the cost of each way, the app's name for VAF and
    that the app's agent uses VAF as a tool with the privacy policy beside it, each on its
    step: the first two before the code, the code hint on the code step. Step 2 heads what
    Veyllo sees and what it costs each in its own card, since the header's small subtitle alone
    was not read as the step's subject. MUTATIONS: delete the agentUsesVaf line from the JSX;
    drop the costTitle heading - red."""
    src = _src(WIZARD)
    pages = {
        "intro": (_between(src, "const intro = (", "const sees = ("),
                  ("canChat", "canReceive", "canTasks", "appName", "agentUsesVaf", "privacyLink",
                   "introTitle", "featureMessages", "featureTasks")),
        "sees": (_between(src, "const sees = (", "const codeStep = ("),
                 ("seesTitle", "seesRelay", "seesDirect", "costTitle", "costSentence")),
        "code": (_between(src, "const codeStep = (", "const qr = ("), ("codeHint",)),
    }
    for page, (block, keys) in pages.items():
        for key in keys:
            assert f"t('{key}')" in block, (page, key)
            for locale in LOCALES:
                assert _window_keys(locale).get(key), (locale, key)
    # Step 2: two cards, each under its own heading, the text below the heading it belongs to.
    sees = pages["sees"][0]
    cards = sees.split("<section className={card}>")[1:]
    assert len(cards) == 2, "what Veyllo sees and what it costs are two cards"
    for card, (title, texts) in zip(cards, (("seesTitle", ("seesRelay", "seesDirect")),
                                            ("costTitle", ("costSentence",)))):
        heading = f'<h3 className="font-semibold text-gray-900">{{t(\'{title}\')}}</h3>'
        assert heading in card, title
        for text in texts:
            assert card.index(heading) < card.index(f"t('{text}')"), (title, text)
    # The two card titles are the steps' header lines now.
    subtitles = dict(re.findall(r"(\w+): '([\w.]+)'", _const_block(src, "STEP_SUBTITLE")))
    assert subtitles["intro"] == "canDoTitle" and subtitles["sees"] == "seesTitle"
    assert "subtitle={t(STEP_SUBTITLE[stepId])}" in src
    # The code step's action shows the QR.
    assert "t('showQr')" in _between(src, "const action = ", "const back = ")
    # A plain link to the policy, opened in a new tab; VAF never fetches it.
    assert "const PRIVACY_URL = 'https://veyllo.app/legal/privacy';" in src
    link = _between(pages["intro"][0], "<a href={PRIVACY_URL}", "</a>")
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
    code_step = _between(src, "const codeStep = (", "const qr = (")
    gate = code_step.index(") : impossible ? (")
    assert code_step.index("t('impossible')") > gate
    assert code_step.index("<form") > code_step.index(") : (", gate), "the code field shows when pairing is impossible"
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
    # X, Escape and the backdrop all go through close (the shell's onClose, pinned in
    # test_the_shell_is_the_discord_frame); so does a close from outside.
    assert "onClose={close}" in src and "return () => leave();" in src


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
    """Black and white only: success and failure are said by the text, also by the frame's
    completed steps. MUTATIONS: `text-green-600` on the paired icon; `bg-green-500` on a done
    circle in the shell - red."""
    for path in (WIZARD, SHELL):
        src = _src(path)
        assert not re.search(r"\btext-(?:green|teal|blue|emerald|sky|indigo|amber)-\d+", src), path.name
        assert not re.search(r"\bbg-(?:green|teal|blue|emerald|sky|indigo|amber)-\d+", src), path.name


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
    # On the code step, where impossibleAdmin points at it ("switch the relay on below").
    assert "{isAdmin && stepId === 'code' && <RelayAdminSection" in src
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


# The relay's two prices as Intl.NumberFormat(locale, {style: 'currency', currency: 'EUR'})
# writes them; German keeps a no-break space so an amount never breaks across two lines.
_AMOUNTS = {
    "en": ("€0.01", "€0.005"),
    "de": ("0,01\u00a0€", "0,005\u00a0€"),
    "ja": ("€0.01", "€0.005"),
    "ko": ("€0.01", "€0.005"),
    "th": ("€0.01", "€0.005"),
    "tr": ("€0,01", "€0,005"),
    "zh": ("€0.01", "€0.005"),
}


def test_the_relay_admin_block_is_always_open():
    """The admin block is shown open and cannot be folded away: a folded heading read as the
    state of the pairing in progress. It names itself, says what it decides, and loads when it
    mounts. MUTATION: put back useState(false) with the `{open && (` gate - red."""
    src = _src(WIZARD)
    section = src[src.index("function RelayAdminSection("):]
    for gone in ("aria-expanded", "const [open,", "setOpen", "{open &&", "ChevronRight"):
        assert gone not in section, gone
    assert "useEffect(() => { void load(); }, [load]);" in section
    head = section.index('<section className="mt-6 pt-4 border-t border-gray-200" aria-labelledby="veyllo-relay-admin-title">')
    order = [section.index(n, head) for n in ('<h3 id="veyllo-relay-admin-title"', "{t('adminTitle')}",
                                              "{t('adminIntro')}", "<Switch")]
    assert order == sorted(order), order
    lucide = _between(src, "import {", "} from 'lucide-react';")
    assert "ChevronRight" not in lucide
    for locale in LOCALES:
        keys = _window_keys(locale)
        assert keys["adminIntro"].strip() and keys["costTitle"].strip(), locale


def test_the_window_promises_what_each_way_delivers():
    """Push reaches a phone only when it paired through the Veyllo relay (no relay member, no
    notify), a message costs 0.01 euro and a task for the phone 0.005 euro per direction, written
    in each locale's own number format, 2FA is set up at sign-in or after an admin's reset, and
    the switch that gives the phone a direct address is named as the Local Network tab names it.
    MUTATIONS: the old Local text without the push sentence in one locale; the Turkish cost
    sentence with its old "1 sent" - red."""
    for locale in LOCALES:
        cat = _catalog(locale)
        keys = cat["settings"]["veylloApp"]
        assert "veyllo relay" in keys["canReceive"].lower(), locale
        assert "Veyllo Relay" in keys["explainLocal"], locale
        for key in ("costSentence", "explainRelay"):
            for amount in _AMOUNTS[locale]:
                assert amount in keys[key], (locale, key, amount)
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


# ── the frame and the steps ─────────────────────────────────────────────────────

# The Discord window's frame, by part. Each string occurs in DiscordSetupWizard.tsx and in the
# shell, so the shell cannot drift from the setup windows it was taken from.
_DISCORD_FRAME = {
    "overlay": "fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-sm max-md:p-0",
    "panel": ("bg-white rounded-2xl shadow-2xl w-full max-w-2xl mx-4 overflow-hidden border border-gray-200 "
              "max-md:max-w-none max-md:mx-0 max-md:h-[100dvh] max-md:rounded-none max-md:border-0 "
              "max-md:flex max-md:flex-col"),
    "header": "flex items-center justify-between p-6 border-b border-gray-200 bg-gray-50 max-md:p-4 max-md:shrink-0",
    "icon box": "w-10 h-10 rounded-xl bg-gray-900 dark:bg-[#2e2e2e] flex items-center justify-center",
    "title": 'className="text-xl font-bold text-gray-900"',
    "stepper": "px-6 pt-4",
    "circle": "w-8 h-8 rounded-full flex items-center justify-center text-sm font-medium transition-all",
    "current": "bg-gray-900 text-white dark:bg-[#3a3a3a] dark:text-white",
    "content": "p-6 min-h-[350px] max-md:min-h-0 max-md:flex-1 max-md:overflow-y-auto max-md:p-4",
    "footer": "flex items-center justify-between p-6 border-t border-gray-200 bg-gray-50 max-md:p-4 max-md:shrink-0",
    "back": "flex items-center gap-2 px-4 py-2 rounded-lg transition-colors",
    "next": "flex items-center gap-2 px-6 py-2 rounded-lg font-medium transition-colors",
    "next on": ("bg-gray-900 hover:bg-gray-800 text-white dark:bg-[#e6e6e6] dark:text-[#181818] "
                "dark:hover:bg-[#f5f5f5] dark:shadow-none"),
    "next off": "bg-gray-200 text-gray-400 cursor-not-allowed",
}


def test_the_shell_is_the_discord_frame():
    """The shared setup frame carries the Discord window's classes verbatim, plus the dialog
    semantics: role, aria-modal, its title as the name, a named X, and Escape and a click
    beside the panel closing like the X. MUTATIONS: the shell header to `p-5`; drop
    aria-modal - red."""
    shell, discord = _src(SHELL), _src(DISCORD)
    for part, classes in _DISCORD_FRAME.items():
        assert classes in discord, f"{part}: the Discord window changed; move the shell with it"
        assert classes in shell, part
    assert shell.startswith("'use client';\n// SPDX-FileCopyrightText: ")
    for needle in ('role="dialog"', 'aria-modal="true"', "aria-label={title}",
                   "aria-label={tCommon('close')}", "onClick={e => e.stopPropagation()}"):
        assert needle in shell, needle
    overlay = _between(shell, f'<div className="{_DISCORD_FRAME["overlay"]}"', ">")
    assert "onClick={onClose}" in overlay
    assert "useEscapeLayer({ active: true, level: 60, onEscape: onClose });" in shell
    # A step taller than the screen scrolls inside the panel on a desktop too.
    panel = _between(shell, _DISCORD_FRAME["panel"], '">')
    assert "max-h-[calc(100dvh-2rem)]" in panel and "flex flex-col" in panel and "max-md:max-h-none" in panel
    content = _between(shell, _DISCORD_FRAME["content"], '">')
    assert "flex-1 overflow-y-auto" in content
    # Every word is the catalogue's; the step count is read out, the circles are not.
    assert "tCommon('back')" in shell and "tCommon('stepOf', { step: current + 1, steps })" in shell
    assert 'className="flex items-center gap-2" aria-hidden="true"' in shell


def test_the_current_step_is_ringed():
    """The current step carries a ring with a gap, dark in the light theme and white in the
    dark one, since the completed steps share its black and white. The gap takes the panel's
    surface colour, so the dark panel shows no light halo in it. Only the current step is
    ringed. MUTATIONS: drop dark:ring-white; move the ring to the completed steps;
    ring-offset-white for the gap - red."""
    shell = _src(SHELL)
    circle = _between(shell, "idx < current ? 'bg-gray-900", ")}>")
    done, rest = circle.split("idx === current ?", 1)
    current, future = rest.split("' :", 1)
    ring = ("ring-2", "ring-gray-900", "dark:ring-white", "ring-offset-2",
            "ring-offset-[rgb(var(--sfc-white))]")
    classes = current.split()
    for cls in ring:
        assert cls in classes, cls
    assert current.strip().startswith("'" + _DISCORD_FRAME["current"] + " ")
    assert "ring-offset-white" not in classes
    assert "ring-" not in done and "ring-" not in future
    # The ring's gap follows the panel: the panel is bg-white, which is that same variable.
    assert 'white: v("sfc-white")' in _src(WEB / "tailwind.config.ts")


def test_the_veyllo_window_uses_the_shell():
    """The window is the shared frame, not a frame of its own. MUTATION: put back the old
    `max-w-md` card - red."""
    src = _src(WIZARD)
    assert "import SetupWizardShell, { WizardNextButton } from '@/components/connections/SetupWizardShell';" in src
    assert src.count("<SetupWizardShell") == 1 and "onClose={close}" in src
    assert "fixed inset-0" not in src and "max-w-md w-full" not in src
    assert "useEscapeLayer(" not in src
    assert "steps={STEPS.length}" in src and "current={stepIndex}" in src


def test_the_steps_run_in_order():
    """Six steps: the three pages before the QR, then one step per pairing view. A running
    pairing never lands on a page before the code, and a note about it lands on the code step.
    MUTATIONS: swap 'sees' and 'code'; drop setIntroPage(2) from the window_closed branch - red."""
    src = _src(WIZARD)
    steps = re.findall(r"'(\w+)'", _const_block(src, "STEPS"))
    assert steps == ["intro", "sees", "code", "scan", "confirm", "done"]
    view_step = dict(re.findall(r"(\w+): '(\w+)'", _const_block(src, "VIEW_STEP")))
    assert view_step == {"qr": "scan", "ended": "scan", "confirm": "confirm", "paired": "done"}
    views = set(re.findall(r"^\s*\w+: '(\w+)',$", _const_block(src, "PAIRING_VIEWS"), re.M))
    assert views - {"explain"} == set(view_step)
    assert "const stepId: StepId = view === 'explain' ? STEPS[introPage] : VIEW_STEP[view];" in src
    assert set(re.findall(r"(\w+): '", _const_block(src, "STEP_SUBTITLE"))) == set(steps)
    leave = _between(src, "const leave = useCallback(() => {", "}, []);")
    assert "setIntroPage(0);" in leave
    issue = _between(src, "const issue = useCallback(async (mode: Way) => {", "}, [refusalText, t]);")
    closed = _between(issue, "if (data.code === 'window_closed') {", "return;")
    assert "setIntroPage(2);" in closed
    apply = _between(src, "const applyPairing = useCallback(", "}, [loadStatus, onComplete, t]);")
    none = _between(apply, "if (next.state === 'none' && prev !== 'explain') {", "}")
    assert "setCodeNote(t('pairingStates.none'));" in none and "setIntroPage(2);" in none
    # Back walks the pages before the QR only.
    back = _between(src, "const back = ", ";\n")
    assert "stepId === 'sees' ? () => setIntroPage(0)" in back
    assert "stepId === 'code' ? () => setIntroPage(1)" in back
    assert back.rstrip().endswith(": undefined")


def test_the_approved_texts_are_word_for_word():
    """The approved sentences keep their words. The relay texts replace the earlier ones that
    priced in cents and named a relay secret and a relay instance: the window now says euro, a
    relay key and signing the installation out of the relay, and no new text of the window
    carries a colon. MUTATIONS: change one word of the DE dissolveBody; put the colon back into
    the DE relayStates.off; a colon in the EN relayStates.unavailable - red."""
    kept = {
        "en": {
            "agentUsesVaf": "The agent in the Veyllo app uses this VAF as a tool, reads its "
                "answers and processes them with the app's chat model.",
            "privacyLink": "Veyllo privacy policy",
            "seesTitle": "What Veyllo sees",
            "canDoTitle": "What the app can do",
            "codeHint": "Pairing is for your own account and starts with your 2FA code.",
        },
        "de": {
            "agentUsesVaf": "Der Agent in der Veyllo App nutzt dieses VAF als Werkzeug, liest "
                "seine Antworten und verarbeitet sie mit dem Chat-Modell der App.",
            "privacyLink": "Datenschutzerkl\u00e4rung von Veyllo",
            "seesTitle": "Was Veyllo sieht",
            "canDoTitle": "Was die App kann",
            "codeHint": "Gekoppelt wird nur mit deinem eigenen Konto, und es beginnt mit "
                "deinem 2FA-Code.",
        },
    }
    relay = {
        "en": {
            "seesRelay": "Through the Veyllo Relay, Veyllo only sees when messages travel, how "
                "many there are, how large they are and which VAF installation they come from, "
                "plus technical connection data such as the IP address. Veyllo never sees the "
                "content, because it is end-to-end encrypted.",
            "seesDirect": "If you connect directly through your own network, Veyllo sees "
                "nothing.",
            "costTitle": "What it costs",
            "costSentence": "Through the Veyllo Relay, each message costs \u20ac0.01, in both "
                "directions and also when VAF writes on its own. A task from VAF to your phone "
                "costs \u20ac0.005 per direction. Your subscription pays first, then your "
                "credit. The direct way is free.",
            "adminTitle": "Veyllo Relay for this VAF installation",
            "adminIntro": "As an admin, you decide here for all accounts of this installation "
                "whether phones may connect through the Veyllo Relay. Whether your own phone "
                "connects through the relay or directly through your network is chosen in the "
                "next step, below the QR code.",
            "relayStates.idle": "The relay is allowed. VAF only connects to the relay while a "
                "phone needs it.",
            "noInstance": "This installation is not registered with the relay yet. This "
                "happens automatically as soon as the first phone pairs through the relay.",
            "rotate": "Renew relay key",
            "dissolve": "Sign out of the relay",
            "dissolveBody": "This installation will be signed out of the Veyllo Relay, for all "
                "accounts. Phones stay paired and can still reach VAF directly through your "
                "network, for example at home or over a VPN. They can only reach VAF through "
                "the relay again after pairing anew.",
            "explainRelay": "With the Veyllo Relay you reach VAF from anywhere. Each message "
                "costs \u20ac0.01, also when VAF writes on its own, a task to the phone "
                "\u20ac0.005 per direction. Veyllo only sees when messages travel and how "
                "large they are, never the content.",
            "relayStates.off": "The relay is switched off. Phones pair and connect only "
                "directly.",
            "relayStates.replaced": "Another VAF with the same relay key took the connection.",
            "relayStates.revoked": "The relay ended this installation's access.",
            "relayStates.secret_rejected": "The relay does not accept this installation's "
                "relay key.",
            "relayStates.secret_not_stored": "The relay key could not be stored on this "
                "machine.",
            "relayStateHelp.secret_rejected": "Renew the relay key, or sign this installation "
                "out of the relay and pair the phones again.",
            "relayStateHelp.replaced": "A backup restored on a second machine uses the same "
                "relay key. Renew it on one of the two machines. VAF tries again after five "
                "minutes.",
            "relayStateHelp.revoked": "Sign this installation out of the relay and pair the "
                "phones again for the relay.",
            "relayErrors.secret_rejected": "The relay does not accept this installation's "
                "relay key. An admin can renew it.",
            "relayErrors.secret_not_stored": "The relay key could not be stored on this "
                "machine. An admin should check the machine's key ring, then you pair the "
                "phone again.",
            "rotateTitle": "Renew the relay key?",
            "rotateBody": "The relay key is renewed, and VAF then reconnects to the relay. "
                "Phones stay paired.",
            "rotateConfirm": "Renew",
            "rotated": "The relay key was renewed.",
            "dissolveConfirm": "Sign out",
            "dissolved": "This installation is signed out of the relay.",
        },
        "de": {
            "seesRelay": "\u00dcber das Veyllo Relay sieht Veyllo nur, wann Nachrichten "
                "laufen, wie viele und wie gro\u00df sie sind und von welcher VAF-Installation "
                "sie kommen, dazu technische Verbindungsdaten wie die IP-Adresse. Den Inhalt "
                "sieht Veyllo nie, denn er ist Ende-zu-Ende verschl\u00fcsselt.",
            "seesDirect": "Verbindest du dich direkt \u00fcber dein eigenes Netz, sieht Veyllo "
                "nichts.",
            "costTitle": "Was es kostet",
            "costSentence": "\u00dcber das Veyllo Relay kostet jede Nachricht "
                "0,01\u00a0\u20ac, in beide Richtungen und auch dann, wenn VAF von sich aus "
                "schreibt. Ein Auftrag von VAF an dein Handy kostet 0,005\u00a0\u20ac pro "
                "Richtung. Bezahlt wird zuerst mit deinem Abo, dann mit deinem Guthaben. Der "
                "direkte Weg ist kostenlos.",
            "adminTitle": "Veyllo Relay f\u00fcr diese VAF-Installation",
            "adminIntro": "Hier legst du als Admin f\u00fcr alle Konten dieser Installation "
                "fest, ob sich Handys \u00fcber das Veyllo Relay verbinden d\u00fcrfen. Ob "
                "dein eigenes Handy \u00fcber das Relay oder direkt \u00fcber dein Netz "
                "verbunden wird, w\u00e4hlst du im n\u00e4chsten Schritt unter dem QR-Code.",
            "relayStates.idle": "Das Relay ist erlaubt. VAF verbindet sich nur dann mit dem "
                "Relay, wenn ein Handy es gerade braucht.",
            "noInstance": "Diese Installation ist beim Relay noch nicht angemeldet. Das "
                "geschieht automatisch, sobald das erste Handy \u00fcber das Relay koppelt.",
            "rotate": "Relay-Schl\u00fcssel erneuern",
            "dissolve": "Beim Relay abmelden",
            "dissolveBody": "Diese Installation wird beim Veyllo Relay abgemeldet, f\u00fcr "
                "alle Konten. Die Handys bleiben gekoppelt und erreichen VAF weiter direkt "
                "\u00fcber dein Netz, etwa zu Hause oder per VPN. \u00dcber das Relay "
                "erreichen sie VAF erst wieder, wenn sie neu koppeln.",
            "explainRelay": "\u00dcber das Veyllo Relay erreichst du VAF von \u00fcberall. "
                "Jede Nachricht kostet 0,01\u00a0\u20ac, auch wenn VAF von sich aus schreibt, "
                "ein Auftrag ans Handy 0,005\u00a0\u20ac pro Richtung. Veyllo sieht dabei nur, "
                "wann Nachrichten laufen und wie gro\u00df sie sind, den Inhalt nie.",
            "relayStates.off": "Das Relay ist ausgeschaltet. Handys koppeln und verbinden sich "
                "nur direkt.",
            "relayStates.replaced": "Ein anderes VAF mit demselben Relay-Schl\u00fcssel hat "
                "die Verbindung \u00fcbernommen.",
            "relayStates.revoked": "Das Relay hat dieser Installation den Zugang entzogen.",
            "relayStates.secret_rejected": "Das Relay nimmt den Relay-Schl\u00fcssel dieser "
                "Installation nicht an.",
            "relayStates.secret_not_stored": "Der Relay-Schl\u00fcssel konnte auf diesem "
                "Rechner nicht gespeichert werden.",
            "relayStateHelp.secret_rejected": "Erneuere den Relay-Schl\u00fcssel, oder melde "
                "diese Installation beim Relay ab und kopple die Handys neu.",
            "relayStateHelp.replaced": "Eine Sicherung, die auf einem zweiten Rechner "
                "wiederhergestellt wurde, nutzt denselben Relay-Schl\u00fcssel. Erneuere ihn "
                "auf einem der beiden Rechner. VAF versucht es nach f\u00fcnf Minuten erneut.",
            "relayStateHelp.revoked": "Melde diese Installation beim Relay ab und kopple die "
                "Handys f\u00fcr das Relay neu.",
            "relayErrors.secret_rejected": "Das Relay nimmt den Relay-Schl\u00fcssel dieser "
                "Installation nicht an. Ein Admin kann ihn erneuern.",
            "relayErrors.secret_not_stored": "Der Relay-Schl\u00fcssel konnte auf diesem "
                "Rechner nicht gespeichert werden. Ein Admin sollte den Schl\u00fcsselbund des "
                "Rechners pr\u00fcfen, danach koppelst du das Handy neu.",
            "rotateTitle": "Relay-Schl\u00fcssel erneuern?",
            "rotateBody": "Der Relay-Schl\u00fcssel wird erneuert, und VAF verbindet sich "
                "danach neu mit dem Relay. Die Handys bleiben gekoppelt.",
            "rotateConfirm": "Erneuern",
            "rotated": "Der Relay-Schl\u00fcssel wurde erneuert.",
            "dissolveConfirm": "Abmelden",
            "dissolved": "Diese Installation ist beim Relay abgemeldet.",
        },
    }
    for table in (kept, relay):
        for locale, texts in table.items():
            keys = _window_keys(locale)
            for key, text in texts.items():
                assert _key(keys, key) == text, (locale, key)
    # No colon in running text: the new texts, and every relay state and its help text.
    for locale, texts in relay.items():
        keys = _window_keys(locale)
        checked = list(texts) + [f"{group}.{state}" for group in ("relayStates", "relayStateHelp")
                                 for state in keys[group]]
        for key in checked:
            assert ":" not in _key(keys, key), (locale, key)


def test_no_old_relay_terms_in_the_window():
    """The window names a relay key and signing the installation out of the relay, priced in
    euro: no text of it says relay secret, relay instance, dissolve or cent any more, so no
    help text points at a button that is no longer there. MUTATION: put back "Replace relay
    secret" as the EN rotate - red."""
    old = {"en": re.compile(r"secret|instance|dissolv|\bcents?\b", re.I),
           "de": re.compile(r"Geheimnis|Instanz|aufl\u00f6s|Cent")}

    def texts(node, path=""):
        for key, value in node.items():
            if isinstance(value, dict):
                yield from texts(value, f"{path}{key}.")
            else:
                yield f"{path}{key}", value

    for locale, pattern in old.items():
        hits = [key for key, text in texts(_window_keys(locale)) if pattern.search(text)]
        assert not hits, (locale, hits)


def test_the_footer_actions():
    """The code step submits its form from the footer (Enter in the field too), the run-out
    pairing and a code VAF refused to issue get a new code there (with the relay off nothing
    else on that step can ask again), the connected one closes. MUTATIONS: drop the `form=`
    attribute; offer New code for the ended view only - red."""
    src = _src(WIZARD)
    action = _between(src, "const action = ", "const back = ")
    code = _between(action, "stepId === 'code' ? (", "</WizardNextButton>")
    assert 'type="submit"' in code and 'form="veyllo-app-code-form"' in code
    assert "disabled={busy !== null || !code.trim() || !status || impossible}" in code
    code_step = _between(src, "const codeStep = (", "const qr = (")
    assert '<form id="veyllo-app-code-form"' in code_step
    assert "onSubmit={e => { e.preventDefault(); void openWindow(); }}" in code_step
    assert 'type="submit"' not in code_step, "one submit button: the footer's"
    ended = _between(action, "view === 'ended' || (view === 'qr' && issueRefusal) ? (", "</WizardNextButton>")
    assert "issue(relayAllowed ? way : 'local')" in ended and "t('newCode')" in ended
    # The refusal is the only QR-step state without a QR; it is what issue() sets on a refusal.
    issue = _between(src, "const issue = useCallback(async (mode: Way) => {", "}, [refusalText, t]);")
    assert "setView('qr');\n                setIssueRefusal(refusalText(data, res));" in issue
    paired_action = _between(action, "view === 'paired' ? (", "</WizardNextButton>")
    assert "onClick={close}" in paired_action and "t('done')" in paired_action
    paired = _between(src, "const paired = (", "const action = ")
    assert "onClick={() => { close(); onOpenAccount(); }}" in paired
    # The shell's next button forwards both attributes to the element.
    button = _between(_src(SHELL), "export function WizardNextButton(", "\n}\n")
    assert "type={type}" in button and "form={form}" in button


def test_the_new_words_are_in_every_catalogue():
    """The step names and the two card titles, and the frame's Back, Next and step count, in
    all seven catalogues. Back, Next and the step count reuse words the catalogues already
    had. MUTATION: drop steps.scan from tr.json - red."""
    for locale in LOCALES:
        cat = _catalog(locale)
        keys = cat["settings"]["veylloApp"]
        for key in ("introTitle", "featureMessages", "featureTasks"):
            assert isinstance(keys.get(key), str) and keys[key].strip(), (locale, key)
        assert set(keys["steps"]) == {"code", "scan", "confirm", "done"}, locale
        assert all(isinstance(v, str) and v.strip() for v in keys["steps"].values()), locale
        common = cat["common"]
        assert common["back"] == cat["settings"]["githubWizard"]["back"], locale
        assert common["next"] == cat["settings"]["githubWizard"]["next"], locale
        assert common["stepOf"] == cat["localEngine"]["stepOf"], locale
        assert "{step}" in common["stepOf"] and "{steps}" in common["stepOf"], locale
