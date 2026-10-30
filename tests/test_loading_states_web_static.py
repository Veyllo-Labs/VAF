# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Loading states in the web UI: no enter animation on a tab, one placeholder for a first load.

A Settings tab appears the same way as every other one, without sliding or fading in. A
region waiting for its first data shows the shared placeholder (web/components/ui/Skeleton.tsx)
in the shape and height of what is coming, so nothing below it moves when the data lands, and
it says so once to a screen reader. The motion lives in one place (globals.css: opacity only, a
reveal after 180 ms so fast loads never flash, still under reduced motion), and no site writes
its own pulse. A refetch with data on screen keeps the data.

The heights are measured in a browser (the jump probe of the build report); what is pinned here
is the wiring that makes them hold, and that each site no longer renders nothing, a spinner or
a false "empty" while it waits.
"""
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


def _src(path: Path) -> str:
    # Bytes, decoded: a CRLF checkout must read the same as an LF one for these needles.
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


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
    colour of its own beyond the folded grey. MUTATION: drop `role="status"`, swap in
    `animate-pulse`, or add `bg-gradient-to-r`."""
    src = _src(_SKELETON)
    assert src.startswith("// SPDX-FileCopyrightText: 2026 Veyllo GmbH\n")
    assert 'role="status"' in src
    assert "useTranslations('common')" in src and "label ?? t('loading')" in src
    assert '<span className="sr-only">' in src
    assert '<div aria-hidden="true">' in src
    assert "export default function Skeleton(" in src
    assert "export function SkeletonBar(" in src and "export function SkeletonRows(" in src
    assert "'vaf-skel overflow-hidden'" in src and "'vaf-skel-bar rounded bg-gray-200'" in src
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
    cannot drift apart. MUTATION: inline `animation: 'skelPulse ...'` in any component."""
    offenders = [str(p.relative_to(_ROOT)) for p in (_WEB / "components").rglob("*.tsx")
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
