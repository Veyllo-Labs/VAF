# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The ratchet: German keyword matching goes through the primitive, and stays there.

`tests/test_text_match.py` proves the primitive works. This file proves it is WIRED, which
is the half that rots: a keyword list is edited far more often than a matcher, and the
cheap-looking repair is always the wrong one - add the ASCII spelling next to the umlaut
one. That is what this guard exists to prevent, because doubling entries writes the broken
spelling into the source permanently and has to be repeated for every future list.

Three things are pinned, and the third is the one that would otherwise fail silently:

1. **Nobody hand-writes a transliteration.** No `(?:ü|ue)` alternation, no `[üu]` class, no
   `"taeglich"` next to `"täglich"`. Source stays proper German; `compile_de` generates the
   tolerance.
2. **The security-relevant gates catch both spellings.** Behaviour, not structure: a
   refactor that keeps the imports but loses the call would pass a structural check.
3. **Every pattern built with `compile_de` actually compiles.** `vaf/workflows/selector.py`
   wraps its matching in `except re.error: continue`, so a pattern this module refuses to
   expand would not raise anywhere a human can see it - the workflow would simply never
   trigger again. Import-time compilation is the only place that failure becomes visible.
"""
import ast
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_VAF = _REPO / "vaf"

# The transliterated spellings, as whole words. Narrow on purpose: "queue" and "message"
# contain the digraphs without being German.
_HAND_ROLLED = re.compile(
    r"\b(fuer|ueber|koennt\w*|koenn\w*|waer\w*|muess\w*|moecht\w*|taeglich|woechentlich"
    r"|stuendlich|monatlich_ascii|groess\w*|loesch\w*|loese|aender\w*|oeffn\w*|pruef\w*"
    r"|fuehr\w*|zurueck\w*|erklaer\w*|erzaehl\w*|enthaelt|bestaetig\w*|ausgefuehrt"
    r"|durchgefuehrt|faehigkeit|boerse|muenchen|koeln|duesseldorf|zuerich|universitaet"
    r"|ueberweisung|vollstaendig\w*|ausfuehrlich\w*|saetze|ueberschrift|loesung"
    r"|datentraeger|entwuerfe|spaeter|frueher\w*)\b",
    re.IGNORECASE,
)

# A hand-built umlaut alternation or class: exactly what compile_de replaces.
_HAND_ALTERNATION = re.compile(r"\(\?:[äöüßÄÖÜ]\|(?:ae|oe|ue|ss)\)|\[[äöüß][aou]\]|\[[aou][äöüß]\]")

# Files the guard deliberately does not read.
_SKIP_DIRS = (
    "vaf/vendor/",   # third-party source, kept verbatim
    "vaf/cli/",      # separate lane; folded in once that work lands
)

# The module that defines the concept has to be able to name both spellings.
_SKIP_FILES = {"vaf/core/text_match.py"}


def _code_strings(path: Path):
    """String literals that are DATA, not prose.

    Docstrings are excluded deliberately: a comment or docstring explaining this very
    problem has to be able to quote the transliterated form, and several do. Only values
    the code actually compares against are in scope.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                yield node.lineno, node.value

# Named exceptions, each with the reason. This set may only SHRINK.
_ALLOWED_TRANSLITERATIONS = {
    "vaf/tools/mail_utils.py":
        "geschaeftsfuehrung sits next to geschäftsführung in _EXEC_IMPERSONATION_WORDS on "
        "purpose: it is matched against a sender's DISPLAY NAME, which mail clients and "
        "directories routinely store ASCII-only, and that list is checked with plain "
        "containment against an address, not with the folding matcher.",
    "vaf/mail/sync.py":
        "SPECIAL_USE_FALLBACK lists folder names as MAIL SERVERS actually send them, not "
        "as we would write them. 'Entwuerfe' and 'Geloeschte Elemente' are spellings that "
        "arrive on the wire from real IMAP servers, so they are observed data rather than "
        "a keyword we chose, and dropping them would lose those mailboxes.",
}

_ALLOWED_ALTERNATIONS_PENDING = {
    "vaf/tools/project_git.py":
        "zur[üu]cksetzen covers a THIRD spelling the ae/oe/ue convention does not model: "
        "bare 'u'. It is therefore not the hand-rolled form of what compile_de generates, "
        "and converting it would silently drop that coverage in a veto path. Measured: "
        "'setze bitte zuruck auf <sha>' stops matching. Needs its own decision, not a "
        "mechanical conversion.",
}

_ALLOWED_ALTERNATIONS = {
    "vaf/core/voice_agent.py":
        "h[aä]+h? in _UNCLEAR_REPLY_RE is not a German keyword but an onomatopoeic class "
        "for a spoken 'hä'/'haa', where the two vowels are alternative SOUNDS rather than "
        "two spellings of one word. compile_de refuses umlauts inside a class for good "
        "reason, and expanding this one would change what it hears.",
}


def _tracked_py_files():
    for path in sorted(_VAF.rglob("*.py")):
        rel = path.relative_to(_REPO).as_posix()
        if any(rel.startswith(skip) for skip in _SKIP_DIRS) or rel in _SKIP_FILES:
            continue
        yield rel, path


def test_no_hand_written_transliteration_in_a_german_keyword():
    """Add the umlaut spelling only. `compile_de` and `contains_any` make both match."""
    offenders = []
    for rel, path in _tracked_py_files():
        if rel in _ALLOWED_TRANSLITERATIONS:
            continue
        for num, value in _code_strings(path):
            if _HAND_ROLLED.search(value):
                offenders.append(f"{rel}:{num}: {value[:100]!r}")
    assert not offenders, (
        "Transliterated German found in source. Write the umlaut spelling only and let "
        "vaf/core/text_match.py match both:\n  " + "\n  ".join(offenders[:20])
    )


def test_no_hand_built_umlaut_alternation():
    """`(?:ü|ue)` and `[üu]` are the hand-rolled form of what compile_de generates."""
    offenders = []
    for rel, path in _tracked_py_files():
        if rel in _ALLOWED_ALTERNATIONS or rel in _ALLOWED_ALTERNATIONS_PENDING:
            continue
        for num, value in _code_strings(path):
            if _HAND_ALTERNATION.search(value):
                offenders.append(f"{rel}:{num}: {value[:100]!r}")
    assert not offenders, (
        "Hand-built umlaut alternation found. Use compile_de(pattern) instead:\n  "
        + "\n  ".join(offenders[:20])
    )


def test_the_exception_lists_carry_a_reason():
    for name, reason in {**_ALLOWED_TRANSLITERATIONS, **_ALLOWED_ALTERNATIONS,
                         **_ALLOWED_ALTERNATIONS_PENDING}.items():
        assert len(reason) > 60, f"{name} needs a reason a stranger can act on"


# ── behaviour, not structure ─────────────────────────────────────────────────

@pytest.mark.parametrize("spelling", ["Überweisung", "Ueberweisung"])
def test_the_outgoing_high_risk_gate_sees_both_spellings(spelling):
    """A phishing-shaped request must not slip through by dropping an umlaut."""
    from vaf.tools.send_mail import _high_risk_send_reasons

    reasons = _high_risk_send_reasons(
        to="someone@gmail.com", subject=f"Bitte {spelling} veranlassen", body="", attachments=[]
    )
    assert "high_risk_request_language_detected" in reasons


@pytest.mark.parametrize("spelling", ["Konto bestätigen", "Konto bestaetigen"])
def test_the_phishing_score_sees_both_spellings(spelling):
    from vaf.tools.mail_utils import _phishing_score

    score, reasons = _phishing_score(
        {"from": "a@b.de", "subject": spelling, "body_snippet": "", "category": ""}
    )
    assert "social_engineering_language" in reasons and score >= 2


@pytest.mark.parametrize("spelling", ["ausgeführt", "ausgefuehrt"])
def test_the_unearned_outcome_guard_sees_both_spellings(spelling):
    """The measured defect this whole round started from: the guard against claimed-but-
    unearned success looked for the umlaut spelling, so a model that transliterates walked
    straight past the check built to catch it."""
    from vaf.core.agent import _UNEARNED_OUTCOME_VERB_RE

    # The sentence must carry NO other alternative from the pattern. An earlier version
    # said "erfolgreich ausgefuehrt" and passed through "erfolgreich" even with the
    # tolerance removed, so it proved nothing - caught by mutating compile_de back to
    # re.compile and watching this test stay green.
    assert _UNEARNED_OUTCOME_VERB_RE.search(f"Der Auftrag wurde {spelling}.")


@pytest.mark.parametrize("text", [
    "Task successfully completed.",
    "The script ran with 2 errors.",
    "Die Datei wurde abgespeichert.",
    "Die erstellte Datei liegt im Ordner.",
    "Der Auftrag wurde ausgefuehrt.",
])
def test_the_ungrounded_claim_prefilter_matches_stems_not_whole_words(text):
    """This prefilter gates the WHOLE ungrounded-claim check: when it says no, neither the
    bookkeeping rule nor the LLM judge ever runs. Its needles are stems, so whole-word
    matching silently disables it - "success" never reaches "successfully", "gespeichert"
    never reaches "abgespeichert". That mistake was made once during this very round and
    only surfaced under adversarial review, so it is pinned here rather than trusted."""
    from vaf.core.agent import Agent

    src = Agent._detect_ungrounded_result_claim
    assert src is not None
    from vaf.core.text_match import contains_any

    outcome_kw = (
        "failed", "success", "succeed", "saved", "wrote", "written", "created", "deleted",
        "removed", "sent", "crashed", "error", "not found", "no results", "executed",
        "task complete", "fehlgeschlagen", "gespeichert", "erstellt", "gelöscht", "gesendet",
        "ausgeführt", "bestätigt", "nicht gefunden", "kein ergebnis",
    )
    assert contains_any(text.lower(), outcome_kw), (
        "the prefilter must still fire for this text; if it does not, the guard behind it "
        "is dead for that shape"
    )


def test_an_english_sentence_is_not_mistaken_for_german():
    """The counter-direction, and the reason whole-word matching is the default: "über"
    folds to "ueber", which sits inside "blueberry"."""
    from vaf.core.text_match import contains_any_word

    assert not contains_any_word("I want a blueberry muffin recipe", ["über", "für"])


# ── the silent failure ───────────────────────────────────────────────────────

def test_every_expanded_pattern_compiles_at_import():
    """`vaf/workflows/selector.py` swallows `re.error` per pattern and continues, so a
    pattern that failed to expand would not raise, not log, and not fail a test - the
    workflow would just stop triggering. Importing the modules that build patterns is what
    turns that into a visible error."""
    import importlib

    for module in (
        "vaf.core.agent",
        "vaf.core.context",
        "vaf.core.text_match",
        "vaf.workflows.selector",
        "vaf.tools.mail_utils",
        "vaf.tools.send_mail",
    ):
        importlib.import_module(module)   # raises if any compile_de call refused


def test_the_primitive_is_not_on_the_public_facade_yet():
    """Named boundary. The 94 hand-rolled matches that justified this module are all
    internal; no embedder need has been measured, so it stays off `vaf.__all__` rather than
    being exported on speculation. When a third-party tool is shown to need it, that
    measurement is what moves it - and this test is where the decision is recorded."""
    import vaf

    assert "text_match" not in vaf.__all__
    assert "contains_any_word" not in vaf.__all__
