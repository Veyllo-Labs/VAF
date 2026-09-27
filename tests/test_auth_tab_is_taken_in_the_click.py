# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A sign-in page the backend has to produce first is opened through `web/lib/authTab.ts`.

Measured before the change: four sign-ins (mail, calendar, cloud, MCP services) fetched the
authorization address and only then called `window.open(url, '_blank', 'noopener,...')`. In a
Chromium with its popup blocker on, a tab opened 6.5 s after the click (an MCP start that has to
discover metadata and register a client can take that long) is blocked, and with `noopener` the
call answers null either way, so the page could not tell and showed nothing. Taking the tab in the
click and sending it to the address later opened it in the same run; a tab the blocker refused
anyway makes `open()` answer false, and the caller offers the address as a link.

Source guards, because the behaviour needs a real browser with its blocker on to show at all.
"""
import re
from pathlib import Path

_WEB = Path(__file__).resolve().parents[1] / "web"

# A component that handles an authorization address and still calls window.open itself, with
# the reason it may.
_ALLOWED_OPEN = {
    # The device flow's verification page is a fixed address the component already has; it is
    # opened synchronously inside the click that copies the code, before any await.
    "components/connections/GitHubSetupWizard.tsx",
}


def _components():
    for path in sorted((_WEB / "components").rglob("*.tsx")):
        yield path.relative_to(_WEB).as_posix(), path.read_text(encoding="utf-8")


def test_no_sign_in_opens_its_page_after_the_fetch():
    offenders = [rel for rel, text in _components()
                 if re.search(r"authorization_url|authUrl", text) and "window.open(" in text
                 and rel not in _ALLOWED_OPEN]
    assert not offenders, ("open the sign-in page through reserveAuthTab() (web/lib/authTab.ts), "
                           "taken in the click:\n" + "\n".join(offenders))


def test_every_reserved_tab_is_taken_before_the_first_await():
    """The reservation only works while the click is fresh: it has to come before any await in
    the handler, and every such handler either sends the tab somewhere or closes it."""
    users = [(rel, text) for rel, text in _components() if "reserveAuthTab()" in text]
    assert len(users) >= 4, [rel for rel, _ in users]
    for rel, text in users:
        for match in re.finditer(r"const (\w+) = reserveAuthTab\(\);", text):
            name = match.group(1)
            start = text.rfind("=>", 0, match.start())
            head = text[start:match.start()]
            assert "await " not in head and ".then(" not in head, f"{rel}: reserved after an await"
            rest = text[match.end():match.end() + 3000]
            assert f"{name}.open(" in rest and f"{name}.cancel()" in rest, (
                f"{rel}: the reserved tab is neither sent to the address nor closed on failure")


# The state each sign-in keeps its fallback link in, and how it is emptied.
_LINK_CLEARED = ("setSignInPage('')", "setAuthUrl('')", "delete next[")


def test_a_new_attempt_clears_the_link_of_the_last_one():
    """The fallback link belongs to one attempt: a later attempt that fails, or one for another
    provider, must not leave the earlier address on screen."""
    for rel, text in _components():
        for match in re.finditer(r"reserveAuthTab\(\);", text):
            before_fetch = text[match.end():text.find("fetch(", match.end())]
            assert any(clear in before_fetch for clear in _LINK_CLEARED), (
                f"{rel}: the link of the previous attempt is not cleared before the new start")


def test_the_reserved_tab_loses_its_opener_before_it_leaves_the_origin():
    src = (_WEB / "lib" / "authTab.ts").read_text(encoding="utf-8")
    reserve = src[src.index("export function reserveAuthTab"):]
    assert reserve.index("tab.opener = null") < reserve.index("tab.location.replace(url)")
    assert "opened.opener = null" in reserve, "the retry tab is cut from its opener too"
