# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The inbox window in the sidebar footer (web/components/inbox/InboxWindow.tsx, web/app/page.tsx,
web/components/SettingsModal.tsx): the footer row sits between the calendar and the logs with a
badge driven by the signal, the window reads the shell's pieces instead of copying them, every
channel window and the mail client accept a jump, and the compose box obeys the WhatsApp rule.

MUTATION: poll the summary on a timer and the interval test goes red; drop the `inbox_changed`
branch and the signal test goes red; copy the bubble block into the window and the shell test
goes red; give Discord no jump and the jump test goes red.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
WINDOW = WEB / "components" / "inbox" / "InboxWindow.tsx"
PAGE = WEB / "app" / "page.tsx"
SETTINGS = WEB / "components" / "SettingsModal.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_the_footer_row_sits_between_the_calendar_and_the_logs_with_the_signal_badge():
    page = _read(PAGE)
    cal = page.index("title={tNav('calendar')}")
    inbox = page.index("title={tNav('inbox')}")
    logs = page.index("title={tNav('notifications')}")
    assert cal < inbox < logs, "the inbox row is the fourth footer row, between Kalender and Logs"
    row = page[inbox - 400:inbox + 1400]
    assert "bg-amber-500" in row and "inboxSummary.waits" in row, "the amber badge counts who waits"
    assert "bg-red-500 rounded-full" in row and "inboxSummary.unread" in row, "the red dot says something is unread"
    assert "data-agent-hint=\"nav-inbox\"" in row


def test_the_badge_and_the_window_follow_the_signal_not_a_timer():
    page = _read(PAGE)
    assert "else if (data.type === 'inbox_changed') {" in page
    assert page.count("setInboxVersion(v => v + 1);") == 2, "inbox_changed and rooms_changed both bump the version"
    rooms = page.split("else if (data.type === 'rooms_changed') {", 1)[1].split("else if", 1)[0]
    assert "setInboxVersion(v => v + 1);" in rooms
    assert "fetch(`${getApiBase()}/api/inbox/summary`" in page
    assert "}, [inboxVersion, isConnected]);" in page
    assert page.count("setInterval(") == 9, "no new interval; the inbox is a signal, not a poll"
    window = _read(WINDOW)
    assert "setInterval(" not in window
    assert "}, [version, isOpen]);" in window and "setTimeout(() => { void loadRef.current(); }, 400);" in window


def test_the_window_reads_the_shells_pieces_and_registers_its_own_escape_rungs():
    src = _read(WINDOW)
    for piece in ("ConversationBubbles", "StateChips", "WaitsChip", "useConversationHistory", "initials", "fmtWhen"):
        assert piece in src.split("from '@/components/connections/ChannelDashboardShell';", 1)[0], piece
    assert "rounded-tr-sm" not in src and "const FIELD" not in src, "the bubbles and the compose field live in the shell"
    levels = re.findall(r"useEscapeLayer\(\{ active: [^\n]+?, level: (\d+)", src)
    assert levels == ["67", "66", "65"], levels
    assert "addEventListener('keydown'" not in src
    assert "fetch(api('api/inbox/marks'), {" in src and "seen: true" in src
    assert "done: value" not in src and "done: true" not in src and "setDoneMark" not in src and "t('markDone')" not in src and "t('reopen')" not in src, \
        "no done or read button: opening a row reads it, and the reader decides"
    assert "fetch(api(`api/inbox/summary?${summaryParams}`)" in src, "the rail's counts are the whole inbox's, not the narrowed result's"
    assert "setCounts(sum?.ok ? await sum.json() : (json.counts ?? null));" in src
    assert "t('unanswered', { name: selected.name || selected.id })" in src, "the reason is said in one sentence"
    # Reading takes a row off "waits for you": the chips clear at once, the opened row outlives the
    # list it may leave, and the amber sentence keeps the reason the row was opened with.
    assert "const readOf = (r: InboxRow) => r.waits_reason !== 'invitation' && (r.key === selectedKey || marked.get(r.key) === stateOf(r));" in src
    assert "const waitsOf = (r: InboxRow) => !readOf(r) && r.waits;" in src and "waits={waitsOf(r)}" in src and "{waitsOf(selected) && <WaitsChip" in src
    assert "if (!isOpen || !live || !selectedNeedsMark) return;" in src, "a room row is read like any other"
    assert "const selectedNeedsMark = !!live && live.waits_reason !== 'invitation' && (live.unread > 0 || live.waits);" in src, "an invitation posts nothing"
    assert "const stateOf = (r: InboxRow) => `${r.unread}:${r.waits ? 1 : 0}:${r.last_ts}`;" in src, "a newer message is news even at the same count"
    assert "}).then(res => { if (!res.ok) refused(); }).catch(refused);" in src and "if (prior === selectedState || prior === `refused:${selectedState}`) return;" in src, \
        "a refused mark is remembered as refused for that state: the pill stays and nothing retries until the state changes"
    assert "const movedOn = !!(live && opened && (live.last_ts > opened.row.last_ts || live.answered_by_agent || live.done));" in src
    assert "const selected = live ?? (opened && opened.row.key === selectedKey ? opened.row : null);" in src
    assert "const noteReason = live?.waits ? live.waits_reason : (opened && opened.row.key === selectedKey && !movedOn ? opened.reason : '');" in src
    assert "setOpened({ row: r, reason: r.waits ? r.waits_reason : '' });" in src
    assert "{noteReason === 'unanswered' && (" in src and "{noteReason === 'owner_asked' && (" in src and "{noteReason === 'invitation' && (" in src
    # "Mark all as read": the whole selection at once, through the bulk route, then a reload.
    assert "fetch(api('api/inbox/marks/all'), {" in src and "body: JSON.stringify({ channels: channel ? [channel] : 'all', groups, bulk })," in src
    # Bulk mail stays out of the list unless the toggle asks; the list, the rail and the bulk read follow it.
    assert "<Toggle on={bulk} onChange={setBulk} label={t('showBulk')} />" in src
    assert "done: String(done), bulk: String(bulk), limit: '200'" in src and "new URLSearchParams({ groups: String(groups), done: String(done), bulk: String(bulk) })" in src
    assert "disabled={markingAll || !anythingToRead} title={t('markAllReadHint')}" in src and "{t('markAllRead')}" in src
    body = src.split("const anythingToRead = (() => {", 1)[1].split("})();", 1)[0]
    assert "const invitations = counts.invitations ?? 0;" in body and "counts.unread_per_channel?.[channel]" in body and "counts.waits - invitations > 0" in body, \
        "the button is offered only while the selection holds something a read can clear"
    mark = src.split("const markAllRead = async () => {", 1)[1].split("};", 1)[0]
    assert "await load();\n        setMarkingAll(false);" in mark and "setMarked(new Map())" not in mark
    assert "selected.waits &&" not in src and "selected.waits_reason ===" not in src, "the header and the notes read the local state, not the stale server flag"
    assert "api/inbox/history?channel=" in src


def test_the_window_closes_before_it_opens_a_channel_window_and_every_window_accepts_a_jump():
    src = _read(WINDOW)
    body = src.split("const openElsewhere = ", 1)[1].split("};", 1)[0]
    assert body.index("onClose();") < body.index("onOpenInChannel(") and "onOpenRoom(r.id, r.name)" in body
    page = _read(PAGE)
    assert "initialChatJump={settingsChatJump}" in page and "onChatJumpConsumed={() => setSettingsChatJump(null)}" in page
    assert "setSettingsInitialTab('connections');" in page.split("<InboxWindow", 1)[1].split("/>", 1)[0]
    settings = _read(SETTINGS)
    assert "channel: 'whatsapp' | 'telegram' | 'discord'; chatId: string; draft?: boolean" in settings
    assert "initialChatId={chatJump?.channel === 'discord' ? chatJump.chatId : null}" in settings
    assert "initialThread={mailJump?.threadId ?? null}" in settings and "initialDraft={mailJump?.draft ?? false}" in settings
    assert "initialDraft={chatJump?.channel === 'whatsapp' ? !!chatJump.draft : false}" in settings
    assert "onChatJumpConsumed?.();" in settings
    discord = _read(WEB / "components" / "connections" / "DiscordDashboard.tsx")
    assert "initialChatId?: string | null;" in discord and "setSelectedChatId(initialChatId);" in discord
    mail = _read(WEB / "app" / "mail" / "page.tsx")
    assert "void openThread({ thread_id: initialThread } as ThreadRow);" in mail
    assert "jumpHandledRef.current === initialThread" in mail and "draftPendingRef.current = initialDraft ? initialThread : null" in mail


def test_the_inbox_has_no_input_field_and_a_draft_starts_the_composer_in_the_channel_window():
    src = _read(WINDOW)
    assert "ComposeBox" not in src and "api/whatsapp/send" not in src and "api/telegram/send" not in src, \
        "the inbox reads and decides; writing happens in the channel window"
    assert "const canDraft = (r: InboxRow) => (r.channel === 'whatsapp' && r.can_compose) || r.channel === 'mail';" in src
    assert "openElsewhere(selected, true)" in src, "write a draft jumps with the draft flag"
    wa = _read(WEB / "components" / "connections" / "WhatsAppDashboard.tsx")
    assert "runDraftRef.current = (chatId: string) => {" in wa and "void runComposer(s, 'draft');" in wa, \
        "the WhatsApp window runs the Composer's draft for a draft jump"
    assert "if (runDraftRef.current?.(pending)) draftPendingRef.current = null;" in wa
    mail = _read(WEB / "app" / "mail" / "page.tsx")
    assert "autoDraft={composeAutoDraft}" in mail and "void runComposer('draft');" in mail.split("autoDraftDone.current = true;", 1)[1][:200]
    assert "bg-[#e05d44]" not in src, "the unread pill comes from the shell, not a second definition"
    assert "bg-[#25a244]" in src and "bg-[#2aabee]" in src and "bg-[#5865f2]" in src and "bg-[#e0a03c]" in src and "bg-[#a78bfa]" in src


def test_mobile_is_additive_and_the_three_panes_stack():
    src = _read(WINDOW)
    assert "grid-cols-[210px_380px_1fr] max-md:grid-cols-1 max-md:grid-rows-[auto_1fr]" in src
    assert "gridTemplateColumns" not in src, "an inline style outranks the max-md class; the columns are a class"
    shell = _read(WEB / "components" / "connections" / "ChannelDashboardShell.tsx")
    assert "grid-cols-[320px_1fr] max-md:grid-cols-1" in shell and "gridTemplateColumns" not in shell
    assert "mobilePane === 'preview' && 'max-md:hidden'" in src and "mobilePane === 'list' && 'max-md:hidden'" in src
    assert 'className="md:hidden p-1.5' in src, "the back button exists on a phone only"
    assert "setMobilePane('list')" in src and "!selected) setMobilePane('list')" not in src, \
        "the opened row outlives the list, so the phone never lands on an empty preview and never steps back on its own"
    assert "max-md:flex-row max-md:flex-nowrap max-md:overflow-x-auto" in src, "the rail becomes a chip strip that scrolls sideways"


def test_the_seven_catalogues_carry_the_nav_entry_and_the_block_with_the_same_arguments():
    ref = json.loads(_read(WEB / "messages" / "de.json"))
    for p in sorted((WEB / "messages").glob("*.json")):
        d = json.loads(_read(p))
        assert d["nav"]["inbox"], p.name
        block = d["inbox"]
        def walk(a, b, path=""):
            assert set(a) == set(b), (p.name, path, set(a) ^ set(b))
            for k in a:
                if isinstance(a[k], dict):
                    walk(a[k], b[k], f"{path}{k}.")
                else:
                    assert set(re.findall(r"\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?=[},])", a[k])) == set(re.findall(r"\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?=[},])", b[k])), (p.name, path + k)
        walk(ref["inbox"], block)
    used = set(re.findall(r"\bt\('([\w.]+)'", _read(WINDOW)))
    flat = set()
    def flatten(node, prefix=""):
        for k, v in node.items():
            if isinstance(v, dict):
                flatten(v, f"{prefix}{k}.")
            else:
                flat.add(prefix + k)
    flatten(ref["inbox"])
    assert used <= flat, used - flat
