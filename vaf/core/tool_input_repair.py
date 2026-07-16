# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Central tool-input validation & repair.

Weak local models (VAF's default is a local Qwen-4B) frequently produce tool
arguments that are valid JSON but the wrong *shape* for the tool's declared
JSON Schema: a bare string where an array is expected, a stringified JSON array,
null for an optional field, or a single-key placeholder object. Today those
either run silently as ``{}`` or surface as an opaque ``Tool Error:``.

This module validates model-supplied args against the tool's ``parameters``
schema and repairs the common shape mistakes BEFORE dispatch. It never touches
valid input, and never rewrites free-text / code fields.

Pure and side-effect free: callers handle telemetry and error presentation.
"""
from __future__ import annotations

import json
from typing import Any

# Fields that carry free-form prose or source code: never coerce these, even if
# a weak model mis-shapes neighbouring args. Wrapping/parsing here would corrupt
# the payload (e.g. write_file.content, python_exec.code / python_sandbox.code).
PROTECTED_FIELDS = frozenset({"content", "code"})


def _type_set(prop: dict) -> set:
    t = prop.get("type")
    if isinstance(t, str):
        return {t}
    if isinstance(t, list):
        return set(t)
    return set()


def _looks_like_json_array(s: str) -> bool:
    s = s.strip()
    return s.startswith("[") and s.endswith("]")


def _localize(err) -> str:
    """Turn a jsonschema ValidationError into a short, model-readable line."""
    if err.validator == "required":
        # err.message reads e.g. "'attachment_paths' is a required property"
        return err.message
    path = ".".join(str(p) for p in err.absolute_path)
    # The expects/got wording ONLY fits actual type failures. Rendering every
    # field error this way turned e.g. an enum violation on a valid string into
    # the nonsense "expects string, got str" (live: update_working_memory
    # tasks.0.status) - the model cannot repair from that. Non-type failures get
    # jsonschema's own message ("'x' is not one of ['pending', 'done']").
    if err.validator == "type" and path:
        expected = err.schema.get("type") if isinstance(err.schema, dict) else None
        if expected:
            return f"field '{path}' expects {expected}, got {type(err.instance).__name__}"
    return f"field '{path}': {err.message}" if path else err.message


def repair_tool_input(schema: Any, args: Any, aliases: Any = None):
    """Validate ``args`` against ``schema``; repair common weak-model shape errors.

    ``aliases`` (optional) maps a canonical property name to a list of synonym
    keys the model might use instead (a tool's ``input_aliases``); R0 remaps a
    present synonym to the canonical name before validation. Kept out of the
    schema so it never reaches a model-facing tool definition.

    Returns ``(repaired_args, applied, errors)``:

    - ``repaired_args`` — a (possibly new) dict; the same content when nothing changed
    - ``applied`` — ``list[str]`` of repairs performed (for telemetry)
    - ``errors`` — ``list[str]`` of remaining, unrepairable schema problems (localized)

    Valid input is returned unchanged with empty ``applied`` / ``errors``.
    The repair order per field is invariant: R2 (json-array-parse) before
    R4 (bare-string-wrap), so ``'["a","b"]'`` becomes ``["a","b"]`` and never
    ``['["a","b"]']``.
    """
    if not isinstance(args, dict) or not isinstance(schema, dict):
        return args, [], []
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return args, [], []

    try:
        from jsonschema import Draft202012Validator
    except Exception:
        # jsonschema unavailable -> behave as a no-op (never block dispatch).
        return args, [], []

    # R0 - key aliases: weak models routinely use a synonym for a property
    # name (write_file: file_path->path, message->content). The tool's
    # input_aliases map (passed in, NOT read from the schema, so no unknown
    # keyword ever reaches a model-facing tool definition) is applied BEFORE
    # the initial validation, so a pure-alias input re-validates clean and
    # dispatches. Conservative: only fills a canonical key that is ABSENT (and
    # is a real schema property), only from an alias that IS present, and never
    # overwrites a value the model already supplied under the real name; two
    # aliases present at once is ambiguous and left alone.
    alias_args = dict(args)
    alias_applied: list[str] = []
    if isinstance(aliases, dict):
        for key, syns in aliases.items():
            if key in alias_args or key not in props or not isinstance(syns, (list, tuple)):
                continue
            present = [a for a in syns if a in alias_args]
            if len(present) == 1:
                src = present[0]
                alias_args[key] = alias_args.pop(src)
                alias_applied.append(f"{key}: alias<-{src}")
    if alias_applied:
        args = alias_args

    errs = list(Draft202012Validator(schema).iter_errors(args))
    if not errs:
        return args, alias_applied, []  # valid after alias remap (or already valid)

    # Only repair fields that THEMSELVES fail validation. A field that is already
    # valid must never be touched just because a *different* field errored — that
    # would silently corrupt good input (e.g. unwrapping an untyped field, or
    # stripping a legitimately-null optional). Field-level errors carry the
    # property name in absolute_path; a missing-required error has an empty path
    # and nothing to repair (the value is absent).
    error_fields = {e.absolute_path[0] for e in errs if e.absolute_path}

    required = set(schema.get("required") or [])
    repaired = dict(args)
    applied: list[str] = list(alias_applied)

    for key, prop in props.items():
        if key not in error_fields:
            continue
        if key in PROTECTED_FIELDS or key not in repaired or not isinstance(prop, dict):
            continue
        types = _type_set(prop)
        val = repaired[key]

        # R2 — stringified JSON array: '["a","b"]' -> ["a","b"] (must precede R4)
        if "array" in types and isinstance(val, str) and _looks_like_json_array(val):
            try:
                parsed = json.loads(val)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                repaired[key] = val = parsed
                applied.append(f"{key}: json-array-parse")

        # R1 — null on an optional field: drop the key so the tool default applies
        if val is None and key not in required:
            del repaired[key]
            applied.append(f"{key}: null-strip")
            continue

        # R3 — single-key placeholder object for a non-object field: unwrap it
        if isinstance(val, dict) and len(val) == 1 and "object" not in types:
            repaired[key] = val = next(iter(val.values()))
            applied.append(f"{key}: unwrap-placeholder")

        # R4 — bare non-empty string where an array is expected: wrap it
        if "array" in types and isinstance(val, str) and val.strip():
            repaired[key] = [val]
            applied.append(f"{key}: bare-string-wrap")

    errors = [_localize(e) for e in Draft202012Validator(schema).iter_errors(repaired)]
    return repaired, applied, errors
