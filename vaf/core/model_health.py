# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Is the model stuck in this chat, or did a tool merely fail? One score per chat.

A small local model in a long chat can stop acting: the person asks for work, the model
answers that it will do it and calls nothing, the person says "yes, go", and the same reply
comes back word for word. A tool that RAN and failed (a timeout, a refusal) is a different
thing: that is a normal error, and the person reads its result. This module tells the two
apart with signals that need no model call and work in any language the chat is written in,
and adds them up per chat, so one odd turn costs nothing and a series is seen.

THE SIGNALS. Each one counts at most once per turn, and only on a turn in which no progress
tool ran (a tool other than the bookkeeping ones passed the plan gate; one that ran and
FAILED counts as progress, which is exactly the normal-error case):

==========================  ==============================================================  ======
signal                      fires when                                                      weight
==========================  ==============================================================  ======
reasoning_action_mismatch   the visible reply names a tool of the turn's task set            1
                            (the router's pick, without riders, discovery and
                            bookkeeping tools) as a whole word
action_tag_mismatch         an ``<Action>`` block matches a task tool with score >= 0.7      3
                            (only while ``action_tag_enabled`` is on)
step_repetition             the reply is >= 0.85 similar to the chat's previous text reply   3
                            (a bounded head AND tail of each, 1000 characters apiece)
language_drift              a reply of >= 40 characters is in another language than the      2
                            chat, both detected concretely
confirmation_unheeded       the person's message is a plain yes (<= 40 characters, no        1
                            question mark), in the chat's language
user_repeats                the person's message is >= 0.6 similar to their previous one     1
ungrounded                  result grounding found an unsupported claim in this turn         2
==========================  ==============================================================  ======

A QUESTION (a question mark of any script in the person's message, `vocab.QUESTION_MARKS`)
fires none of the three "should have acted" signals (reasoning_action_mismatch,
step_repetition, user_repeats), and a question turn is never re-run, never forced and never
noted: it asked for an answer, and a text answer is what it got. Measured before this rule:
four German follow-up questions about host_bash climbed 0, 1, 2, 3, mid on the fourth (mid on
the second with a near-identical answer), and the re-run forced a call of the tool asked
about. Drift, an unsupported claim and an <Action> promise still count on a question turn.

The names follow the published failure taxonomy for multi-agent systems (MAST,
arXiv 2503.13657): step repetition (1.3), loss of conversation history (1.4),
reasoning-action mismatch (2.6); premature termination (3.1) is the accumulation itself.

THE SCORE is a cumulative sum in the CUSUM shape (arXiv 2608.02464 measured that shape on
per-step telemetry, with no model judging it): a scored turn without progress sets
``S = max(0, S_prev + sum(weights) - K)``, a turn with progress sets ``S = 0``, and inside a
turn the provisional ``S'`` with the signals so far is what the agent reacts to. ``K = 1``
drains a turn that only names a tool, so a chat ABOUT tools never climbs. Levels: below 3
none, from 3 mid (the agent re-runs the turn once, forcing a tool call where the lane can),
from 8 high (after that re-run the turn ends with an honest note).

WHAT IS NEVER FORCED (`forceable`). The forced call is a real call, so the re-run forces only
task tools a person would not have to confirm and could take back: never a tool declared
`dangerous` or `system`, never one whose effect is `irreversible`, never one that sends to
somebody (a channel send tool, a mail send) and never one that deletes. Where the task holds
only such tools the re-run runs unforced, with the same note naming them; the measured
incident stays covered, because the sandbox it needed is `write` and `reversible`.

MEASURED on every stored chat of one installation (decrypted read-only, nothing written):
50 chats, 698 turns; 550 person turns scored in 33 chats, 318 of them without a progress
tool; 10 turns hand-labelled stuck, all in one long chat on a 4B local model. Replayed again
after the question rule (557 person turns, 257 of them questions, every tool name counted
as a task tool): the same turns reach mid or high as before, the stuck series' three
confirmations, so the rule cost no catch there, and no question turn acts.

=========  ===================  ===============
threshold  stuck turns caught   false crossings
=========  ===================  ===============
S >= 1     8/10                 3
S >= 3     6/10                 0
S >= 8     2/10                 0
=========  ===================  ===============

The highest score on any unlabelled turn was 1.0. What each gate is worth, measured by
removing it: tool naming at weight 2 lifted two chats that discussed tools to 5 (a false
mid); drift without the 40-character gate gave 9 false hits ("Gerne.", "danke" are detected
as English); a confirmation without the question-mark gate counted "sicher ?"; scoring timer
turns lifted the early part of the long chat to 10 on legitimate timer replies, which is why
only a person's own turn is scored. One similarity ratio over 2000 characters measured
0.9 ms; the reaction costs at most one model call per turn.

LANGUAGE. The chat's language is sticky: it is taken from a message of the person of at
least 20 characters whose language the agent's detector names, and kept in the ledger, so
a turn whose whole text is "ja" (detected as unclear) does not lose it. The detector
separates German, English and the scripts of Chinese, Japanese, Korean and Thai; a Turkish
or French chat reads as English on both sides, so drift never fires falsely there and can
miss there. The confirmation vocabulary is the one the voice confirmation uses
(`speaker_confirm.parse_reply`, 17 languages), never a second word list.

THE PREVIOUS MESSAGE is the history's last user message, never the stored intent, which the
model writes too (`update_intent` rides along on every turn): an injected page could
otherwise make "the person's previous request" whatever it liked. The re-run's note quotes
only the words of this turn; for a plain yes it points at the person's own earlier message
in the conversation instead of quoting anything back with system authority.

THE LEDGER. One small file per chat (`MainPersistenceManager.get_model_health`), holding the
score, the chat's language and the time, never a word of the chat; tagged ``LEDGER_FORMAT``.
A file with another tag is discarded (the score starts at 0), one without a tag is read. A
lane with no per-chat store starts each turn at 0: the score fails open. It is written on
every exit of a turn that answered (the final answer, a delegation to a sub-agent, a question
to the person, a draft), never on one that ended without an answer.

NAMED BOUNDARIES:

- LLM judge: not built. A no-LLM detector of hallucinated tool calls beat the strongest LLM
  judge by 35 per cent F1 at 1.5 ms against 17.7 s (arXiv 2610.32648), and a deterministic
  "was every required call made" check tripped 0 of 1825 healthy episodes (2608.02464). The
  judge of the guard this module replaced posted to a fixed local port and was itself the
  local counter-example. A later stage at high score only, on API providers, if a
  measurement shows misses these signals cannot see.
- Token entropy: not built. llama.cpp refuses ``logprobs`` with tools and streaming (the
  server binary's own message), so the signal would exist on vLLM and some API providers
  only, and would need its own measurement.
- Handing a stuck local turn to a paid API model: not built. It spends money nobody asked
  to spend, and with the local model as main provider the chat loop has no failover chain
  (docs/llm/PROVIDER_MODES.md); it would be new machinery, and never a second concurrent
  local inference. The honest note tells the person what helps instead.
- Tool-call loops stay with the redundant-call filter, anti-spin and the no-progress streak
  in the agent loop: this score covers turns WITHOUT a progress tool, which those never see.
- Not on the facade. Measured N = 0: no harness file and no CLI file would call the scorer
  or read its verdict; the reaction and the note happen inside `chat_step`, which every lane
  runs (an embedder's `vaf.Agent` included), and the health row reads `metrics()` in core.
  The day a host needs the verdict for its own UI, the export is a field on `vaf.TurnStatus`.

Pure: no model client, no file access; the event counter behind `metrics()` is the only
state, in memory, counts only.
"""
from __future__ import annotations

import difflib
import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple

__all__ = [
    "LEDGER_FORMAT", "SIGNALS", "WEIGHTS", "K", "MID", "HIGH",
    "NONE", "MID_LEVEL", "HIGH_LEVEL",
    "REACTION_NONE", "REACTION_FORCED", "REACTION_RETRY", "REACTION_NOTE",
    "State", "Verdict", "evaluate", "commit", "level_of", "chat_language",
    "is_confirmation", "is_question", "forceable", "names_a_tool", "similarity",
    "from_payload", "to_payload",
    "retry_note", "STUCK_NOTE", "stuck_note", "pick_note", "note_language",
    "record", "metrics", "reset_metrics",
]

# The ledger's format tag (see THE LEDGER above).
LEDGER_FORMAT = "modelhealth-1-d52e8a"

# In the order they are reported in the log line and the verdict.
SIGNALS: Tuple[str, ...] = (
    "reasoning_action_mismatch",
    "action_tag_mismatch",
    "step_repetition",
    "language_drift",
    "confirmation_unheeded",
    "user_repeats",
    "ungrounded",
)

# Weights, measured on the corpus above. Naming a tool is the weakest signal on purpose: at 2
# two chats that only discussed tools climbed to a false mid. A repeat and an <Action> that
# promised a task tool are the strong ones: a healthy chat produced neither without progress.
WEIGHTS: Dict[str, float] = {
    "reasoning_action_mismatch": 1.0,
    "action_tag_mismatch": 3.0,
    "step_repetition": 3.0,
    "language_drift": 2.0,
    "confirmation_unheeded": 1.0,
    "user_repeats": 1.0,
    "ungrounded": 2.0,
}

# The drain per scored turn. 1 keeps a turn that only names a tool at 0 forever (the failure
# mode of the guard this replaced), and lets the measured stuck series climb to mid on its
# second turn and to high on its third.
K = 1.0

# Mid: the measured threshold with no false crossing in 550 person turns (6 of 10 stuck
# turns caught). High: reached by 2 of 10, only deep in a stuck series.
MID = 3.0
HIGH = 8.0

NONE, MID_LEVEL, HIGH_LEVEL = "none", "mid", "high"

REACTION_NONE = "none"        # scored, nothing done
REACTION_FORCED = "forced"    # the re-run with the tool call forced
REACTION_RETRY = "retry"      # the re-run on a lane that cannot force (tool_choice auto)
REACTION_NOTE = "note"        # the honest note under the reply

# Similarity gates. 0.85 caught the measured repeats (ratio 1.00) and no reply of a healthy
# chat; 2000 characters (head and tail) bound the cost (0.9 ms measured for one ratio).
REPEAT_RATIO = 0.85
REPEAT_CHARS = 2000      # compared as a 1000-character head plus a 1000-character tail
# A person saying the same thing again, case folded ("ja bitte fang an", "JA BITTE FANG AN").
USER_REPEAT_RATIO = 0.6
# Drift only on a reply long enough to have a language: "Gerne." and "danke" are detected as
# English (9 false hits without this gate).
DRIFT_MIN_CHARS = 40
# A plain yes is short; a longer message carries its own instruction.
CONFIRM_MAX_CHARS = 40
# The chat's language moves only on a message long enough for the detector to be sure of it.
LANGUAGE_MIN_CHARS = 20
# The high-confidence half of an <Action> match (`agent._match_action_to_tools`).
ACTION_MATCH_MIN = 0.7

# Never forced, whatever the router picked: a call that cannot be taken back or that a
# person has to confirm is never one the model is MADE to emit. A question about such a tool
# naming it in the answer is the normal case, and a forced call there would turn a question
# into an action (measured: four German follow-up questions about host_bash climbed to mid,
# and the request went out with tool_choice "required" narrowed to it). The re-run for those
# stays unforced, with the same note.
NEVER_FORCED_PERMISSIONS = ("dangerous", "system")
NEVER_FORCED_SIDE_EFFECTS = ("irreversible",)
# Deleting is never forced either, also where a tool files its delete as reversible (a mail to
# the trash, an automation): the person did not see the call before it ran.
NEVER_FORCED_PREFIXES = ("delete_", "remove_")


@dataclass(frozen=True)
class State:
    """One chat's ledger: the score after its last scored turn and its sticky language."""
    score: float = 0.0
    language: Optional[str] = None


@dataclass(frozen=True)
class Verdict:
    """One generation's reading: the provisional score, its level, the signals of the turn so
    far (each once) and the chat's language this turn."""
    score: float
    level: str
    signals: Tuple[str, ...]
    chat_language: Optional[str]
    # The person's message asks something: answered in text, it got what it asked for, so the
    # turn is never re-run, never forced and never noted (see `evaluate`).
    asks: bool = False


def level_of(score: float) -> str:
    if score >= HIGH:
        return HIGH_LEVEL
    if score >= MID:
        return MID_LEVEL
    return NONE


def _concrete(code) -> Optional[str]:
    """A language code that names a language, two letters; None for an unclear one."""
    text = str(code or "").strip().lower().replace("_", "-").split("-", 1)[0]
    if not text or text == "auto":
        return None
    return text


def _detect(detect_language: Optional[Callable[[str], str]], text: str) -> Optional[str]:
    if detect_language is None:
        return None
    try:
        return _concrete(detect_language(text))
    except Exception:
        return None


def chat_language(prev: Optional[State], user_text: str,
                  detect_language: Optional[Callable[[str], str]],
                  fallback_language: Optional[str] = None) -> Optional[str]:
    """The chat's language for this turn: from the person's message when it is long enough
    and its language is clear, else the one the ledger kept, else the fallback (the agent's
    per-turn language) when that is concrete."""
    text = (user_text or "").strip()
    if len(text) >= LANGUAGE_MIN_CHARS:
        code = _detect(detect_language, text)
        if code:
            return code
    kept = _concrete(prev.language) if prev is not None else None
    return kept or _concrete(fallback_language)


def _bounded(text: str) -> str:
    """At most REPEAT_CHARS characters: the head AND the tail of a longer text, so two long
    replies that only open the same way are not a repeat."""
    text = text or ""
    if len(text) <= REPEAT_CHARS:
        return text
    half = REPEAT_CHARS // 2
    return text[:half] + text[-half:]


def similarity(a: str, b: str) -> float:
    """Ratio of two texts over a bounded head and tail (REPEAT_CHARS characters each)."""
    a, b = _bounded(a), _bounded(b)
    if not a.strip() or not b.strip():
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def names_a_tool(text: str, tool_names: Iterable[str]) -> bool:
    """Does the text name one of the tools as a whole word ("python_sandbox", not
    "python_sandbox_v2")?"""
    if not text:
        return False
    for name in tool_names:
        name = str(name or "").strip()
        if name and re.search(r"(?<![\w])" + re.escape(name) + r"(?![\w])", text, re.IGNORECASE):
            return True
    return False


def is_question(text: str) -> bool:
    """Does the person's message ask something? A question mark of any script anywhere in
    it (`vocab.QUESTION_MARKS`, the one multi-script set): "Was macht host_bash? Erklär mal."
    asks too. Language-independent and without a model."""
    from vaf.core.vocab import QUESTION_MARKS
    t = text or ""
    return any(mark in t for mark in QUESTION_MARKS)


def is_confirmation(text: str, language: Optional[str]) -> bool:
    """A plain yes in the chat's language (English is always matched alongside): short, no
    question mark ("sicher ?" asks, it does not confirm)."""
    t = (text or "").strip()
    if not t or len(t) > CONFIRM_MAX_CHARS or is_question(t):
        return False
    try:
        from vaf.core.speaker_confirm import parse_reply
        return parse_reply(t, language or "en") == ("yes", None)
    except Exception:
        return False


def evaluate(prev: Optional[State], *, reply: str, user_text: str,
             prev_reply: str = "", prev_user: str = "",
             task_tools: Sequence[str] = (),
             action_matches: Sequence[Tuple[str, float]] = (),
             grounding_hit: bool = False,
             detect_language: Optional[Callable[[str], str]] = None,
             fallback_language: Optional[str] = None,
             prior: Iterable[str] = (),
             progressed: bool = False) -> Verdict:
    """Score one generation of a turn. ``reply`` is the visible text (no think blocks),
    ``user_text`` the person's own words, ``prev_reply`` the chat's previous text reply and
    ``prev_user`` the person's previous message. ``prior`` are the signals this turn already
    fired: a signal counts once per turn, however many generations the turn has. A turn that
    ran a progress tool scores 0 and fires nothing.

    A QUESTION fires none of the three signals that read "should have acted": naming the tool
    asked about, answering a repeated question the same way and asking again are what a
    conversation ABOUT a tool looks like. Without this, follow-up questions about one tool
    climbed by one per turn and reached mid on the fourth (measured). Drift, an unsupported
    claim and an <Action> promise still count; the turn is never re-run (`Verdict.asks`)."""
    prev = prev or State()
    language = chat_language(prev, user_text, detect_language, fallback_language)
    asks = is_question(user_text)
    if progressed:
        return Verdict(0.0, NONE, (), language, asks)
    fired = set(s for s in prior if s in WEIGHTS)
    reply = reply or ""
    tools = [str(t) for t in task_tools if t]
    if tools and not asks and names_a_tool(reply, tools):
        fired.add("reasoning_action_mismatch")
    if any(name in tools and float(score or 0) >= ACTION_MATCH_MIN
           for name, score in (action_matches or ())):
        fired.add("action_tag_mismatch")
    if prev_reply and not asks and similarity(reply, prev_reply) >= REPEAT_RATIO:
        fired.add("step_repetition")
    if language and len(reply.strip()) >= DRIFT_MIN_CHARS:
        said = _detect(detect_language, reply)
        if said and said != language:
            fired.add("language_drift")
    if is_confirmation(user_text, language):
        fired.add("confirmation_unheeded")
    if prev_user and not asks and similarity((user_text or "").casefold(),
                                (prev_user or "").casefold()) >= USER_REPEAT_RATIO:
        fired.add("user_repeats")
    if grounding_hit:
        fired.add("ungrounded")
    signals = tuple(s for s in SIGNALS if s in fired)
    score = max(0.0, prev.score + sum(WEIGHTS[s] for s in signals) - K)
    return Verdict(score, level_of(score), signals, language, asks)


def forceable(name: str, permission_level: str, side_effect_class: str, *,
              outward: bool = False) -> bool:
    """May the re-run FORCE a call of this tool? Never for a tool a person has to confirm
    (`dangerous`, `system`), one whose effect cannot be taken back (`irreversible`), one that
    sends to somebody (``outward``: a channel send tool, a mail send) or deletes something.
    Read from the tool's declared contract (`tool_contract.resolve_tool_contract`)."""
    if outward:
        return False
    if str(permission_level or "").lower() in NEVER_FORCED_PERMISSIONS:
        return False
    if str(side_effect_class or "").lower() in NEVER_FORCED_SIDE_EFFECTS:
        return False
    return not str(name or "").lower().startswith(NEVER_FORCED_PREFIXES)


def commit(prev: Optional[State], verdict: Optional[Verdict], progressed: bool) -> State:
    """The ledger after a finished turn. Progress resets to 0, a tool that failed included;
    a turn with no scored generation drains by K like a turn without signals."""
    prev = prev or State()
    language = verdict.chat_language if verdict is not None else prev.language
    if progressed:
        return State(0.0, language)
    if verdict is None:
        return State(max(0.0, prev.score - K), language)
    return State(verdict.score, language)


def to_payload(state: State, now: Optional[datetime] = None) -> dict:
    """What the ledger file holds: the tag, the score, the language, the time. No text."""
    when = now or datetime.now(timezone.utc)
    return {
        "format": LEDGER_FORMAT,
        "score": float(state.score),
        "language": _concrete(state.language),
        "updated_at": when.isoformat(),
    }


def from_payload(payload) -> State:
    """The ledger read back; anything unreadable or with another tag starts at 0."""
    if not isinstance(payload, Mapping):
        return State()
    tag = payload.get("format")
    if tag is not None and tag != LEDGER_FORMAT:
        return State()
    try:
        score = float(payload.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    if not math.isfinite(score) or score < 0:
        score = 0.0
    return State(score, _concrete(payload.get("language")))


# ── what the agent says ──────────────────────────────────────────────────────────────────

def retry_note(user_text: str, task_tools: Sequence[str], *, confirmed: bool = False) -> str:
    """The system note of the one re-run. English like every correction the loop writes, and
    specific: it quotes what the person wrote this turn and names the tools that do it,
    because a generic "you did not call a tool" feeds retry loops while a note naming exactly
    what is missing lifted recovery from 23.9 to 37.0 per cent (arXiv 2608.10430). It also
    says the reasons the earlier reply gave come from no tool result: a small model repeats
    what it just read (arXiv 2608.23651), and in the measured incident it repeated an invented
    excuse.

    It quotes only this turn's own words. For a plain yes it points at the person's earlier
    message in the conversation instead of quoting a stored request: the stored intent can be
    written by the model itself (`update_intent`), and a note with system authority must never
    hand back as "the user's request" what an injected page made the model write."""
    asked = (user_text or "").strip()[:300]
    if confirmed:
        said = (f"the user confirmed: \"{asked}\" (what they confirmed is their own "
                "earlier message in the conversation above)")
    else:
        said = f"the user asked: \"{asked}\""
    tools = [str(t) for t in task_tools if t]
    if len(tools) == 1:
        which = f": the tool {tools[0]},"
    elif tools:
        which = f": one of {', '.join(tools)},"
    else:
        which = ","
    return (
        f"No tool ran this turn, although {said}. Your reply above stays on the user's "
        "screen; do not repeat it, and do not repeat any reason given above: no tool result "
        f"reported it. Call the tool that does this now{which} with real arguments, and then "
        "report its actual result."
    )


# The line under a reply when the chat stays stuck after the re-run. The seven languages of
# the UI catalogue (web/messages); English for every other one.
STUCK_NOTE: Dict[str, str] = {
    "de": "Hinweis: Ich schaffe es in diesem Chat gerade nicht, deine Anweisung auszuführen. "
          "Meist hilft ein neuer Chat; passiert das öfter, kommt ein größeres Modell damit "
          "besser zurecht.",
    "en": "Note: I keep failing to carry out your request in this chat. Starting a new chat "
          "usually helps; if it keeps happening, a larger model will do better.",
    "ja": "注意：このチャットでは、ご依頼をうまく実行できない状態が続いています。新しいチャットを"
          "始めると改善することが多いです。何度も起きる場合は、より大きなモデルのほうがうまく"
          "対応できます。",
    "ko": "참고: 이 채팅에서는 요청하신 작업을 계속 제대로 실행하지 못하고 있어요. 보통 새 채팅을 "
          "시작하면 나아져요. 이런 일이 자주 생기면 더 큰 모델이 더 잘 처리해요.",
    "th": "หมายเหตุ: ในแชทนี้ฉันยังทำตามคำขอของคุณไม่สำเร็จ การเริ่มแชทใหม่มักจะช่วยได้ "
          "ถ้ายังเกิดขึ้นบ่อย โมเดลที่ใหญ่กว่าจะทำได้ดีกว่า",
    "tr": "Not: Bu sohbette isteğini bir türlü yerine getiremiyorum. Yeni bir sohbet başlatmak "
          "genellikle işe yarar; bu sık oluyorsa daha büyük bir model daha iyi sonuç verir.",
    "zh": "注意：在这个对话里，我一直没能完成你的请求。开启一个新对话通常会有帮助；如果经常发生，"
          "换用更大的模型效果会更好。",
}


def note_language(user_language) -> str:
    """The two-letter code a fixed note is picked by ("de-DE" and "de_DE" are "de")."""
    return str(user_language or "").strip().lower().replace("_", "-").split("-", 1)[0]


def pick_note(table: Mapping[str, str], user_language) -> str:
    """A fixed note from a table keyed by language, English for an unknown or unclear one.
    Shared by every fixed line the loop appends (this one and result grounding's)."""
    return table.get(note_language(user_language), table["en"])


def stuck_note(user_language) -> str:
    return pick_note(STUCK_NOTE, user_language)


# ── counts for the health row ────────────────────────────────────────────────────────────

_EVENTS_MAX = 4096
_events: "deque[Tuple[float, str, str]]" = deque(maxlen=_EVENTS_MAX)
_events_lock = threading.Lock()


def record(level: str, reaction: str, now: Optional[float] = None) -> None:
    """Count one scored generation of this process: its level and what the agent did."""
    with _events_lock:
        _events.append((time.time() if now is None else float(now), str(level), str(reaction)))


def metrics(window_s: float = 3600.0, now: Optional[float] = None) -> dict:
    """Counts of the last ``window_s`` seconds in this process. Counts only, never who."""
    cutoff = (time.time() if now is None else float(now)) - float(window_s)
    with _events_lock:
        recent = [e for e in _events if e[0] >= cutoff]
    out = {"window_s": int(window_s), "scored": len(recent), "mid": 0, "high": 0,
           "retries": 0, "forced": 0, "notes": 0}
    for _, level, reaction in recent:
        if level == MID_LEVEL:
            out["mid"] += 1
        elif level == HIGH_LEVEL:
            out["high"] += 1
        if reaction in (REACTION_FORCED, REACTION_RETRY):
            out["retries"] += 1
        if reaction == REACTION_FORCED:
            out["forced"] += 1
        elif reaction == REACTION_NOTE:
            out["notes"] += 1
    return out


def reset_metrics() -> None:
    with _events_lock:
        _events.clear()
