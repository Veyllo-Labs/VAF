# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The "who does what" overview in Settings > AI & Model, held against the resolver.

The overview renders the rows of `POST /api/config/model-roles`, which are
`vaf.core.model_roles.resolve_roles()`. Three things can drift from that list without a
type error or a red unit test anywhere else, and each is pinned here against the
resolver's own tables rather than against a copy:

- a row whose setting has no jump target (a role added to `ROLES` and the click goes
  nowhere), or a target on the wrong tab (the click switches to a tab that does not hold it);
- a role, source or reason code with no text in one of the seven catalogues (next-intl
  prints the raw key path to the person);
- logic creeping back into the browser: a provider list or a capability table in the
  component, or the TypeScript vision copy the backend predicate replaced.

The jump target is the `data-setting-role` attribute, never the slug of a translated
section title: in zh, ja, ko and th every title slugs to the same id.
"""
import json
import re
from pathlib import Path

import pytest

from vaf.core import config as config_mod
from vaf.core import model_roles

_ROOT = Path(__file__).resolve().parent.parent
_MODAL = _ROOT / "web" / "components" / "SettingsModal.tsx"
_OVERVIEW = _ROOT / "web" / "components" / "settings" / "ModelRolesOverview.tsx"
_CSS = _ROOT / "web" / "app" / "globals.css"
_MESSAGES = _ROOT / "web" / "messages"
_LOCALES = ("de", "en", "ja", "ko", "th", "tr", "zh")


def _src(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _tab_regions(src: str) -> dict:
    """Where each tab's content is rendered: from its `{activeTab === '<tab>'` block to the next."""
    starts = [(m.start(), m.group(1)) for m in re.finditer(r"\{activeTab === '(\w+)'", src)]
    regions = {}
    for i, (pos, tab) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(src)
        regions.setdefault(tab, []).append((pos, end))
    return regions


@pytest.mark.parametrize("role", model_roles.ROLES, ids=lambda r: r.name)
def test_every_role_has_one_jump_target_on_its_tab(role):
    """A click on a row goes to `row.tab` and `[data-setting-role="<row.role>"]`. Every
    role the resolver can return needs exactly one such target, inside the content of the
    tab the API names. MUTATION: drop or rename one anchor, or move the voice-call anchor
    onto the AI tab."""
    src = _src(_MODAL)
    hits = [m.start() for m in re.finditer(
        r'(?:data-setting-role|anchor)="' + re.escape(role.name) + r'"', src)]
    assert len(hits) == 1, f"{role.name}: {len(hits)} jump targets"
    regions = _tab_regions(src).get(role.tab, [])
    assert any(a <= hits[0] < b for a, b in regions), f"{role.name} is not on the {role.tab} tab"


def test_the_api_rows_carry_the_tab_and_role_the_targets_use():
    """The row's `tab` and `role` are the jump's only inputs; `as_dict` must hand both
    over from the role list. MUTATION: drop `tab` from `as_dict`."""
    row = model_roles._row("voice_call", None, None, "unavailable", "no_api_key").as_dict()
    assert row["role"] == "voice_call" and row["tab"] == "voice"


def test_the_section_renders_its_anchor_as_a_locale_free_attribute():
    """MUTATION: drop `data-setting-role={anchor}` from `Section`."""
    src = _src(_MODAL)
    section = src[src.index("const Section = ("):]
    section = section[:section.index("\n);")]
    assert "data-setting-role={anchor}" in section


def test_the_overview_and_the_search_share_one_jump():
    """One jump function serves both, and it waits for the tab to render instead of a
    fixed timeout. MUTATION: remove the pending-jump effect, or put the 80 ms timeout back."""
    src = _src(_MODAL)
    assert "onPick={(row) => jumpToSetting(row.tab, `[data-setting-role=\"${row.role}\"]`, true)}" in src
    search = src[src.index("const goToSearchResult = "):]
    search = search[:search.index("};")]
    assert "jumpToSetting(e.tab," in search
    assert "setTimeout(() => document.getElementById(" not in src
    effect = src[src.index("const settingFlash = useRef"):]
    effect = effect[:effect.index("}, [activeTab, pendingJump]);")]
    assert "requestAnimationFrame(" in effect
    assert "document.querySelector(selector)" in effect
    assert "scrollIntoView(" in effect
    assert "classList.add('vaf-setting-flash')" in effect
    assert "setTimeout(endSettingFlash, 1600)" in effect


def test_the_highlight_moves_opacity_only():
    """The repaint rule (globals.css): an animation moves transform and opacity, nothing
    else, and the ring's ink comes from a folded variable so both themes see it.
    MUTATION: animate box-shadow, or write the ring in a raw colour."""
    css = _src(_CSS)
    frames = []
    for m in re.finditer(r"@keyframes vafSettingFlash \{", css):
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        frames.append(css[m.end():i - 1])
    assert len(frames) == 2, "one rule and its reduced-motion twin"
    for body in frames:
        props = set(re.findall(r"([a-z-]+)\s*:", body))
        assert props == {"opacity"}, props
    ring = css[css.index(".vaf-setting-flash::after {"):]
    ring = ring[:ring.index("}")]
    assert "animation: vafSettingFlash" in ring
    assert "var(--txt-gray-900)" in ring and "#" not in ring
    reduced = css[css.index("@media (prefers-reduced-motion: reduce)"):]
    assert "@keyframes vafSettingFlash" in reduced


def test_the_component_holds_no_provider_or_role_logic():
    """It renders the rows it is given, in their order. MUTATION: add a provider list or
    a role-name branch to the component."""
    src = _src(_OVERVIEW)
    assert "rows.map(" in src
    assert ".sort(" not in src and ".filter(row" not in src
    for provider in list(config_mod.Config.PROVIDER_MODELS) + ["local", "elevenlabs"]:
        assert f"'{provider}'" not in src and f'"{provider}"' not in src, provider
    for role in model_roles.ROLES:
        assert f"'{role.name}'" not in src and f'"{role.name}"' not in src, role.name


def _catalogue(locale: str) -> dict:
    return json.loads((_MESSAGES / f"{locale}.json").read_bytes())["settings"]["ai"]


@pytest.mark.parametrize("locale", _LOCALES)
def test_every_role_source_and_reason_has_a_text(locale):
    """Every code the resolver can send has its text in every catalogue, and no stale one
    is left. MUTATION: delete one reason from tr.json, or add a code to REASON_TEXT."""
    ai = _catalogue(locale)
    assert set(ai["role"]) == {r.name for r in model_roles.ROLES}
    assert set(ai["roleSource"]) == set(model_roles.SOURCES)
    assert set(ai["roleReason"]) == set(model_roles.REASON_TEXT)
    for key in ("rolesTitle", "rolesIntro", "rolesLoading", "rolesError", "rolesRetry",
                "rolesJumpHint", "rolesShowLess", "rolesAllFine", "localSpeechEngine",
                "visionFollowMain", "visionMainCannot"):
        assert ai.get(key, "").strip(), key
    assert "{count}" in ai["rolesShowAll"]
    for text in [*ai["role"].values(), *ai["roleSource"].values(), *ai["roleReason"].values()]:
        assert text.strip() and "\u2014" not in text


@pytest.mark.parametrize("locale", _LOCALES)
def test_the_vision_option_no_longer_claims_an_error(locale):
    """An empty vision provider follows the main provider when it can read images; the
    option said "None (show error)". MUTATION: restore `visionNone`."""
    ai = _catalogue(locale)
    assert "visionNone" not in ai
    assert ai["visionFollowMain"] != ai["visionSameProvider"]


def test_the_vision_hint_reads_the_backend_row():
    """The TypeScript capability copy ignored the model; the hint now reads the vision
    row of the overview, which the backend predicate decided. MUTATION: restore
    `VISION_CAPABLE_PROVIDERS`, or drop the amber line."""
    src = _src(_MODAL)
    assert "VISION_CAPABLE_PROVIDERS" not in src
    assert "visionNone" not in src
    assert "{ value: '', label: tAi('visionFollowMain') }" in src
    assert "const visionRole = modelRoleRows?.find(r => r.role === 'vision');" in src
    assert re.search(r"visionRole\?\.source === 'follows_main' && \(\s*<p[^>]*text-green-600", src)
    assert re.search(r"visionRole\.reason === 'main_cannot_see' && \(\s*<p className=\"text-xs text-amber-700[^\"]*\">\{tAi\('visionMainCannot'\)\}", src)


def test_the_sub_agent_select_shows_and_writes_the_gate():
    """The select shows what sub-agents run on (a name without its gate is inert) and a
    pick writes the name and the gate together, so picking a stored inert name again is a
    change the save can see. MUTATION: drop the gate write, show the raw name, or derive the
    tool-model provider from the raw name again."""
    src = _src(_MODAL)
    choice = re.search(r"const subagentChoice = (.*?);\n", src, re.S)
    assert choice and "localConfig.subagent_use_separate_provider" in choice.group(1)
    m = re.search(r"label=\{tAdvanced\('subAgentProvider'\)\}\s*value=\{subagentChoice\}\s*"
                  r"onChange=\{\(v: string\) => \{(.*?)\}\}", src, re.S)
    assert m, "the sub-agent select does not show the gate-aware choice"
    assert "handleChange('subagent_provider', v);" in m.group(1)
    assert "handleChange('subagent_use_separate_provider', v !== 'inherit');" in m.group(1)
    tool = re.search(r"const toolModelProvider = (.*?);\n", src, re.S).group(1)
    assert "subagentChoice" in tool and "localConfig.subagent_provider" not in tool


@pytest.mark.parametrize("stored_name, stored_gate, picked, expected", [
    ("openai", False, "openai", "openai"),     # an inert name an older version stored: switched on
    ("openai", True, "inherit", None),          # back to the main provider
    ("inherit", False, "anthropic", "anthropic"),
    ("openai", True, "deepseek", "deepseek"),
])
def test_what_the_select_writes_takes_effect_on_save(stored_name, stored_gate, picked, expected):
    """The select writes `subagent_provider = v` and the gate `v !== 'inherit'`; the save
    paths pair that against the stored pair. Picking the provider whose name is stored
    without its gate must switch it on, which the name-only select could not do.
    MUTATION: make `pair_subagent_provider` keep the old gate when only the gate changed."""
    existing = {"subagent_provider": stored_name, "subagent_use_separate_provider": stored_gate}
    form = {**existing, "subagent_provider": picked, "subagent_use_separate_provider": picked != "inherit"}
    saved = {**existing, **config_mod.pair_subagent_provider(existing, form)}
    assert config_mod.subagent_provider_override(saved) == expected


def test_the_gate_the_select_writes_is_the_one_the_backend_reads():
    """The key name the select writes is the backend's gate. MUTATION: rename the gate key."""
    assert config_mod.SUBAGENT_PROVIDER_KEYS == ("subagent_provider", "subagent_use_separate_provider")
    assert "subagent_use_separate_provider" in model_roles.ROLE_CONFIG_KEYS


def test_the_overview_asks_the_backend_with_the_unsaved_draft():
    """POST with the draft cut to the answer's own `keys`, debounced, the newest question
    winning, admin only. MUTATION: send the whole form, drop the AbortController, or fetch
    for a non-admin."""
    src = _src(_MODAL)
    block = src[src.index("const [modelRoles, setModelRoles]"):]
    block = block[:block.index("const visionRole =")]
    assert "/api/config/model-roles" in block and "method: 'POST'" in block
    assert "(modelRoles?.keys || [])" in block
    assert "JSON.stringify(localConfig[k]) !== JSON.stringify(config?.[k])" in block
    assert "body: JSON.stringify({ draft: JSON.parse(modelRolesDraft) })" in block
    assert "new AbortController()" in block and "ctl.abort()" in block
    assert "modelRolesAsked.current ? 300 : 0" in block
    assert "activeTab === 'ai' && currentUser?.role === 'admin'" in block
    assert "}, [apiBase, modelRolesShown, modelRolesDraft, config, modelRolesRetry]);" in block


# ── Round of the compact card ────────────────────────────────────────────────────────


def _const_set(src: str, name: str) -> set:
    body = re.search(r"const " + name + r" = new Set\(\[(.*?)\]\)", src, re.S).group(1)
    return set(re.findall(r"'([a-z_]+)'", body))


def test_warning_reasons_are_real_codes_and_include_a_blind_browser():
    """The warning set decides which rows say their reason and stay visible on a collapsed
    phone card. A code the resolver does not send is a silent typo. A browser agent that
    works blind is a lost capability, not a note. MUTATION: drop `browser_dom_only` from the
    set, or misspell a code."""
    codes = _const_set(_src(_OVERVIEW), "WARNING_REASONS")
    assert codes <= set(model_roles.REASON_TEXT), codes - set(model_roles.REASON_TEXT)
    assert "browser_dom_only" in codes and "no_api_key" in codes


def test_one_line_per_job_with_the_reason_only_where_it_needs_attention():
    """Every row is one line; a reason is in the hover title and for screen readers. Only the
    rows that need attention have their reason written out, once, at the card's full width
    under the grid. MUTATION: write every reason out again, or drop the warning filter."""
    src = _src(_OVERVIEW)
    assert "title={reasons.join(' ') || undefined}" in src
    assert "{reasons.length > 0 && <span className=\"sr-only\">{reasons.join(' ')}</span>}" in src
    notes = src[src.index("{warningCount > 0 && ("):]
    notes = notes[:notes.index("</ul>")]
    assert "rows.filter(isWarning).map(" in notes and "text-amber-700" in notes
    row = src[src.index("{rows.map(row => {"):src.index("</ul>")]
    assert "roleReason" not in row.replace("t(`roleReason.${r}`)", "", 1)


def test_a_phone_shows_the_rows_that_need_attention_until_asked_for_all():
    """Collapsed by default on a phone, remembered for the session; desktop always shows
    every row (the hiding is behind `max-md:`). MUTATION: hide rows on desktop too, drop the
    session memory, or show the toggle on desktop."""
    src = _src(_OVERVIEW)
    assert "useState(false)" in src.split("const [showAll, setShowAll] = ")[1][:20]
    assert "!warn && !showAll && 'max-md:hidden'" in src
    assert "sessionStorage.getItem(SHOW_ALL_KEY)" in src and "sessionStorage.setItem(SHOW_ALL_KEY" in src
    toggle = src[src.index("onClick={toggleShowAll}"):]
    toggle = toggle[:toggle.index(">")]
    assert "hidden" in toggle.split() or " hidden " in toggle
    assert "max-md:inline-block" in toggle and "aria-expanded={showAll}" in toggle


def test_the_skeleton_keeps_the_card_height():
    """As many placeholder lines as there are jobs, so nothing below jumps when the rows
    arrive; the loading text is a status for screen readers. MUTATION: six lines again, or
    the label back on a role-less div."""
    src = _src(_OVERVIEW)
    assert int(re.search(r"const SKELETON_ROWS = (\d+);", src).group(1)) == len(model_roles.ROLES)
    assert 'role="status"' in src and "<span className=\"sr-only\">{t('rolesLoading')}</span>" in src
    assert "aria-labelledby={headingId}" in src


def test_amber_is_the_only_coloured_chip():
    """An own choice is not an alarm: the chips are neutral grays, and amber marks only a
    job that is not available. MUTATION: put the black `set` chip back."""
    src = _src(_OVERVIEW)
    chip = src[src.index("'shrink-0 whitespace-nowrap rounded px-1.5"):]
    chip = chip[:chip.index(")}>")]
    families = set(re.findall(r"(?:bg|border|text)-([a-z]+)-\d{2,3}", chip))
    assert families <= {"gray", "amber"}, families
    assert "bg-gray-900" not in chip and "text-white" not in chip


def test_a_failed_request_shows_no_stale_rows():
    """The previous answer stays in state for its `keys`, but its rows are hidden from the
    card and from the vision hint while the failure stands. MUTATION: pass `modelRoles.rows`
    again."""
    src = _src(_MODAL)
    assert "const modelRoleRows = modelRolesFailed ? null : (modelRoles?.rows ?? null);" in src
    assert "rows={modelRoleRows}" in src
    assert "const visionRole = modelRoleRows?.find(r => r.role === 'vision');" in src


def test_the_tab_strip_follows_a_jump():
    """On a phone the tabs are a horizontal strip; a jump to another tab brings that tab into
    it. MUTATION: drop the strip scroll or the tab attribute."""
    src = _src(_MODAL)
    assert "data-settings-tab={cat.id}" in src
    effect = src[src.index("const settingFlash = useRef"):]
    effect = effect[:effect.index("}, [activeTab, pendingJump]);")]
    assert "document.querySelector(`[data-settings-tab=\"${tab}\"]`)?.scrollIntoView(" in effect


def test_a_bare_control_lands_with_room_above_it():
    """Anchors on a plain wrapper (no section id) get more scroll margin than a section.
    MUTATION: drop the rule, or give a plain wrapper an id."""
    css = _src(_CSS)
    assert "[data-setting-role]:not([id]) { scroll-margin-top: 1.5rem; }" in css
    src = _src(_MODAL)
    for m in re.finditer(r'<div([^>]*)data-setting-role="(\w+)"', src):
        assert "id=" not in m.group(1), m.group(2)


def test_the_vision_description_steps_aside_for_the_amber_line():
    """MUTATION: show the gray description unconditionally again."""
    src = _src(_MODAL)
    assert re.search(r"\{!\(!selectedVisionProvider && visionRole\?\.reason === 'main_cannot_see'\) && \(\s*"
                     r"<p className=\"text-xs text-gray-400 mb-3\">\{tAi\('visionProviderDesc'\)\}", src)


@pytest.mark.parametrize("locale", _LOCALES)
def test_the_chips_use_the_main_settings_own_word(locale):
    """The chip for the main provider names the thing the main select is called, in every
    language. MUTATION: en chip back to "Main provider"."""
    ai = _catalogue(locale)
    assert ai["roleSource"]["main"] in ai["primaryProvider"]


@pytest.mark.parametrize("locale", ("en", "de"))
def test_models_read_images_rather_than_see(locale):
    """The source sentences say "read images", as every other vision text does; the
    translations were derived from them. MUTATION: restore "No model here can see"."""
    reasons = _catalogue(locale)["roleReason"]
    word = {"en": r"\bsee\b", "de": r"\bsieht\b|\bsehen\b"}[locale]
    for code in ("browser_described", "browser_dom_only"):
        assert not re.search(word, reasons[code]), reasons[code]


def test_following_the_primary_provider_needs_no_visible_mark():
    """A half-width line has room for the job, the provider and ONE short chip; following
    the primary provider is the default, so it carries no chip (the intro says unmarked jobs
    follow it) and stays in the text for screen readers. The model gives way, the provider
    name does not. MUTATION: render the follows-main chip again, or let the provider shrink."""
    src = _src(_OVERVIEW)
    assert re.search(r"\{row\.source === 'follows_main' \? \(\s*<span className=\"sr-only\">\{t\(`roleSource\.\$\{row\.source\}`\)\}</span>", src)
    assert "{name && <span className=\"max-w-full shrink-0 truncate\">{name}</span>}" in src
    assert "<span className=\"min-w-0 truncate font-mono text-[11px] text-gray-500\">{model}</span>" in src
