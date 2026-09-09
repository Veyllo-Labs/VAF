# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The channel shell carries the inbox's state (web/components/connections/ChannelDashboardShell.tsx):
one unread token, one waits chip, one "N waiting for you" button, one seen mark on opening,
and the pieces the inbox window reads too (bubbles, history hook, compose box) exported from
the shell instead of copied. Every window maps the fields; the mail page shows the same chip.

MUTATION: copy the bubble block back into a window and the single-home test goes red; drop the
seen POST and the marks test goes red; bump an Escape level and the ladder test goes red.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
SHELL = WEB / "components" / "connections" / "ChannelDashboardShell.tsx"
WINDOWS = {
    "whatsapp": WEB / "components" / "connections" / "WhatsAppDashboard.tsx",
    "telegram": WEB / "components" / "connections" / "TelegramDashboard.tsx",
    "discord": WEB / "components" / "connections" / "DiscordDashboard.tsx",
}
MAIL = WEB / "app" / "mail" / "page.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _tsx_files():
    return [p for sub in ("app", "components", "hooks", "lib") for p in (WEB / sub).rglob("*.tsx") if "node_modules" not in p.parts]


def test_the_shell_exports_the_pieces_the_inbox_window_reads():
    src = _read(SHELL)
    for name in ("UnreadPill", "WaitsChip", "StateChips", "ComposeBox", "ConversationBubbles"):
        assert f"export function {name}(" in src, name
    assert "export function useConversationHistory(" in src
    for const in ("UNREAD_PILL", "WAITS_CHIP", "AGENT_CHIP", "DONE_CHIP", "FIELD", "ROUND_BTN"):
        assert f"export const {const} = " in src, const
    assert "export type InboxChannel = 'whatsapp' | 'telegram' | 'discord' | 'mail' | 'room'" in src


def test_the_bubbles_the_compose_field_and_the_history_fetch_have_one_home():
    # The contact book's timeline draws bubbles of its own and stays outside this rule:
    # it is not a conversation pane, and it predates the shell.
    surfaces = [SHELL, MAIL, *WINDOWS.values()]
    homes = {p.relative_to(ROOT).as_posix() for p in surfaces if "rounded-tr-sm" in _read(p)}
    assert homes == {"web/components/connections/ChannelDashboardShell.tsx"}, homes
    field_homes = {p.relative_to(ROOT).as_posix() for p in surfaces if re.search(r"^(?:export )?const (?:FIELD|ROUND_BTN) = ", _read(p), re.M)}
    assert field_homes == {"web/components/connections/ChannelDashboardShell.tsx"}, field_homes
    hook = _read(SHELL).split("export function useConversationHistory(", 1)[1].split("\nexport ", 1)[0]
    assert "const requestNo = ++historyRequest.current;" in hook, "the request counter drops late answers"
    assert "}, [historyKey, isOpen, historyVersion]);" in hook, "the fetch keys on the key and the version, never the URL builder"
    assert "historyUrlRef.current = historyUrl;" in hook


def test_the_unread_token_is_the_mail_windows_and_the_chips_keep_their_colours():
    src = _read(SHELL)
    assert "export const UNREAD_PILL = 'text-[11px] leading-[18px] px-1.5 rounded-full bg-[#e05d44] text-white'" in src
    assert "bg-[#4a3b1e] text-[#e0b866]" in src.split("export const WAITS_CHIP", 1)[1].split("\n", 1)[0]
    assert "bg-[#1f4d2a] text-[#9fe0b0]" in src.split("export const AGENT_CHIP", 1)[1].split("\n", 1)[0]
    mail = _read(MAIL)
    assert "bg-[#e05d44] text-white" in mail, "the mail window's own pill is the token the shell copied"


def test_opening_a_chat_posts_its_seen_mark_and_reading_is_not_answering():
    src = _read(SHELL)
    body = src.split("export default function ChannelDashboardShell(", 1)[1]
    assert "fetch(api('api/inbox/marks'), {" in body and "body: JSON.stringify({ channel, id, seen: true })" in body
    assert "for (const id of selected.markIds ?? [selected.id])" in body, "an @lid merged into its number marks both store rows"
    assert "if (!channel || !isOpen || !selected || selectedUnread <= 0 || markedUnread.get(selected.id) === selectedUnread) return;" in body
    assert "const waiting = useMemo(() => chats.filter(c => c.waits), [chats]);" in body
    assert "onSelect(waiting[(idx + 1) % waiting.length].id);" in body, "the header button walks the waiting chats round and round"
    assert "{selected.waits && selected.waitsReason === 'owner_asked' && (" in body, "the agent's question to the person is said in the header"
    assert "t('waitsUnanswered', { name: selected.label })" in body, "and so is an unanswered last message, by name"
    assert "t('waitsHeader', { count: waiting.length })" in body and "t('newestFirst')" in body


def test_every_window_maps_the_state_and_names_its_channel():
    for channel, path in WINDOWS.items():
        src = _read(path)
        assert f'channel="{channel}"' in src, path.name
        for field in ("waits: s.waits", "waitsReason: s.waits_reason", "answeredByAgent: s.answered_by_agent", "done: s.done", "unread: s.unread"):
            assert field in src, (path.name, field)
    wa = _read(WINDOWS["whatsapp"])
    assert "markIds: s.store_chat_ids" in wa
    assert "<ComposeBox fieldRef={composeRef}" in wa and "const FIELD" not in wa and "const ROUND_BTN" not in wa


def test_the_mail_page_shows_the_same_chip_and_the_same_header_button():
    src = _read(MAIL)
    assert "import { WaitsChip } from '@/components/connections/ChannelDashboardShell';" in src
    assert "{row.waits && <WaitsChip reason={row.waits_reason} />}" in src
    assert "const tc = useTranslations('settings.channelDashboard');" in src
    assert "tc('waitsHeader', { count: waiting.length })" in src
    assert "void openThread(waiting[(idx + 1) % waiting.length]);" in src


def test_the_shells_escape_ladder_is_unchanged():
    src = _read(SHELL)
    levels = re.findall(r"useEscapeLayer\(\{ active: [^,]+, level: (\d+)", src)
    assert levels == ["52", "51", "50"], levels
    assert "addEventListener('keydown'" not in src


def test_the_seven_catalogues_carry_the_chip_keys_with_the_same_arguments():
    import json
    keys = {"waitsForYou": set(), "waitsOwnerAsked": set(), "waitsUnanswered": {"name"}, "waitsHeader": {"count"},
            "jumpWaiting": set(), "unreadCount": {"count"}, "agentAnswered": set(), "done": set()}
    for p in sorted((WEB / "messages").glob("*.json")):
        block = json.loads(_read(p))["settings"]["channelDashboard"]
        for key, args in keys.items():
            assert key in block, (p.name, key)
            assert set(re.findall(r"\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?=[},])", block[key])) == args, (p.name, key)
