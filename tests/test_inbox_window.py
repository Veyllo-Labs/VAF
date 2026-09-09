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
    for piece in ("ComposeBox", "ConversationBubbles", "StateChips", "WaitsChip", "useConversationHistory", "initials", "fmtWhen"):
        assert piece in src.split("from '@/components/connections/ChannelDashboardShell';", 1)[0], piece
    assert "rounded-tr-sm" not in src and "const FIELD" not in src, "the bubbles and the compose field live in the shell"
    levels = re.findall(r"useEscapeLayer\(\{ active: [^\n]+?, level: (\d+)", src)
    assert levels == ["67", "66", "65"], levels
    assert "addEventListener('keydown'" not in src
    assert "fetch(api('api/inbox/marks'), {" in src and "seen: true" in src and "done: value" in src
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


def test_the_compose_box_obeys_the_whatsapp_rule_and_the_unread_token_is_mails():
    src = _read(WINDOW)
    assert "selected.channel === 'whatsapp' && selected.can_compose ? (" in src
    assert "fetch(api('api/whatsapp/send'), {" in src and "api/telegram/send" not in src and "api/discord/send" not in src
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
