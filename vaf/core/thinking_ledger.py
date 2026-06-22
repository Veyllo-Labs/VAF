"""
Thinking-run Stufe-0 ledger: the deterministic housekeeping floor of a background run.

A run's "ledger" is the set of open automation notes/todos captured at run START. The completion gate
(in thinking_mode's outer loop) uses this to enforce that the run does not finish while a captured item
is still unhandled: each item must be ACTED+cleared (note deleted/handled, todo deleted/done) OR turned
into a tracked question raised THIS run (a thinking_request carrying its source id). This logic is pure
and side-effect free (just reads the per-user stores), so it is unit-testable without an agent.

User-isolation: every function takes the user's RAW scope id and passes it straight to the raw-scoped
stores (automation_planner, thinking_requests). The caller (the thinking run) resolves None -> the local
admin scope BEFORE building the ledger, so the ledger reads the exact same per-user data the agent's
tools write under. Mirrors thinking_requests.py.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# A ledger item is one Stufe-0 housekeeping obligation captured at run start.
#   kind:  "note" | "todo"
#   id:    the source note/todo id
#   label: short human text used only for the gate nudge
LedgerItem = Dict[str, Any]

_MAX_NUDGE_ITEMS = 5  # name at most this many items in a single gate nudge


def build_ledger(user_scope_id: Optional[str]) -> List[LedgerItem]:
    """Snapshot the open notes/todos at run START. Deterministic, no model judgment.

    The caller MUST pass an already-resolved scope (None -> admin done upstream)."""
    from vaf.core import automation_planner as ap

    ledger: List[LedgerItem] = []
    try:
        for n in ap.list_notes(user_scope_id, include_handled=False):
            nid = (n.get("id") or "").strip()
            if not nid:
                continue
            label = (n.get("title") or n.get("content") or "").strip().replace("\n", " ")[:80]
            ledger.append({"kind": "note", "id": nid, "label": label})
    except Exception:
        pass
    try:
        for t in ap.list_todos(user_scope_id):
            tid = (t.get("id") or "").strip()
            if not tid or bool(t.get("done")):
                continue
            label = (t.get("text") or "").strip().replace("\n", " ")[:80]
            ledger.append({"kind": "todo", "id": tid, "label": label})
    except Exception:
        pass
    return ledger


def _request_covers(user_scope_id: Optional[str], current_run_seq: int, *, note_id: str = "", todo_id: str = "") -> bool:
    """True if a tracked request was raised THIS run (within_runs=1) for the given source note/todo id."""
    from vaf.core import thinking_requests as treq

    try:
        recent = treq.list_requests(user_scope_id, within_runs=1, current_run_seq=current_run_seq)
    except Exception:
        return False
    for r in recent:
        if note_id and (r.get("source_note_id") or "") == note_id:
            return True
        if todo_id and (r.get("source_todo_id") or "") == todo_id:
            return True
    return False


def item_resolved(user_scope_id: Optional[str], item: LedgerItem, current_run_seq: int) -> bool:
    """True if this captured ledger item has been handled during the run.

    note: id no longer in list_notes(include_handled=False) (deleted OR set_note_handled) OR a request
          raised this run carries source_note_id == id.
    todo: id no longer in list_todos (deleted) OR that todo's done == True OR a request raised this run
          carries source_todo_id == id."""
    from vaf.core import automation_planner as ap

    kind = item.get("kind")
    iid = (item.get("id") or "").strip()
    if not iid:
        return True  # nothing to track

    if kind == "note":
        try:
            still_open = any((n.get("id") or "") == iid for n in ap.list_notes(user_scope_id, include_handled=False))
        except Exception:
            still_open = True
        if not still_open:
            return True
        return _request_covers(user_scope_id, current_run_seq, note_id=iid)

    if kind == "todo":
        try:
            todos = ap.list_todos(user_scope_id)
        except Exception:
            todos = []
        match = next((t for t in todos if (t.get("id") or "") == iid), None)
        if match is None:  # deleted
            return True
        if bool(match.get("done")):
            return True
        return _request_covers(user_scope_id, current_run_seq, todo_id=iid)

    return True  # unknown kind -> never block


def unresolved_items(user_scope_id: Optional[str], ledger: List[LedgerItem], current_run_seq: int) -> List[LedgerItem]:
    """The ledger items that are still not handled."""
    return [it for it in (ledger or []) if not item_resolved(user_scope_id, it, current_run_seq)]


def build_gate_nudge(items: List[LedgerItem]) -> str:
    """A targeted, SPECIFIC nudge naming the unresolved items. No generic phrasing — the model gets the
    exact ids so it can act on them or ask one specific question carrying the source id."""
    shown = [it for it in (items or []) if (it.get("id") or "").strip()][:_MAX_NUDGE_ITEMS]
    lines = [
        "[System: you called thinking_done, but you have NOT yet handled every item the user saved. "
        "Do NOT finish until each of these is acted-on or asked about:",
    ]
    for it in shown:
        kind = it.get("kind") or "item"
        label = (it.get("label") or "").strip() or "(no text)"
        lines.append(f'- {kind} [{it.get("id")}]: "{label}"')
    extra = len(items) - len(shown)
    if extra > 0:
        lines.append(f"- (+{extra} more)")
    lines.append(
        "For EACH: either ACT on it and clear it "
        "(delete_automation_note(note_id=...) / delete_automation_todo(todo_id=...)), "
        "OR ask ONE specific question about it via "
        "ask_user(message=..., source_note_id=\"<id>\") / source_todo_id=\"<id>\" "
        "(or thinking_done(message=..., source_note_id=\"<id>\")). "
        "Only if an item genuinely cannot be acted on or asked about, say so explicitly in your "
        "thinking_done summary. Then call thinking_done.]"
    )
    return "\n".join(lines)
