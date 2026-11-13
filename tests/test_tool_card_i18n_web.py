# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The chat timeline's cards speak the page's language (web/components/ToolMessage.tsx,
web/components/TurnActionsTimeline.tsx, web/components/chat/toolCardText.ts, and the
ThinkingDetails/ActionDetails cards and tool-name fallbacks in web/app/page.tsx).

A finished tool card used to say the German "Fertig" and "N Zeilen" on every page, while the
action count, the thinking block and the watchdog row were English on every page. Every one of
those strings now comes from the `main`/`common` catalogues.

The helpers and the two components are the web's REAL code, transpiled with the app's own
TypeScript and run in node (the pattern of tests/test_turn_rounds_web.py). The components are
rendered with the real catalogues through use-intl's own translator and formatter, which is what
next-intl's hooks wrap; only the motion and icon libraries are stubbed. Skipped without node.

MUTATIONS, each turns the named test red:
- put `return 'Fertig';` back into ToolMessage, or `<span>Thinking Process</span>` back into
  page.tsx -> test_the_measured_literals_are_gone;
- restore `title="Diesen Sub-Agent killen"` or `<span>Output</span>`
  -> test_no_jsx_copy_in_the_tool_card_files;
- rename a call to `tMain('toolStatDoen')` -> test_every_used_key_exists;
- call `tMain('toolStatLines', { n: stat.count })` -> test_every_call_passes_the_message_arguments;
- set en.json `toolStatDone` back to `Fertig` -> test_english_says_done_and_german_fertig and
  test_the_card_renders_in_the_page_language;
- `Math.floor(s)` for seconds, `lines >= 1`, or drop the KB branch
  -> test_the_helpers_pick_the_old_branch;
- `useGrouping: true`, or `.toFixed(1)` instead of the formatter
  -> test_the_card_renders_in_the_page_language;
- a hardcoded `${n} actions` label -> test_the_timeline_counts_in_the_page_language;
- restore the em-dash return in the duration helper, or the stale-label dash
  -> test_no_em_dash_in_the_tool_card_files.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
TOOL_MESSAGE = WEB / "components" / "ToolMessage.tsx"
TIMELINE = WEB / "components" / "TurnActionsTimeline.tsx"
HELPERS = WEB / "components" / "chat" / "toolCardText.ts"
PAGE = WEB / "app" / "page.tsx"
MESSAGES = WEB / "messages"
_TS = WEB / "node_modules" / "typescript"
_LOCALES = ("de", "en", "tr", "zh", "ja", "ko", "th")


def _needs_node():
    if shutil.which("node") is None or not _TS.is_dir():
        pytest.skip("node or web/node_modules/typescript is not installed")


def _source(path):
    return path.read_bytes().decode("utf-8")


def _strip_comments(source):
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?<![:'\"`])//[^\n]*", "", source)


def _page_cards():
    """ThinkingDetails through the end of ActionDetails: the two cards on the timeline."""
    page = _source(PAGE)
    start = page.index("const ThinkingDetails = ")
    action = page.index("const ActionDetails = ", start)
    end = page.index("\n};\n", action) + len("\n};\n")
    return page[start:end]


def _page_fallback_lines():
    return [line for line in _source(PAGE).splitlines() if re.search(r"\bname=\{(?:msg|m)\.toolName \|\|", line)]


def _catalogue(locale):
    return json.loads((MESSAGES / f"{locale}.json").read_bytes())


# --------------------------------------------------------------------------
# Source-level guards.
# --------------------------------------------------------------------------

# Each measured literal in the source form it had, file by file.
_GONE = {
    TOOL_MESSAGE: (
        "'Fertig'", "Zeilen", "'running…'", "`running… ", "'Error'", "<span>Watchdog</span>",
        "No heartbeat", "'Active'", "Diesen Sub-Agent", "<span>Output</span>", "Waiting for output",
        "'Temporary'", "Persistent Workflow", "'Delete'", "'List'", "item${", "toFixed(1)} KB",
    ),
    TIMELINE: ("'action'", "'actions'", "more`"),
    HELPERS: ("'Fertig'", "Zeilen", "\u2014"),
}
_PAGE_CARDS_GONE = (">Thinking<", "Thinking Process", '"Action..."', '"Action"', "toFixed(1)}s")


def test_the_measured_literals_are_gone():
    left = []
    for path, literals in _GONE.items():
        code = _strip_comments(_source(path))
        left += [f"{path.name}: {lit!r}" for lit in literals if lit in code]
        # The kill button's label stood alone on its own JSX line.
        if re.search(r"^\s*Kill\s*$", code, flags=re.M):
            left.append(f"{path.name}: the bare 'Kill' label")
    cards = _strip_comments(_page_cards())
    left += [f"page.tsx cards: {lit!r}" for lit in _PAGE_CARDS_GONE if lit in cards]
    fallbacks = _page_fallback_lines()
    assert len(fallbacks) == 2, f"expected the two tool-card name fallbacks, found {len(fallbacks)}"
    left += [f"page.tsx fallback: {line.strip()}" for line in fallbacks if "tMain('toolNameUnknown')" not in line]
    assert not left, "hardcoded copy is back on the tool card or the timeline:\n" + "\n".join(left)


def test_no_em_dash_in_the_tool_card_files():
    hits = []
    for path in (TOOL_MESSAGE, TIMELINE, HELPERS):
        for number, line in enumerate(_source(path).splitlines(), 1):
            if "\u2014" in line:
                hits.append(f"{path.name}:{number}")
    if "\u2014" in _page_cards():
        hits.append("page.tsx: ThinkingDetails/ActionDetails")
    assert not hits, "em dash in the tool card files:\n" + "\n".join(hits)


_CALL = re.compile(r"\b(tMain|tCommon)\(\s*'([^']+)'")
_KEY_LITERAL = re.compile(r"\bkey:\s*'([^']+)'")
_NAMESPACE = {"tMain": "main", "tCommon": "common"}


def _used_keys():
    """(namespace, key, where) for every literal key the cards can look up."""
    used = []
    for path in (TOOL_MESSAGE, TIMELINE):
        code = _strip_comments(_source(path))
        used += [(_NAMESPACE[fn], key, path.name) for fn, key in _CALL.findall(code)]
        # workflowBadge returns a key the component looks up; the one routed to tCommon is
        # named in a tCommon call, every other one resolves under main.
        common = {key for fn, key in _CALL.findall(code) if fn == "tCommon"}
        used += [("common" if key in common else "main", key, f"{path.name} key:")
                 for key in _KEY_LITERAL.findall(code)]
    used += [("main", key, "toolCardText.ts key:") for key in _KEY_LITERAL.findall(_source(HELPERS))]
    page = _strip_comments(_page_cards()) + "\n".join(_page_fallback_lines())
    used += [(_NAMESPACE[fn], key, "page.tsx") for fn, key in _CALL.findall(page)]
    return used


def test_every_used_key_exists():
    en = _catalogue("en")
    used = _used_keys()
    assert len(used) > 25, f"the key scan found only {len(used)} lookups; did the call shape change?"
    missing = [f"{where}: {ns}.{key}" for ns, key, where in used if key not in en.get(ns, {})]
    assert not missing, "the card looks up a key the catalogue does not have:\n" + "\n".join(missing)


# --------------------------------------------------------------------------
# Catalogue values.
# --------------------------------------------------------------------------

_NEW_KEYS = (
    "toolStatDone", "toolStatLines", "toolStatKb", "toolRunning", "toolRunningFor", "toolStatError",
    "durationSeconds", "durationMinutesSeconds", "toolArgItems", "toolWfTemporary", "toolWfPersistent",
    "toolWfList", "toolWatchdog", "toolWatchdogStale", "toolKill", "toolKillTitle", "toolOutput",
    "toolWaitingForOutput", "toolNameUnknown", "turnActions", "turnActionsMore", "thinkingLive",
    "thinkingDone", "actionLive", "actionDone",
)


@pytest.mark.parametrize("locale", _LOCALES)
def test_the_new_keys_are_in_every_catalogue(locale):
    main = _catalogue(locale)["main"]
    missing = [key for key in _NEW_KEYS if not isinstance(main.get(key), str) or not main[key].strip()]
    assert not missing, f"{locale}.json lacks {missing}"


def test_english_says_done_and_german_fertig():
    en, de = _catalogue("en")["main"], _catalogue("de")["main"]
    assert en["toolStatDone"] == "Done" and de["toolStatDone"] == "Fertig"
    assert en["toolStatLines"] == "{count, plural, one {# line} other {# lines}}"
    assert de["toolStatLines"] == "{count, plural, one {# Zeile} other {# Zeilen}}"
    assert (en["toolRunning"], de["toolRunning"]) == ("running…", "läuft…")
    assert en["turnActions"] == "{count, plural, one {# action} other {# actions}}"
    assert de["turnActions"] == "{count, plural, one {# Aktion} other {# Aktionen}}"
    assert (en["thinkingDone"], de["thinkingDone"]) == ("Thinking Process", "Denkprozess")
    assert (en["durationSeconds"], de["durationSeconds"]) == ("{seconds}s", "{seconds} s")


# --------------------------------------------------------------------------
# The real code, run in node.
# --------------------------------------------------------------------------

def _call(fn, *args):
    _needs_node()
    script = (
        "const ts = require(process.argv[1]);"
        "const fs = require('fs');"
        "const src = fs.readFileSync(process.argv[2], 'utf8');"
        "const js = ts.transpileModule(src, {compilerOptions: {module: ts.ModuleKind.CommonJS,"
        " target: ts.ScriptTarget.ES2019}}).outputText;"
        "const m = {exports: {}}; new Function('module', 'exports', js)(m, m.exports);"
        "const [fn, args] = JSON.parse(process.argv[3]);"
        "process.stdout.write(JSON.stringify(m.exports[fn](...args)));"
    )
    out = subprocess.run(["node", "-e", script, str(_TS), str(HELPERS), json.dumps([fn, list(args)])],
                         capture_output=True, text=True, check=True, encoding="utf-8").stdout
    return json.loads(out)


def test_the_helpers_pick_the_old_branch():
    """Same branches and numbers as the German-only helpers they replace."""
    done = {"key": "toolStatDone"}
    assert _call("toolResultStat", None) == done
    assert _call("toolResultStat", "") == done
    assert _call("toolResultStat", "  \n\t ") == done
    assert _call("toolResultStat", "ok") == done
    assert _call("toolResultStat", "a\nb") == {"key": "toolStatLines", "count": 2}
    assert _call("toolResultStat", "\na\n\n  \nb\nc\n") == {"key": "toolStatLines", "count": 3}
    assert _call("toolResultStat", "x" * 1023) == done
    assert _call("toolResultStat", "x" * 1024) == {"key": "toolStatKb", "kb": 1}
    assert _call("toolResultStat", "x" * 2048) == {"key": "toolStatKb", "kb": 2}
    assert _call("durationText", 0.4) == {"key": "durationSeconds", "seconds": 0}
    assert _call("durationText", 59.6) == {"key": "durationSeconds", "seconds": 60}
    assert _call("durationText", 60) == {"key": "durationMinutesSeconds", "minutes": 1, "seconds": 0}
    assert _call("durationText", 65) == {"key": "durationMinutesSeconds", "minutes": 1, "seconds": 5}
    assert _call("durationText", 3599.6) == {"key": "durationMinutesSeconds", "minutes": 59, "seconds": 60}


_AST_SCRIPT = r"""
const ts = require(process.argv[1]);
const fs = require('fs');
const files = JSON.parse(fs.readFileSync(0, 'utf8'));
const ATTRS = new Set(['title', 'aria-label', 'placeholder', 'alt']);
const LETTER = /\p{L}/u;
const ENTITY = /&(?:[A-Za-z]+|#\d+|#x[0-9A-Fa-f]+);/g;
const copy = [], calls = [];
// The string a JSX expression puts on screen: a literal, or a literal reached through a
// conditional, a fallback or a template's fixed text.
function visible(node, out) {
  if (!node) return;
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) out.push(node.text);
  else if (ts.isTemplateExpression(node)) { out.push(node.head.text); node.templateSpans.forEach(s => out.push(s.literal.text)); }
  else if (ts.isParenthesizedExpression(node)) visible(node.expression, out);
  else if (ts.isConditionalExpression(node)) { visible(node.whenTrue, out); visible(node.whenFalse, out); }
  else if (ts.isBinaryExpression(node) && [ts.SyntaxKind.BarBarToken, ts.SyntaxKind.QuestionQuestionToken,
           ts.SyntaxKind.AmpersandAmpersandToken].includes(node.operatorToken.kind)) visible(node.right, out);
}
function tagOf(el) { return el && el.openingElement ? el.openingElement.tagName.getText() : ''; }
for (const [label, text] of files) {
  const sf = ts.createSourceFile(label, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const where = (n) => label + ':' + (sf.getLineAndCharacterOfPosition(n.getStart()).line + 1);
  (function walk(node) {
    if (ts.isJsxText(node)) {
      const t = node.text.replace(ENTITY, '').trim();
      if (LETTER.test(t)) copy.push(where(node) + ' text ' + JSON.stringify(t));
    } else if (ts.isJsxAttribute(node) && ATTRS.has(node.name.getText())) {
      const init = node.initializer, out = [];
      if (init && ts.isStringLiteral(init)) out.push(init.text);
      else if (init && ts.isJsxExpression(init)) visible(init.expression, out);
      out.filter(s => LETTER.test(s)).forEach(s => copy.push(where(node) + ' ' + node.name.getText() + ' ' + JSON.stringify(s)));
    } else if (ts.isJsxExpression(node) && node.parent && ts.isJsxElement(node.parent) && tagOf(node.parent) !== 'style') {
      const out = []; visible(node.expression, out);
      out.filter(s => LETTER.test(s)).forEach(s => copy.push(where(node) + ' child ' + JSON.stringify(s)));
    } else if (ts.isCallExpression(node) && ts.isIdentifier(node.expression)
               && ['tMain', 'tCommon'].includes(node.expression.text)
               && node.arguments.length && ts.isStringLiteral(node.arguments[0])) {
      const arg = node.arguments[1];
      const props = arg && ts.isObjectLiteralExpression(arg)
        ? arg.properties.map(p => p.name ? p.name.getText() : null) : null;
      calls.push([where(node), node.expression.text, node.arguments[0].text, props]);
    }
    ts.forEachChild(node, walk);
  })(sf);
}
process.stdout.write(JSON.stringify({copy, calls}));
"""


def _ast():
    _needs_node()
    files = [[path.name, _source(path)] for path in (TOOL_MESSAGE, TIMELINE, HELPERS)]
    files.append(["page.tsx (ThinkingDetails, ActionDetails)", _page_cards()])
    out = subprocess.run(["node", "-e", _AST_SCRIPT, str(_TS)], input=json.dumps(files),
                         capture_output=True, text=True, check=True, encoding="utf-8").stdout
    return json.loads(out)


def test_no_jsx_copy_in_the_tool_card_files():
    copy = _ast()["copy"]
    assert not copy, "visible text written straight into the card's JSX:\n" + "\n".join(copy)


_ICU_ARG = re.compile(r"\{\s*([A-Za-z_]\w*)\s*[,}]")


def test_every_call_passes_the_message_arguments():
    """A missing or misnamed value renders as the raw `{count}`, and nothing at build time sees it."""
    en = _catalogue("en")
    calls = _ast()["calls"]
    assert len(calls) > 20, f"the AST found only {len(calls)} translation calls"
    wrong = []
    for where, fn, key, props in calls:
        value = en.get(_NAMESPACE[fn], {}).get(key)
        if not isinstance(value, str):
            continue  # test_every_used_key_exists reports it
        needed = set(_ICU_ARG.findall(value))
        given = set(props or [])
        if needed != given:
            wrong.append(f"{where}: {fn}('{key}') needs {sorted(needed)}, gets {sorted(given)}")
    assert not wrong, "translation call and message disagree on their arguments:\n" + "\n".join(wrong)


_RENDER_SCRIPT = r"""
const path = require('path'), fs = require('fs');
const W = process.argv[1];
const req = require('module').createRequire(path.join(W, 'package.json'));
const ts = req('typescript'), React = req('react'), intl = req('use-intl');
const { renderToStaticMarkup } = req('react-dom/server');
const [file, exportName, locale, propsList] = JSON.parse(fs.readFileSync(0, 'utf8'));
const messages = JSON.parse(fs.readFileSync(path.join(W, 'messages', locale + '.json'), 'utf8'));
function load(rel, stubs) {
  const src = fs.readFileSync(path.join(W, rel), 'utf8');
  const js = ts.transpileModule(src, {compilerOptions: {module: ts.ModuleKind.CommonJS,
    jsx: ts.JsxEmit.React, target: ts.ScriptTarget.ES2019, esModuleInterop: true}}).outputText;
  const m = {exports: {}};
  new Function('require', 'module', 'exports', js)((id) => (id in stubs ? stubs[id] : req(id)), m, m.exports);
  return m.exports;
}
const plain = (tag) => React.forwardRef(({initial, animate, exit, transition, layout, ...rest}, ref) =>
  React.createElement(tag, {...rest, ref}));
const stubs = {
  // next-intl's hooks are use-intl's translator and formatter bound to the provider's locale.
  'next-intl': {
    useTranslations: (ns) => intl.createTranslator({locale, messages, namespace: ns}),
    useFormatter: () => intl.createFormatter({locale}),
  },
  'framer-motion': {
    motion: new Proxy({}, {get: (_, tag) => plain(tag)}),
    AnimatePresence: ({children}) => React.createElement(React.Fragment, null, children),
  },
  'lucide-react': new Proxy({}, {get: (_, name) => (name === '__esModule' ? undefined : () => null)}),
  '@/lib/utils': {cn: (...a) => a.filter(Boolean).join(' '), getApiBase: () => ''},
  '@/hooks/useIsMobile': {useIsMobile: () => false},
  '@/components/AgentAvatar': {AgentAvatar: () => null},
};
stubs['@/components/chat/toolCardText'] = load('components/chat/toolCardText.ts', {});
const mod = load(file, stubs);
const out = propsList.map((p) => {
  if (p.actionsCount != null) {
    p = {...p, actions: Array.from({length: p.actionsCount}, (_, i) => ({key: 'a' + i, kind: 'tool', state: 'done', node: null})),
         onToggle() {}, avatarMode: 'idle'};
  }
  return renderToStaticMarkup(React.createElement(mod[exportName], p));
});
process.stdout.write(JSON.stringify(out));
"""


def _render(file, export, locale, props_list):
    _needs_node()
    if not (WEB / "node_modules" / "use-intl").is_dir():
        pytest.skip("web/node_modules/use-intl is not installed")
    out = subprocess.run(["node", "-e", _RENDER_SCRIPT, str(WEB)], input=json.dumps([file, export, locale, props_list]),
                         capture_output=True, text=True, check=True, encoding="utf-8").stdout
    return json.loads(out)


def _text(html):
    return re.sub(r"<[^>]+>", "", html).replace("&amp;", "&")


_CARDS = [
    {"id": "1", "name": "write_file", "status": "completed", "result": ""},
    {"id": "2", "name": "read_file", "status": "completed", "result": "a\nb\nc"},
    {"id": "3", "name": "web_fetch", "status": "completed", "result": "x" * 1536},
    {"id": "4", "name": "web_fetch", "status": "completed", "result": "x" * 1536 * 1024},
    {"id": "5", "name": "render_check", "status": "running"},
    {"id": "6", "name": "render_check", "status": "running", "startTime": 1000, "endTime": 66000},
    {"id": "7", "name": "bash", "status": "error", "result": "boom"},
    {"id": "8", "name": "create_agent_workflow", "status": "completed", "args": '{"action": "create"}'},
    {"id": "9", "name": "create_agent_workflow", "status": "completed", "args": '{"action": "delete"}'},
    {"id": "10", "name": "update_working_memory", "status": "completed", "args": '{"items": [1, 2, 3]}'},
]

# What each card shows, per locale, in the order of _CARDS.
_EXPECTED = {
    "en": ["Done", "3 lines", "1.5 KB", "1536.0 KB", "running…", "running… 1m 5s", "Error",
           "Persistent Workflow", "Delete", "items: [3 items]"],
    "de": ["Fertig", "3 Zeilen", "1,5 KB", "1536,0 KB", "läuft…", "läuft… 1 min 5 s", "Fehler",
           "Dauerhafter Workflow", "Entfernen", "items: [3 Einträge]"],
    "ja": ["完了", "3行", "1.5 KB", "1536.0 KB", "実行中…", "実行中…1分5秒", "エラー",
           "永続ワークフロー", "削除", "items: [3件]"],
}


@pytest.mark.parametrize("locale", sorted(_EXPECTED))
def test_the_card_renders_in_the_page_language(locale):
    rendered = [_text(html) for html in _render("components/ToolMessage.tsx", "ToolMessage", locale, _CARDS)]
    for card, text, expected in zip(_CARDS, rendered, _EXPECTED[locale]):
        assert expected in text, f"{locale} card {card['id']}: {expected!r} not in {text!r}"
    foreign = {"en": ("Fertig", "Zeilen", "läuft"), "de": ("Done", "running", "Error", "lines"),
               "ja": ("Fertig", "Done", "running")}[locale]
    leaked = [(card["id"], word) for card, text in zip(_CARDS, rendered) for word in foreign if word in text]
    assert not leaked, f"{locale} card shows another language: {leaked}"


@pytest.mark.parametrize("locale,one,many,more", [
    ("en", "1 action", "15 actions", "2 more"),
    ("de", "1 Aktion", "15 Aktionen", "2 weitere"),
    ("ja", "1件の操作", "15件の操作", "他2件"),
])
def test_the_timeline_counts_in_the_page_language(locale, one, many, more):
    single, elided = _render("components/TurnActionsTimeline.tsx", "TurnActionsTimeline", locale, [
        {"actionsCount": 1, "isLive": False, "expanded": False},
        {"actionsCount": 15, "isLive": False, "expanded": False},
    ])
    assert one in _text(single)
    assert many in _text(elided)
    assert f'aria-label="{more}"' in elided and f'title="{more}"' in elided
