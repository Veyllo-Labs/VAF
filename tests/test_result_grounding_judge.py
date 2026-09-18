# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Result grounding: the judge rules on what it can see, and its verdict can be acted on.

Live incident. A turn did 21 tool calls to draft an email, wrote the file
(`write_file: File written successfully ...`) and answered in German. The guard called that
answer a confabulation twice, and the user ended up reading the model's own English
reasoning instead of the draft. Three inputs were wrong, none of them the verdict:

1. EVIDENCE. The results were joined oldest-first and the STRING was cut at 1500 chars, so
   the judge saw five fruitless searches and never the write. Measured: the block was 4537
   chars, the cut landed inside entry 6, the write sat 1881 chars past it, and the cut was
   byte-identical on both attempts, so the evidence the correction demanded was appended
   exactly where the judge could never read it. The prompt asks about "a tool that was never
   run this turn", so the truncation did not weaken the evidence, it manufactured the charge.
2. THE JUDGED TEXT. The reply went in raw, and on a provider whose reasoning is folded into
   the content field the first 900 chars were pure thinking, including the words "File
   written successfully" from the think block. The guard graded the thinking.
3. THE VERDICT. `"UNGROUNDED" not in content.upper()` is a substring test, so a judge that
   thinks out loud ("is this ungrounded? no") convicts by deliberating, with no CLAIM line to
   go with it. The code then invented the accusation, and the model spent two rounds guessing
   what it was accused of, in front of the user.

The verdict is a SENTINEL (`</grounded>` / `</ungrounded>`), the shape the workflow and
sub-agent validators in the same file already use, because the default provider reasons on
every call: a plain English word either drowns in that reasoning or never arrives inside the
token budget, and a guard that is on by default must not be able to go silently inert.
"""
from types import SimpleNamespace

import pytest

from vaf.core.agent import (
    Agent,
    _grounding_correction,
    _grounding_evidence,
)

WRITE_RESULT = (
    "File written successfully to /home/user/Documents/VAF_Projects/ab12cd34/green123456/"
    "E-Mail_Uwe_Servicevertraege_VAF.md (1.3 KB)"
)
GERMAN_ANSWER = (
    "Ich habe den Entwurf erstellt, er liegt im Projektordner. Die Vertragsnummern SV2026-2 "
    "und SV2026-3 stehen drin, die Adresse fehlt noch."
)
GUILTY = "</ungrounded>\nCLAIM: die Mail wurde an Uwe gesendet"


def _tools(**levels):
    """A tools dict in the shape the agent holds: name -> instance declaring its contract."""
    return {name: SimpleNamespace(permission_level=level) for name, level in levels.items()}


def _judge_agent(*verdicts, tools=None):
    """An agent whose only live part is the judge: it records prompts and answers in order."""
    a = Agent.__new__(Agent)
    a.use_server = False
    a.api_backend = object()
    a.llm = None
    a.tools = tools if tools is not None else {}
    a.seen_prompts = []
    a.seen_kwargs = []
    answers = list(verdicts) or [""]

    def _fake(messages, **kwargs):
        a.seen_prompts.append(messages[0]["content"])
        a.seen_kwargs.append(kwargs)
        return answers[min(len(a.seen_prompts) - 1, len(answers) - 1)]

    a._run_validation_llm = _fake
    return a


def _incident_turn():
    """The incident's own turn shape: many lookups, one write, the write LAST."""
    rows = []
    for i in range(19):
        rows.append(("memory_search", f"[Source {i}] (Relevance: 8{i % 10}%) " + "x" * 300))
    rows.append(("update_working_memory", "Working Memory updated."))
    rows.append(("write_file", WRITE_RESULT))
    return rows


# ---- 1. evidence -------------------------------------------------------------

def test_the_write_result_is_never_cut_out_of_the_evidence():
    """MUTATION: restore the head slice (`block[:1500]`) or drop the outcome tier.

    The reply claims the file was written; the result that says so is the whole evidence.
    With the old window it was 1881 chars outside the prompt.
    """
    tools = _tools(memory_search="read", update_working_memory="system", write_file="write")
    block, shown, omitted, omitted_outcomes = _grounding_evidence(_incident_turn(), tools.get)
    assert WRITE_RESULT in block
    assert (shown, omitted, omitted_outcomes) == (21, 0, 0)

    # And it survives into the prompt the judge actually reads, which is where the old window
    # lost it: the block was assembled whole and then sliced at 1500 chars.
    agent = _judge_agent("</grounded>", tools=tools)
    agent._detect_ungrounded_result_claim(GERMAN_ANSWER, _incident_turn())
    results_section = agent.seen_prompts[0].split("ACTUAL TOOL RESULTS THIS TURN:", 1)[1]
    assert WRITE_RESULT in results_section


def test_every_tool_of_the_turn_is_named_in_the_evidence():
    """A judge asked whether a tool ran must be told which tools ran. A lookup is abbreviated
    rather than dropped, so absence in the prompt keeps meaning absence in the turn."""
    block, _, _, _ = _grounding_evidence(
        _incident_turn(),
        _tools(memory_search="read", update_working_memory="system", write_file="write").get,
    )
    for name in ("memory_search", "update_working_memory", "write_file"):
        assert f"- {name}: " in block


def test_a_failed_result_counts_as_an_outcome():
    """A reply can claim an error just as much as a success, and the prompt asks about "a
    specific error" - so a failed or blocked result keeps the outcome tier's budget."""
    failure = "[BLOCKED] You already ran 'list_files' with these EXACT arguments earlier this turn. " * 6
    block, _, omitted, _ = _grounding_evidence(
        [("list_files", failure)], _tools(list_files="read").get
    )
    assert omitted == 0
    assert "[BLOCKED]" in block and len(block) > 200


def test_a_document_that_mentions_a_failure_is_not_a_failed_result():
    """MUTATION: drop `content_carrying` from the `tool_result_is_error` call.

    `read_file` declares `result_is_deliverable` precisely because the error classifier scans
    a whole result for failure vocabulary: without the flag a successful read of a log saying
    "the nightly tool run failed" is promoted to the outcome tier, and thirty such reads push
    the real write out of the evidence - the incident's shape, rebuilt by the code meant to
    remove it. Both other callers of that classifier pass the flag; this is the third.
    """
    log = ("2026-09-18 02:00 nightly job started\n"
           "2026-09-18 02:04 the nightly tool run failed, execution retried later\n") * 12
    doc_tool = SimpleNamespace(permission_level="read", result_is_deliverable=True)
    rows = [("write_file", WRITE_RESULT)] + [("read_file", log)] * 30
    block, _, _, dropped = _grounding_evidence(
        rows, {"write_file": SimpleNamespace(permission_level="write"), "read_file": doc_tool}.get
    )
    assert WRITE_RESULT in block, "the write must survive thirty document reads"
    assert dropped == 0

    # A real failure from the same tool still reads as one: its error returns are anchored.
    block2, _, _, _ = _grounding_evidence(
        [("read_file", "Error: file not found: /home/user/missing.md")], {"read_file": doc_tool}.get
    )
    assert "Error: file not found" in block2


def test_an_unclassifiable_tool_is_never_silently_dropped():
    """No tool instance means no contract to read. Over-including is the safe direction for a
    guard whose false positive costs the user an answer."""
    block, shown, omitted, _ = _grounding_evidence(
        [("some_third_party_tool", "y" * 900)], lambda _name: None
    )
    assert (shown, omitted) == (1, 0)
    assert len(block) > 250


def test_a_short_turn_is_not_judged_on_less_evidence_than_before():
    """MUTATION: fix the lookup share at `_RG_LOOKUP_CHARS` instead of spreading the budget.

    Most turns are short. A flat per-entry share of 120 chars would hand the judge LESS than
    the old 300-char window while leaving 97 percent of the budget unspent, and a starved
    judge is how the incident started. The quoted fact below sits at offset 160.
    """
    mail = (
        "From: uwe@example.com\nSubject: Servicevertraege\n\n"
        "Guten Tag, wir haben Ihr Angebot geprueft. Bitte bestaetigen Sie die Vertragsnummern. "
        "Die Kuendigungsfrist von sechs Monaten ist fuer uns in Ordnung, sofern die Wartung "
        "enthalten bleibt."
    )
    block, _, _, _ = _grounding_evidence([("find_mail", mail)], _tools(find_mail="read").get)
    assert "sechs Monaten" in block, len(block)


def test_an_over_long_turn_reports_what_it_omitted():
    """MUTATION: return a zero omission count, or drop the NOTE from the prompt.

    Past the budget the oldest lookups do fall out, and the judge has to be told, or a missing
    result reads as proof that the tool never ran.
    """
    # The write sits EARLY, the way it did in the incident (the reads that verify it come
    # after). Newest-first alone would drop it; the outcome tier is what keeps it.
    rows = [("write_file", WRITE_RESULT)] + [("memory_search", "z" * 400) for _ in range(120)]
    tools = _tools(memory_search="read", write_file="write")
    block, shown, omitted, omitted_outcomes = _grounding_evidence(rows, tools.get)
    assert omitted > 0 and shown < len(rows)
    assert WRITE_RESULT in block, "the write survives even a turn that overflows the budget"
    assert omitted_outcomes == 0

    agent = _judge_agent("</grounded>", tools=tools)
    agent._detect_ungrounded_result_claim(GERMAN_ANSWER, rows)
    prompt = agent.seen_prompts[0]
    assert "are not listed (length limit)" in prompt
    assert "every write result of this turn IS listed" in prompt
    assert "not evidence that the tool did not run" in prompt


def test_the_prompt_never_swears_a_write_is_listed_when_one_was_dropped():
    """MUTATION: word the NOTE from a constant again ("Every omitted one is a plain lookup").

    Outcomes shrink before they are dropped, but a hundred writes in one turn still overflow.
    Telling the judge that no write is missing is then the exact false premise the incident
    turned on, handed over as a fact one sentence before "a result you cannot see is not
    evidence". Measured: 54 of 100 shown, 46 dropped, all of them writes.
    """
    rows = [("write_file", f"File written successfully to /home/user/part_{i:03d}.md" + " " * 460)
            for i in range(100)]
    tools = _tools(write_file="write")
    block, shown, omitted, omitted_outcomes = _grounding_evidence(rows, tools.get)
    assert omitted and omitted_outcomes == omitted, (shown, omitted, omitted_outcomes)
    assert "part_000" not in block

    agent = _judge_agent("</grounded>", tools=tools)
    agent._detect_ungrounded_result_claim("part_000 ist erstellt, die Datei liegt bereit.", rows)
    prompt = agent.seen_prompts[0]
    assert "of the omitted ones are write or failure results" in prompt
    assert "every write result of this turn IS listed" not in prompt


def test_the_prompt_cannot_balloon_on_a_turn_made_of_outcomes():
    """MUTATION: drop the share-shrinking passes and let outcome entries keep 300 chars each.

    A failed result is an outcome, so a turn of fifty refused calls would otherwise send a
    30 KB prompt on a check that runs after every final reply. The shares shrink before
    anything is dropped, so the bound holds without the guard going blind: measured, shrinking
    first names 54 of the 60 results inside the same budget, dropping at full share only 12.
    """
    rows = [("list_files", "[BLOCKED] You already ran this. " * 20) for _ in range(60)]
    block, shown, omitted, _ = _grounding_evidence(rows, _tools(list_files="read").get)
    assert len(block) <= 4600, len(block)
    assert shown + omitted == 60
    assert shown >= 45, shown


# ---- 2. the judged text ------------------------------------------------------

def test_the_judge_grades_the_answer_not_the_thinking():
    """MUTATION: pass `response_text` unstripped again.

    The think block recites the tool output it just read. Judged raw, the guard grades that
    recitation; the user-visible answer is what can mislead a person.
    """
    thinking = f"<think>I called write_file and it said: {WRITE_RESULT}. It succeeded. " + "x" * 900 + "</think>"
    agent = _judge_agent("</grounded>", tools=_tools(write_file="write"))
    agent._detect_ungrounded_result_claim(
        f"{thinking}\n\n{GERMAN_ANSWER}", [("write_file", WRITE_RESULT)]
    )
    reply_section = agent.seen_prompts[0].split("ASSISTANT REPLY:", 1)[1].split("ACTUAL TOOL RESULTS", 1)[0]
    assert GERMAN_ANSWER in reply_section
    assert "<think>" not in reply_section and "It succeeded" not in reply_section


def test_the_prefilter_reads_the_whole_generation():
    """MUTATION: run the prefilter on the stripped reply instead of the raw generation.

    The prefilter only decides whether a look is worth paying for. A bilingual turn (English
    thinking, German answer) is the incident's own shape, and that German sentence carries
    none of the keywords, so filtering on the stripped text alone means the judge is never
    asked about a real confabulation.
    """
    reply = (
        "<think>write_file returned File written successfully, and I will say the mail is out."
        "</think>\n\nDie Mail ist raus an Uwe, der Entwurf liegt im Projektordner."
    )
    agent = _judge_agent(GUILTY, tools=_tools(write_file="write"))
    ungrounded, claim = agent._detect_ungrounded_result_claim(reply, [("write_file", WRITE_RESULT)])
    assert agent.seen_prompts, "the judge was never asked"
    assert ungrounded is True and claim


def test_a_reasoning_only_generation_is_never_judged():
    """Thinking claims nothing to anybody, so it is not this guard's business - the
    empty-response lane owns a generation without an answer."""
    agent = _judge_agent(GUILTY, tools=_tools(write_file="write"))
    ungrounded, claim = agent._detect_ungrounded_result_claim(
        "<think>Die Datei wurde gespeichert, ich melde das jetzt.</think>",
        [("write_file", WRITE_RESULT)],
    )
    assert (ungrounded, claim) == (False, None)
    assert agent.seen_prompts == [], "the judge must not even be asked"


# ---- 3. the verdict contract -------------------------------------------------

@pytest.mark.parametrize("verdict", [
    "</ungrounded>",
    "</ungrounded>\nCLAIM:",
    "</ungrounded>\nCLAIM: [the unsupported claim, short]",
    "</ungrounded>\nCLAIM: n/a",
])
def test_a_verdict_without_a_usable_claim_is_grounded(verdict):
    """MUTATION: restore the invented fallback claim.

    The correction quotes the claim back at the model. With nothing to quote it quoted the
    placeholder, so the accusation contained no accusation and the model could only guess. A
    bounce that cannot be answered costs the user an answer and buys nothing.
    """
    agent = _judge_agent(verdict, tools=_tools(write_file="write"))
    assert agent._detect_ungrounded_result_claim(
        GERMAN_ANSWER, [("write_file", WRITE_RESULT)]) == (False, None)


def test_a_thinking_judge_does_not_convict_by_deliberating():
    """MUTATION: restore `if "UNGROUNDED" not in content.upper()`, or check the sentinels in
    the other order.

    The default provider always reasons. Weighing the word out loud convicted the reply, and
    without a CLAIM line the correction then quoted a placeholder - the live incident. The
    benign sentinel is checked first, so an answer carrying both keeps the reply.
    """
    acquittals = [
        "<think>Is this ungrounded? The write_file result supports it, so no.</think>\n</grounded>",
        "<think>Candidate CLAIM: die Datei liegt im Projektordner. But write_file confirms it, "
        "so this is not ungrounded.</think>\n</grounded>",
        "</grounded>. The reply is not ungrounded, write_file returned success.\n"
        "CLAIM: die Datei liegt im Projektordner (supported)",
        "<think>Could be </ungrounded>, but the write is right there.</think>\n</grounded>",
    ]
    for verdict in acquittals:
        agent = _judge_agent(verdict, tools=_tools(write_file="write"))
        assert agent._detect_ungrounded_result_claim(
            GERMAN_ANSWER, [("write_file", WRITE_RESULT)]) == (False, None), verdict


def test_a_judge_that_never_answers_is_re_asked_once_and_then_keeps_the_reply():
    """MUTATION: drop the stricter re-ask, or hard-code one attempt.

    A reasoning judge can spend its whole budget thinking and return no verdict at all. That
    must not silently disable a guard that is on by default: it gets one stricter re-ask, and
    an answer that still carries no sentinel keeps the reply (and says so in the backend log).
    """
    truncated = "<think>The reply says the file was written. Let me check the results once more"
    agent = _judge_agent(truncated, truncated, tools=_tools(write_file="write"))
    assert agent._detect_ungrounded_result_claim(
        GERMAN_ANSWER, [("write_file", WRITE_RESULT)]) == (False, None)
    assert len(agent.seen_prompts) == 2, "the judge must get exactly one stricter re-ask"
    assert "EXACTLY </grounded> or </ungrounded>" in agent.seen_prompts[1]

    # And the re-ask is what convicts when the second answer is decisive.
    agent = _judge_agent(truncated, GUILTY, tools=_tools(write_file="write"))
    ungrounded, claim = agent._detect_ungrounded_result_claim(
        "Die Mail wurde an Uwe gesendet.", [("write_file", WRITE_RESULT)])
    assert ungrounded is True and claim == "die Mail wurde an Uwe gesendet"


def test_a_named_claim_still_convicts():
    """The guard keeps its job: a real verdict with a quotable claim fires, and the claim is
    handed on verbatim so the correction can name it."""
    agent = _judge_agent(
        "<think>nothing supports the mailbox sentence</think>\n" + GUILTY,
        tools=_tools(write_file="write"),
    )
    ungrounded, claim = agent._detect_ungrounded_result_claim(
        "Die Mail wurde an Uwe gesendet.", [("write_file", WRITE_RESULT)]
    )
    assert ungrounded is True
    assert claim == "die Mail wurde an Uwe gesendet"


def test_a_one_line_verdict_carries_its_own_claim():
    """MUTATION: parse the claim only from a `CLAIM:` line.

    "</ungrounded> - the mail was never sent" is the commonest short verdict shape. Throwing
    that remainder away acquits a judge that has just named the claim, and a named claim is the
    whole precondition for a correction the model can act on.
    """
    agent = _judge_agent("</ungrounded> - die Mail wurde nie gesendet",
                         tools=_tools(write_file="write"))
    ungrounded, claim = agent._detect_ungrounded_result_claim(
        "Die Mail wurde an Uwe gesendet.", [("write_file", WRITE_RESULT)]
    )
    assert ungrounded is True
    assert claim == "die Mail wurde nie gesendet"


def test_the_judge_call_is_time_bounded_and_has_room_for_a_verdict():
    """MUTATION: drop `timeout_s`, or put `max_tokens` back to 160.

    An unbounded validation call has hung this codebase before (343 silent seconds). And too
    small a budget is its own failure: a reasoning judge then returns thinking only, which is
    an acquittal by truncation on a guard that is on by default.
    """
    agent = _judge_agent("</grounded>", tools=_tools(write_file="write"))
    agent._detect_ungrounded_result_claim(GERMAN_ANSWER, [("write_file", WRITE_RESULT)])
    kwargs = agent.seen_kwargs[0]
    assert 0 < kwargs["timeout_s"] <= 15
    assert kwargs["max_tokens"] >= 300


# ---- 4. the correction ------------------------------------------------------

def test_the_correction_never_orders_a_call_that_loop_protection_refuses():
    """MUTATION: restore "Do NOT call the tool again to verify", or the old single
    "Either CALL the tool now".

    In the incident the model obeyed the order to call, `_find_redundant_read_call` refused
    the identical re-call, and the turn ended with no answer. A blanket ban is wrong too: for
    this guard's founding incident (a narrated workflow success after a turn of nothing but
    bookkeeping) running the tool IS the remedy. What is forbidden is repeating a call whose
    result is already in the turn.
    """
    text = _grounding_correction("der Entwurf wurde erstellt")
    assert "der Entwurf wurde erstellt" in text
    assert "run it" in text and "NOW" in text
    assert "never repeat a call whose result is already above" in text
    assert "Do NOT call the tool again" not in text


def test_the_correction_says_the_reply_was_not_removed():
    """The reply stays on screen, so the model must ADD a correction instead of rewriting text
    it cannot reach. The guard only ever fires on a reply that is on the screen (pinned in
    tests/test_result_grounding_bounce.py), so this wording is always true."""
    text = _grounding_correction("die Datei liegt im Ordner")
    assert "still on the user's screen" in text


def test_the_prefilter_can_see_every_word_the_deterministic_tier_quotes():
    """MUTATION: remove "abgeschlossen", "durchgeführt" or "complete" from the keywords.

    The prefilter decides whether the check runs at all, and the deterministic tier behind it
    needs no LLM to be right - so a word the tier would quote as the claim, but the prefilter
    cannot see, silently kills that tier for every reply using it. Three words were in exactly
    that state ("Der Auftrag ist abgeschlossen." after a bookkeeping-only turn walked free).
    """
    from vaf.core.text_match import contains_any
    from vaf.core.agent import _RG_CLAIM_WORDS, _RG_OUTCOME_KEYWORDS

    for word in _RG_CLAIM_WORDS:
        assert contains_any(word.lower(), _RG_OUTCOME_KEYWORDS), word


def test_the_deterministic_tier_still_convicts_a_narrated_success():
    """The guard's founding incident, end to end through the public method: a turn of nothing
    but bookkeeping plus a reply claiming a finished workflow, with no judge involved."""
    agent = _judge_agent(tools=_tools(update_working_memory="system"))
    for reply in (
        "Ich habe den Workflow erfolgreich ausgeführt, die Ergebnisse liegen in der Datei.",
        "Der Auftrag ist abgeschlossen, die Recherche wurde durchgeführt.",
        "The workflow completed and the report was created.",
    ):
        ungrounded, claim = agent._detect_ungrounded_result_claim(
            reply, [("update_working_memory", "Working Memory updated.")] * 3)
        assert ungrounded is True and claim, reply
    assert agent.seen_prompts == [], "the deterministic tier must not need a judge"
