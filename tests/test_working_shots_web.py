# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The browser's half of check pictures (web/components/chat/workingShots.ts, ShotStrip.tsx,
web/app/page.tsx).

A picture the agent took to check its own work arrives with `purpose: "check"` and the
`toolId` of its step. It is drawn in that step while the turn's timeline is open, and as one
sideways strip for the turn while it is folded. Every other file stays an attachment chip,
untouched. The split is the web's REAL code, transpiled with the app's own TypeScript and run
in node (the pattern of tests/test_turn_rounds_web.py); skipped without node. The wiring in
page.tsx and the strip's static rules are pinned at the source.

MUTATIONS, each turns the named test red:
- classify by count instead of purpose -> test_purpose_decides_not_count;
- fill byStep while collapsed -> test_collapsed_is_one_strip;
- reorder `files` -> test_deliverables_unchanged_and_in_order;
- add an extension to one list only -> test_extension_list_matches_the_image_viewer;
- drop toolId from newChip, or rebuild {path, name} in withChips -> test_page_forwards_both_fields;
- map turnChips in the chip row -> test_chip_row_draws_only_the_attachments;
- add animate-pulse or blur-sm to the strip -> test_strip_has_no_animation_and_no_filter;
- drop a key from th.json -> test_four_keys_in_every_catalogue;
- hand splitTurnFiles [] instead of the step ids -> test_page_places_the_strips;
- keep tool steps without an id, or take 'say' steps -> test_step_tool_ids;
- swap atStart and atEnd in edgeMask -> test_edge_mask;
- a plain {} for byStep -> test_provider_ids_are_plain_keys (__proto__);
- read byStep[id] without the own-key check -> test_provider_ids_are_plain_keys (constructor);
- snap the row again, or a scroll padding inside the fade -> test_keyboard_focus_clears_the_fade.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "web" / "components" / "chat" / "workingShots.ts"
STRIP = ROOT / "web" / "components" / "chat" / "ShotStrip.tsx"
VIEWER = ROOT / "web" / "components" / "ImageViewer.tsx"
PAGE = ROOT / "web" / "app" / "page.tsx"
MESSAGES = ROOT / "web" / "messages"
_TS = ROOT / "web" / "node_modules" / "typescript"


def _src(path: Path) -> str:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def _call(fn, *args):
    if shutil.which("node") is None or not _TS.is_dir():
        pytest.skip("node or web/node_modules/typescript is not installed")
    script = (
        "const ts = require(process.argv[1]);"
        "const fs = require('fs');"
        "const src = fs.readFileSync(process.argv[2], 'utf8');"
        "const js = ts.transpileModule(src, {compilerOptions: {module: ts.ModuleKind.CommonJS,"
        " target: ts.ScriptTarget.ES2019}}).outputText;"
        "const m = {exports: {}}; new Function('module', 'exports', js)(m, m.exports);"
        "const [fn, args] = JSON.parse(process.argv[3]);"
        "const v = m.exports[fn];"
        "const r = typeof v === 'function' ? v(...args) : v;"
        "process.stdout.write(JSON.stringify(r === undefined ? null : r));"
    )
    out = subprocess.run(["node", "-e", script, str(_TS), str(MODULE), json.dumps([fn, list(args)])],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def _shot(n, tool="t1-a", ext="jpg"):
    return {"path": f"/ws/render_check_{n:03d}.{ext}", "name": f"render_check_{n:03d}.{ext}",
            "purpose": "check", "toolId": tool}


PDF = {"path": "/ws/report.pdf", "name": "report.pdf"}
CHART = {"path": "/ws/chart.png", "name": "chart.png"}
# A check-tagged file that is not a picture is never moved: it stays an attachment.
CHECK_LOG = {"path": "/ws/check.log", "name": "check.log", "purpose": "check", "toolId": "t1-a"}


def test_deliverables_unchanged_and_in_order():
    chips = [PDF, _shot(1), CHART, CHECK_LOG, _shot(2)]
    for expanded in (False, True):
        out = _call("splitTurnFiles", chips, ["t1-a"], expanded)
        assert out["files"] == [PDF, CHART, CHECK_LOG]
    assert _call("splitTurnFiles", [], [], False) == {"files": [], "byStep": {}, "strip": []}


def test_purpose_decides_not_count():
    # Five pictures the person asked for stay attachments ...
    charts = [{"path": f"/ws/chart{i}.png", "name": f"chart{i}.png"} for i in range(5)]
    out = _call("splitTurnFiles", charts, [], False)
    assert out["files"] == charts and out["strip"] == []
    # ... and a single check picture is still a check picture.
    out = _call("splitTurnFiles", [_shot(1)], [], False)
    assert out["files"] == [] and out["strip"] == [_shot(1)]


def test_collapsed_is_one_strip():
    shots = [_shot(i, tool="t2-c") for i in range(60)]
    out = _call("splitTurnFiles", [PDF] + shots, ["t2-c"], False)
    assert out["byStep"] == {} and out["strip"] == shots and out["files"] == [PDF]


def test_expanded_puts_each_picture_in_its_step():
    a, b = _shot(1, "t1-a"), _shot(2, "t1-b")
    stray = _shot(3, "gone")
    bare = {"path": "/ws/shot.png", "name": "shot.png", "purpose": "check"}
    out = _call("splitTurnFiles", [a, CHART, b, stray, bare], ["t1-a", "t1-b", ""], True)
    assert out["byStep"] == {"t1-a": [a], "t1-b": [b]}
    assert out["strip"] == [stray, bare], "a picture whose step is not on screen goes to the turn strip"
    assert out["files"] == [CHART]
    # Two pictures of one step keep their arrival order.
    out = _call("splitTurnFiles", [_shot(5, "x"), _shot(4, "x")], ["x"], True)
    assert [s["name"] for s in out["byStep"]["x"]] == ["render_check_005.jpg", "render_check_004.jpg"]


def test_extension_list_matches_the_image_viewer():
    viewer = re.search(r"const IMAGE_EXTS = \[([^\]]*)\]", _src(VIEWER)).group(1)
    ours = re.search(r"export const SHOT_IMAGE_EXTS = \[([^\]]*)\]", _src(MODULE)).group(1)
    as_list = lambda s: [x.strip().strip("'\"") for x in s.split(",") if x.strip()]  # noqa: E731
    assert as_list(viewer) == as_list(ours)
    assert _call("isWorkingShot", _shot(1, ext="PNG")) is True


def test_page_forwards_both_fields():
    src = _src(PAGE)
    branch = src[src.index("const newChip = {"):]
    branch = branch[:branch.index("};")]
    assert "purpose: typeof data.purpose === 'string' ? data.purpose : undefined" in branch
    assert "toolId: typeof data.toolId === 'string' ? data.toolId : undefined" in branch
    with_chips = src[src.index("const withChips = "):]
    with_chips = with_chips[:with_chips.index("\n};")]
    assert "purpose: c.purpose, toolId: c.toolId" in with_chips, \
        "withChips rebuilds the chip and drops what decides where it is drawn"


def test_chip_row_draws_only_the_attachments():
    src = _src(PAGE)
    assert "{isBot && shotSplit.files.length > 0 && (" in src
    row = src[src.index("{isBot && shotSplit.files.length > 0 && ("):]
    row = row[:row.index("</>")]
    assert "shotSplit.files.map((f, fi) => (" in row and "turnChips.map(" not in row
    # The pills themselves are unchanged, byte for byte.
    for cls in (
        "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full border border-emerald-200 bg-emerald-50 text-emerald-700 text-xs font-medium hover:bg-emerald-100 transition-colors",
        "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full border border-orange-200 bg-orange-50 text-orange-700 text-xs font-medium hover:bg-orange-100 transition-colors",
        "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full border border-violet-200 bg-violet-50 text-violet-700 text-xs font-medium hover:bg-violet-100 transition-colors",
        "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full border border-blue-200 bg-blue-50 text-blue-700 text-xs font-medium hover:bg-blue-100 transition-colors",
    ):
        assert cls in row
    assert '<div className="mt-2 flex flex-wrap gap-2">' in row


def test_page_places_the_strips():
    src = _src(PAGE)
    assert "hasTimeline && timelineExpanded," in src, "collapsed or timeline-less turns use one strip"
    assert "hasTimeline ? stepToolIds(turnTl!.actions) : []," in src, \
        "without the step ids nothing is ever drawn inside its step"
    assert "<ShotStrip shots={stepShots(shotSplit.byStep, m.toolId)} onOpen={openImageInViewer} />" in src
    assert "shotSplit.byStep[" not in src, "a bare index finds inherited members for ids like 'constructor'"
    assert re.search(r"<ShotStrip shots=\{shotSplit\.strip\} onOpen=\{openImageInViewer\}\s+"
                     r"label=\{tMain\('workShotsLabel'", src)


def test_strip_has_no_animation_and_no_filter():
    src = _src(STRIP)
    code = re.sub(r"^\s*(//|\*|/\*).*$", "", src, flags=re.M)  # rules apply to code, not prose
    for banned in ("animation", "animate-", "filter", "backdrop-blur", "blur-", "setInterval", "setTimeout"):
        assert banned not in code, banned
    assert "prefers-reduced-motion: reduce" in src
    assert "behavior: prefersReducedMotion() ? 'auto' : 'smooth'" in src
    assert "maskImage: mask" in src and "WebkitMaskImage: mask" in src


def test_strip_keyboard_and_mobile_rules():
    src = _src(STRIP)
    assert "tabIndex={i === current ? 0 : -1}" in src, "one tab stop per strip"
    for key in ("ArrowRight", "ArrowLeft", "Home", "End"):
        assert f"e.key === '{key}'" in src
    assert "focus-visible:ring-2" in src
    arrow = re.search(r"const arrowClass = '([^']*)'", src).group(1)
    assert arrow.startswith("max-md:hidden "), "the arrows are a desktop affordance only"
    assert "overscroll-x-contain" in src and "overflow-x-auto" in src
    assert 'loading="lazy"' in src
    # At 390px three 112px pictures fill the row exactly and hide the fourth entirely.
    assert "max-md:w-[96px]" in src


def test_step_tool_ids():
    actions = [{"kind": "think", "msg": {}}, {"kind": "tool", "msg": {"toolId": "c1"}},
               {"kind": "say", "msg": {"toolId": "s1"}}, {"kind": "tool", "msg": {}},
               {"kind": "tool", "msg": {"toolId": ""}}, {"kind": "tool", "msg": {"toolId": "c2"}}]
    assert _call("stepToolIds", actions) == ["c1", "c2"]


def test_edge_mask():
    assert _call("edgeMask", True, True) is None, "a row that fits has sharp edges"
    start = _call("edgeMask", True, False)
    middle = _call("edgeMask", False, False)
    end = _call("edgeMask", False, True)
    assert start == "linear-gradient(to right, #000 0, #000 calc(100% - 28px), transparent 100%)"
    assert middle == "linear-gradient(to right, transparent 0, #000 28px, #000 calc(100% - 28px), transparent 100%)"
    assert end == "linear-gradient(to right, transparent 0, #000 28px, #000 100%)"


def test_provider_ids_are_plain_keys():
    """Tool-call ids come from the provider. An id that names an Object member must be a key
    like any other: '__proto__' used to throw while splitting, 'constructor' handed the strip a
    function while rendering."""
    odd = [_shot(1, "__proto__"), _shot(2, "constructor"), _shot(3, "hasOwnProperty")]
    out = _call("splitTurnFiles", odd, ["__proto__", "constructor", "hasOwnProperty"], True)
    assert out["byStep"] == {"__proto__": [odd[0]], "constructor": [odd[1]], "hasOwnProperty": [odd[2]]}
    assert out["strip"] == []
    # A step with such an id and no pictures finds nothing (the page's lookup).
    for name in ("constructor", "hasOwnProperty", "isPrototypeOf", "toString"):
        assert _call("stepShots", {"c1": [odd[0]]}, name) == []
    assert _call("stepShots", {"c1": [odd[0]]}, "c1") == [odd[0]]
    assert _call("stepShots", {"c1": [odd[0]]}, None) == []


def test_keyboard_focus_clears_the_fade():
    """Measured in a browser: with proximity snapping a picture the arrow keys moved to was
    pulled back to a snap point and stayed cut at the edge, under the fade and the arrow
    button; and a 8px scroll padding left it inside the 28px fade. The row does not snap, and
    the padding clears the fade plus the arrow button (left-1/right-1, 28px wide, so 32px)."""
    src = _src(STRIP)
    code = re.sub(r"^\s*(//|\*|/\*).*$", "", src, flags=re.M)
    assert not re.search(r"\bsnap-", code), "scroll snapping fights scrollIntoView"
    assert "scrollPaddingInline: KEEP_CLEAR_PX" in src
    extra = int(re.search(r"const KEEP_CLEAR_PX = FADE_PX \+ (\d+);", src).group(1))
    fade = int(re.search(r"export const FADE_PX = (\d+);", _src(MODULE)).group(1))
    assert fade + extra > 4 + 28, "the padding must clear the arrow button over the fade"
    assert "inline: 'nearest'" in src
    assert "title={s.name}" not in src, "the raw file name is no hover text; the aria-label names the picture"


def test_four_keys_in_every_catalogue():
    keys = {"workShotsLabel": {"count"}, "workShotOpen": {"index", "count"},
            "workShotsEarlier": set(), "workShotsLater": set()}
    for path in sorted(MESSAGES.glob("*.json")):
        main = json.loads(path.read_bytes())["main"]
        for key, args in keys.items():
            assert key in main, f"{path.name}: {key}"
            assert set(re.findall(r"\{(\w+)\}", main[key])) == args, f"{path.name}: {key}"
            assert "Handy" not in main[key] and "phone" not in main[key].lower()
    assert len(list(MESSAGES.glob("*.json"))) == 7
