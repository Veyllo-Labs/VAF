# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""ask_user tool: ONE clean question to the user, in three lanes.

The background thinking run uses this to contact the user with ONE clean, user-facing message - a
specific question or a concrete proposal (e.g. "Soll ich dir eine Erinnerung einrichten?"). The
user-facing text is the explicit `message` argument, so the agent's chain-of-thought can never leak
into the chat (the old heuristic scraped the last assistant text and leaked reasoning).

The call also records a tracked request (status: asked) so the next run does not re-ask, and the main
agent can pick it up and carry it out when the user replies (see thinking_requests + chat_step pickup).
A scheduled automation hands its whole working context over the same way (a handoff bundle).

In a CHAT the person is right there, so nothing is tracked: the question, with `options` to pick
from, is the turn's answer and the turn ends at it (`ends_turn`, BaseTool.turn_closing). The
person's reply is simply the chat's next message, typed or picked. The turn is not held open for
it: one chat worker serves every chat, and the person may answer in an hour. The options travel as
a JSON line in the tool result, which is what the history holds and the web chat reads to draw
them as buttons (web/components/chat/askChoices.ts); every other surface shows the numbered list
the closing carries, and a number or a word is an answer there.
"""
import json
from typing import Any, List, Optional

from vaf.tools.base import BaseTool

#: The first words of a chat question's tool result. The web chat keys on them
#: (web/components/chat/askChoices.ts, pinned by tests/test_ask_user_chat.py).
ASKED_PREFIX = "ASKED THE USER."
MAX_OPTIONS = 8


def normalize_options(raw: Any) -> List[str]:
    """The options as short labels, in order, each once. Accepts a list of strings or of
    {"label": ...} objects, or a JSON / newline-separated string (what a weak model sends)."""
    if isinstance(raw, str):
        text = raw.strip()
        try:
            raw = json.loads(text) if text[:1] in "[{" else text.splitlines()
        except Exception:
            raw = text.splitlines()
    if isinstance(raw, dict):
        raw = [raw]
    out: List[str] = []
    for item in raw or ():
        if isinstance(item, dict):
            item = item.get("label") or item.get("text") or item.get("value") or ""
        label = " ".join(str(item or "").split())
        if label and label not in out:
            out.append(label)
    return out


def options_block(options: List[str]) -> str:
    """The numbered list a surface without buttons shows, and the web chat cuts off again."""
    return "\n".join(f"{i}. {opt}" for i, opt in enumerate(options, 1))


def chat_closing(question: str, options: List[str]) -> str:
    """What the person reads: the question, then the options as a numbered list."""
    question = str(question or "").strip()
    return f"{question}\n\n{options_block(options)}" if options else question


class AskUserTool(BaseTool):
    """Ask the user one clean question: tracked in a background run, the turn's end in a chat."""

    name = "ask_user"
    category    = "automations"
    permission_level = "system"
    side_effect_class = "reversible"
    ends_turn = True
    description = (
        "Ask the user ONE clean, user-facing question or concrete proposal and wait for the answer "
        "(e.g. 'Es wird heute heiss in Berlin (34 Grad). Soll ich dir eine Erinnerung "
        "einrichten, deine Kleidung zu checken?'). Put ONLY the final, polished text in `message` - no "
        "reasoning, no tool talk, no 'I should...'. When the answer is one of a few choices, list them "
        "in `options`; the user can still answer in their own words. Only when you genuinely need the "
        "user's decision; do not also write the question as plain text. "
        "In a CHAT with the user, your turn ENDS with this call: the question is shown with the options "
        "to pick, and the answer arrives as the user's next message. "
        "In a background run the system delivers it, tracks it as a request, and waits for the reply; "
        "use it at most once per run. "
        "In a scheduled automation this is a HIGH BAR: use it ONLY for a genuine blocker or an important "
        "clarification you cannot resolve on your own - NEVER for status ('starting', 'working on it'). If "
        "you can proceed on a reasonable assumption, do so and note the assumption in your result instead "
        "of asking. When you do ask, your full working context is handed to the user's main agent, which "
        "continues the task after they reply."
    )
    parameters = {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "The final, user-facing message. Short, natural, no reasoning or tool talk.",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    f"Optional: 2 to {MAX_OPTIONS} short answers the user can pick from (e.g. "
                    "['Variante A: schnell', 'Variante B: gruendlich']). Leave out for an open question."
                ),
            },
            "proposed_action": {
                "type": "string",
                "description": (
                    "Optional: a short note of what you would do if the user agrees (e.g. 'create a "
                    "reminder automation: check clothes when Berlin forecast > 30C'). The main agent "
                    "uses this to carry the task out after the user confirms."
                ),
            },
            "details": {
                "type": "string",
                "description": (
                    "IMPORTANT when your message references something you found or prepared (e.g. 'I "
                    "found 15 cooling methods, want the list?'): put the ACTUAL content here - the real "
                    "list/facts/findings. It is NOT shown to the user, but it is handed to the main agent "
                    "so that when the user asks for specifics it answers with YOUR real findings instead "
                    "of making something up. Never tease content without filling this."
                ),
            },
            "source_note_id": {
                "type": "string",
                "description": (
                    "Optional: the id of the automation NOTE this question is about (from "
                    "list_automation_notes). When the user confirms, that note is marked handled so it "
                    "stops re-surfacing in future runs. Pass it whenever your question stems from a note."
                ),
            },
            "source_todo_id": {
                "type": "string",
                "description": "Optional: the id of the automation TODO this question is about; marked done on confirm.",
            },
            "user_scope_id": {
                "type": "string",
                "description": "Internal: user scope id injected by the framework. Leave blank.",
            },
        },
        "required": ["message"],
    }

    def run(self, **kwargs) -> str:
        import os

        message = (kwargs.get("message") or "").strip()
        if not message:
            return "Error: message must not be empty."
        options = normalize_options(kwargs.get("options"))
        if len(options) > MAX_OPTIONS:
            return (f"Error: at most {MAX_OPTIONS} options, got {len(options)}. Offer the likeliest "
                    "few; the user can always answer in their own words.")

        # In a scheduled automation, this is a background handoff to the user's MAIN agent: store the
        # automation agent's FULL working context as a bundle, record the linked request, then end. The
        # main agent loads the bundle and continues when the user replies. (The thinking-mode path below
        # is untouched.)
        # The branch keys on the CALLING agent's per-instance run kind, never on the
        # process-global env: env is shared across threads, so a concurrent automation
        # made a thinking run's question take this handoff path (live 2026-07-13, and
        # twice before in the same 07:00 window). Env fallback only when no agent
        # instance was injected (direct tool invocation, tests).
        _agent = kwargs.get("_agent")
        _MISSING = object()
        _rk = getattr(_agent, "_run_kind", _MISSING) if _agent is not None else _MISSING
        if _rk is not _MISSING:
            # Real agents always carry _run_kind (set in __init__); None means a
            # plain chat agent - explicitly NOT an automation, even mid-window.
            in_automation = _rk == "automation"
        else:
            in_automation = os.environ.get("VAF_IN_AUTOMATION", "").strip() in ("1", "true", "yes")
        if _rk is not _MISSING and _rk == "chat":
            return self._ask_in_chat(message, options)
        # The background lanes deliver a text: the options become its numbered list.
        if options:
            kwargs["message"] = message = chat_closing(message, options)
        if in_automation:
            return self._run_automation_handoff(**kwargs)

        try:
            # Single shared delivery path (also used by the thinking_done fallback): records a tracked
            # request, sets waiting_for_reply, and emits the exact text to the Web UI. The user's REAL
            # data scope is resolved inside (never the normalized "default" key, or the main agent would
            # not find the request).
            from vaf.core.thinking_mode import deliver_tracked_message
        except Exception as e:  # pragma: no cover - defensive
            return f"Error: ask_user is unavailable: {e}"

        req = deliver_tracked_message(
            kwargs.get("user_scope_id"),
            message,
            proposed_action=kwargs.get("proposed_action"),
            source_note_id=kwargs.get("source_note_id"),
            source_todo_id=kwargs.get("source_todo_id"),
            username=kwargs.get("username"),
            details=kwargs.get("details"),
        )
        if not req:
            # message was non-empty (checked above) but delivery returned nothing. WHY depends on the
            # per-run proactive MODE - give mode-specific guidance so the model stops retrying blindly.
            try:
                from vaf.core.thinking_mode import get_proactive_mode, take_reject_reason
                _reason = take_reject_reason(kwargs.get("user_scope_id"))
                _mode = get_proactive_mode(kwargs.get("user_scope_id"))
            except Exception:
                _reason = ""
                _mode = "grounded"
            if _reason == "too_similar":
                # The semantic-dedup gate rejected it: too close to a question already asked/declined.
                # Steer the model to a CLEARLY different area instead of rewording the same topic.
                # The retry invitation is BUDGETED, and the budget is enforced in the gate itself, not
                # by this sentence: an unbounded "call it again" is what turned a mis-calibrated gate
                # into a 12-turn loop. Say how many are left so the model stops guessing.
                try:
                    from vaf.core.thinking_mode import get_ask_rejects, ask_rejects_exhausted
                    _spent = get_ask_rejects(kwargs.get("user_scope_id"))
                    _last = ask_rejects_exhausted(kwargs.get("user_scope_id"))
                except Exception:
                    _spent, _last = 0, False
                _tail = (
                    "This was your last retry: call ask_user once more and the question will be sent "
                    "exactly as you write it, so make it a good one."
                    if _last else
                    "Call ask_user again now with a fresh, different question."
                )
                return (
                    f"Not sent (attempt {_spent}): that question is too similar to one you already asked "
                    "recently. Pick a CLEARLY different area - e.g. a hobby, daily life, people in their "
                    "life, learning, or future goals - NOT work/VAF if that was your recent topic. "
                    + _tail
                )
            if _mode == "off":
                # Not in a proactive step (gather / forced-resolution), or a message was already delivered
                # this run. Retrying cannot succeed - stop now.
                return (
                    "Not sent: you are not in a proactive step right now (or a message was already "
                    "delivered this run). Do NOT retry ask_user - call thinking_done now."
                )
            # grounded step: the evidence gate dropped it (not grounded in real retrieved memory).
            return (
                "Not sent: a proactive suggestion must quote the REAL memory it is based on, verbatim, in "
                "`message` or `details` (paraphrasing or inventing is rejected). If you have nothing "
                "grounded, call thinking_done - you will then ask the user a get-to-know question instead."
            )
        if req.get("delivered"):
            return (
                f"Message delivered to the user (tracked as request {req['id']}). Stop now and wait for "
                "their reply - do not ask anything else this run; call thinking_done."
            )
        return (
            f"Recorded your question (request {req['id']}) and set the waiting state, but the user's chat "
            "was not reachable right now; it will surface on their next visit. Call thinking_done now."
        )

    @staticmethod
    def _ask_in_chat(message: str, options: List[str]) -> str:
        """The person is right here: the question is the turn's answer. The JSON line is for the
        web chat (buttons) and the model's own next turn alike, so neither has to guess."""
        spec = json.dumps({"question": message, "options": options}, ensure_ascii=False)
        return (f"{ASKED_PREFIX} Your turn ends here; the answer arrives as their next message, "
                f"typed or picked.\n{spec}")

    def turn_closing(self, args, result) -> Optional[str]:
        """Only a question actually asked in a chat ends the turn: a background delivery reports
        to its run, and an error has to be seen by the model."""
        spec = question_of(result)
        return chat_closing(spec["question"], spec["options"]) if spec else None

    def _run_automation_handoff(self, **kwargs) -> str:
        """A scheduled automation that hit a genuine blocker hands off its FULL working context to the
        user's main agent. Stores the agent history as a handoff bundle + records the linked request, then
        ends - the main agent loads the bundle and continues with full context when the user replies."""
        message = (kwargs.get("message") or "").strip()
        try:
            from vaf.core.handoff_bundle import deliver_handoff
        except Exception as e:  # pragma: no cover - defensive
            return f"Error: ask_user handoff is unavailable: {e}"

        _agent = kwargs.get("_agent")
        _history = None
        if _agent is not None:
            try:
                _history = list(getattr(_agent, "history", None) or [])
            except Exception:
                _history = None

        # session_id left as injected (None for automations) -> deliver_handoff anchors to the user's
        # latest real web session, so the question lands in the main chat where the reply is picked up.
        req = deliver_handoff(
            kwargs.get("user_scope_id"),
            message=message,
            proposed_action=kwargs.get("proposed_action"),
            details=kwargs.get("details"),
            history=_history,
            session_id=kwargs.get("session_id"),
            username=kwargs.get("username"),
        )
        if not req:
            return "Error: the handoff message was empty and was not sent."
        if req.get("delivered"):
            return (
                f"Handoff delivered to the user (request {req['id']}, bundle {req.get('bundle_id')}). Stop "
                "now - your full working context is saved; the user's main agent continues with it when they "
                "reply. Do not ask or do anything else."
            )
        return (
            f"Handoff recorded (request {req['id']}, bundle {req.get('bundle_id')}); the user's chat was not "
            "reachable right now, so it surfaces on their next visit. Stop now - do not ask anything else."
        )


def question_of(result: Any) -> Optional[dict]:
    """The {"question", "options"} a chat question's tool result carries, or None."""
    text = str(result or "")
    if not text.startswith(ASKED_PREFIX):
        return None
    try:
        spec = json.loads(text.split("\n", 1)[1])
    except Exception:
        return None
    if not isinstance(spec, dict) or not str(spec.get("question") or "").strip():
        return None
    return {"question": str(spec["question"]).strip(), "options": normalize_options(spec.get("options"))}
