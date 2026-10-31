# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The engine choice in the web UI, read from the sources (the pattern of
tests/test_model_roles_web_static.py): Settings > AI & Model, the other local pickers under
vLLM, the first-run wizard, Update and Repair, the banners, and every code of the core in all
seven catalogues.

MUTATIONS, each turns the named test red:
- render `{children}` outside the llama branch -> test_gguf_field_only_under_llama;
- drop `aria-describedby` from EngineChoice -> test_disabled_option_shows_reason;
- POST `install: true` without the ConfirmDialog -> test_install_through_confirm_dialog;
- drop the `downloadModelStatus` read from the progress -> test_progress_reads_job_and_download;
- restore the GGUF options in the Thinker's local branch -> test_other_local_pickers_under_vllm;
- drop the Lean branch (`disabled: lean`) -> test_lean_disables_both;
- revert localProvider to "Local (Llama)" -> test_no_bare_llama_label;
- POST the switch before the bootstrap -> test_wizard_engine_choice;
- add the engine node to `issues` -> test_engine_node_not_an_issue;
- show "Please do nothing and wait." for the engine switch -> test_banner_vllm_kind;
- keep `data.models || []` -> test_done_frame_without_models_keeps_list;
- delete one phase key from th.json -> test_every_code_in_all_catalogues;
- keep the "pickers" boundary paragraph -> test_pickers_boundary_gone.
"""
import json
import re
from pathlib import Path

import pytest

from vaf.core import local_engine as le
from vaf.core import vllm_server as vs

_ROOT = Path(__file__).resolve().parent.parent
_WEB = _ROOT / "web"
_MODAL = _WEB / "components" / "SettingsModal.tsx"
_SECTION = _WEB / "components" / "settings" / "LocalEngineSection.tsx"
_CHOICE = _WEB / "components" / "settings" / "EngineChoice.tsx"
_REPAIR = _WEB / "components" / "settings" / "UpdateRepairModal.tsx"
_PAGE = _WEB / "app" / "page.tsx"
_LOGIN = _WEB / "app" / "login" / "page.tsx"
_MESSAGES = _WEB / "messages"
_LOCALES = ("de", "en", "ja", "ko", "th", "tr", "zh")


def _src(path: Path) -> str:
    # Text mode: the needles below hold newlines, and a CRLF checkout must read as LF.
    return path.read_text(encoding="utf-8")


def _between(src: str, start: str, end: str) -> str:
    a = src.index(start)
    return src[a:src.index(end, a)]


def _local_model_section(src: str) -> str:
    return _between(src, "<Section title={tAi('localModelSettings')}>", "</Section>")


def test_engine_choice_above_model_field():
    """The "Local model" section starts with the engine; the GGUF picker and its download box
    are the section's children, below the choice."""
    section = _local_model_section(_src(_MODAL))
    engine = section.index("<LocalEngineSection")
    inner = _between(section, "<LocalEngineSection", "</LocalEngineSection>")
    assert section.index("{localModelField}") > engine
    assert "{localModelField}" in inner and "tAi('downloadModelFromHf')" in inner
    body = _src(_SECTION)
    render = body[body.index("return (\n        <div className=\"mb-5\" data-local-engine"):]
    assert render.index("<EngineChoice") < render.index("{children}")
    assert "t('appliesNow')" in render
    assert "import { Input, Select } from './settings/SettingsFields';" in _src(_MODAL)
    assert "const Select = (" not in _src(_MODAL) and "const Input = (" not in _src(_MODAL)


def test_gguf_field_only_under_llama():
    body = _src(_SECTION)
    assert body.count("{children}") == 1
    block = body[body.index("{!lean && shown === 'llama' && data.engine === 'llama' && !progress && ("):]
    block = block[:block.index("\n            )}\n")]
    assert "{children}" in block, "the GGUF picker renders outside the llama branch"
    picker = body[body.index("{showVllmPicker && ("):]
    assert "label={t('model')}" in picker[:400]
    assert "const showVllmPicker = !lean && (shown === 'vllm');" in body


def test_disabled_option_shows_reason():
    choice = _src(_CHOICE)
    assert 'role="radiogroup"' in choice and 'role="radio"' in choice
    assert "aria-checked={checked}" in choice and "aria-disabled={opt.disabled || undefined}" in choice
    assert "aria-describedby={noteId}" in choice
    note = _between(choice, "{opt.note && (", ")}")
    assert "opacity-50" not in note, "the reason of a disabled option is dimmed with it"
    assert "max-md:grid-cols-1" in choice
    body = _src(_SECTION)
    assert "disabled: lean || !supported," in body and "note: vllmNote," in body


def test_install_through_confirm_dialog():
    body = _src(_SECTION)
    assert body.count("install: true") == 1
    dialog = _between(body, "open={confirming === 'install'}", "onCancel={() => setConfirming(null)}")
    assert "install: true" in dialog, "the install starts without the confirmation"
    assert "{sizeLine}" in dialog
    assert "if (needsInstall) { setConfirming('install'); return; }" in body


def test_progress_reads_job_and_download():
    body = _src(_SECTION)
    progress = _between(body, "{progress && (", "{note && (")
    assert "progress.step / progress.steps" in progress
    assert "coded('phase', progress.phase" in progress
    assert "downloadModelStatus?.bytes_done" in progress
    assert "progress?.phase === 'download_model' && downloadModelStatus?.status === 'downloading'" in body
    assert "downloadModelStatus={downloadModelStatus}" in _src(_MODAL)


def test_other_local_pickers_under_vllm():
    """Thinker, Tool / Workflow, voice call, failover and the overview's chat row show the
    read-only vLLM line under vLLM, never the GGUF list."""
    src = _src(_MODAL)
    thinker = _between(src, "const thinkingModelSelect = (() => {", "const voiceProviderSelect")
    guard = thinker.index("if (isLocal && vllmEngine) return vllmModelLine(tAdvanced('thinkerModel'));")
    assert guard < thinker.index("(availableModels ?? []).map"), "the Thinker lists GGUFs under vLLM"
    voice = _between(src, "const voiceModelField = (() => {", "// API model")
    assert voice.index("if (vChoice === 'local' && vllmEngine) return vllmModelLine(") < voice.index("...(availableModels")
    assert "tAi('roleReason.vllm_voice_api_main')" in voice
    tool = _between(src, "const spLabel = p ? p.label", "toolModelIgnored")
    assert "if (isLocal && vllmEngine) {" in tool and "vllmModelLine(tAdvanced('toolWorkflowModel'))" in tool
    assert "{showLocal && vllmEngine && (" in src and "{showLocal && !vllmEngine && (" in src
    assert "vllmModelLine(tAdvanced('failoverLocalModel'))" in src
    chat = _between(src, "case 'chat':", "case 'vision':")
    assert "(vllmEngine ? vllmModelLine(tAi('localModelFile')) : localModelField)" in chat
    assert "tLocalEngine('runsOnVllm', { model: vllmModelName })" in src


def test_lean_disables_both():
    body = _src(_SECTION)
    assert "{ id: 'llama', name: t('llamaName'), line: t('llamaLine'), disabled: lean }," in body
    assert "const vllmNote = lean ? t('profileApi')" in body
    section = _local_model_section(_src(_MODAL))
    src = _src(_MODAL)
    head = src[src.index(section) - 120:src.index(section)]
    assert "!leanProfile &&" not in head, "the section is hidden under Lean again"
    assert "{!leanProfile && (<>" in section


def _flat(obj, prefix=""):
    for key, value in obj.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            yield from _flat(value, path + ".")
        else:
            yield path, value


def test_no_bare_llama_label():
    """Llama is Meta's model family; the engine is llama.cpp and the provider is the local
    machine."""
    for locale in _LOCALES:
        cat = json.loads((_MESSAGES / f"{locale}.json").read_bytes())
        shown = {"settings.ai.localProvider": cat["settings"]["ai"]["localProvider"],
                 "settings.voice.agentLlmLocal": cat["settings"]["voice"]["agentLlmLocal"]}
        shown.update({f"localEngine.{k}": v for k, v in _flat(cat["localEngine"])})
        for path, value in shown.items():
            assert not re.search(r"\bLlama\b(?!\.cpp)", value), (locale, path, value)
        assert "localLlama" not in cat["settings"]["ai"]
    en = json.loads((_MESSAGES / "en.json").read_bytes())
    de = json.loads((_MESSAGES / "de.json").read_bytes())
    assert en["settings"]["ai"]["localProvider"] == "Local (this machine)"
    assert de["settings"]["ai"]["localProvider"] == "Lokal (dieser Rechner)"
    assert en["localEngine"]["llamaLine"] == "GGUF models, runs on almost any hardware, starts in seconds"
    assert de["localEngine"]["llamaLine"] == "GGUF-Modelle, läuft auf fast jeder Hardware, startet in Sekunden"
    assert "tAi('localProvider')" in _src(_MODAL)


def test_wizard_engine_choice():
    src = _src(_LOGIN)
    assert "import EngineChoice, { type EngineId } from '@/components/settings/EngineChoice';" in src
    assert "useState<EngineId>('llama')" in src, "llama.cpp is not preselected"
    block = _between(src, "{setupProfile !== 'api' && setupVllm && (", "{engineChoice === 'vllm' && (")
    assert "disabled: setupVllm.level !== 'supported'," in block
    post = src.index("`${getApiBase()}/api/system/local-engine`")
    assert src.index("`${getApiBase()}/api/auth/bootstrap`") < post, "the switch is sent before the account exists"
    call = src[post:post + 300]
    assert "JSON.stringify({ engine: 'vllm', install: true })" in call
    assert "engineChoice === 'vllm' && setupProfile !== 'api' && setupVllm?.level === 'supported'" in src
    assert "tEngine('wizardAfterSetup')" in src


def test_engine_node_not_an_issue():
    src = _src(_REPAIR)
    assert "const issues = useMemo(() => rows.filter(" in src
    issues = _between(src, "const issues = useMemo(", ";\n")
    assert "localEngine" not in issues and "local_engine" not in issues
    assert "setRepairTargets(issuesRef.current.map((s) => s.service_key));" in src
    assert "id: 'local-engine'," in src and "tEngine('node')" in src
    assert "local_engine?: LocalEngineRow | null;" in src
    assert "...poolNodes, ...engineNodes]" in src


def test_banner_vllm_kind():
    src = _src(_PAGE)
    banner = _between(src, "{maintenanceStatus && (", "{/* Token Stats")
    assert "maintenanceStatus.kind === 'vllm_engine'" in banner
    assert "tMain('maintenanceVllmTitle'" in banner and "tMain('maintenanceVllmFailed')" in banner
    wait = banner[banner.index("tMain('maintenanceWait')") - 400:banner.index("tMain('maintenanceWait')")]
    assert "maintenanceStatus.kind !== 'vllm_engine'" in wait


def test_done_frame_without_models_keeps_list():
    src = _src(_PAGE)
    handler = _between(src, "else if (data.type === 'model_download_done') {", "else if (data.type === 'maintenance_progress')")
    assert "if (Array.isArray(data.models)) setAvailableModels(data.models);" in handler
    assert "data.models || []" not in handler


_CODES = {
    "support": le.SUPPORT_CODES,
    "result": le.SWITCH_CODES,
    "phase": le.PHASES,
    "fit": vs.FIT_CODES,
    "conflict": le.CONFLICT_CODES,
    "status": le.STATUS_CODES + ("unknown",),
    "server": ("running", "loading", "idle"),
}


@pytest.mark.parametrize("locale", _LOCALES)
def test_every_code_in_all_catalogues(locale):
    """Every code the core gives has its sentence in every language (the codes are read
    from the Python tables, not from a copy)."""
    cat = json.loads((_MESSAGES / f"{locale}.json").read_bytes())["localEngine"]
    missing = [f"{ns}.{code}" for ns, codes in _CODES.items() for code in codes
               if not str((cat.get(ns) or {}).get(code) or "").strip()]
    assert not missing, (locale, missing)
    main = json.loads((_MESSAGES / f"{locale}.json").read_bytes())["main"]
    assert "{phase}" in main["maintenanceVllmTitle"] and main["maintenanceVllmFailed"].strip()


def test_pickers_boundary_gone():
    doc = _src(_ROOT / "docs" / "llm" / "VLLM_ENGINE.md")
    assert "three model pickers do not know the engine" not in doc
    assert "**The pickers:**" not in doc


def test_review_round_layout_fixes():
    """The progress bar moves by transform only (DESIGN.md motion rule); the banner's phase
    wraps on a phone; the two cards share one height side by side; the download row stacks on a phone;
    the wizard's engine choice sits above the button that takes the local path."""
    body = _src(_SECTION)
    assert "transition-[width]" not in body and "scaleX(" in body and "origin-left" in body
    page = _src(_PAGE)
    banner = _between(page, "{maintenanceStatus && (", "{/* Token Stats")
    assert 'className="truncate max-md:whitespace-normal"' in banner
    assert "items-stretch max-md:grid-cols-1" in _src(_CHOICE)
    modal = _src(_MODAL)
    row = modal[modal.index("tAi('downloadModelFromHf')") - 500:modal.index("tAi('downloadModelFromHf')")]
    assert "max-md:flex-col max-md:items-stretch" in row
    login = _src(_LOGIN)
    assert login.index("data-wizard-engine") < login.index("t('veylloSkipApi') : t('veylloSkip')")


def test_sizes_read_in_gb():
    """The confirm says GB, so the fit and the download bytes do too."""
    body = _src(_SECTION)
    assert "need: gb(fit.need_mb), budget: gb(fit.budget_mb)" in body
    assert "GB / ${num(downloadModelStatus.bytes_total / 1024 ** 3, 2)} GB" in body
    assert "t('stoppedVllm')" in body


@pytest.mark.parametrize("locale", _LOCALES)
def test_round_one_wording(locale):
    cat = json.loads((_MESSAGES / f"{locale}.json").read_bytes())
    le_cat = cat["localEngine"]
    assert "vaf models config vllm_tool_call_parser" in le_cat["result"]["parser_unknown"]
    assert le_cat["stoppedVllm"].strip()
    for path, value in _flat(le_cat):
        assert "<" not in value and ">" not in value, (locale, path, "tags in a message")
    for key in ("too_big", "share_above_free", "fits"):
        assert "MiB" not in le_cat["fit"][key] and "GB" in le_cat["fit"][key], (locale, key)
    assert "\u2013" not in cat["main"]["maintenanceFailed"]
    if locale == "de":
        assert le_cat["modelOtherPlaceholder"] == "owner/name"
        assert "ließ sich nicht anhalten" in le_cat["result"]["llama_stop_failed"]


def test_automatic_option_fits_a_phone():
    """A native option cannot wrap: the automatic one names the model without owner or size
    (the curated options below carry both)."""
    body = _src(_SECTION)
    assert "t('modelAutoNoSize', { repo: data.vllm.auto_model.split('/').pop()" in body
    assert "t('modelAuto'," not in body


@pytest.mark.parametrize("locale", _LOCALES)
def test_cuda_facts_and_size_follow_the_core(locale):
    """The wizard's size is the core's DOWNLOAD_GB_AT_LEAST (with the decimal comma in de and
    tr), and no sentence names the CUDA version of the earlier recipe. MUTATION: leave 1.8 in
    one catalogue - red."""
    cat = json.loads((_MESSAGES / f"{locale}.json").read_bytes())["localEngine"]
    size = f"{vs.DOWNLOAD_GB_AT_LEAST:.1f}"
    if locale in ("de", "tr"):
        size = size.replace(".", ",")
    assert f"{size} GB" in cat["wizardAfterSetup"], (locale, cat["wizardAfterSetup"])
    for path, value in _flat(cat):
        assert "12.9" not in value and "cu129" not in value, (locale, path)
    for key in ("cuda_probe_failed",):
        assert vs.CUDA_VERSION in cat["result"][key] and "580" in cat["result"][key], locale
    assert vs.CUDA_VERSION in cat["support"]["nvidia_driver_old"] and "580" in cat["support"]["nvidia_driver_old"]
    # The probe verdicts name what to do, in every language: the GPU must not be hidden.
    assert "CUDA_VISIBLE_DEVICES" in cat["result"]["cuda_probe_failed"], locale
    floors = [f"{v[0]}.{v[1]}" for v in le.MIN_GLIBC.values()]
    assert all(f in cat["support"]["glibc_old"] for f in floors), (locale, floors)
    assert "{" not in cat["support"]["glibc_old"]
    assert "{version}" in cat["replaceNote"] and "{version}" in cat["status"]["outdated"]
    assert "{" not in cat["conflict"]["outdated"]
    assert "{" not in cat["support"]["nvidia_driver_old"], "the wizard renders support codes without params"


def test_libs_failure_gets_the_log_hint():
    body = _src(_SECTION)
    assert "r.code === 'vllm_libs_failed') lines.push(t('logHint'" in body


def test_an_outdated_environment_is_shown_as_such():
    """An environment a start refuses is never shown as installed and ready: the server line
    needs a current one, and the install says it replaces the old one. MUTATION: show the
    server line for every installed version - red."""
    body = _src(_SECTION)
    assert "{data.vllm.installed_version && !needsInstall && (" in body
    assert "const replaceNote = needsInstall && data.vllm.installed_version" in body
    assert body.count("{replaceNote && ") == 2, "the panel and the confirmation both say it"
    en = json.loads((_MESSAGES / "en.json").read_bytes())["localEngine"]
    assert "not installed yet" not in en["result"]["needs_install"]
