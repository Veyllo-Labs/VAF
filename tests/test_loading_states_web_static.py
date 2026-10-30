# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Loading states in the web UI: no enter animation on a tab, one placeholder for a first load.

A Settings tab appears the same way as every other one, without sliding or fading in. A
region waiting for its first data, in Settings and in the chat, shows the shared placeholder (web/components/ui/Skeleton.tsx)
in the shape and height of what is coming, so nothing below it moves when the data lands, and
it says so once to a screen reader. The motion lives in one place (globals.css: opacity only, a
reveal after 180 ms so fast loads never flash, still under reduced motion), and no site writes
its own pulse. A refetch with data on screen keeps the data.

The heights are measured in a browser (the jump probe of the build report); what is pinned here
is the wiring that makes them hold, and that each site no longer renders nothing, a spinner or
a false "empty" while it waits.
"""
import json
import re
from pathlib import Path

import pytest

from test_model_roles_web_static import _tab_regions

_ROOT = Path(__file__).resolve().parent.parent
_WEB = _ROOT / "web"
_MODAL = _WEB / "components" / "SettingsModal.tsx"
_SKELETON = _WEB / "components" / "ui" / "Skeleton.tsx"
_CSS = _WEB / "app" / "globals.css"
_SETTINGS = _WEB / "components" / "settings"
_PAGE = _WEB / "app" / "page.tsx"


def _src(path: Path) -> str:
    # Text mode: the needles below hold newlines, and a CRLF checkout must read as LF.
    return path.read_text(encoding="utf-8")


def _block(css: str, start: int) -> str:
    """The body of the brace block opened at the first `{` from `start`, brace-matched."""
    i = css.index("{", start) + 1
    depth, j = 1, i
    while depth:
        depth += {"{": 1, "}": -1}.get(css[j], 0)
        j += 1
    return css[i:j - 1]


def test_no_settings_tab_enters_with_an_animation():
    """Every tab's content root is plain: the Local Network tab used to slide in from the
    right and Persona to fade in, while the other ten simply appeared. MUTATION: put
    `animate-in fade-in slide-in-from-right-4` back on local_network, or `fade-in` on persona."""
    src = _src(_MODAL)
    regions = _tab_regions(src)
    assert {"persona", "local_network", "general", "ai"} <= set(regions)
    checked = 0
    for tab, spans in regions.items():
        for a, _b in spans:
            head = src[a:a + 260]
            m = re.search(r'className="([^"]*)"', head)
            if not m:
                continue   # a tab rendered by a component of its own: no root of its own here
            checked += 1
            for word in ("animate-in", "fade-in", "slide-in-from"):
                assert word not in m.group(1), f"{tab}: {m.group(1)}"
    assert checked >= 10


def test_the_placeholder_is_one_quiet_primitive():
    """The primitive carries the status, the label and the hidden shapes, and no motion or
    colour of its own beyond the folded grey. The root is positioned, so the absolute sr-only
    line is contained by the box and cannot make an outer, overflow-hidden ancestor scrollable
    (a search jump scrolled the whole Settings window while a section loaded). MUTATION: drop
    `role="status"`, swap in `animate-pulse`, add `bg-gradient-to-r`, or drop `relative`."""
    src = _src(_SKELETON)
    assert src.startswith("// SPDX-FileCopyrightText: 2026 Veyllo GmbH\n")
    assert 'role="status"' in src
    assert "useTranslations('common')" in src and "label ?? t('loading')" in src
    assert '<span className="sr-only">' in src
    assert '<div aria-hidden="true" className="h-full">' in src
    assert "export default function Skeleton(" in src
    assert "export function SkeletonBar(" in src and "export function SkeletonRows(" in src
    assert "'vaf-skel relative overflow-hidden'" in src and "'vaf-skel-bar rounded bg-gray-200'" in src
    for banned in ("animate-pulse", "gradient", "background-position"):
        assert banned not in src, banned
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", src)


def test_the_motion_is_opacity_only_revealed_late_and_still_when_asked():
    """The repaint rule (globals.css): opacity only. The reveal waits 150-250 ms as a 0 s step
    so a fast answer never flashes; the pulse has a reduced-motion twin that holds still.
    MUTATION: animate `background-position`, set the delay to 0 ms, delete the twin, or make
    the twin `.5`/`1`."""
    css = _src(_CSS)
    reduced_at = css.index("@media (prefers-reduced-motion: reduce)")
    for name in ("vafSkelReveal", "skelPulse"):
        bodies = [_block(css, m.start()) for m in re.finditer(r"@keyframes " + name + r"\b", css)]
        assert bodies, name
        for body in bodies:
            props = set(re.findall(r"([a-z-]+)\s*:", body))
            assert props == {"opacity"}, (name, props)
    rule = _block(css, css.index(".vaf-skel {"))
    assert re.search(r"opacity:\s*0\s*;", rule)
    m = re.search(r"animation:\s*vafSkelReveal\s+0s\s+\S+\s+(\d+)ms\s+forwards", rule)
    assert m and 150 <= int(m.group(1)) <= 250, rule
    bar = _block(css, css.index(".vaf-skel-bar {"))
    assert "animation: skelPulse" in bar
    twins = [m.start() for m in re.finditer(r"@keyframes skelPulse\b", css) if m.start() > reduced_at]
    assert len(twins) == 1, "the reduced-motion skelPulse twin"
    values = re.findall(r"opacity:\s*([\d.]+)", _block(css, twins[0]))
    assert values and len(set(values)) == 1, values


# (file, text that must be gone, text that must be there). The gone text is what the site did
# while it waited: nothing, a spinner, English text, or a false "empty"; empty where the old
# text legitimately stays for a later state.
SITES = [
    ("SettingsModal.tsx", 'border-t-gray-900 rounded-full animate-spin" /></div>', "{personaFirstLoad ? ("),
    ("SettingsModal.tsx", '<p className="text-sm text-gray-500">Loading…</p>', "<SkeletonBar className=\"mt-3 h-9 w-20 rounded-lg\" />"),
    ("SettingsModal.tsx", "<p className=\"text-xs text-gray-400 mt-4\">{tUsage('loading')}</p>", "<Skeleton label={tUsage('loading')}"),
    # The amber "resolving" line stays for an answer without an address; before any answer
    # the placeholder stands in front of it.
    ("SettingsModal.tsx", "", ") : !accessUrlResolved ? (\n                                                                    <Skeleton label={tLocalNet('lanUrlLoading')}>"),
    ("SettingsModal.tsx", "{usersLoading ? (", "<Skeleton label={tLocalNet('loadingUsers')}>"),
    ("SettingsModal.tsx", "useState<ArchivedChat[]>([])", "{archiveChats === null ? ("),
    ("SettingsModal.tsx", "", "{archiveMsgsFor !== archiveOpenId ? ("),
    ("settings/ModelRolesOverview.tsx", "skelPulse", "<Skeleton label={t('rolesLoading')}>"),
    ("settings/InferenceProfileSection.tsx", "if (!data && !loadFailed) return null;", "{!data && !loadFailed && ("),
    ("settings/SshSection.tsx", "if (!data) return null;", "if (!data && settled) return null;"),
    ("settings/FtpSection.tsx", "if (servers === null && !loadFailed) return null;", "if (hidden) return null;"),
    ("settings/SandboxSection.tsx", "if (!data && !loadFailed) return null;", "{!data ? (loadFailed ? null : ("),
    ("settings/StandingGrantsSection.tsx", "if (!loadFailed) return null;", "if (!loadFailed) return ("),
    ("settings/RemoteAccessSection.tsx", "if (!loadFailed) return null;", "if (hidden) return null;"),
    ("settings/CertificateSection.tsx", "if (!loadFailed) return null;", "if (hidden) return null;"),
    ("settings/SessionsSection.tsx", "if (!loadFailed) return null;", "if (!loadFailed) return ("),
    ("settings/SecretsSection.tsx", "useState<Row[]>([])", "{rows === null ? ("),
    ("settings/BackupSection.tsx", "{loadFailed && <p className=\"text-sm text-red-600 mb-3\">{t('backupLoadFailed')}</p>}\n            {status && (",
     "{!data && !loadFailed && ("),
]


@pytest.mark.parametrize("name,gone,there", SITES, ids=[f"{s[0]}:{i}" for i, s in enumerate(SITES)])
def test_every_settings_first_load_shows_the_placeholder(name, gone, there):
    """Each site waits behind the shared placeholder instead of nothing, a spinner or a false
    "empty". MUTATION: revert any one site (for example `if (!data) return null;` in the SSH
    section, or the persona spinner)."""
    path = (_SETTINGS.parent / name) if name == "SettingsModal.tsx" else (_SETTINGS / name.split("/", 1)[1])
    src = _src(path)
    assert not gone or gone not in src, gone
    assert there in src, there
    assert "<Skeleton" in src


def test_no_site_draws_a_placeholder_pulse_of_its_own():
    """The pulse and its pace live in globals.css and the primitive only, so two placeholders
    cannot drift apart. MUTATION: inline `animation: 'skelPulse ...'` in any component or in
    the chat page."""
    offenders = [p.relative_to(_ROOT).as_posix() for d in ("components", "app") for p in (_WEB / d).rglob("*.tsx")
                 if p != _SKELETON and "skelPulse" in _src(p)]
    assert offenders == [], offenders
    assert "Loading…" not in _src(_MODAL)


def test_a_refetch_keeps_the_data_on_screen():
    """Only the FIRST load stands as a placeholder: a Persona revisit keeps its fields, a user
    reload keeps the table. MUTATION: `personaLoading ?` alone, or `usersLoading ?` alone."""
    src = _src(_MODAL)
    assert "const personaFirstLoad = personaLoading && !personaData;" in src
    persona = src[src.index("{activeTab === 'persona' && ("):src.index("{activeTab === 'ai' && ")]
    assert persona.count("{personaFirstLoad ? (") == 2
    assert "personaLoading ?" not in src
    assert "{usersLoading && users.length === 0 ? (" in src


# ---- The chat (web/app/page.tsx) ----

def _between(src: str, start: str, end: str) -> str:
    a = src.index(start)
    return src[a:src.index(end, a)]


def test_a_fresh_load_waits_for_the_newest_chat_not_the_welcome_screen():
    """The newest chat is auto-loaded from the first session list, past handleSessionSwitch, so
    the flag starts true: the welcome screen never shows before the history, and the composer
    stands at the bottom where the conversation will put it. A dead socket ends after 15 s.
    MUTATION: `useState(false)`, drop `!historyLoading` from the centring, or delete the guard."""
    src = _src(_PAGE)
    assert "const [historyLoading, setHistoryLoading] = useState(true);" in src
    guard = _between(src, "const [historyLoading, setHistoryLoading] = useState(true);", "const snapshotLenRef")
    assert "setTimeout(() => setHistoryLoading(false), 15000)" in guard
    centre = _between(src, '"absolute left-0 right-0 w-full z-40', '"top-auto bottom-0 translate-y-0"')
    assert "messages.length === 0 && !roomView && !historyLoading && !roomOpening" in centre
    assert "{messages.length === 0 && !roomView && !historyLoading && !roomOpening && (" in src
    handler = _between(src, "else if (data.type === 'history_update') {", "else if (data.type === 'context_checkpoint')")
    assert "setHistoryLoading(false);" in handler


def test_a_transcript_from_a_snapshot_arrives_without_the_enter_animation():
    """Rows from a history snapshot (or the cache on a switch) mount as they are; only rows
    past the snapshot (a send, a streamed reply, a tool) slide in. MUTATION: make the class
    unconditional again on either row, or stop recording the snapshot length."""
    src = _src(_PAGE)
    gated = 'trueIndex >= snapshotLenRef.current && "animate-in fade-in slide-in-from-bottom-2 duration-300"'
    assert src.count(gated) == 2, "the tool row and the bubble"
    assert '"flex vaf-msg-row animate-in' not in src and '"flex gap-4 pt-4 animate-in' not in src
    handler = _between(src, "else if (data.type === 'history_update') {", "else if (data.type === 'context_checkpoint')")
    assert "snapshotLenRef.current = finalServerMsgs.length;\n                            setMessages(finalServerMsgs);" in handler
    assert "snapshotLenRef.current = finalMsgs.length;\n                    setMessages(finalMsgs);" in handler
    assert "snapshotLenRef.current = cached.length;\n        setMessages(cached);" in src
    assert "if (messages.length < snapshotLenRef.current) snapshotLenRef.current = messages.length;" in src


def test_the_sidebar_shows_rows_until_the_first_list():
    """Until the first session list, placeholder rows in the session row's chrome stand below
    New Chat, cleared by the list or after 15 s. MUTATION: remove the set, or the rows."""
    src = _src(_PAGE)
    branch = _between(src, "else if (data.type === 'session_list') {", "else if (data.type === 'rooms_changed')")
    assert "setSessionsLoaded(true);" in branch
    assert "setTimeout(() => setSessionsLoaded(true), 15000)" in src
    sidebar = _between(src, "{/* Session list: outer box", "{sessions.map(s => isRoom(s) ? (")
    assert "{!sessionsLoaded && (" in sidebar
    assert "<SkeletonRows count={6}" in sidebar and 'rowClassName="p-2 pl-3.5 h-9 max-md:min-h-[44px] rounded-lg"' in sidebar
    assert 'textClassName="opacity-0 group-hover:opacity-100' in sidebar


def test_the_auth_check_and_the_page_fallback_show_the_app_frame():
    """Both full-screen spinners became the app-shell placeholder, with the status label.
    MUTATION: put the spinner back in either place."""
    src = _src(_PAGE)
    auth = _between(src, "    if (authChecking) {", "    if (authError) {")
    assert "<AppShellSkeleton label={tAuth('checkingSession')} />" in auth and "animate-spin" not in auth
    fallback = _between(src, "<Suspense fallback=", "<VAFDashboardContent />")
    assert "<AppShellSkeleton />" in fallback and "animate-spin" not in fallback
    shell = _between(src, "const AppShellSkeleton = ", "\n);\n")
    assert "<Skeleton label={label}" in shell and "max-md:hidden" in shell and "animate-spin" not in shell


def test_the_conversation_placeholder_is_the_shared_one():
    """The chat's and the room's loading illusion render through the primitive (status role,
    reveal delay), not a hidden box of their own; the room's sending note keeps its pulse
    through a class. MUTATION: revert the root to `<div className="pt-2" aria-hidden="true">`."""
    src = _src(_PAGE)
    body = _between(src, "const LoadingIllusion = ", "\n);\n")
    assert '<Skeleton className="pt-2">' in body
    assert 'aria-hidden="true"' not in body and "skelPulse" not in body
    assert "vaf-pulse-note" in src and ".vaf-pulse-note { animation: skelPulse" in _src(_CSS)


def test_an_edit_during_a_persona_refetch_is_not_overwritten():
    """A revisit refetches while the fields stay editable. An answer that lands after an edit
    began was read before it; applying it would put the stored value back over the typing, and
    the soul's blur would then save that old value. Every edit goes through the counter, and
    the effect drops a late answer. MUTATION: drop the `!== editsAtStart` return, or let the
    soul (or a date and time field) bypass the counter."""
    src = _src(_MODAL)
    effect = _between(src, "const personaEditsRef = useRef(0);", "// Sync Date & Time fields")
    assert "const editsAtStart = personaEditsRef.current;" in effect
    assert ("if (personaEditsRef.current !== editsAtStart) return;\n"
            "                    setPersonaData(data);") in effect
    assert "personaEditsRef.current += 1;\n        setPersonaData(next);" in effect
    # No edit site writes the persona around the counter.
    assert "setPersonaData({ ...personaData" not in src
    assert src.count("editPersona({ ...personaData!, identity: newIdentity });") == 2
    assert "onChange={(e) => editPersona({ ...personaData!, soul: e.target.value })}" in src
    assert "editPersona({ ...personaData, soul: content });" in src
    for setter in ("setDateTimeTimezone", "setDateTimeDateFormat", "setDateTimeTimeFormat"):
        assert f"onChange={{(v: string) => {{ personaEditsRef.current += 1; {setter}(v); }}}}" in src, setter


def test_a_failed_first_usage_load_ends_the_placeholder():
    """An error status, a network failure or an answer without `totals` leaves the card without
    figures; it then says so with its refresh button instead of pulsing forever, in every
    language. That holds as well for an account whose earlier answer had no calls (its card
    has no Refresh of its own). MUTATION: drop `else setUsageFailed(true);`, the key from one
    catalogue, or narrow the condition back to `!usage && usageFailed`."""
    src = _src(_MODAL)
    load = _between(src, "const loadUsage = useCallback(() => {", "}, [apiBase, currentUser?.role]);")
    assert "setUsageFailed(false);" in load
    assert "if (d && d.totals) setUsage(d);\n                    else setUsageFailed(true);" in load
    assert ".catch(() => setUsageFailed(true))," in load
    card = _between(src, "{usageFailed && (!usage || usage.totals.calls === 0) ? (", ") : !usage ? (")
    assert "{tUsage('loadFailed')}" in card and "onClick={loadUsage}" in card and "{tUsage('refresh')}" in card
    for loc in ("de", "en", "ja", "ko", "th", "tr", "zh"):
        cat = json.loads((_WEB / "messages" / f"{loc}.json").read_text(encoding="utf-8"))
        assert cat["settings"]["usage"].get("loadFailed"), loc


def test_a_late_first_session_list_waits_for_its_history_again():
    """A session list that arrives after the 15 s guard gave up auto-selects the newest chat;
    its history is on its way again, so the welcome screen and the centred composer must not
    show until history_update. MUTATION: ask with `wait` false, or send load_session raw."""
    src = _src(_PAGE)
    branch = _between(src, "else if (data.type === 'session_list') {", "else if (data.type === 'rooms_changed')")
    assert "requestHistory(wsSocketRef.current, chats[0].id, true);" in branch


def test_every_history_request_restarts_the_wait():
    """The 15 s guard runs from the latest history request, also when the flag is already true
    (a list landing just before the first deadline, a switch between two uncached chats, a
    proactive message adopting a chat): an older timer must not end the wait while the newer
    load_session is pending. So the page holds exactly ONE load_session send, inside
    `requestHistory`, which restarts the guard; every request path calls it, and a raw send
    added anywhere fails here. MUTATION: send load_session directly at any call site, drop the
    counter bump from the helper, or drop `historyRequests` from the guard's dependencies."""
    src = _src(_PAGE)
    sends = [m.start() for m in re.finditer(r"type: 'load_session'", src)]
    helper_at = src.index("const requestHistory = useCallback(")
    helper = src[helper_at:src.index("}, []);", helper_at)]
    assert len(sends) == 1 and helper_at < sends[0] < helper_at + len(helper), \
        "every load_session goes through requestHistory"
    assert "setHistoryLoading(wait);" in helper and "setHistoryRequests(n => n + 1);" in helper
    guard = _between(src, "const [historyRequests, setHistoryRequests] = useState(0);", "const requestHistory")
    assert "setTimeout(() => setHistoryLoading(false), 15000)" in guard
    assert "}, [historyLoading, historyRequests]);" in guard
    # The four request paths: a switch, the first session list, and a proactive update or
    # append that adopts a chat while none is open.
    assert "requestHistory(ws, id, cached.length === 0 && !syncedSessions.current.has(id));" in src
    for kind in ("agent_message_update", "agent_message_append"):
        start = src.index(f"else if (data.type === '{kind}') {{")
        branch = src[start:src.index("else if (data.type === ", start + 30)]
        assert "requestHistory(wsSocketRef.current, data.sessionId, true);" in branch, kind
    assert src.count("requestHistory(") == 4, "the four callers"


def test_every_persona_writer_counts_against_a_late_refetch():
    """Besides the person's typing, a save's answer writes the persona (the user identity and
    its change log, the date and time); a refetch that started before the save must not put
    the old state back. Only three places write the state: the counted edit, the counted save
    answer, and the guarded fetch. MUTATION: write a save's answer with `setPersonaData`
    directly, or drop the count from `applySavedUserIdentity`."""
    src = _src(_MODAL)
    assert src.count("setPersonaData(") == 3, "editPersona, applySavedUserIdentity, the guarded fetch"
    saved = _between(src, "const applySavedUserIdentity = ", "};")
    assert "personaEditsRef.current += 1;" in saved
    assert src.count("applySavedUserIdentity(data.user_identity);") == 2


def test_the_korean_usage_failure_speaks_like_its_neighbours():
    """The usage strings in ko.json use the polite -요 register; the failure line does too.
    MUTATION: the formal `-습니다` again."""
    cat = json.loads((_WEB / "messages" / "ko.json").read_text(encoding="utf-8"))
    assert cat["settings"]["usage"]["loadFailed"] == "토큰 사용량을 불러오지 못했어요."
