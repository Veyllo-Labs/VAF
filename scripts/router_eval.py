#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Measure the tool router on fixed cases, with the configured provider.

Runs ``Agent._select_turn_tools``, the selection a chat turn runs, on twenty cases: ten single
messages in a fresh chat (set A) and ten follow-ups whose earlier turn is in the history (set
B). A case passes when every must-group has at least one of its tools in the final set the
model would be offered. Only the router's own LLM call is made: no main-model call, no tool
runs, nothing is sent.

    venv/bin/python scripts/router_eval.py --label baseline --out router_baseline.json

The cases use synthetic people and addresses. What a case expects is the smallest tool set
that can do the job; several tools in one group are equivalent ways to do it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# (id, message, must-groups, earlier turns). An earlier turn is (user, tools it used, answer).
CASES = [
    ("A1", "Schick Bob per WhatsApp, dass ich 10 Minuten später komme.",
     [["send_whatsapp"], ["get_contact", "list_contacts"]], []),
    ("A2", "Was steht morgen in meinem Kalender?",
     [["list_calendar_events"]], []),
    ("A3", "Wie wird das Wetter am Wochenende in Hamburg?",
     [["web_search", "research_agent", "webfetch", "browser_agent"]], []),
    ("A4", "Hat Alice mir auf Telegram geantwortet?",
     [["read_telegram_chat", "find_telegram_messages", "inbox"]], []),
    ("A5", "Prüf per SSH auf meinem Server, ob der Ordner /var/backups noch zu groß ist.",
     [["ssh"]], []),
    ("A6", "Erinnere mich morgen früh um 8 an den Zahnarzttermin.",
     [["schedule_reminder", "set_timer", "create_automation"]], []),
    ("A7", "Lies die Datei bericht.pdf aus meinem Workspace und fass den Report kurz zusammen.",
     [["read_file", "document_viewer", "librarian_agent"]], []),
    ("A8", "Leg eine Automation an, die mir jeden Montag um 8 Uhr die Termine der Woche schickt.",
     [["create_automation"]], []),
    ("A9", "Such in meinen Mails die Rechnung der Stadtwerke und leite sie an buchhaltung@example.com weiter.",
     [["find_mail", "inbox", "read_mail"], ["forward_mail"]], []),
    ("A10", "Schreib in den Raum 'Projekt X', dass ich heute später komme.",
     [["room_send"]], []),

    ("B1", "Verschieb das zweite auf 16 Uhr.",
     [["update_calendar_event"]],
     [("Was steht morgen in meinem Kalender?", ["list_calendar_events"],
       "Morgen hast du zwei Termine: 10:00 Zahnarzt und 15:00 Teammeeting.")]),
    ("B2", "Antworte ihm, dass es passt.",
     [["reply_mail"]],
     [("Such mir die letzte Mail von Bob.", ["find_mail", "read_mail"],
       "Bob hat gestern geschrieben: 'Angebot Gartenbau - passt dir Donnerstag?'")]),
    ("B3", "Schick das Alice auf WhatsApp.",
     [["send_whatsapp"], ["get_contact", "list_contacts"]],
     [("Wie ist das Wetter in Berlin?", ["web_search"],
       "In Berlin ist es sonnig bei 22 Grad, abends leichter Wind.")]),
    ("B4", "Lösch die erste.",
     [["delete_automation"]],
     [("Zeig mir meine Automationen.", ["list_automations"],
       "Du hast zwei Automationen: 'Wetter 07:15' und 'Kalender-Check 08:20'.")]),
    ("B5", "Führ es auf meinem Server aus.",
     [["ssh"]],
     [("Schreib mir ein Python-Skript, das CSV-Dateien zusammenführt.", ["coding_agent"],
       "Fertig: merge_csv.py liegt in deinem Workspace.")]),
    ("B6", "Trag bei ihr ein, dass sie am 3. Mai Geburtstag hat.",
     [["update_contact"]],
     [("Wer ist eigentlich Carol?", ["get_contact"],
       "Carol ist in deinen Kontakten: Carol Meyer, Beispiel GmbH.")]),
    ("B7", "Füg noch Butter hinzu.",
     [["edit_file", "write_file"]],
     [("Lies bitte notes.txt.", ["read_file"],
       "notes.txt enthält eine Einkaufsliste: Milch, Brot, Eier.")]),
    ("B8", "Und erinner mich eine Stunde vorher.",
     [["schedule_reminder", "set_timer", "update_calendar_event", "create_automation"]],
     [("Welche Termine habe ich diese Woche?", ["list_calendar_events"],
       "Am Mittwoch um 14:00 hast du ein Meeting mit Dave.")]),
    ("B9", "Mach daraus ein Word-Dokument.",
     [["document_writer", "document_agent"]],
     [("Was gibt es Neues bei Rust?", ["web_search"],
       "Kurzüberblick: neue Edition, schnellere Kompilierung, besseres async.")]),
    ("B10", "Antworte ihm mit ja.",
     [["send_whatsapp"]],
     [("Zeig mir den WhatsApp-Chat mit Dave.", ["read_whatsapp_chat"],
       "Dave hat zuletzt geschrieben: 'Kommst du morgen?'")]),
]


def _reset(agent):
    agent.history = []
    agent._recent_tools.clear()
    agent._note_tools_this_turn = set()
    agent._active_tools = None
    agent.context_manager.intent = type(agent.context_manager.intent)()


def _add_earlier_turn(agent, n, user, tools, answer):
    agent.context_manager.update_intent(user)
    agent.history.append({"role": "user", "content": user})
    calls = [{"id": f"call_{n}_{i}", "type": "function",
              "function": {"name": t, "arguments": "{}"}} for i, t in enumerate(tools)]
    agent.history.append({"role": "assistant", "content": "", "tool_calls": calls})
    for c in calls:
        agent.history.append({"role": "tool", "tool_call_id": c["id"],
                              "name": c["function"]["name"], "content": "ok"})
    agent.history.append({"role": "assistant", "content": answer})
    for t in tools:
        agent._record_tool_used(t)


def run(label: str, only: str) -> dict:
    from vaf.cli.tui import UI
    from vaf.core.agent import Agent

    agent = Agent(register_signals=False, run_kind="chat")
    events: list = []
    real_event = UI.event

    def capture(type_name, title, style="info", end="\n"):
        if type_name == "Router":
            events.append(str(title))
    UI.event = staticmethod(capture)

    results = []
    try:
        for cid, message, must, earlier in CASES:
            if only != "all" and not cid.startswith(only):
                continue
            _reset(agent)
            for n, (user, tools, answer) in enumerate(earlier):
                _add_earlier_turn(agent, n, user, tools, answer)
            agent.context_manager.update_intent(message)
            agent.history.append({"role": "user", "content": message})
            events.clear()
            t0 = time.monotonic()
            agent._select_turn_tools(message, 2000, 128000)
            secs = time.monotonic() - t0
            # None means ALL tools: resolve it to what the model would be offered, so the case
            # is scored against real names and its size is the real count.
            all_tools = agent._active_tools is None
            final = list(agent.visible_tools()) if all_tools else list(agent._active_tools)
            missing = [g for g in must if not any(t in final for t in g)]
            results.append({
                "id": cid, "message": message, "pass": not missing,
                "missing": missing, "final": final, "size": len(final), "all_tools": all_tools,
                "router_events": list(events), "seconds": round(secs, 1),
            })
    finally:
        UI.event = real_event

    return {"label": label, "provider": getattr(agent.api_backend, "provider_name", None),
            "results": results}


def _llm_picks(events) -> list:
    for e in events:
        if e.startswith("LLM-based:"):
            return [t.strip() for t in e.split(":", 1)[1].split(",") if t.strip()]
    return []


def score(report: dict) -> dict:
    """Three readings beyond pass/fail, all taken from the recorded run:
    - llm_alone: would the case pass on the router LLM's own picks plus the earlier turn's
      tools, without the keyword table? (Shows what the keywords carried.)
    - plan_tool: is update_working_memory offered? The plan gate refuses a write tool until a
      plan is in the working memory, and this is the tool that writes it.
    - extras: task tools in the set that are neither expected, nor from the earlier turn, nor
      offered on every turn."""
    from vaf.core.agent import _TURN_RIDERS
    every_turn = set(_TURN_RIDERS) | {"list_tools", "search_tools", "analyze_image", "send_to_user"}
    cases = {c[0]: c for c in CASES}
    for r in report["results"]:
        _cid, _msg, must, earlier = cases[r["id"]]
        recent = {t for _u, tools, _a in earlier for t in tools}
        own = set(_llm_picks(r["router_events"])) | recent
        r["llm_alone_pass"] = all(any(t in own for t in g) for g in must)
        r["plan_tool"] = "update_working_memory" in r["final"]
        wanted = {t for g in must for t in g} | recent | every_turn
        # Nothing was chosen when every tool is offered, so nothing counts as an extra.
        r["extras"] = [] if r.get("all_tools") else [t for t in r["final"] if t not in wanted]
    res = report["results"]
    report["summary"] = {
        s: {"cases": sum(1 for r in res if r["id"].startswith(s)),
            "pass": sum(1 for r in res if r["id"].startswith(s) and r["pass"]),
            "llm_alone": sum(1 for r in res if r["id"].startswith(s) and r["llm_alone_pass"]),
            "plan_tool": sum(1 for r in res if r["id"].startswith(s) and r["plan_tool"]),
            "extras": sum(len(r["extras"]) for r in res if r["id"].startswith(s))}
        for s in ("A", "B")}
    return report


def show(report: dict) -> None:
    print(f"\n=== router_eval {report['label']} ({report['provider']}) ===")
    for r in report["results"]:
        mark = "PASS" if r["pass"] else "FAIL"
        miss = "" if r["pass"] else "  missing: " + " | ".join("/".join(g) for g in r["missing"])
        alone = "llm-alone ok" if r["llm_alone_pass"] else "llm-alone MISS"
        plan = "plan-tool" if r["plan_tool"] else "NO plan-tool"
        print(f"{r['id']:<4} {mark}  {r['size']:>2} tools  {r['seconds']:>5}s  {alone}  {plan}{miss}")
        for e in r["router_events"]:
            print(f"       {e[:160]}")
        if r["extras"]:
            print(f"       extras: {', '.join(r['extras'])}")
        if r.get("all_tools"):
            print(f"       final: ALL ({r['size']} tools)")
        else:
            print(f"       final: {', '.join(r['final'])}")
    for s, v in report["summary"].items():
        n = v["cases"]
        if not n:
            print(f"=== set {s}: not run ===")
            continue
        print(f"=== set {s}: pass {v['pass']}/{n}, llm-alone {v['llm_alone']}/{n}, "
              f"plan-tool {v['plan_tool']}/{n}, extras {v['extras']} ===")
    sys.stdout.flush()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--label", default="run")
    ap.add_argument("--out", default="")
    ap.add_argument("--only", default="all", help="A, B or all")
    ap.add_argument("--show", default="", help="print a saved report instead of running")
    args = ap.parse_args()
    if args.show:
        report = json.loads(Path(args.show).read_text(encoding="utf-8"))
    else:
        report = run(args.label, args.only)
    score(report)
    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    show(report)
    return 0


if __name__ == "__main__":
    code = main()
    os._exit(code)
