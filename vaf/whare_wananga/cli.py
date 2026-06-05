"""
Whare Wananga training CLI -- run and inspect tool self-learning from the terminal.

Why a CLI (next to the web dashboard): it runs train_tool SYNCHRONOUSLY in the foreground, so a
full run (with the live phase/probe output) can be driven and read directly from a shell -- handy
for testing the learner, re-training a single tool, or training every tool in one queue.

    python -m vaf.whare_wananga.cli train create_contact          # train one tool (live)
    python -m vaf.whare_wananga.cli train memory_search --quick   # small batches (fast smoke test)
    python -m vaf.whare_wananga.cli train --all                   # queue: every not-yet-learned tool
    python -m vaf.whare_wananga.cli retrain update_intent         # alias for train (a run is a fresh assessment)
    python -m vaf.whare_wananga.cli list                          # learned tools + state
    python -m vaf.whare_wananga.cli show create_contact           # the three baskets
    python -m vaf.whare_wananga.cli delete create_contact         # drop the stored knowledge
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any, Dict, List, Optional

from vaf.whare_wananga import store


# ── small output helpers (ASCII only, terminal-friendly) ──────────────────────
def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _mark(ok: Optional[bool]) -> str:
    return "ok " if ok else "MISS"


def _pct(c: Any) -> str:
    try:
        return f"{round(float(c) * 100)}%"
    except Exception:
        return "-"


def _make_progress(verbose: bool):
    """Return a progress callback for train_tool that prints a concise live trace."""
    def _cb(ev: Dict[str, Any]) -> None:
        et = ev.get("event")
        if et == "start":
            mode = ev.get("mode", "?")
            _p(f"  start: mode={mode} learn={ev.get('learn_n', '?')} "
               f"validate={ev.get('validate_n', '?')} refine={ev.get('refine_n', '?')} "
               f"rounds<={ev.get('max_rounds', '?')}")
        elif et == "declare":
            canary = ev.get("canary")
            _p(f"  declare: not safely probeable -> learning from declaration"
               + (f" (canary accepted: {canary!r})" if canary else ""))
        elif et == "prep_start":
            _p(f"  prep: running prerequisites {ev.get('prereqs')}")
        elif et == "prep":
            _p(f"    prereq {ev.get('tool')}: {str(ev.get('actual'))[:80]}")
        elif et == "attempt" and verbose:
            ph = ev.get("phase", "?")
            _p(f"    {ph:<8} {ev.get('i')}: {_mark(ev.get('match'))}  "
               f"pred={ev.get('predicted_outcome')} actual={ev.get('actual_outcome')}"
               + (f"  [{ev.get('verdict')}]" if ev.get('verdict') else ""))
        elif et == "distil":
            _p("  * distilled the three baskets")
        elif et == "validate_start":
            _p(f"  validate: round {ev.get('round')}")
        elif et == "validate_result":
            _p(f"    validate round {ev.get('round')}: {ev.get('hits')}/{ev.get('n')} correct")
        elif et == "challenge_start":
            _p(f"  challenge: need {ev.get('need')} passes, max {ev.get('max_fails')} fails")
        elif et == "challenge_round":
            _p(f"    challenge round: {ev.get('round_pass')} pass / {ev.get('round_fail')} fail "
               f"(total fails {ev.get('total_fails')})")
        elif et == "challenge_result":
            _p(f"  challenge {'PASSED' if ev.get('passed') else 'not passed'} "
               f"(total fails {ev.get('total_fails')})")
        elif et == "halt":
            _p(f"  HALTED: {ev.get('reason')}")
    return _cb


def _summary_line(s: Dict[str, Any]) -> str:
    """One-line result for a finished train_tool summary."""
    if s.get("skipped"):
        return f"SKIPPED  {s.get('reason', '')}"
    if not s.get("ok"):
        return f"ERROR    {s.get('error', 'failed')}"
    if s.get("declared"):
        return (f"DECLARED status={s.get('status')} confirmed={s.get('confirmed')} "
                f"(learned from declaration, not probed)")
    if s.get("halted"):
        return f"HALTED   status={s.get('status')} confidence={_pct(s.get('confidence'))}"
    return (f"mode={s.get('mode')} status={s.get('status')} confirmed={s.get('confirmed')} "
            f"challenge_passed={s.get('challenge_passed')} confidence={_pct(s.get('confidence'))} "
            f"attempts={s.get('attempts')}")


def _baskets_brief(tool: str) -> str:
    rec = store.load(tool) or {}
    a = (rec.get("aronui") or {})
    pit = (rec.get("tuatea") or {}).get("pitfalls") or []
    proc = (rec.get("tuarua") or {}).get("procedure") or []
    when = (a.get("when_to_use") or "").strip()
    return (f"    aronui: {'set' if when else 'empty'} | tuatea: {len(pit)} pitfalls | "
            f"tuarua: {len(proc)} steps")


# ── kwargs for a fast smoke test ──────────────────────────────────────────────
_QUICK = dict(learn_n=4, validate_n=3, refine_n=2, max_rounds=2,
              challenge_pass=2, challenge_round_fails=2, challenge_max_fails=4)


def _build_agent():
    _p("Building agent (loading tools)...")
    from vaf.core.agent import Agent
    return Agent(verbose=False)


def _train_one(agent, tool: str, *, quick: bool, verbose: bool) -> Dict[str, Any]:
    from vaf.whare_wananga import runner
    _p(f"\n=== {tool} ===")
    kw = dict(_QUICK) if quick else {}
    t0 = time.time()
    try:
        s = runner.train_tool(agent, tool, progress=_make_progress(verbose), **kw)
    except Exception as e:
        s = {"ok": False, "tool": tool, "error": f"{type(e).__name__}: {e}"}
    s.setdefault("tool", tool)
    _p(f"  -> {_summary_line(s)}  ({time.time() - t0:.0f}s)")
    if s.get("ok"):
        _p(_baskets_brief(tool))
    # Teacher/Noho: a weak result may co-learn with a stronger API model (opt-in, foreground here).
    try:
        from vaf.whare_wananga import teacher
        if teacher.teach_now(agent, tool, s):
            r2 = store.load(tool) or {}
            _p(f"  teacher (Noho) -> status={r2.get('status')} "
               f"challenge_passed={r2.get('challenge_passed')} confidence={_pct(r2.get('confidence'))}")
    except Exception:
        pass
    return s


# ── subcommands ───────────────────────────────────────────────────────────────
def cmd_train(args) -> int:
    agent = _build_agent()
    tools = getattr(agent, "tools", {})

    if args.all:
        names = sorted(tools.keys())
    else:
        names = args.tools
        unknown = [n for n in names if n not in tools]
        if unknown:
            _p(f"Unknown tool(s): {', '.join(unknown)}")
            return 2

    # In --all mode, skip already-learned tools (unless --force) and unconfigured connections.
    try:
        from vaf.whare_wananga.preconditions import tool_precondition
    except Exception:
        tool_precondition = None

    results: List[Dict[str, Any]] = []
    queued = []
    for n in names:
        # In --all (sweep) mode, attempt each tool ONCE: skip any tool that already has a stored
        # record (learned, draft or declare) so chunks focus on NEW tools and don't re-run a hard
        # tool that never confirms. Use --force to re-train everything.
        if args.all and not args.force and store.load(n) is not None:
            results.append({"tool": n, "ok": True, "skipped": True, "reason": "already attempted"})
            continue
        if tool_precondition is not None:
            try:
                pc = tool_precondition(n)
                if pc.get("requires_config") and not pc.get("configured"):
                    results.append({"tool": n, "ok": False, "skipped": True,
                                    "reason": "connection not configured"})
                    continue
            except Exception:
                pass
        queued.append(n)

    _p(f"\nQueue: {len(queued)} tool(s) to train"
       + (f"  ({len(names) - len(queued)} skipped)" if len(queued) != len(names) else ""))
    for n in queued:
        results.append(_train_one(agent, n, quick=args.quick, verbose=args.verbose or not args.all))

    # Final table
    _p("\n" + "=" * 60)
    _p(f"{'TOOL':<28} RESULT")
    _p("-" * 60)
    for r in results:
        _p(f"{r.get('tool', '?'):<28} {_summary_line(r)}")
    halted = [r for r in results if r.get("halted")]
    return 1 if halted else 0


def cmd_list(args) -> int:
    names = store.list_tools()
    if not names:
        _p("No tools learned yet.")
        return 0
    states = store.learned_states(names)
    _p(f"{'TOOL':<28} {'STATE':<10} CONFIDENCE")
    _p("-" * 50)
    for n in sorted(names):
        rec = store.load(n) or {}
        _p(f"{n:<28} {states.get(n, '?'):<10} {_pct(rec.get('confidence'))}")
    return 0


def cmd_show(args) -> int:
    rec = store.load(args.tool)
    if not rec:
        _p(f"No stored knowledge for '{args.tool}'.")
        return 1
    a = rec.get("aronui") or {}
    t = rec.get("tuatea") or {}
    u = rec.get("tuarua") or {}
    _p(f"=== {args.tool} ===")
    _p(f"state={store.learned_state(args.tool)}  status={rec.get('status')}  "
       f"mode={rec.get('learn_mode', 'probe')}  confidence={_pct(rec.get('confidence'))}  "
       f"side_effect_class={rec.get('side_effect_class')}")
    _p("\n[Aronui - when to use / output]")
    _p(f"  when_to_use : {a.get('when_to_use', '')}")
    _p(f"  output_shape: {a.get('output_shape', '')}")
    _p("\n[Tuatea - pitfalls]")
    for p in (t.get("pitfalls") or []):
        _p(f"  - {p.get('text') if isinstance(p, dict) else p}")
    _p("\n[Tuarua - procedure / verification]")
    for step in (u.get("procedure") or []):
        _p(f"  procedure  : {step}")
    for v in (u.get("verification") or []):
        _p(f"  verify     : {v}")
    return 0


def cmd_delete(args) -> int:
    ok = store.delete(args.tool)
    _p(f"{'Deleted' if ok else 'Nothing to delete for'} '{args.tool}'.")
    return 0 if ok else 1


def cmd_eager(args) -> int:
    from vaf.whare_wananga import eager
    action = getattr(args, "action", None) or "status"
    if action == "on":
        eager.set_enabled(True)
        _p("EAGER enabled (whare_wananga_eager_enabled=true). The app trains safe, configured, "
           "not-yet-learned tools in the background, one at a time.")
        return 0
    if action == "off":
        eager.set_enabled(False)
        _p("EAGER disabled.")
        return 0
    if action == "status":
        learned = sum(1 for n in store.list_tools() if store.is_learned(n))
        _p(f"EAGER enabled : {eager.is_enabled()}")
        _p(f"learned tools : {learned}")
        _p("scope         : SAFE tools only (never send/communication or irreversible tools)")
        _p("note          : the periodic scanner runs inside the app; 'eager scan' below trains "
           "eligible tools here in the foreground.")
        return 0
    if action == "scan":
        from vaf.whare_wananga import eager
        agent = _build_agent()
        names = eager.eligible_tools(agent)
        _p(f"\nEligible (safe, configured, not yet attempted): {len(names)}")
        for n in names:
            _p(f"  - {n}")
        if not names:
            return 0
        if not eager.is_enabled() and not args.yes:
            _p("\nEAGER is disabled. Run 'vaf ww eager on' first, or pass --yes to train anyway.")
            return 2
        results = [_train_one(agent, n, quick=args.quick, verbose=args.verbose) for n in names]
        _p("\n" + "=" * 60)
        _p(f"{'TOOL':<28} RESULT")
        _p("-" * 60)
        for r in results:
            _p(f"{r.get('tool', '?'):<28} {_summary_line(r)}")
        return 0
    _p(f"Unknown eager action: {action}")
    return 2


def cmd_teacher(args) -> int:
    from vaf.whare_wananga import teacher
    action = getattr(args, "action", None) or "status"
    if action == "on":
        teacher.set_enabled(True)
        _p("Teacher/Noho enabled (whare_wananga_teacher_enabled=true). After a weak LOCAL training "
           "run, a stronger configured API model co-learns the tool (opt-in, serialized, rate-limited).")
        return 0
    if action == "off":
        teacher.set_enabled(False)
        _p("Teacher/Noho disabled.")
        return 0
    if action == "status":
        st = teacher.status()
        tea = st.get("teacher")
        _p(f"Teacher enabled : {st.get('enabled')}")
        _p(f"available       : {teacher.teacher_available()}  (needs a local student + a configured API)")
        _p(f"teacher model   : {(tea[0] + '/' + tea[1]) if tea else '(none configured)'}")
        return 0
    _p(f"Unknown teacher action: {action}")
    return 2


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="vaf-ww", description="Whare Wananga tool self-learning CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pt = sub.add_parser("train", help="train one or more tools (foreground, live)")
    pt.add_argument("tools", nargs="*", help="tool names (omit with --all)")
    pt.add_argument("--all", action="store_true", help="train every tool in a queue")
    pt.add_argument("--force", action="store_true", help="with --all, retrain already-learned tools too")
    pt.add_argument("--quick", action="store_true", help="small batches for a fast smoke test")
    pt.add_argument("-v", "--verbose", action="store_true", help="print every probe")
    pt.set_defaults(func=cmd_train)

    pr = sub.add_parser("retrain", help="alias for train (a run is a fresh assessment)")
    pr.add_argument("tools", nargs="*")
    pr.add_argument("--all", action="store_true")
    pr.add_argument("--force", action="store_true")
    pr.add_argument("--quick", action="store_true")
    pr.add_argument("-v", "--verbose", action="store_true")
    pr.set_defaults(func=cmd_train)

    pl = sub.add_parser("list", help="list learned tools + state")
    pl.set_defaults(func=cmd_list)

    ps = sub.add_parser("show", help="show the three baskets for a tool")
    ps.add_argument("tool")
    ps.set_defaults(func=cmd_show)

    pd = sub.add_parser("delete", help="delete a tool's stored knowledge")
    pd.add_argument("tool")
    pd.set_defaults(func=cmd_delete)

    pe = sub.add_parser("eager", help="opt-in proactive training of safe tools (status|on|off|scan)")
    pe.add_argument("action", nargs="?", choices=["status", "on", "off", "scan"], default="status")
    pe.add_argument("--quick", action="store_true", help="with scan: small batches (fast)")
    pe.add_argument("--yes", action="store_true", help="with scan: train even if EAGER is disabled")
    pe.add_argument("-v", "--verbose", action="store_true", help="with scan: print every probe")
    pe.set_defaults(func=cmd_eager)

    ptc = sub.add_parser("teacher", help="opt-in offline co-learning with a stronger API (status|on|off)")
    ptc.add_argument("action", nargs="?", choices=["status", "on", "off"], default="status")
    ptc.set_defaults(func=cmd_teacher)

    args = ap.parse_args(argv)
    if getattr(args, "all", False) and getattr(args, "tools", None):
        _p("Use either tool names or --all, not both.")
        return 2
    if args.cmd in ("train", "retrain") and not args.all and not args.tools:
        _p("Give at least one tool name, or use --all.")
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
