# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A message nobody asked for gets its own bubble, live and after a reload.

The agent writes on its own in three places: a thinking-run question, the
"are you there?" nudge that chases it, and an automation result. All three go out
through `emit_agent_message_append`, whose documented contract is "a complete,
standalone message that is always appended as its own new bubble - never streamed
or merged in-place".

The transcript broke that contract on the way to the screen. It groups a turn's
assistant messages into ONE actions timeline, a turn being the span between two
USER messages, and the turn's LAST assistant supplies the visible answer. An
unanswered proactive message has no user message in front of it, so it fell into
the previous turn, became its last assistant - and the reply the user had already
read was folded away into the collapsed rail while "Hey, are you there?" took its
place as that turn's answer. Reported as "the new message overwrites the old one".

Two halves are pinned here, because either alone leaves the defect:

- the transcript keeps a `kind`-tagged message out of the grouping entirely
  (never an anchor, never the answer, never a rail step, never consumed);
- every proactive lane TAGS its message and PERSISTS the tag, since after a reload
  `kind` is the only thing that distinguishes such a message from the tail of the
  turn before it.
"""
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_PAGE = _REPO / "web" / "app" / "page.tsx"
_VAF = _REPO / "vaf"

# The one caller that deliberately passes no assistant `kind`: the timer wake card is
# emitted as role="user", which is already a turn boundary in the transcript, and the
# stored history carries the trigger as a plain user message.
_ROLE_USER_EMITTER = "vaf/core/headless_runner.py"


def _src(path: Path) -> str:
    # CRLF-normalised: git can check this out with CRLF on the Windows CI runner.
    assert path.exists(), f"{path} is missing"
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def _code(path: Path) -> str:
    """Source without comments, so a guard cannot be satisfied by the comment
    that explains it."""
    src = _src(path)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(line.split("//", 1)[0] for line in src.split("\n"))


def _call_text(src: str, start: int) -> str:
    """The full argument list of the call starting at `start` (balanced parens)."""
    open_idx = src.index("(", start)
    depth = 0
    for i in range(open_idx, len(src)):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                return src[open_idx:i + 1]
    raise AssertionError("unbalanced call at offset %d" % start)


def _emit_sites():
    """Every `emit_agent_message_append(...)` call in the product."""
    out = []
    for path in sorted(_VAF.rglob("*.py")):
        src = _src(path)
        for m in re.finditer(r"\.emit_agent_message_append\b", src):
            rel = path.relative_to(_REPO).as_posix()
            line = src.count("\n", 0, m.start()) + 1
            out.append((rel, line, _call_text(src, m.end())))
    return out


def test_every_proactive_emit_tags_its_message():
    sites = _emit_sites()
    assert len(sites) >= 3, f"expected the known proactive lanes, found {len(sites)}"
    for rel, line, args in sites:
        assert "kind=" in args, (
            f"{rel}:{line} emits a proactive message without a `kind`. Untagged, the web "
            "transcript cannot tell it from the tail of the turn before it and folds it "
            "into that turn's actions timeline, where it replaces the visible answer."
        )


def test_persisted_proactive_messages_carry_the_same_tag():
    """Emitting the tag is not enough - the reload path reads the stored message."""
    for rel, line, args in _emit_sites():
        if rel == _ROLE_USER_EMITTER:
            continue                      # role="user" is already a turn boundary
        assert 'role="user"' not in args.replace("'", '"'), (
            f"{rel}:{line} emits a user-role card; add it to the named exception above "
            "with the reason, or tag it like the assistant lanes."
        )
    # The lanes persist through SessionManager.append_background_message (the one
    # way a background lane writes into a chat); the primitive itself is where the
    # kind reaches Message.add_message.
    for rel in ("vaf/core/thinking_mode.py", "vaf/core/automation.py"):
        src = _src(_REPO / rel)
        assert re.search(r"append_background_message\([^)]*kind=", src, flags=re.S), (
            f"{rel} persists a proactive message without its `kind`: it comes back from "
            "disk indistinguishable from an ordinary reply and is folded into the "
            "preceding turn on the next chat load."
        )
    primitive = _src(_REPO / "vaf/core/session.py").split("def append_background_message(")[1]
    assert re.search(r"add_message\([^)]*kind=kind", primitive), (
        "the background-append primitive drops the kind on the way to the stored message"
    )


def test_the_transcript_keeps_a_proactive_message_out_of_the_turn_grouping():
    code = _code(_PAGE)
    assert "const isStandalone = (m: typeof vm[number]) => m.role === 'assistant' && !!m.kind;" in code, (
        "the predicate that recognises a proactive bubble is gone from the turn grouping"
    )
    # The grouping walks its span three times: to count the turn's assistants and tools,
    # to build the rail steps, and to mark rows as consumed. A proactive message must be
    # skipped in ALL three - one miss puts it back in the timeline in a different way
    # (as the answer, as a 'say' step, or as a row that renders nowhere at all).
    assert code.count("if (isStandalone(vm[k])) continue;") == 3, (
        "expected all three passes of the turn grouping to skip proactive messages"
    )
    grouping = code[code.index("const isStandalone"):code.index("return visibleMessages.map")]
    for needle in ("assistants.push(k)", "actions.push({ kind: 'say'", "consumedVmIdx.add(k)"):
        before = grouping.index(needle)
        assert "if (isStandalone(vm[k])) continue;" in grouping[:before], (
            f"the pass containing `{needle}` no longer skips proactive messages"
        )
