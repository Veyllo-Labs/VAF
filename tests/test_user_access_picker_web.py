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

The merge and the presets are the web's REAL code, transpiled with the app's own TypeScript and
run in node (the pattern of tests/test_tool_card_i18n_web.py); skipped without node or
web/node_modules. The wiring in SettingsModal is pinned statically and always runs.

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
- `[...tools, ...coderOnlyTools.map(...)]` back in SettingsModal -> test_no_concat_of_the_coder_list_remains;
- `activeTab !== 'users'` back in the fetch gate -> test_universe_fetch_gate_names_a_real_tab;
- `[accessPreset, tools, workflows]` back on the new-user preset effect
  -> test_preset_effects_follow_the_merged_list;
- `tools: [], workflows: []` back in the create reset -> test_create_reset_restores_the_standard_preset;
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


def _source(path):
    # A Windows checkout (core.autocrlf) holds the sources with CRLF; the searches expect LF.
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def _strip_comments(source):
    # Only where a comment can start: a block comment after whitespace or a JSX brace, a line
    # comment after whitespace. A bare `/*` also occurs inside strings ('image/*'), and
    # stripping from there eats real code up to the next comment's end.
    source = re.sub(r"(?<=[\s{])/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?:^|(?<=\s))//[^\n]*", "", source, flags=re.M)


_SCRIPT = r"""
const fs = require('fs');
const ts = require(process.argv[1]);
const src = fs.readFileSync(process.argv[2], 'utf8');
const js = ts.transpileModule(src, {compilerOptions: {module: ts.ModuleKind.CommonJS,
  target: ts.ScriptTarget.ES2019}}).outputText;
const m = {exports: {}};
new Function('require', 'module', 'exports', js)(require, m, m.exports);
const lib = m.exports;
const out = [];
for (const [op, args] of JSON.parse(fs.readFileSync(0, 'utf8'))) {
  if (op === 'merge') out.push(lib.mergePickerTools(args[0], args[1]));
  else if (op === 'preset') out.push(lib.resolveAccessPreset(args[0], lib.mergePickerTools(args[1], args[2]), args[3]));
  else throw new Error('unknown op ' + op);
}
process.stdout.write(JSON.stringify(out));
"""


def _run(ops):
    if shutil.which("node") is None or not _TS.is_dir():
        pytest.skip("node or web/node_modules/typescript is not installed")
    out = subprocess.run(["node", "-e", _SCRIPT, str(_TS), str(LIB)], input=json.dumps(ops),
                         capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _coder_names():
    from vaf.tools.coder import CODER_ONLY_TOOL_NAMES
    return sorted(CODER_ONLY_TOOL_NAMES)


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
    # The route's measured answer; the four below are the ones the main registry lacks.
    assert set(_SHARED) <= set(coder)
    coder_own = ("bash", "run_tests", "project_history", "project_rollback")
    assert set(coder_own) <= set(coder)
    registry = [{"name": n, "description": n} for n in _SHARED]
    registry += [{"name": "host_bash", "description": "host shell", "account_opt_in": True},
                 {"name": "delete_x", "description": "deletes"}]
    workflows = [{"id": "wf1"}]
    full, standard, readonly = _run([
        ["preset", ["full", registry, coder, workflows]],
        ["preset", ["standard", registry, coder, workflows]],
        ["preset", ["readonly", registry, coder, workflows]],
    ])
    for result in (full, standard, readonly):
        assert len(result["tools"]) == len(set(result["tools"])), result["tools"]
    assert sorted(full["tools"]) == sorted(set(_SHARED) | set(coder) | {"host_bash", "delete_x"})
    assert set(coder_own) <= set(standard["tools"])
    assert "host_bash" not in standard["tools"] and "delete_x" not in standard["tools"]
    assert standard["workflows"] == ["wf1"]
    assert "git_status" in readonly["tools"] and "bash" not in readonly["tools"]
    assert readonly["workflows"] == []


# --------------------------------------------------------------------------
# The wiring in SettingsModal, static.
# --------------------------------------------------------------------------

def test_no_concat_of_the_coder_list_remains():
    offenders = []
    for base in ("app", "components", "lib"):
        for path in sorted((WEB / base).rglob("*.ts*")):
            if path.suffix not in (".ts", ".tsx"):
                continue
            if re.search(r"\.\.\.\s*coderOnly", _strip_comments(_source(path))):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
    code = _strip_comments(_source(SETTINGS))
    assert re.search(r"const pickerTools = useMemo\(\(\) => mergePickerTools\(tools, coderOnlyTools\)", code)


def test_universe_fetch_gate_names_a_real_tab():
    code = _strip_comments(_source(SETTINGS))
    fetch_at = code.index("'/api/users/tool-universe'")
    effect = code[code.rindex("useEffect(", 0, fetch_at):fetch_at]
    gate = re.search(r"activeTab !== '(\w+)'", effect)
    assert gate, "the tool-universe fetch is no longer gated on the tab"
    block = code[code.index("const CATEGORIES = ["):]
    tab_ids = re.findall(r"\{ id: '(\w+)'", block[:block.index("];")])
    assert gate.group(1) in tab_ids, (gate.group(1), tab_ids)
    # ... and it is the tab that renders the user manager (the Add User button).
    add_user = code.index("setShowAddUserModal(true)")
    renders = re.findall(r"\{activeTab === '(\w+)'", code[:add_user])
    assert gate.group(1) == renders[-1] == "local_network"


def test_preset_effects_follow_the_merged_list():
    code = _strip_comments(_source(SETTINGS))
    deps = []
    for m in re.finditer(r"useEffect\(\(\) => \{(.*?)\n    \}, \[([^\]]*)\]\);", code, flags=re.S):
        if "resolveAccessPreset(" in m.group(1):
            deps.append([d.strip() for d in m.group(2).split(",")])
    assert len(deps) == 2, deps
    for items in deps:
        assert "pickerTools" in items and "tools" not in items, items


def test_create_reset_restores_the_standard_preset():
    code = _strip_comments(_source(SETTINGS))
    handler = code[code.index("const handleCreateUser = async"):code.index("const handleUpdateUser = async")]
    reset = handler[handler.index("setShowAddUserModal(false);"):handler.index("setAccessPreset('standard');")]
    assert "resolveAccessPreset('standard', pickerTools, workflows)" in reset
    statement = reset[reset.index("setNewUser("):]
    statement = statement[:statement.index(");") + 2]
    assert "tools: []" not in statement and "workflows: []" not in statement, statement
    assert "tools: standard.tools" in statement and "workflows: standard.workflows" in statement


def test_preset_rules_live_in_one_place():
    lib = _strip_comments(_source(LIB))
    for definition in ("export function resolveAccessPreset(", "export const WW_DESTRUCTIVE_RE =",
                       "export const WW_READABLE_RE =", "export type AccessPreset =",
                       "export function mergePickerTools<"):
        assert definition in lib, definition
    defined_elsewhere = []
    pattern = re.compile(r"(function\s+(resolveAccessPreset|mergePickerTools)\b"
                         r"|(const|let|var)\s+(WW_DESTRUCTIVE_RE|WW_READABLE_RE)\s*="
                         r"|type\s+AccessPreset\s*=)")
    for base in ("app", "components", "lib"):
        for path in sorted((WEB / base).rglob("*.ts*")):
            if path.suffix in (".ts", ".tsx") and path != LIB and pattern.search(_strip_comments(_source(path))):
                defined_elsewhere.append(str(path.relative_to(ROOT)))
    assert defined_elsewhere == []
    assert re.search(r"import \{[^}]*\bresolveAccessPreset\b[^}]*\} from '@/lib/accessPresets';",
                     _source(SETTINGS))
