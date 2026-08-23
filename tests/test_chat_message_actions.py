# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Saving and copying a reply works on every host, not just the developer's.

The two lanes behind the actions under an agent reply each had a way of failing
silently on a host nobody tested on:

- The clipboard. Six sites called `navigator.clipboard.writeText` by hand and
  four had no fallback. That API exists only in a SECURE context, so for a user
  reaching VAF over plain HTTP on the LAN two of them threw and two did nothing,
  and the code they were meant to hand over never arrived.
- The download. The one existing text-save branched on the PRESENCE of the
  desktop bridge, not on its answer, so a bridge that failed left no file and no
  message, and a cancelled Save dialog fell through to a browser download that
  wrote the file the person had just declined.

Pinned here: the fallbacks exist, the bridge's ANSWER decides, the anchor never
carries the target that hijacks the desktop window, and the reply's markdown
source is the cleaned answer rather than the raw stored message.
"""
import json
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_CLIPBOARD = _REPO / "web" / "lib" / "clipboard.ts"
_DOWNLOAD = _REPO / "web" / "lib" / "download.ts"
_PAGE = _REPO / "web" / "app" / "page.tsx"

# The one place allowed to write to the clipboard without the shared helper: it
# copies the ACTIVE SELECTION, which is the payload itself, so it has nothing to
# hand the helper. Any other entry here is a hand-rolled copy that will fail for
# a LAN user over plain HTTP.
_SELECTION_COPIER = "web/components/CopyOnRightClick.tsx"


def _src(path: Path) -> str:
    # CRLF-normalised: git can check this out with CRLF on the Windows CI runner.
    assert path.exists(), f"{path} is missing"
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def _code(path: Path) -> str:
    """Source without comments. A guard that forbids a token must not trip over
    the comment explaining why it is forbidden."""
    src = _src(path)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(line.split("//", 1)[0] for line in src.split("\n"))


def test_the_clipboard_helper_keeps_a_fallback_for_insecure_contexts():
    code = _code(_CLIPBOARD)
    assert "navigator.clipboard" in code, "the fast path is gone"
    assert "execCommand('copy')" in code, \
        "the execCommand fallback is gone - copying is a silent no-op over plain HTTP on the LAN"
    assert "document.createElement('textarea')" in code, \
        "execCommand copies the SELECTION, so the fallback must manufacture one"


def test_the_download_helper_asks_the_host_before_it_falls_back():
    src = _src(_DOWNLOAD)
    bridge = src.find("save_text_as")
    blob = src.find("createObjectURL")
    assert bridge != -1 and blob != -1, "the download helper lost one of its two paths"
    assert bridge < blob, \
        "the browser download runs before the host's Save dialog - the desktop window needs the dialog first"
    assert "res?.ok" in src, \
        "the bridge's answer is ignored again: a failed save would leave no file and no message"
    assert "res?.cancelled" in src, \
        "a cancelled Save dialog falls through to a browser download, writing the file the person declined"
    code = _code(_DOWNLOAD)
    assert "_blank" not in code and "target" not in code, \
        "an anchor with a target hijacks the desktop window onto the URL instead of downloading"


def test_every_reply_action_copies_the_answer_and_not_the_raw_message():
    """`msg.content` carries the thinking block and the tool-call JSON, and the
    rendered bubble shows only a 300-char preview once a reply is collapsed."""
    src = _src(_PAGE)
    assert "from '@/lib/clipboard'" in src and "from '@/lib/download'" in src, \
        "the chat page no longer uses the shared lanes"
    for call in ("copyText(cleanAnswer)", "downloadText(cleanAnswer, replyFileName(msg.timestamp))"):
        assert call in src, f"the reply actions no longer read cleanAnswer: {call} is missing"
    assert "copyText(msg.content)" not in src and "downloadText(msg.content" not in src, \
        "an action reads the raw stored message, which carries the thinking block and tool-call JSON"


def test_no_surface_hand_rolls_a_clipboard_write_any_more():
    offenders = []
    for sub in ("app", "components"):
        for path in sorted((_REPO / "web" / sub).rglob("*")):
            if path.suffix not in (".ts", ".tsx") or not path.is_file():
                continue
            if ".next" in path.parts or "node_modules" in path.parts:
                continue
            rel = path.relative_to(_REPO).as_posix()
            if rel == _SELECTION_COPIER:
                continue
            if "navigator.clipboard" in path.read_bytes().decode("utf-8"):
                offenders.append(rel)
    assert not offenders, (
        f"hand-rolled clipboard writes are back in {offenders}. Use copyText from lib/clipboard: "
        "navigator.clipboard is undefined outside a secure context, which is every LAN user on plain HTTP."
    )


def test_the_action_labels_exist_in_both_catalogues():
    """A key present in one locale renders as its raw path in the other, and the
    fallback locale here is German, so an English-only key breaks English."""
    keys = ["msgActionDownload", "msgActionCopy", "msgActionCopied"]
    for locale in ("en", "de"):
        cat = json.loads(_src(_REPO / "web" / "messages" / f"{locale}.json"))
        for key in keys:
            assert key in cat.get("main", {}), f"main.{key} is missing from {locale}.json"


def test_read_aloud_sits_with_the_other_actions_on_the_reply():
    """All four things you can do with an answer are one row under it. Read aloud
    used to float beside the bubble on its own, which read as a different kind of
    control than saving or copying the same text."""
    src = _src(_PAGE)
    row = src.split("{isBot && cleanAnswer.trim() !== ''", 1)
    assert len(row) == 2, "the reply action row changed shape"
    # The row ends where the timestamp block does, at the streaming-status line.
    end = "{/* Show status steps below the active message"
    assert end in row[1], "the anchor that bounds the action row is gone"
    body = row[1].split(end, 1)[0]
    for needed in ("handleSpeak(trueIndex, cleanAnswer)", "copyText(cleanAnswer)",
                   "downloadText(cleanAnswer", "regenerate_last_reply"):
        assert needed in body, f"{needed} left the reply action row"
    # The workflow card keeps its OWN read-aloud (it speaks the card's text, not
    # this answer), so exactly one speaker may live outside the row.
    outside = src.split("{isBot && cleanAnswer.trim() !== ''", 1)[0]
    assert outside.count("handleSpeak(") == 1, \
        "a second read-aloud button lives outside the action row again"


def test_the_actions_stay_off_the_bubbles_they_would_lie_about():
    """A workflow bubble's text is the internal marker its card is rendered from,
    and a bubble the agent produced on its own (a timer, a nudge) had no question,
    so "ask again" there would rewind an earlier, unrelated exchange."""
    src = _src(_PAGE)
    assert "!parseWorkflowAsync(answer) && (" in src, \
        "the actions are offered on a workflow bubble, whose text is an internal marker"
    regen = src.split("{isLastMessage && !isGenerating", 1)
    assert len(regen) == 2, "the ask-again gate changed shape"
    assert regen[1].startswith(" && !msg.kind"), \
        "ask again is offered on a proactive bubble, which no question produced"


def test_a_reloaded_chat_keeps_its_own_attachments():
    """The backend has always sent `images` for a user message that carried one.
    The field-by-field rebuild is where an unforwarded field disappears silently,
    which is how this one was lost, and how `diffs` and `activity` were lost before."""
    src = _src(_PAGE)
    rebuild = src.split("const serverMsgs: Array<Message & { _order: number }>", 1)
    assert len(rebuild) == 2, "the history rebuild changed shape"
    block = rebuild[1].split("}));", 1)[0]
    assert "images: m.images" in block, \
        "history_update drops the images field again, so a reloaded chat loses its own pictures"


def test_the_reply_file_name_carries_no_chat_title():
    """A chat title is free text someone typed: spaces, slashes and names that do
    not belong in a file on another person's disk."""
    src = _src(_PAGE)
    body = src.split("function replyFileName", 1)[1].split("\n}", 1)[0]
    assert "vaf-reply-" in body, "the saved reply lost its stable file-name prefix"
    assert not re.search(r"session|title|name", body), \
        "the file name is built from the chat's own text again"
