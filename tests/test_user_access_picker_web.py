# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The user manager's per-user tool picker (web/lib/accessPresets.ts, web/components/SettingsModal.tsx).

Three defects in one list. The picker joined the live registry and the coding agent's own tool
names (`GET /api/users/tool-universe`) by plain concatenation, and six names are in both (the four
git tools, `update_codex`, `add_memory`), so 'full' and "Select all" stored them twice. That never
showed, because the fetch was gated on a tab id ('users') that does not exist, so `bash`,
`run_tests`, `project_history` and `project_rollback` were never offered and no preset granted
them: every account saved through the picker got a coding agent without a shell. And after a
create the form went back to `tools: []` while the preset stayed 'standard', so the preset effect
did not run again and the next account was saved with an empty list, which means unrestricted.

Two timing holes once the fetch ran: a preset saved before the coder's names arrived (or after
the request failed) left them out without a word, and the create handler reset the form over the
lists of the render that sent the request, so names that arrived while it was pending were lost
for the next account. The request now has a state of its own, a preset saves only once it is
'ready', a failure says so with a retry, and the reset asks the preset hook to resolve again
instead of resolving itself.

The merge, the presets and both hooks are the web's REAL code, transpiled with the app's own
TypeScript and run in node (the pattern of tests/test_tool_card_i18n_web.py). The hooks run in
React's own client renderer on a minimal host: the components here render nothing, so only the
root container and the event-priority lookup touch it. The notice is the real
AccessPresetSection, cut out of SettingsModal.tsx as it stands and rendered with the real
catalogues. Skipped without node or web/node_modules; the wiring in SettingsModal is pinned
statically and always runs.

MUTATIONS, each turns the named test red:
- `mergePickerTools` returning `[...tools, ...coderOnly.map(...)]`
  -> test_merge_lists_each_tool_once_registry_entry_wins, test_presets_over_the_merged_list,
     test_no_concat_of_the_coder_list_remains;
- last entry wins (a coder row replaces the registry row)
  -> test_merge_lists_each_tool_once_registry_entry_wins;
- the string/empty filter on the endpoint's names removed
  -> test_merge_lists_each_tool_once_registry_entry_wins;
- the coder half dropped from `mergePickerTools` -> test_presets_over_the_merged_list;
- `!t.account_opt_in` dropped from the 'standard' filter -> test_presets_over_the_merged_list;
- `useAccessPreset`'s `reapply` resolving over its own closure's lists, or `epoch` dropped from
  its dependencies -> test_a_list_that_arrives_while_the_create_is_pending_reaches_the_next_form;
- `key` dropped from its dependencies -> test_the_preset_follows_the_account_being_edited;
- a 200 without a list counted as 'ready', the failure kept as 'loading', `retry` a no-op, or a
  loaded list fetched again -> test_the_tool_universe_request_has_its_own_state;
- `presetSaveBlocked` letting 'loading' or 'failed' through, or blocking 'custom'
  -> test_a_preset_saves_only_with_the_full_list;
- `disabled=` or the handler guard dropped from a save button
  -> test_both_save_buttons_wait_for_the_full_list;
- the failure notice or its retry button dropped, or the loading line shown on 'ready'
  -> test_the_picker_says_when_the_list_is_incomplete;
- `[...tools, ...coderOnlyTools.map(...)]` back in SettingsModal -> test_no_concat_of_the_coder_list_remains;
- `activeTab === 'users'` back in the universe gate -> test_universe_fetch_gate_names_a_real_tab;
- a preset hook over `tools` instead of `pickerTools` -> test_preset_hooks_follow_the_merged_list;
- `tools: [], workflows: []`, a resolution in the handler, or no `reapplyNewUserPreset()` in the
  create reset -> test_create_reset_restores_the_standard_preset;
- a local `function resolveAccessPreset` back in SettingsModal -> test_preset_rules_live_in_one_place.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
LIB = WEB / "lib" / "accessPresets.ts"
SETTINGS = WEB / "components" / "SettingsModal.tsx"
_TS = WEB / "node_modules" / "typescript"

# The names both the main registry and the coding agent register (measured against
# Agent._load_tools and CODER_ONLY_TOOL_NAMES).
_SHARED = ("add_memory", "git_add_commit", "git_init", "git_log", "git_status", "update_codex")
_CODER_OWN = ("bash", "run_tests", "project_history", "project_rollback")


def _source(path):
    # A Windows checkout (core.autocrlf) holds the sources with CRLF; the searches expect LF.
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def _strip_comments(source):
    # Only where a comment can start: a block comment after whitespace or a JSX brace, a line
    # comment after whitespace. A bare `/*` also occurs inside strings ('image/*'), and
    # stripping from there eats real code up to the next comment's end.
    source = re.sub(r"(?<=[\s{])/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?:^|(?<=\s))//[^\n]*", "", source, flags=re.M)


_PRELUDE = r"""
const path = require('path'), fs = require('fs');
const W = process.argv[1];
const req = require('module').createRequire(path.join(W, 'package.json'));
const ts = req('typescript'), React = req('react');
const h = React.createElement, act = React.act;
const opts = {compilerOptions: {module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.React,
  target: ts.ScriptTarget.ES2019, esModuleInterop: true}};
function load(src, stubs) {
  const js = ts.transpileModule(src, opts).outputText;
  const m = {exports: {}};
  new Function('require', 'module', 'exports', js)((id) => (id in stubs ? stubs[id] : req(id)), m, m.exports);
  return m.exports;
}
const lib = load(fs.readFileSync(path.join(W, 'lib', 'accessPresets.ts'), 'utf8'), {});
global.IS_REACT_ACT_ENVIRONMENT = true;
global.window = {event: undefined, HTMLIFrameElement: function () {}};
const noop = () => {};
const host = {nodeType: 1, nodeName: 'DIV', tagName: 'DIV', namespaceURI: 'http://www.w3.org/1999/xhtml',
  addEventListener: noop, removeEventListener: noop,
  ownerDocument: {nodeType: 9, activeElement: null, addEventListener: noop, removeEventListener: noop}};
const root = req('react-dom/client').createRoot(host);
const settle = () => new Promise((r) => setTimeout(r, 0));
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
"""


def _node(scenario, payload):
    if shutil.which("node") is None or not _TS.is_dir():
        pytest.skip("node or web/node_modules/typescript is not installed")
    for pkg in ("react", "react-dom"):
        if not (WEB / "node_modules" / pkg).is_dir():
            pytest.skip(f"web/node_modules/{pkg} is not installed")
    script = _PRELUDE + "(async () => {\n" + scenario + "\n})().then((out) => process.stdout.write(JSON.stringify(out)))" \
        ".catch((e) => { console.error(e); process.exit(1); });"
    out = subprocess.run(["node", "-e", script, str(WEB)], input=json.dumps(payload),
                         capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


_OPS = r"""
const out = [];
for (const [op, args] of input) {
  if (op === 'merge') out.push(lib.mergePickerTools(args[0], args[1]));
  else if (op === 'preset') out.push(lib.resolveAccessPreset(args[0], lib.mergePickerTools(args[1], args[2]), args[3]));
  else if (op === 'blocked') out.push(lib.presetSaveBlocked(args[0], args[1]));
  else throw new Error('unknown op ' + op);
}
return out;
"""


def _run(ops):
    return _node(_OPS, ops)


def _coder_names():
    from vaf.tools.coder import CODER_ONLY_TOOL_NAMES
    return sorted(CODER_ONLY_TOOL_NAMES)


def _registry():
    registry = [{"name": n, "description": n} for n in _SHARED]
    return registry + [{"name": "host_bash", "description": "host shell", "account_opt_in": True},
                       {"name": "delete_x", "description": "deletes"}]


# --------------------------------------------------------------------------
# The merge and the presets, run in node.
# --------------------------------------------------------------------------

def test_merge_lists_each_tool_once_registry_entry_wins():
    registry = [
        {"name": "git_status", "description": "Show the repository status", "category": "git"},
        {"name": "read_file", "description": "Read a file"},
        {"name": "git_status", "description": "a second registry copy"},
    ]
    [merged] = _run([["merge", [registry, ["git_status", "bash", "", "   ", 5, None, "bash"]]]])
    assert [t["name"] for t in merged] == ["git_status", "read_file", "bash"]
    assert merged[0] == registry[0], "the registry entry keeps its description and fields"
    assert merged[2] == {"name": "bash", "description": "Coder sub-agent tool"}


def test_presets_over_the_merged_list():
    coder = _coder_names()
    # The route's measured answer; _CODER_OWN are the names the main registry lacks.
    assert set(_SHARED) <= set(coder) and set(_CODER_OWN) <= set(coder)
    registry = _registry()
    workflows = [{"id": "wf1"}]
    full, standard, readonly = _run([
        ["preset", ["full", registry, coder, workflows]],
        ["preset", ["standard", registry, coder, workflows]],
        ["preset", ["readonly", registry, coder, workflows]],
    ])
    for result in (full, standard, readonly):
        assert len(result["tools"]) == len(set(result["tools"])), result["tools"]
    assert sorted(full["tools"]) == sorted(set(_SHARED) | set(coder) | {"host_bash", "delete_x"})
    assert set(_CODER_OWN) <= set(standard["tools"])
    assert "host_bash" not in standard["tools"] and "delete_x" not in standard["tools"]
    assert standard["workflows"] == ["wf1"]
    assert "git_status" in readonly["tools"] and "bash" not in readonly["tools"]
    assert readonly["workflows"] == []


def test_a_preset_saves_only_with_the_full_list():
    cases = [(p, s) for p in ("standard", "full", "readonly", "custom")
             for s in ("idle", "loading", "failed", "ready")]
    blocked = _run([["blocked", [p, s]] for p, s in cases])
    for (preset, state), refused in zip(cases, blocked):
        assert refused is (preset != "custom" and state != "ready"), (preset, state, refused)


# --------------------------------------------------------------------------
# The two hooks, run in React.
# --------------------------------------------------------------------------

_PENDING_CREATE = r"""
const workflows = input.workflows;
let latest;
function Form({tools}) {
  const [preset, setPreset] = React.useState('standard');
  const [sel, setSel] = React.useState({tools: [], workflows: []});
  const reapply = lib.useAccessPreset(preset, tools, workflows, setSel);
  latest = {sel, setSel, setPreset, reapply};
  return null;
}
const before = lib.mergePickerTools(input.registry, []);
const after = lib.mergePickerTools(input.registry, input.coder);
await act(async () => root.render(h(Form, {tools: before})));
// The create handler's closure: what the render that sent the request held.
const atSubmit = latest;
const first = latest.sel.tools;
// The coder's names land while the request is pending.
await act(async () => root.render(h(Form, {tools: after})));
const pending = latest.sel.tools;
// The request succeeds: the handler resets through ITS closure, in the worst order (the
// selection emptied, the preset already 'standard', so setting it again changes nothing).
await act(async () => { atSubmit.setSel({tools: [], workflows: []}); atSubmit.setPreset('standard'); atSubmit.reapply(); });
return {first, pending, next: latest.sel};
"""


def test_a_list_that_arrives_while_the_create_is_pending_reaches_the_next_form():
    out = _node(_PENDING_CREATE, {"registry": _registry(), "coder": _coder_names(), "workflows": [{"id": "wf1"}]})
    assert "bash" not in out["first"]
    assert set(_CODER_OWN) <= set(out["pending"])
    nxt = out["next"]
    assert set(_CODER_OWN) <= set(nxt["tools"]), "the next account would lose the coder's tools"
    assert len(nxt["tools"]) == len(set(nxt["tools"]))
    assert "host_bash" not in nxt["tools"] and nxt["workflows"] == ["wf1"]


_EDITED_ACCOUNT = r"""
const workflows = [];
const tools = lib.mergePickerTools(input.registry, input.coder);
let latest;
function Edit({id}) {
  const [sel, setSel] = React.useState({tools: ['kept'], workflows: []});
  lib.useAccessPreset('full', tools, workflows, setSel, id);
  latest = {sel, setSel};
  return null;
}
await act(async () => root.render(h(Edit, {id: 1})));
await act(async () => latest.setSel({tools: ['opened'], workflows: []}));
const sameAccount = latest.sel.tools;
await act(async () => root.render(h(Edit, {id: 2})));
return {sameAccount, otherAccount: latest.sel.tools};
"""


def test_the_preset_follows_the_account_being_edited():
    out = _node(_EDITED_ACCOUNT, {"registry": _registry(), "coder": _coder_names()})
    assert out["sameAccount"] == ["opened"]
    assert "bash" in out["otherAccount"] and "opened" not in out["otherAccount"]


_UNIVERSE = r"""
const calls = [];
let respond;
global.fetch = (url, init) => { calls.push([url, init && init.credentials]); return new Promise((r) => { respond = r; }); };
const reply = (ok, status, body) => ({ok, status, json: async () => body});
let latest;
function U({enabled}) { latest = lib.useToolUniverse(enabled); return null; }
const out = {};
await act(async () => root.render(h(U, {enabled: false})));
out.disabled = [latest.state, calls.length];
await act(async () => root.render(h(U, {enabled: true})));
out.requested = [latest.state, calls.length, calls[0]];
await act(async () => { respond(reply(false, 503, {})); await settle(); });
out.failed = [latest.state, latest.names];
await act(async () => latest.retry());
out.retrying = [latest.state, calls.length];
await act(async () => { respond(reply(true, 200, {})); await settle(); });
out.noList = latest.state;
await act(async () => latest.retry());
await act(async () => { respond(reply(true, 200, {coder_only: ['bash', 'run_tests']})); await settle(); });
out.ready = [latest.state, latest.names];
await act(async () => root.render(h(U, {enabled: false})));
await act(async () => root.render(h(U, {enabled: true})));
out.again = [latest.state, calls.length];
return out;
"""


def test_the_tool_universe_request_has_its_own_state():
    out = _node(_UNIVERSE, {})
    assert out["disabled"] == ["idle", 0]
    assert out["requested"] == ["loading", 1, ["/api/users/tool-universe", "include"]]
    assert out["failed"] == ["failed", []]
    assert out["retrying"] == ["loading", 2]
    assert out["noList"] == "failed", "a 200 without a list is not a loaded list"
    assert out["ready"] == ["ready", ["bash", "run_tests"]]
    assert out["again"] == ["ready", 3], "a loaded list is not fetched again"


_SECTION = r"""
const modal = fs.readFileSync(path.join(W, 'components', 'SettingsModal.tsx'), 'utf8');
const i = modal.indexOf('function AccessPresetSection('), j = modal.indexOf('\n}\n', i) + 3;
const src = "import React, { useState } from 'react';\nimport { useTranslations } from 'next-intl';\n" +
  "import { cn } from '@/lib/utils';\nexport " + modal.slice(i, j);
const out = {};
for (const locale of input.locales) {
  const messages = JSON.parse(fs.readFileSync(path.join(W, 'messages', locale + '.json'), 'utf8'));
  const t = (ns) => (key, args) => {
    const value = key.split('.').reduce((o, k) => (o || {})[k], messages[ns]);
    if (typeof value !== 'string') throw new Error('missing message ' + ns + '.' + key);
    return value.replace(/\{(\w+)\}/g, (_, x) => String(args && args[x]));
  };
  const section = load(src, {'next-intl': {useTranslations: t}, '@/lib/utils': {cn: (...a) => a.filter(Boolean).join(' ')}});
  const {renderToStaticMarkup} = req('react-dom/server');
  out[locale] = {};
  for (const universe of ['idle', 'loading', 'failed', 'ready']) {
    out[locale][universe] = renderToStaticMarkup(h(section.AccessPresetSection, {
      tools: [{name: 'git_status'}], workflows: [], preset: 'standard', selectedTools: ['git_status'],
      selectedWorkflows: [], onPresetChange: noop, onToolsChange: noop, onWorkflowsChange: noop,
      universe, onRetryUniverse: noop}));
  }
}
return out;
"""


def _escaped(text):
    return text.replace("&", "&amp;").replace("'", "&#x27;").replace('"', "&quot;").replace("<", "&lt;")


def test_the_picker_says_when_the_list_is_incomplete():
    locales = ("en", "de", "tr", "zh", "ja", "ko", "th")
    out = _node(_SECTION, {"locales": list(locales)})
    for locale in locales:
        add_user = json.loads((WEB / "messages" / f"{locale}.json").read_bytes())["modals"]
        loading = _escaped(add_user["addUser"]["toolUniverseLoading"])
        failed = _escaped(add_user["addUser"]["toolUniverseFailed"])
        retry = _escaped(add_user["updateRepair"]["retry"])
        html = out[locale]
        assert failed in html["failed"] and retry in html["failed"] and 'role="alert"' in html["failed"], locale
        assert loading not in html["failed"]
        for state in ("idle", "loading"):
            assert loading in html[state] and failed not in html[state], (locale, state)
        assert loading not in html["ready"] and failed not in html["ready"] and retry not in html["ready"], locale


# --------------------------------------------------------------------------
# The wiring in SettingsModal, static.
# --------------------------------------------------------------------------

def test_no_concat_of_the_coder_list_remains():
    offenders = []
    for base in ("app", "components", "lib"):
        for path in sorted((WEB / base).rglob("*.ts*")):
            if path.suffix not in (".ts", ".tsx"):
                continue
            if re.search(r"\.\.\.\s*(coderOnly|toolUniverse)", _strip_comments(_source(path))):
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []
    code = _strip_comments(_source(SETTINGS))
    assert "const pickerTools = useMemo(() => mergePickerTools(tools, toolUniverse.names)" in code


def test_universe_fetch_gate_names_a_real_tab():
    code = _strip_comments(_source(SETTINGS))
    assert "'/api/users/tool-universe'" not in code, "one fetch of the list, in web/lib/accessPresets.ts"
    gate = re.search(r"useToolUniverse\(activeTab === '(\w+)' && currentUser\?\.role === 'admin'\)", code)
    assert gate, "the tool-universe request is no longer gated on the tab and the admin role"
    block = code[code.index("const CATEGORIES = ["):]
    tab_ids = re.findall(r"\{ id: '(\w+)'", block[:block.index("];")])
    assert gate.group(1) in tab_ids, (gate.group(1), tab_ids)
    # ... and it is the tab that renders the user manager (the Add User button).
    add_user = code.index("setShowAddUserModal(true)")
    renders = re.findall(r"\{activeTab === '(\w+)'", code[:add_user])
    assert gate.group(1) == renders[-1] == "local_network"


def test_preset_hooks_follow_the_merged_list():
    code = _strip_comments(_source(SETTINGS))
    calls = re.findall(r"useAccessPreset\((\w+), (\w+), workflows,", code)
    assert sorted(calls) == [("accessPreset", "pickerTools"), ("editAccessPreset", "pickerTools")], calls
    assert "resolveAccessPreset(" not in code, "a preset resolved outside the hook misses a later list"


def test_create_reset_restores_the_standard_preset():
    code = _strip_comments(_source(SETTINGS))
    handler = code[code.index("const handleCreateUser = async"):code.index("const handleUpdateUser = async")]
    assert "resolveAccessPreset(" not in handler
    reset = handler[handler.index("setShowAddUserModal(false);"):]
    assert "setAccessPreset('standard');" in reset and "reapplyNewUserPreset();" in reset
    statement = reset[reset.index("setNewUser("):]
    statement = statement[:statement.index(");") + 2]
    assert "tools: []" not in statement and "workflows: []" not in statement, statement
    assert "const reapplyNewUserPreset = useAccessPreset(accessPreset," in code


def test_both_save_buttons_wait_for_the_full_list():
    code = _strip_comments(_source(SETTINGS))
    for handler, preset in (("handleCreateUser", "accessPreset"), ("handleUpdateUser", "editAccessPreset")):
        guard = f"presetSaveBlocked({preset}, toolUniverse.state)"
        assert re.search(rf"<button onClick=\{{{handler}\}} disabled=\{{{re.escape(guard)}\}}", code), handler
        body = code[code.index(f"const {handler} = async () => {{"):]
        assert f"{guard}) return;" in body[:250], f"{handler} has no guard of its own"
    assert code.count("universe={toolUniverse.state}") == 2
    assert code.count("onRetryUniverse={toolUniverse.retry}") == 2


def test_preset_rules_live_in_one_place():
    lib = _strip_comments(_source(LIB))
    for definition in ("export function resolveAccessPreset(", "export const WW_DESTRUCTIVE_RE =",
                       "export const WW_READABLE_RE =", "export type AccessPreset =",
                       "export function mergePickerTools<", "export function useAccessPreset(",
                       "export function useToolUniverse(", "export function presetSaveBlocked("):
        assert definition in lib, definition
    defined_elsewhere = []
    pattern = re.compile(r"(function\s+(resolveAccessPreset|mergePickerTools|useAccessPreset|useToolUniverse"
                         r"|presetSaveBlocked)\b"
                         r"|(const|let|var)\s+(WW_DESTRUCTIVE_RE|WW_READABLE_RE)\s*="
                         r"|type\s+AccessPreset\s*=)")
    for base in ("app", "components", "lib"):
        for path in sorted((WEB / base).rglob("*.ts*")):
            if path.suffix in (".ts", ".tsx") and path != LIB and pattern.search(_strip_comments(_source(path))):
                defined_elsewhere.append(path.relative_to(ROOT).as_posix())
    assert defined_elsewhere == []
    assert re.search(r"import \{[^}]*\buseAccessPreset\b[^}]*\} from '@/lib/accessPresets';",
                     _source(SETTINGS))
