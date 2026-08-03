# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Matching German text when the spelling of its umlauts cannot be relied on.

Keyword lists and patterns in this codebase are written in proper German: `täglich`,
`ausgeführt`, `löschen`. What they are matched AGAINST is not. Two independent sources
produce the transliterated spelling instead:

- **People.** A keyboard without umlaut keys produces `taeglich`, and plenty of users
  type that way by habit even when the keys exist.
- **Models.** Since 2026-07 the chat lane emits German with ASCII substitutions, mixed
  word by word inside a single reply (measured: both spellings of the same word 15
  characters apart). That is a model property, not something this codebase causes.

The cost was measured, and it is not cosmetic. `_detect_ungrounded_result_claim` looks
for `ausgeführt` and `bestätigt` to decide whether a model claimed success it did not
earn - so a model that transliterates walks straight past the guard built to catch it.
The outgoing-mail risk gate looks for `überweisung`, which a phishing mail spelling it
`ueberweisung` never trips. Ninety-four sites had this shape.

**The rule this module exists to make possible: nobody writes `ae`, `oe`, `ue` or `ss`
by hand, anywhere.** Source stays proper German. The tolerance is generated here, at the
comparison, and never by doubling a list.

Two functions, because one cannot serve both shapes:

- `contains_any` / `contains_any_word` fold BOTH sides and answer yes or no. Use for
  plain membership tests.
- `expand` rewrites the PATTERN and leaves the subject string untouched. Use for regular
  expressions, where folding the subject would be wrong: folding changes length (one
  character becomes two), so capture-group offsets shift and an extracted value would
  come back in its folded form rather than as the user wrote it.

## Two hazards, both measured, both with a rule

**Do not use this on an umlaut PRESENCE test.** Code that asks "does this text contain
any umlaut at all" (a language heuristic) must keep comparing raw characters. Folding
would turn the needles into `ae`/`oe`/`ue`/`ss`, and `ss` occurs in most English prose
(`success`, `assistant`, `less`), so the test would answer yes for English.

**Prefer `contains_any_word` for whole words.** Folding creates substrings that did not
exist before: `über` folds to `ueber`, which sits inside `blueberry`; `spaß` folds to
`spass`, which sits inside `trespass`. Measured against 48262 English dictionary entries,
`blueberry` was the only real collision across 577 needles, but one is enough - a
language heuristic reading "I want a blueberry muffin recipe" answered German. Use
`contains_any` only for deliberate stem matching (`erinner`, `überarbeit`).
"""
from __future__ import annotations

import re
from typing import Iterable

# The German transliteration convention. Folding goes TOWARD ASCII on purpose: the
# reverse direction (ae -> ä) is not a function, it is a guess, and it destroys
# Michael, Israel, Manuel and queue.
_FOLD = {
    "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
    "Ä": "ae", "Ö": "oe", "Ü": "ue", "ẞ": "ss",
}

_FOLD_RE = re.compile("[" + "".join(_FOLD) + "]")

# A quantifier directly after an umlaut would rebind onto the generated group:
# "hä+" becomes "h(?:ä|ae)+", which newly matches "haeae". Refused rather than
# silently changed. No pattern in the tree has this shape today.
_QUANTIFIERS = "*+?{"


def fold(text: str) -> str:
    """Lowercase `text` and replace each umlaut with its ASCII transliteration.

    For COMPARISON only. Never store or display the result: it is a lossy form that
    exists to make two spellings of the same word meet.
    """
    if not text:
        return ""
    return _FOLD_RE.sub(lambda m: _FOLD[m.group()], text).lower()


def fold_all(needles: Iterable[str]) -> frozenset:
    """The folded form of a whole vocabulary, for EXACT membership and set intersection.

    `contains_any` answers "does this text contain one of these"; several sites instead ask
    "is this token exactly one of these" (`t in acks`, stopword filtering, token-set
    intersection). Those need the needles folded once, up front:

        _ACKS = fold_all({"ja", "bin zurück"})
        if fold(reply) in _ACKS: ...

    Build it at module level, not per call.
    """
    return frozenset(fold(n) for n in needles if n)


def contains_any(text: str, needles: Iterable[str]) -> bool:
    """True if any needle occurs anywhere in `text`, both sides folded.

    Substring semantics, so it matches stems: `überarbeit` hits `überarbeitet`. That
    reach is also the risk - see the module docstring on `blueberry`. For whole words,
    use `contains_any_word`.
    """
    if not text:
        return False
    folded = fold(text)
    return any(fold(n) in folded for n in needles if n)


def contains_any_word(text: str, needles: Iterable[str]) -> bool:
    """True if any needle occurs in `text` as a whole word, both sides folded.

    The needle may be a phrase; word boundaries are applied at its outer edges only, so
    `schritt für schritt` still matches inside a sentence. This is the safe default for
    lists of complete words, because it is what keeps `über` out of `blueberry`.
    """
    if not text:
        return False
    folded = fold(text)
    for needle in needles:
        if not needle:
            continue
        if re.search(r"(?<!\w)" + re.escape(fold(needle)) + r"(?!\w)", folded):
            return True
    return False


def expand(pattern: str) -> str:
    """Rewrite umlauts in a regex PATTERN so it also matches the ASCII spelling.

    `löse` becomes `l(?:ö|oe)se`. The subject string is never touched, so offsets,
    capture groups and extracted values stay exactly as they were. Group NUMBERING is
    preserved as well, because the generated group is non-capturing - that is
    load-bearing, not cosmetic: a capturing group here would turn a `findall` result
    into tuples.

    Refuses rather than guessing in the two cases where a rewrite would change meaning:

    - **Umlaut inside a character class.** `[äöüa-z]` cannot be expanded, because a
      class matches one character and the replacement is two. Inserting the group
      anyway compiles WITHOUT error and silently adds `(`, `)`, `?`, `:` and `|` to the
      accepted set - measured on eleven real patterns, all of them silent. A caller who
      needs this must restructure the pattern.
    - **Quantifier directly after an umlaut**, which would rebind onto the group.
    - **Umlaut inside a lookbehind.** Python requires a fixed-width lookbehind, and the
      generated alternation is one character or two, so `(?<=über)` would expand into a
      pattern `re.compile` rejects outright.

    Escapes and `(?#...)` comments are copied verbatim, never expanded.
    """
    out: list[str] = []
    i, n = 0, len(pattern)
    in_class = False
    # Depth of the innermost lookbehind, so nested groups inside it still count.
    lookbehind_depth = 0
    group_depth = 0
    while i < n:
        ch = pattern[i]
        if not in_class and pattern.startswith(("(?<=", "(?<!"), i):
            lookbehind_depth = group_depth + 1
        if not in_class and ch == "(" and (i == 0 or pattern[i - 1] != "\\"):
            group_depth += 1
        elif not in_class and ch == ")" and (i == 0 or pattern[i - 1] != "\\"):
            if lookbehind_depth and group_depth <= lookbehind_depth:
                lookbehind_depth = 0
            group_depth = max(0, group_depth - 1)
        if ch == "\\" and i + 1 < n:            # escape: copy the pair, never expand
            out.append(pattern[i:i + 2])
            i += 2
            continue
        if not in_class and pattern.startswith("(?#", i):   # comment: copy verbatim
            end = pattern.find(")", i)
            end = n if end < 0 else end + 1
            out.append(pattern[i:end])
            i = end
            continue
        if not in_class and ch == "[":
            in_class = True
            out.append(ch)
            i += 1
            # "^" and a leading "]" are literal members, not class syntax
            if i < n and pattern[i] == "^":
                out.append(pattern[i])
                i += 1
            if i < n and pattern[i] == "]":
                out.append(pattern[i])
                i += 1
            continue
        if in_class and ch == "]":
            in_class = False
            out.append(ch)
            i += 1
            continue
        if ch in _FOLD:
            if lookbehind_depth:
                raise ValueError(
                    f"expand(): umlaut {ch!r} inside a lookbehind cannot be rewritten - Python "
                    f"requires a fixed-width lookbehind and the alternation is one character or "
                    f"two, so the result would not compile. Pattern: {pattern!r}"
                )
            if in_class:
                raise ValueError(
                    f"expand(): umlaut {ch!r} inside a character class cannot be rewritten "
                    f"(a class matches one character, the replacement is two). Inserting the "
                    f"group here compiles but silently accepts '(', ')', '?', ':' and '|'. "
                    f"Restructure the pattern or leave it out. Pattern: {pattern!r}"
                )
            # "" in "*+?{" is True, so the emptiness has to be tested first: a pattern
            # ENDING in an umlaut has no next character and must not be refused.
            nxt = pattern[i + 1] if i + 1 < n else ""
            if nxt and nxt in _QUANTIFIERS:
                raise ValueError(
                    f"expand(): quantifier {nxt!r} directly after umlaut {ch!r} would apply to "
                    f"the generated group instead of the character, which changes what the "
                    f"pattern matches. Rewrite it explicitly. Pattern: {pattern!r}"
                )
            out.append(f"(?:{ch}|{_FOLD[ch]})")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def compile_de(pattern: str, flags: int = 0) -> re.Pattern:
    """`re.compile(expand(pattern), flags)`.

    Call this at module level, once. `expand` is not idempotent in length: applying it
    twice grows the pattern (22 characters to 29 to 36) while staying semantically
    correct, so a repeated call in a hot path is waste, not a bug.
    """
    return re.compile(expand(pattern), flags)
