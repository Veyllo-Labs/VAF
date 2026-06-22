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
    expected = err.schema.get("type") if isinstance(err.schema, dict) else None
    if expected and path:
        return f"field '{path}' expects {expected}, got {type(err.instance).__name__}"
    return err.message


def repair_tool_input(schema: Any, args: Any):
    """Validate ``args`` against ``schema``; repair common weak-model shape errors.

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

    errs = list(Draft202012Validator(schema).iter_errors(args))
    if not errs:
        return args, [], []  # never touch valid input

    # Only repair fields that THEMSELVES fail validation. A field that is already
    # valid must never be touched just because a *different* field errored — that
    # would silently corrupt good input (e.g. unwrapping an untyped field, or
    # stripping a legitimately-null optional). Field-level errors carry the
    # property name in absolute_path; a missing-required error has an empty path
    # and nothing to repair (the value is absent).
    error_fields = {e.absolute_path[0] for e in errs if e.absolute_path}

    required = set(schema.get("required") or [])
    repaired = dict(args)
    applied: list[str] = []

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
