# Tool Input Validation & Repair

VAF validates the arguments a model supplies for a tool call against that tool's
declared JSON schema, and repairs the common shape mistakes that weak local
models make before the tool runs. This sits at the dispatch boundary, behind the
stable tool contract (Layer 2 in [ARCHITECTURE.md](../ARCHITECTURE.md)).

Module: `vaf/core/tool_input_repair.py` (pure, side-effect free).
Call site: `Agent.execute_tool` in `vaf/core/agent.py`, immediately after the
model arguments are taken and **before** any runtime kwargs are injected — so the
validator compares exactly the model-supplied arguments against the declared
`parameters` schema.

## Why

VAF's default backend is a local Qwen model. Such models often emit arguments
that are valid JSON but the wrong shape for the schema. Before this layer, those
arguments either ran silently as `{}` or surfaced to the model as an opaque
`Tool Error:`. There was no validation: a tool's `parameters` schema was only
sent to the model, never used to check the response.

## Pipeline

For one tool call:

1. If the tool has no schema, or the arguments already validate, return them
   unchanged. **Valid input is never modified.**
2. Otherwise attempt the repairs below, per schema property, in a fixed order.
3. Re-validate. Repaired arguments are dispatched; remaining schema errors are
   returned to the model as a localized message and the tool does not run.

### Repairs

Applied per property, based on the property's declared type. The order is
invariant — R2 runs before R4 so a stringified array is parsed, not wrapped.

| Id | When | Action |
|----|------|--------|
| R0 | the tool's `input_aliases` names a synonym for a property, the canonical key is absent, and exactly one alias is present | move the value under the canonical key (rename, no value change). Runs first, before validation, so a pure-alias input dispatches cleanly |
| R2 | array field, value is a string that looks like a JSON array (`'["a","b"]'`) | `json.loads`; use it if the result is a list |
| R1 | optional field (not in `required`), value is `null` | drop the key so the tool's own default applies |
| R3 | non-object field, value is a single-key object (`{"value": [...]}`) | unwrap to the inner value |
| R4 | array field, value is a non-empty string (`"urgent"`) | wrap as `["urgent"]` |

R0 is conservative on purpose: it never overwrites a canonical key the model
already supplied, and it does nothing when two aliases are present at once
(ambiguous). Because it only renames, it is safe for the excluded `content` /
`code` fields (the value is not coerced). Aliases live on the tool's
`input_aliases` attribute, NOT in the `parameters` schema, so the unknown
keyword never reaches a model-facing tool definition (a strict provider such
as Google Gemini could otherwise reject the whole tool). Example: `write_file`
maps `path` <- `file_path`/`filepath`/`filename`/`file` and `content` <-
`message`/`text`/`body`/`data`, so a weak model's `{file_path, message}` call
is remapped and dispatched instead of lost (incident: a local model's HTML
write silently failed, then the model reported the file as created).

### Excluded fields

Fields named `content` and `code` are never coerced (e.g. `write_file.content`,
`python_exec.code`, `python_sandbox.code`). Repairing a free-text or source-code
payload would corrupt it. Declared string fields are also left untouched by the
array repairs by construction.

## Errors

If arguments still violate the schema after repair (for example a missing
required field, or a type the repairs cannot fix), the tool is not dispatched
and the model receives:

```
Tool Error: invalid arguments for '<tool>': <detail>
```

The `Tool Error:` prefix is intentional: the existing error detection
(`is_err`) and the Whare Wananga reactive-retry (which re-feeds learned tool
know-how on failure) recognise tool failures by this prefix. Reusing it means
the model's self-correction path is unchanged — only the message is now
localized to the offending field instead of an opaque exception.

## Telemetry

When a repair is applied, an event is recorded through `log_timeline_event`
(`vaf/core/log_helper.py`), written when debug logs are enabled:

```
event_type = "tool_input_repaired"
tool       = <tool name>
model      = <model_display_name>
repairs    = ["attachment_paths: bare-string-wrap", ...]
```

This shows which models mis-shape which tool inputs, and how often.

## Examples

| Model sends | Schema | Dispatched as |
|-------------|--------|---------------|
| `{"tags": "urgent"}` | `tags: array` | `{"tags": ["urgent"]}` |
| `{"tags": "[\"a\",\"b\"]"}` | `tags: array` | `{"tags": ["a", "b"]}` |
| `{"limit": null}` | `limit: integer` (optional) | `{}` (tool default applies) |
| `{"attachment_paths": "/x.pdf"}` | `attachment_paths: array` | `{"attachment_paths": ["/x.pdf"]}` |
| `{}` | `to: string` (required) | not run; `Tool Error: ... 'to' is a required property` |

## For tool authors

- Declare an accurate `parameters` schema. It is now used at runtime, not just
  shown to the model. Mark genuinely optional fields as not `required`; a
  declared-required field that is missing after repair stops the call.
- Array fields benefit most: a single value sent as a bare string is wrapped
  instead of failing.
- `content` / `code` fields are passed through verbatim.
- Set `input_aliases = {canonical: [synonyms]}` on the tool class to catch
  common synonym mistakes (e.g. `path` <- `file_path`). Kept off the schema so
  it is never sent to a model. The alias remap (R0) does the rest.
- Repair (and therefore R0) runs only on the MAIN agent / sub-agent dispatch
  path (`Agent.execute_tool`). The coder and the workflow engine have their
  own tool loops and do not call it, so aliases do not help there.

## Notes

- The layer is fully defensive: any internal failure is a no-op and dispatch
  proceeds as before.
- Individual tools may keep their own local normalization as defense-in-depth
  (for direct callers that bypass dispatch); `send_mail` wraps a single
  attachment path locally as well.

See also: [TOOL_ROUTER_ARCHITECTURE.md](TOOL_ROUTER_ARCHITECTURE.md) for where
this sits in `execute_tool`, and [TOOL_SUPERVISION.md](TOOL_SUPERVISION.md) for
tool execution bounds and the error conventions.
