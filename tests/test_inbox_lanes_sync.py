# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The inbox's lanes are declared once, in the backend (`vaf.core.inbox.CHANNELS`, the
registry's `inbox=True` channels plus mail and the agent rooms), and the inbox window carries
four copies of them: the rail's lane list, the colour square per lane, the `InboxChannel` type
and every catalogue's `inbox.channel.<lane>` name. They have to agree.

Live incident: a chat channel became an inbox lane by gaining a bridge, and none of the four
copies followed. Its rows arrived in "All channels" without a square, and the preview pane
asked the catalogue for `inbox.channel.<lane>` on every render, which logged MISSING_MESSAGE
over and over. The catalogue parity test could not see it: it compares every locale against
English, and the key was missing from all seven.

Each check is a function of a lane tuple, so the self-tests at the end can prove that a lane
without its web half is reported, not just that today's tuple passes.
"""
import json
import re
import shutil
from pathlib import Path
from typing import Iterable, List

from vaf.core import inbox

ROOT = Path(__file__).resolve().parent.parent
WINDOW = ROOT / "web" / "components" / "inbox" / "InboxWindow.tsx"
SHELL = ROOT / "web" / "components" / "connections" / "ChannelDashboardShell.tsx"
MESSAGES = ROOT / "web" / "messages"
LOCALES = 7


def _src(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _rail_lanes(src: str) -> List[str]:
    m = re.search(r"const CHANNELS: InboxChannel\[\] = \[([^\]]*)\];", src)
    assert m, "InboxWindow.tsx: `const CHANNELS: InboxChannel[] = [...]` not found"
    return re.findall(r"'(\w+)'", m.group(1))


def _square_lanes(src: str) -> List[str]:
    m = re.search(r"const CHANNEL_SQUARE: Record<InboxChannel, string> = \{(.*?)\};", src, re.S)
    assert m, "InboxWindow.tsx: `const CHANNEL_SQUARE: Record<InboxChannel, string> = {...}` not found"
    return re.findall(r"(\w+)\s*:\s*'", m.group(1))


def _type_lanes(src: str) -> List[str]:
    m = re.search(r"export type InboxChannel =([^;]*);", src)
    assert m, "ChannelDashboardShell.tsx: `export type InboxChannel = ...;` not found"
    return re.findall(r"'(\w+)'", m.group(1))


def rail_problems(lanes: Iterable[str], src: str) -> List[str]:
    lanes, rail = list(lanes), _rail_lanes(src)
    return [] if rail == lanes else [f"rail lanes {rail} != inbox lanes {lanes} (same order: the rail's order)"]


def square_problems(lanes: Iterable[str], src: str) -> List[str]:
    lanes, squares = set(lanes), set(_square_lanes(src))
    return ([f"CHANNEL_SQUARE has no colour for {sorted(lanes - squares)}"] if lanes - squares else []) + \
           ([f"CHANNEL_SQUARE colours a lane the inbox does not have: {sorted(squares - lanes)}"] if squares - lanes else [])


def type_problems(lanes: Iterable[str], src: str) -> List[str]:
    lanes, members = set(lanes), set(_type_lanes(src))
    return [] if members == lanes else [f"InboxChannel {sorted(members)} != inbox lanes {sorted(lanes)}"]


def label_problems(lanes: Iterable[str], messages: Path) -> List[str]:
    files = sorted(messages.glob("*.json"))
    out = [] if len(files) == LOCALES else [f"expected {LOCALES} catalogues in {messages}, found {len(files)}"]
    for f in files:
        names = json.loads(f.read_bytes().decode("utf-8")).get("inbox", {}).get("channel", {})
        for lane in lanes:
            if not (isinstance(names.get(lane), str) and names[lane].strip()):
                out.append(f"{f.name}: inbox.channel.{lane} is missing or empty")
    return out


# -- today's lanes --------------------------------------------------------------------------

def test_the_rail_lists_exactly_the_inbox_lanes_in_order():
    assert not rail_problems(inbox.CHANNELS, _src(WINDOW)), rail_problems(inbox.CHANNELS, _src(WINDOW))


def test_every_lane_has_its_colour_square():
    assert not square_problems(inbox.CHANNELS, _src(WINDOW)), square_problems(inbox.CHANNELS, _src(WINDOW))


def test_the_inbox_channel_type_is_the_inbox_lanes():
    assert not type_problems(inbox.CHANNELS, _src(SHELL)), type_problems(inbox.CHANNELS, _src(SHELL))


def test_every_catalogue_names_every_lane():
    problems = label_problems(inbox.CHANNELS, MESSAGES)
    assert not problems, "\n".join(problems)


# -- the guard sees what it guards ----------------------------------------------------------

def test_a_lane_without_its_web_half_is_reported_everywhere():
    """A chat channel flipped to `inbox=True` without the window's half: all four checks name
    it. Here the Veyllo App, the lane this guard was written on."""
    extended = inbox.CHANNELS + ("veyllo_app",)
    assert rail_problems(extended, _src(WINDOW))
    assert any("veyllo_app" in p for p in square_problems(extended, _src(WINDOW)))
    assert type_problems(extended, _src(SHELL))
    missing = label_problems(extended, MESSAGES)
    assert len(missing) == LOCALES and all("inbox.channel.veyllo_app" in p for p in missing), missing


def test_a_label_missing_from_every_catalogue_is_reported(tmp_path):
    """The case the catalogue parity test cannot see: the key is gone from all seven."""
    for f in MESSAGES.glob("*.json"):
        data = json.loads(f.read_bytes().decode("utf-8"))
        del data["inbox"]["channel"]["discord"]
        (tmp_path / f.name).write_bytes(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    missing = label_problems(inbox.CHANNELS, tmp_path)
    assert len(missing) == LOCALES and all("inbox.channel.discord" in p for p in missing), missing
    shutil.copy(MESSAGES / "en.json", tmp_path / "en.json")
    assert len(label_problems(inbox.CHANNELS, tmp_path)) == LOCALES - 1


def test_a_square_or_rail_entry_dropped_from_the_window_is_reported():
    src = _src(WINDOW)
    no_room_square = re.sub(r",\s*room: 'bg-\[#[0-9a-f]+\]'", "", src, count=1)
    assert no_room_square != src
    assert square_problems(inbox.CHANNELS, no_room_square)
    no_discord_rail = src.replace("'telegram', 'discord', 'mail'", "'telegram', 'mail'", 1)
    assert no_discord_rail != src
    assert rail_problems(inbox.CHANNELS, no_discord_rail)
