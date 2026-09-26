# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Build the argument preview a human approves a tool call by.

An approval dialog is a security control only while the rendered text equals
the text that will execute. Measured before this module existed: a U+202E in a
`host_bash` command reached the browser unchanged (and a `<pre>` applies bidi,
so the visible order reverses), an `Authorization: Bearer sk-...` was rendered
in full, and the 300-character cut left no trace - a truncated command looked
exactly like a short one. That matters most on the one lane docs/security/
SANDBOXING.md deliberately leaves unsandboxed BECAUSE the human approval is the
control.

Three transformations, in this order:
1. `sanitize_args` (vaf/core/subagent_debug.py) - the funnel's existing heavy
   field stripper, so a 2 MB file body never becomes a preview.
2. `neutralize` - characters that change how text READS without changing what
   it MEANS become visible markers.
3. `redact` - credential material becomes a placeholder.

Then the string is cut with a marker that fits INSIDE the limit, because the
event field is documented as at most 300 characters (docs/OBSERVABILITY.md).

Stdlib-only: this runs on the dispatch hot path, before any tool executes.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Tuple

# Bidi controls, zero-width characters and invisible separators: the set is
# shared with the skill scanner (vaf/skills/scanner.py), which flags the same
# codepoints as "smuggled instructions".
HIDDEN_CHARS = frozenset({
    0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF,          # zero width
    0x200E, 0x200F, 0x061C,                          # directional marks
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E,          # embedding / override
    0x2066, 0x2067, 0x2068, 0x2069,                  # isolates
    0x00AD,                                          # soft hyphen
})

# A secret VALUE as it appears in the preview. The preview is JSON text, so a quote the
# command carried arrives escaped (\"...\"); the old bare-word pattern stopped at that
# backslash and "redacted" nothing but the backslash, while the dialog claimed a secret
# had been hidden. Never a value that is already the placeholder (two rules must not
# count one secret twice), and never a $VARIABLE, which names a secret without being one.
_VALUE = r"""(?!\[redacted\])(?!\$)(?:\\"(?:[^"\\]|\\[^"])*\\"|'[^']*'|[^\s"'\\&;]+)"""

# Names that carry a secret, in an assignment (DB_PASSWORD=..., ?token=...) or as a JSON
# key ("password": "..."). "pass" also catches FTP_PASS and passwd.
_SECRET_NAME = r"[A-Za-z0-9_]*?(?:pass|secret|token|api_?key|access_?key|private_?key)[A-Za-z0-9_]*"

# Shared with the access-log filter (vaf/core/log_helper.py), which masks the same
# assignments in request lines: group 1 is kept, the value after it is the secret.
SECRET_ASSIGNMENT = re.compile(r"((?:^|[^A-Za-z0-9_])" + _SECRET_NAME + r"=)" + _VALUE,
                               re.IGNORECASE)

_SECRET_PATTERNS = (
    # (regex, group to keep) - the keep group preserves the surrounding syntax
    # so the reader still sees WHAT was passed, only not its value.
    # A JSON field whose name says it is a secret: a tool argument like {"password": ...}.
    (re.compile(r'("' + _SECRET_NAME + r'"\s*:\s*")(?!\[redacted\])(?:[^"\\]|\\.)+(?=")',
                re.IGNORECASE), r"\1"),
    (re.compile(r"(Bearer\s+)(?!\$)[A-Za-z0-9._~+/=\-]{8,}", re.IGNORECASE), r"\1"),
    (re.compile(r"(Authorization:\s*Basic\s+)[A-Za-z0-9+/=]+", re.IGNORECASE), r"\1"),
    (re.compile(r"((?:X-Api-Key|X-Auth-Token|X-Access-Token|Api-Key|Private-Token)\s*:\s*)"
                + _VALUE, re.IGNORECASE), r"\1"),
    # URL userinfo: the user stays readable, the password goes, the @ and host stay. The
    # value runs to the LAST @ of the segment - a password may contain one.
    (re.compile(r'(//[^/\s:@"\\]+:)(?!\[redacted\])[^/\s"\\]*(?=@)'), r"\1"),
    # --password x / --password=x / --token ... on any command line.
    (re.compile(r"((?:^|[\s\"'])--?(?:password|passwd|pass|token|api[-_]?key|secret|"
                r"client[-_]secret|access[-_]token)(?:=|\s+))" + _VALUE, re.IGNORECASE), r"\1"),
    # curl/wget style user:password (-u, --user, --proxy-user), attached or quoted.
    (re.compile(r"((?:^|[\s\"'])(?:-u|--user|--proxy-user)(?:=|\s*)(?:\\\"|')?[^\s:\"'\\,]+:)"
                r"(?!\[redacted\])(?!\$)[^\s\"'\\]+"), r"\1"),
    # lftp -u user,password
    (re.compile(r"((?:^|[\s\"'])-u\s+(?:\\\"|')?[^\s:\"'\\,]+,)(?!\[redacted\])(?!\$)[^\s\"'\\]+"),
     r"\1"),
    (re.compile(r"(\bsshpass\s+-p\s*)" + _VALUE), r"\1"),
    # mysql -psecret: the password is glued to the flag (a bare -p prompts instead).
    (re.compile(r"(\b(?:mysql|mysqldump|mysqladmin|mariadb)\b[^\n\"]*?\s-p)(?!\[redacted\])"
                r"(?!\$)(?=[^\s-])[^\s\"'\\]+"), r"\1"),
    (re.compile(r"\b(sk-|rk-|pk_live_|ghp_|gho_|ghu_|ghs_|ghr_|xox[baprs]-)"
                r"[A-Za-z0-9_\-]{8,}"), r"\1"),
    (re.compile(r"\b(AKIA)[0-9A-Z]{12,}"), r"\1"),
    (SECRET_ASSIGNMENT, r"\1"),
)

REDACTED = "[redacted]"
TRUNCATION_MARK = "... [cut]"


def neutralize(text: str) -> Tuple[str, int]:
    """Replace invisible/direction-changing characters with visible markers.

    Returns (text, count). A dialog that renders the result cannot be made to
    read differently from what runs.
    """
    if not text:
        return text, 0
    out = []
    count = 0
    for ch in text:
        if ord(ch) in HIDDEN_CHARS:
            out.append(f"[U+{ord(ch):04X}]")
            count += 1
        else:
            out.append(ch)
    return "".join(out), count


_SECRET_KEY = re.compile(r"^" + _SECRET_NAME + r"$", re.IGNORECASE)


def mask_secret_args(args: Any, declared: Tuple[str, ...] | list = ()) -> Any:
    """A copy of a tool's arguments with every credential replaced by [redacted]: the
    arguments the tool DECLARES as secret (BaseTool.secret_args) and any key whose name says
    it is one (password, token, api_key, FTP_PASS, ...). For everything that records or shows
    a call - the tool-use log, the timeline, the event stream, the confirmation dialog.
    Anything that is not a dict comes back unchanged."""
    if not isinstance(args, dict):
        return args
    declared = set(declared or ())
    out: Dict[str, Any] = {}
    for key, value in args.items():
        if value not in (None, "") and (key in declared or _SECRET_KEY.match(str(key))):
            out[key] = REDACTED
        elif isinstance(value, dict):
            out[key] = mask_secret_args(value)
        else:
            out[key] = value
    return out


def redact(text: str) -> Tuple[str, int]:
    """Replace credential material with a placeholder. Returns (text, count)."""
    if not text:
        return text, 0
    count = 0
    for pattern, keep in _SECRET_PATTERNS:
        text, n = pattern.subn(lambda m, k=keep: m.expand(k) + REDACTED, text)
        count += n
    return text, count


def build_preview(tool_name: str, args: Dict[str, Any] | None,
                  limit: int = 300) -> Dict[str, Any]:
    """The preview plus what had to be done to it.

    Returns {"text", "truncated", "neutralized", "redacted"}. Never raises: a
    preview is a convenience, and an argument that cannot be encoded must not
    turn a security decision into an exception.
    """
    result = {"text": "", "truncated": False, "neutralized": 0, "redacted": 0}
    try:
        from vaf.core.subagent_debug import sanitize_args
        from vaf.core.tool_dispatch import make_json_serializable
        payload = make_json_serializable(args or {})
        # sanitize_args replaces a field with len/sha256/preview. That is right
        # for a log, wrong for a dialog: the human is approving THIS command and
        # must read it. So only the fields that are actually oversized go
        # through it; short ones stay verbatim and get neutralized/redacted.
        if isinstance(payload, dict) and any(
            isinstance(v, (str, bytes)) and len(v) > limit for v in payload.values()
        ):
            payload = sanitize_args(tool_name, payload)
        text = json.dumps(payload, ensure_ascii=False)
    except Exception:
        try:
            text = str(args or {})
        except Exception:
            return result

    text, n_hidden = neutralize(text)
    text, n_secret = redact(text)
    if len(text) > limit:
        # The marker lives INSIDE the limit: the field is documented as at most
        # `limit` characters, and a dialog must be able to tell a short command
        # from a cut one.
        text = text[: max(0, limit - len(TRUNCATION_MARK))] + TRUNCATION_MARK
        result["truncated"] = True
    result["text"] = text
    result["neutralized"] = n_hidden
    result["redacted"] = n_secret
    return result


def preview_notes(preview: Dict[str, Any]) -> str:
    """One human line for what the preview had to change, or ''."""
    bits = []
    if preview.get("neutralized"):
        n = preview["neutralized"]
        bits.append(f"{n} hidden character{'s' if n != 1 else ''} made visible")
    if preview.get("redacted"):
        n = preview["redacted"]
        bits.append(f"{n} secret{'s' if n != 1 else ''} redacted")
    if preview.get("truncated"):
        bits.append("arguments truncated")
    return "; ".join(bits)
