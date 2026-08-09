# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Self-poisoning guards: a model must not believe its own fictional diary.

Live incidents (two consecutive chats, same shape): a weak model narrated its
INTENTIONS into working-memory notes ("Web-Suche: läuft", "Workflow wurde
erfolgreich gestartet") without calling a single real tool. The working-memory
block re-injected that fiction into the next generation as trusted context,
the anti-spin guard then forced a text-only turn whose old wording ("state
your result") invited a result - and the model coherently reported work it
never did, complete with an invented file path. Not hallucination out of thin
air: faithful reading of a self-poisoned context.

Three guards pinned here:
1. Note firewall (_working_memory_note_gate): outcome/progress-claiming notes
   are refused while NO non-bookkeeping tool has run this turn.
2. Deterministic grounding: a final reply asserting outcomes after a
   bookkeeping-only turn is UNGROUNDED without needing the LLM judge (which
   waved the incident through).
3. Anti-spin wording: the forced text turn forbids claiming results.
"""
from types import SimpleNamespace

from vaf.core.agent import Agent, _note_claims_unearned_outcome

INCIDENT_NOTES = [
    "Web-Suche Wetter Berlin: läuft",
    "Web-Suche Wetter Berlin: abgeschlossen",
    "Workflow 'research_and_code' wurde erfolgreich gestartet - Pre-extrahierte Variablen enthalten bereits die korrekte Query",
]

HONEST_NOTES = [
    "User möchte mehrstufige Websuche durchführen: Wetter + New York, Ergebnis als HTML",
    "Der Server läuft laut User seit gestern stabil",       # user-stated fact, no action noun
    "Wetter-Quelle wetter.com wirkt zuverlässiger als blick-aufs-wetter",
]


def _bare(progress_ran: bool):
    a = Agent.__new__(Agent)
    a._turn_ran_progress_tool = progress_ran
    return a


# ── 1. note firewall ─────────────────────────────────────────────────────────

def test_incident_notes_are_detected():
    for n in INCIDENT_NOTES:
        assert _note_claims_unearned_outcome([n]) == n, n


def test_honest_notes_pass():
    assert _note_claims_unearned_outcome(HONEST_NOTES) is None


def test_note_gate_blocks_only_before_real_work():
    blocked = _bare(False)._working_memory_note_gate({"add_notes": INCIDENT_NOTES[:1]})
    assert blocked and "[BLOCKED]" in blocked and "no real tool" in blocked.lower()

    # After a real tool ran, the same note is a legitimate record of the result.
    assert _bare(True)._working_memory_note_gate({"add_notes": INCIDENT_NOTES[:1]}) is None
    # Plain planning/notes without outcome claims are never blocked.
    assert _bare(False)._working_memory_note_gate({"add_notes": HONEST_NOTES}) is None
    assert _bare(False)._working_memory_note_gate({"add_plan": ["Suche starten, dann HTML bauen"]}) is None


def test_note_gate_result_reads_as_failure():
    """The block must be flagged as a failed call by the shared detector, so
    the per-turn summary cannot label the refused note as a green OK."""
    from vaf.core.context import tool_result_is_error

    blocked = _bare(False)._working_memory_note_gate({"add_notes": INCIDENT_NOTES[:1]})
    assert tool_result_is_error(blocked)


# ── 2. deterministic grounding ───────────────────────────────────────────────

INCIDENT_FINAL = (
    'Ich habe den "Research & Code" Workflow erfolgreich ausgeführt. Hier sind die '
    "Ergebnisse meiner mehrstufigen Websuche... Die Datei befindet sich unter: "
    "/home/user/Documents/wetter_report.html"
)


def _grounding_agent():
    a = Agent.__new__(Agent)
    a.use_server = False
    a.api_backend = None
    a.llm = None
    return a


def test_bookkeeping_only_turn_with_outcome_claim_is_ungrounded():
    turn_results = [("update_working_memory", "✅ Working Memory updated.")] * 4
    ungrounded, claim = _grounding_agent()._detect_ungrounded_result_claim(
        INCIDENT_FINAL, turn_results)
    assert ungrounded is True
    assert claim and "ausgeführt" in claim.lower()


def test_real_tool_in_turn_falls_through_to_judge():
    """With a real tool result present the deterministic rule stays out of it
    (here: no backend -> judge unavailable -> fail-open False, as before)."""
    turn_results = [("web_search", "### Web Search Results ...")]
    ungrounded, _ = _grounding_agent()._detect_ungrounded_result_claim(
        INCIDENT_FINAL, turn_results)
    assert ungrounded is False


def test_a_committed_memory_save_is_not_bookkeeping():
    """Live incident, opposite polarity: the model was told its own truthful
    confirmation was a confabulation.

    The user asked "remember this"; the turn ran memory_save (bounced by the
    plan gate), update_working_memory (the plan the gate demanded), then
    memory_save again, which committed a row. The reply said "Gespeichert." and
    the deterministic rule flagged it, because memory_save used to be counted
    as bookkeeping - so the one shape where saving IS the whole task was
    unearned by construction. Note the plan gate makes this self-inflicted:
    memory_save is irreversible, so a plan call is FORCED into the same turn and
    nothing non-bookkeeping is left in it.

    Reverting the fix turns this test red on the first assertion; the second
    keeps the guard's actual job pinned, so the fix cannot be "delete the rule".
    """
    live_turn = [
        ("memory_save", "[PLAN REQUIRED] 'memory_save' changes state, so set your REAL approach"),
        ("update_working_memory", "✅ Working Memory updated."),
        ("memory_save", "Memory stored."),
    ]
    reply = "Gespeichert. Deine Headline ist jetzt festgehalten."
    ungrounded, _ = _grounding_agent()._detect_ungrounded_result_claim(reply, live_turn)
    assert ungrounded is False, "a committed memory_save must not be bookkeeping"

    # A save alone is the same answer: the branch reads tool NAMES, so the
    # single-call shape has to be pinned too or a partial revert stays green.
    ungrounded, _ = _grounding_agent()._detect_ungrounded_result_claim(
        reply, [("memory_save", "Memory stored.")])
    assert ungrounded is False

    # The guard's real job is untouched: notes-only plus an outcome claim is
    # still ungrounded by construction, with no judge asked.
    ungrounded, claim = _grounding_agent()._detect_ungrounded_result_claim(
        INCIDENT_FINAL, [("update_working_memory", "✅ Working Memory updated.")])
    assert ungrounded is True and claim


def test_conversational_turn_without_tools_is_not_deterministically_flagged():
    """Zero tool calls = possibly a recap of EARLIER turns ("ich habe vorhin
    die Datei erstellt") - must not be auto-flagged; the LLM judge lane owns it."""
    ungrounded, _ = _grounding_agent()._detect_ungrounded_result_claim(
        "Wie besprochen habe ich vorhin die Datei erstellt.", [])
    assert ungrounded is False


# ── 3. anti-spin wording ─────────────────────────────────────────────────────

def test_forced_text_turn_forbids_result_claims(monkeypatch):
    monkeypatch.delenv("VAF_THINKING_MODE", raising=False)
    a = Agent.__new__(Agent)
    a._anti_spin_streak = 5  # next bookkeeping call crosses max(4)+2
    msg, force = a._anti_spin_step("update_working_memory")
    assert force is True
    assert "do NOT claim" in msg
    assert "state your result" not in msg.lower()  # the fabrication invite is gone
