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
    # Text mode: the needles below hold newlines, and a CRLF checkout must read as LF.
    return path.read_text(encoding="utf-8")


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
    """It renders the rows it is given and decides nothing. The only job names in it are the
    area table; no provider name at all. MUTATION: add a provider list or a role-name branch
    to the component."""
    src = _src(_OVERVIEW)
    groups = src[src.index("export const ROLE_GROUPS"):]
    groups = groups[:groups.index("];") + 2]
    rest = src.replace(groups, "")
    assert ".sort(" not in src
    for provider in list(config_mod.Config.PROVIDER_MODELS) + ["local", "elevenlabs"]:
        assert f"'{provider}'" not in src and f'"{provider}"' not in src, provider
    for role in model_roles.ROLES:
        assert f"'{role.name}'" not in rest and f'"{role.name}"' not in rest, role.name


def _catalogue(locale: str) -> dict:
    return json.loads((_MESSAGES / f"{locale}.json").read_bytes())["settings"]["ai"]


@pytest.mark.parametrize("locale", _LOCALES)
def test_every_role_area_and_reason_has_a_text(locale):
    """Every job, area and reason code has its text in every catalogue, and no stale one is
    left. MUTATION: delete one reason from tr.json, add a code to REASON_TEXT, or drop an
    area's title."""
    ai = _catalogue(locale)
    assert set(ai["role"]) == {r.name for r in model_roles.ROLES}
    assert set(ai["roleReason"]) == set(model_roles.REASON_TEXT)
    assert set(ai["rolesGroup"]) == {g for g, _ in _role_groups()}
    for key in ("rolesTitle", "rolesIntroNoMain", "rolesLoading", "rolesError", "rolesRetry",
                "rolesJumpHint", "rolesNoteMain", "rolesNoteViaVision", "rolesOff", "roleLocal",
                "rolesFollowsMain", "rolesAllOptions", "visionFollowMain", "visionMainCannot"):
        assert ai.get(key, "").strip(), key
    assert "<b>{provider}</b>" in ai["rolesIntro"]
    assert "{chain}" in ai["rolesNoteThen"] and "{save}" in ai["rolesSaveHint"]
    assert "{provider}" in ai["roleReason"]["main_cannot_see"]
    texts = [*ai["role"].values(), *ai["rolesGroup"].values(), *ai["roleReason"].values(),
             *(v for k, v in ai.items() if k.startswith("roles") and isinstance(v, str))]
    for text in texts:
        assert text.strip() and "\u2014" not in text


# The chips and the phone's "show all" toggle of the previous card, gone with them.
_STALE_KEYS = ("roleSource", "rolesShowAll", "rolesShowLess", "rolesAllFine", "localSpeechEngine")


@pytest.mark.parametrize("locale", _LOCALES)
def test_no_key_of_the_chip_card_is_left(locale):
    """MUTATION: put `roleSource` back into one catalogue."""
    ai = _catalogue(locale)
    assert not [k for k in _STALE_KEYS if k in ai]
    src = _src(_OVERVIEW) + _src(_MODAL)
    assert not [k for k in _STALE_KEYS if k in src]


@pytest.mark.parametrize("locale", ("de", "en", "tr"))
def test_every_reason_reads_as_a_sentence(locale):
    """The reason stands where the provider would, so it is a whole sentence: a capital letter
    (or a name) first, a full stop last. MUTATION: lower-case the first letter of
    the blind-browser sentence again."""
    for code, text in _catalogue(locale)["roleReason"].items():
        # A product name keeps its own spelling ("vLLM").
        assert text[0].isupper() or text.startswith(("{provider}", "vLLM")), (code, text)
        assert text.endswith("."), (code, text)


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


# ── The card by area, with rows that open in place ────────────────────────────────────


def _const_set(src: str, name: str) -> set:
    body = re.search(r"const " + name + r" = new Set\(\[(.*?)\]\)", src, re.S).group(1)
    return set(re.findall(r"'([a-z_]+)'", body))


def _role_groups() -> list:
    src = _src(_OVERVIEW)
    table = src[src.index("export const ROLE_GROUPS"):]
    table = table[:table.index("];")]
    return [(g, re.findall(r"'([a-z_]+)'", roles))
            for g, roles in re.findall(r"\['([a-z_]+)', \[(.*?)\]\]", table)]


def test_every_role_is_in_exactly_one_area():
    """Every job the resolver can return is listed in exactly one area, and a job no area
    names still shows (in the last one). MUTATION: drop `ocr` from its area, list `chat`
    twice, or drop the fallback for unknown jobs."""
    listed = [role for _, roles in _role_groups() for role in roles]
    assert sorted(listed) == sorted(r.name for r in model_roles.ROLES), listed
    src = _src(_OVERVIEW)
    assert "i === ROLE_GROUPS.length - 1 ? rows.filter(r => !known.has(r.role)) : []" in src


def test_warning_reasons_are_real_codes_and_include_a_blind_browser():
    """The warning set decides which rows say their reason in place of the provider. A code
    the resolver does not send is a silent typo. A browser agent that works blind is a lost
    capability, not a note, and an ignored Thinker model is not what the person chose.
    MUTATION: drop `browser_dom_only` or `thinking_model_ignored` from the set, or misspell
    a code."""
    codes = _const_set(_src(_OVERVIEW), "WARNING_REASONS")
    assert codes <= set(model_roles.REASON_TEXT), codes - set(model_roles.REASON_TEXT)
    assert {"browser_dom_only", "no_api_key", "thinking_model_ignored"} <= codes


def _function(src: str, head: str) -> str:
    body = src[src.index(head):]
    return body[:body.index("\n    };\n")]


def test_every_source_has_one_way_to_show():
    """The value cell answers every source the resolver sends: a problem (unavailable, or a
    warning reason) says why in amber, `off` says off, a job that follows the main provider
    shows the provider muted, every other source shows provider and model. If the resolver
    grows a source, this list has to be looked at again. MUTATION: add a source to
    `model_roles.SOURCES`, or drop the `off` branch."""
    assert set(model_roles.SOURCES) == {"main", "follows_main", "set", "unavailable", "off",
                                        "local_engine", "via_vision"}
    value = _function(_src(_OVERVIEW), "const value = (row: ModelRoleRow, reasons: string[]) => {")
    order = [value.index(s) for s in ("if (isWarning(row))", "row.source === 'off'",
                                      "row.source === 'follows_main'", "const shown = modelLabel(row);")]
    assert order == sorted(order)
    assert "row.source === 'unavailable'" in _src(_OVERVIEW).split("const isWarning =")[1][:200]


def test_amber_marks_problems_and_nothing_else():
    """Amber is the only colour on the card and only a problem row wears it: its job name and
    its reason. MUTATION: put amber on the note, or a green or black chip on a row."""
    src = _src(_OVERVIEW)
    families = set(re.findall(r"(?:bg|border|text|ring)-([a-z]+)-\d{2,3}", src))
    assert families <= {"gray", "amber", "red"}, families
    amber = [m.start() for m in re.finditer(r"text-amber-", src)]
    assert len(amber) == 2, amber
    value = _function(src, "const value = (row: ModelRoleRow, reasons: string[]) => {")
    assert "text-amber-700" in value.split("if (row.source === 'off')")[0]
    assert "warn ? 'text-amber-700' : 'text-gray-700'" in src
    assert "text-red-600" in src.split("{error && (")[1][:200], "red is the load error only"


_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⭐✅✔✖]")


def test_no_icons_emoji_chips_dots_shadows_or_gradients():
    """A list of plain text: the caret is a text character. MUTATION: import an icon, put a
    status dot or a chip back, or add a shadow."""
    src = _src(_OVERVIEW)
    assert not re.search(r"from '(lucide-react|react-icons[^']*|@heroicons[^']*)'", src)
    assert not _EMOJI.search(src)
    for banned in ("rounded-full", "shadow", "gradient", "rounded px-1.5"):
        assert banned not in src, banned


def test_a_phone_stacks_each_row_and_keeps_every_row():
    """On a phone the job stands above its value and the note under that, the caret on the
    right; nothing is hidden behind a "show all" any more. The stacking is behind `max-md:`,
    so the desktop grid is untouched. MUTATION: hide rows on a phone again, or drop the row
    placement."""
    src = _src(_OVERVIEW)
    assert "sessionStorage" not in src and "showAll" not in src
    row = src[src.index('className="vaf-role-row'):]
    row = row[:row.index('"\n')]
    assert "grid-cols-[9.5rem_minmax(0,1fr)_auto_0.75rem]" in row
    assert "max-md:grid-cols-[minmax(0,1fr)_0.75rem]" in row
    for cell in ("max-md:col-start-1 max-md:row-start-1", "max-md:col-start-1 max-md:row-start-2",
                 "max-md:col-start-1 max-md:row-start-3",
                 "max-md:col-start-2 max-md:row-start-1 max-md:row-span-2"):
        assert cell in src, cell
    assert "max-md:hidden" not in src.replace("!noteText && 'max-md:hidden'", "")


def test_the_skeleton_keeps_the_card_height():
    """One placeholder line per job in each area (ROLE_GROUPS, which names every job the
    resolver has, see the test above), under the real area headings, so nothing below jumps
    when the rows arrive; the shared placeholder carries the status for screen readers.
    MUTATION: six lines again (`count={6}`), or a placeholder of the card's own."""
    src = _src(_OVERVIEW)
    block = src[src.index("{loading && !rows && ("):]
    block = block[:block.index("</Skeleton>")]
    assert "<Skeleton label={t('rolesLoading')}>" in block
    assert "{ROLE_GROUPS.map(([id, roles], g) => (" in block
    assert "{g === 0 && (\n" in block and "<SkeletonRows count={1} " in block
    assert "<SkeletonRows count={g === 0 ? roles.length - 1 : roles.length}" in block
    assert len(re.findall(r"<SkeletonRows count=", block)) == 2
    assert "role=\"status\"" not in src and "skelPulse" not in src


def test_a_retry_shows_no_stale_rows_before_the_new_answer():
    """A retry clears the error at once; the rows of the failed configuration stay hidden (the
    card shows loading) until a fresh answer arrives. MUTATION: derive the rows from the failure
    flag alone, or clear the stale mark on retry."""
    src = _src(_MODAL)
    assert "const modelRoleRows = (modelRolesFailed || modelRolesStale) ? null : (modelRoles?.rows ?? null);" in src
    assert "loading={(!modelRoles || modelRolesStale) && !modelRolesFailed}" in src
    retry = src[src.index("onRetry={() => {"):]
    retry = retry[:retry.index("}}")]
    assert "setModelRolesStale(false)" not in retry
    assert src.count("setModelRolesStale(false)") == 1 and src.count("setModelRolesStale(true)") == 2


def test_a_failed_request_shows_no_stale_rows():
    """The previous answer stays in state for its `keys`, but its rows are hidden from the
    card and from the vision hint while the failure stands. MUTATION: pass `modelRoles.rows`
    again."""
    src = _src(_MODAL)
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
def test_the_main_note_uses_the_main_settings_own_word(locale):
    """The note on the chat row names the thing the main select is called, in every language.
    MUTATION: en note back to "Main provider"."""
    ai = _catalogue(locale)
    assert ai["rolesNoteMain"] in ai["primaryProvider"]


@pytest.mark.parametrize("locale", ("en", "de"))
def test_models_read_images_rather_than_see(locale):
    """The source sentences say "read images", as every other vision text does; the
    translations were derived from them. MUTATION: restore "No model here can see"."""
    reasons = _catalogue(locale)["roleReason"]
    word = {"en": r"\bsee\b", "de": r"\bsieht\b|\bsehen\b"}[locale]
    for code in ("browser_described", "browser_dom_only"):
        assert not re.search(word, reasons[code]), reasons[code]


# The label column is 9.5rem (152 px). Measured at 13 px in the app's font, the longest
# de/en/tr label is 108 px ("Görselleri anlama", 17 characters). A label beyond that is
# looked at again before it ships.
_LABEL_MAX_CHARS = 17


@pytest.mark.parametrize("locale", ("de", "en", "tr"))
def test_every_job_label_fits_the_label_column(locale):
    """MUTATION: put "Thinker (Hintergrund)" back as the German label."""
    src = _src(_OVERVIEW)
    assert "grid-cols-[9.5rem_" in src
    for role, label in _catalogue(locale)["role"].items():
        assert len(label) <= _LABEL_MAX_CHARS, (role, label)


def test_a_local_provider_and_an_engine_use_the_short_local_word():
    """The local model and an engine on this machine are both "Local"; the engine's name
    follows as the model, never the config value (`docker`). MUTATION: name the engine row
    by its config value again."""
    src = _src(_MODAL)
    assert "if (provider === 'local') return tAi('roleLocal');" in src
    assert "if (!provider) return row.source === 'local_engine' ? tAi('roleLocal') : '';" in src
    label = src[src.index("modelLabel={(row) => {"):]
    label = label[:label.index("}}")]
    assert "if (row.role === 'ocr') return 'Tesseract';" in label
    assert "return engine === 'local' ? 'faster-whisper' : 'Whisper';" in label


# ── The open row: the section's own controls, not a copy ────────────────────────────────

# What each job's open row offers: the elements the section below renders. The failover row
# names its picker and selects; a speech job offers provider and model.
_CHOICES = {
    "chat": ["chatProviderSelect", "chatApiModelField", "localModelField"],
    "vision": ["visionProviderSelect", "visionModelField"],
    "ocr": ["ocrEngineSelect"],
    "browser_agent": ["browserProviderSelect", "browserModelField"],
    "subagents": ["subagentProviderSelect"],
    "thinking": ["thinkingProviderSelect", "thinkingModelSelect"],
    "voice_call": ["voiceProviderSelect", "voiceModelField"],
    "speech_to_text": ["sttProviderSelect", "sttModelSelect"],
    "text_to_speech": ["ttsProviderSelect", "ttsModelSelect"],
    "failover": ["failoverLevelPicker", "failoverBackupSelect", "failoverBackupModelSelect"],
}


def _role_choice(src: str) -> str:
    return _function(src, "const roleChoice = (row: ModelRoleRow): React.ReactNode => {")


def test_every_job_has_a_choice_or_a_jump():
    """The open row's switch names every job the resolver knows; one it does not know only
    jumps. MUTATION: drop the vision case."""
    choice = _role_choice(_src(_MODAL))
    cases = re.findall(r"case '([a-z_]+)':", choice)
    assert sorted(cases) == sorted(r.name for r in model_roles.ROLES), cases
    assert "default:\n                return null;" in choice


@pytest.mark.parametrize("role", sorted(_CHOICES))
def test_the_open_row_renders_the_sections_own_elements(role):
    """Each element the open row shows is defined once and rendered by its section too, inside
    the content of the tab that holds the job: the overview and the section show one value
    from one draft and save with the one Save. MUTATION: give the open row its own copy of a
    select, or stop rendering an element in its section."""
    src = _src(_MODAL)
    choice = _role_choice(src)
    case = choice[choice.index(f"case '{role}':"):]
    case = case[:case.index("case '", 6) if "case '" in case[6:] else case.index("default:")]
    tab = next(r.tab for r in model_roles.ROLES if r.name == role)
    regions = _tab_regions(src).get(tab, [])
    for name in _CHOICES[role]:
        assert re.search(r"\b" + name + r"\b", case), (role, name)
        assert len(re.findall(r"const " + name + r" = ", src)) == 1, name
        uses = [m.start() for m in re.finditer(r"\{" + name + r"[ }]", src)
                if not choice.count(src[m.start():m.start() + 80])]
        in_section = [u for u in uses if any(a <= u < b for a, b in regions)]
        assert in_section, f"{name} is not rendered on the {tab} tab"


@pytest.mark.parametrize("key", ["provider", "vision_provider", "browser_agent_provider", "ocr_engine",
                                 "subagent_provider", "thinking_provider", "voice_agent_provider",
                                 "speech_stt_provider", "speech_tts_provider", "failover_level",
                                 "failover_backup_provider"])
def test_each_choice_is_written_in_one_place(key):
    """One handler per setting: a second `handleChange('<key>'` would be a copy of the
    control. MUTATION: write a hand-made vision select into the open row."""
    src = _src(_MODAL)
    writes = re.findall(r"handleChange\('" + key + r"', ", src)
    allowed = {"speech_stt_provider": 3, "speech_tts_provider": 2}.get(key, 1)
    assert len(writes) == allowed, (key, len(writes))


def test_one_row_opens_in_place_and_escape_closes_it():
    """One open row at a time; a second click or Escape closes it (Escape not the window
    around it), and focus goes back to the row. The row is a button that names its panel; a
    row without a choice jumps instead. The panel stays mounted while closed, inert, so the
    toggle is a transition. MUTATION: render the panel only while open, drop aria-controls,
    or let Escape reach the settings window."""
    src = _src(_OVERVIEW)
    assert "const [open, setOpen] = useState<string | null>(null);" in src
    assert "onClick={() => (expandable ? setOpen(isOpen ? null : row.role) : onPick(row))}" in src
    assert "aria-expanded={expandable ? isOpen : undefined}" in src
    assert "aria-controls={expandable ? panelId : undefined}" in src
    esc = src[src.index("const closeOpen = (e: KeyboardEvent) => {"):]
    esc = esc[:esc.index("\n    };")]
    assert "e.key !== 'Escape' || !open" in esc and "e.stopPropagation();" in esc
    assert "setOpen(null);" in esc and ".focus()" in esc
    assert "onKeyDown={closeOpen}" in src
    assert "{expandable && (" in src and "{isOpen && (" not in src
    assert "data-open={isOpen ? 'true' : 'false'}" in src
    assert "inert: isOpen ? undefined : true" in src
    assert "onClick={() => onPick(row)}" in src and "t('rolesAllOptions')" in src
    assert "t('rolesSaveHint', { save: tCommon('saveChanges') })" in src


def test_the_overview_gets_the_choices_and_the_jump():
    """MUTATION: drop `renderChoice` from the overview, or let it jump without the highlight."""
    src = _src(_MODAL)
    assert "renderChoice={roleChoice}" in src
    assert "onPick={(row) => jumpToSetting(row.tab, `[data-setting-role=\"${row.role}\"]`, true)}" in src


def test_an_off_speech_job_jumps_to_its_switch():
    """The speech switch sits with the device and engine settings; the open row would only
    change a provider that does not run. MUTATION: offer the provider select for an off row."""
    choice = _role_choice(_src(_MODAL))
    for role in ("speech_to_text", "text_to_speech"):
        case = choice[choice.index(f"case '{role}':"):]
        assert case.split("\n")[1].strip().startswith("return row.source === 'off' ? null :"), role


# ── The motion of an opening row ────────────────────────────────────────────────────────

_MOVING = {"grid-template-rows", "opacity", "transform"}


def _rules(css: str, prefix: str) -> list:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [(m.group(1).strip(), m.group(2)) for m in
            re.finditer(r"([^{}]*" + re.escape(prefix) + r"[^{}]*)\{([^{}]*)\}", css)]


def _transitioned(body: str) -> set:
    props = set()
    for decl in re.findall(r"transition\s*:\s*([^;]+);", body):
        for part in re.split(r",(?![^(]*\))", decl):
            word = part.strip().split()[0]
            if word != "none":
                props.add(word)
    return props


def test_an_opening_row_moves_only_rows_opacity_and_transform():
    """Short-lived, compositor-friendly motion: the panel's height by grid rows, its opacity,
    a small shift of its content, the caret's turn and the tint's opacity. Nothing else
    transitions or animates, and no rule loops. 320 ms to open and 220 ms to close, on the
    settling curve. MUTATION: transition background-color or box-shadow, or loop it."""
    css = _src(_CSS)
    rules = _rules(css, ".vaf-role-")
    assert rules
    curve = "cubic-bezier(0.32, 0.72, 0, 1)"
    for selector, body in rules:
        assert _transitioned(body) <= _MOVING, (selector, _transitioned(body))
        assert "animation" not in body, selector
        assert "transition-property" not in body and "will-change" not in body, selector
    base = {}
    for selector, body in rules:  # the first rule of a selector, not its reduced-motion twin
        base.setdefault(selector, body)
    assert f"transition: grid-template-rows 220ms {curve}, opacity 220ms {curve};" in base[".vaf-role-panel"]
    assert "grid-template-rows: 1fr; opacity: 1; transition-duration: 320ms;" in base['.vaf-role-panel[data-open="true"]']
    assert f"transition: transform 220ms {curve};" in base[".vaf-role-caret"]
    assert "transform: translateY(-3px);" in base[".vaf-role-panel > div > div"]


def test_the_caret_turns_in_place():
    """The caret is a drawn chevron in a fixed square turning about its centre: closed it points
    left, open down, and it does not move. A text glyph sat off centre in its line box, so the
    turn shifted it. MUTATION: put the text glyph back, or turn it the other way."""
    tsx = _src(_OVERVIEW)
    caret = tsx[tsx.index('className="vaf-role-caret'):]
    caret = caret[:caret.index("</span>")]
    assert "h-3 w-3 self-center" in caret and "<svg" in caret
    assert 'd="M7.5 2.5 4 6l3.5 3.5"' in caret  # points left
    assert "›" not in caret
    base = dict(reversed(_rules(_src(_CSS), ".vaf-role-")))
    assert "transform-origin: center;" in base[".vaf-role-caret"]
    assert "display: inline-flex;" in base[".vaf-role-caret"]
    assert "transform: rotate(-90deg);" in base['.vaf-role-row[aria-expanded="true"] .vaf-role-caret']


def test_reduced_motion_opens_a_row_without_travel():
    """With reduced motion nothing travels or turns: the shift and the caret's transition are
    off and the panel only fades. MUTATION: drop the reduced-motion rule for the panel."""
    css = _src(_CSS)
    reduced = css[css.index("@media (prefers-reduced-motion: reduce)"):]
    reduced = reduced[:reduced.index("\n}\n")]
    assert ".vaf-role-row::before, .vaf-role-caret, .vaf-role-panel > div > div { transition: none; }" in reduced
    assert '.vaf-role-panel, .vaf-role-panel[data-open="true"] { transition: opacity 120ms linear; }' in reduced
    assert ".vaf-role-panel > div > div { transform: none; }" in reduced


# ── The shared controls on a phone and in every language ────────────────────────────────


def test_the_failover_scale_keeps_four_steps_on_one_track():
    """The level picker serves the Advanced section and the open failover row. Its track runs
    from the first dot's centre to the last (12.5% to 87.5%), which only holds while the four
    steps share one row: a 2x2 grid on a phone put two of them under no track at all. On a
    phone the labels wrap instead. MUTATION: put `max-md:grid-cols-2` back."""
    src = _src(_MODAL)
    picker = src[src.index("const failoverLevelPicker = ("):]
    picker = picker[:picker.index("\n    );")]
    assert "left-[12.5%] right-[12.5%]" in picker
    grid = re.search(r'<div className="relative z-10 ([^"]*)">', picker).group(1).split()
    assert "grid-cols-4" in grid and not [c for c in grid if "grid-cols-" in c and c != "grid-cols-4"], grid
    assert "max-md:whitespace-normal max-md:text-center" in picker


def _choice_elements(src: str) -> str:
    start = src.index("The choice behind each \"who does what\" row")
    end = src.index("const roleChoice = (row: ModelRoleRow)")
    return src[start:end]


def test_no_english_words_are_built_into_the_shared_controls():
    """The controls the section and the open row share carry no hand-written English: the
    model label of the main provider, the default and the current-value suffixes come from
    the catalogues. MUTATION: put `${chatApiProvider.label} Model` or `(Default)` back, or
    `(Current)` into `Select`."""
    src = _src(_MODAL)
    block = _choice_elements(src)
    assert "label={tAi('providerModel', { provider: chatApiProvider.label })}" in block
    for english in (" Model`", "(Default)", "(Current)"):
        assert english not in block, english
    select = src[src.index("const Select = ("):]
    select = select[:select.index("\n};")]
    assert "(Current)" not in select and "tCommon('currentOption', { value })" in select
    assert "(Default)" not in src and "(Current)" not in src


@pytest.mark.parametrize("locale", _LOCALES)
def test_the_shared_controls_have_their_words_in_every_language(locale):
    """MUTATION: drop `providerModel` from ko.json, or its placeholder from de.json."""
    cat = json.loads((_MESSAGES / f"{locale}.json").read_bytes())
    assert "{provider}" in cat["settings"]["ai"]["providerModel"]
    assert "{value}" in cat["common"]["currentOption"]
    assert cat["settings"]["advanced"]["failoverProviderDefault"].strip()
