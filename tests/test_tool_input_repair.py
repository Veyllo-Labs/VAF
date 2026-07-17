# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
from vaf.core.tool_input_repair import repair_tool_input

ARR = {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}}}, "required": []}
OPT = {"type": "object", "properties": {"limit": {"type": "integer"}}, "required": []}
REQ = {"type": "object", "properties": {"to": {"type": "string"}}, "required": ["to"]}


def test_valid_input_untouched():
    out, applied, errors = repair_tool_input(ARR, {"tags": ["a", "b"]})
    assert out == {"tags": ["a", "b"]}
    assert applied == [] and errors == []


def test_r4_bare_string_wrap():
    out, applied, errors = repair_tool_input(ARR, {"tags": "urgent"})
    assert out == {"tags": ["urgent"]}
    assert "tags: bare-string-wrap" in applied and errors == []


def test_r2_stringified_array_parsed_not_wrapped():
    # R2 must win over R4: '["a","b"]' -> ["a","b"], NOT ['["a","b"]']
    out, applied, errors = repair_tool_input(ARR, {"tags": '["a","b"]'})
    assert out == {"tags": ["a", "b"]}
    assert "tags: json-array-parse" in applied
    assert "tags: bare-string-wrap" not in applied and errors == []


def test_r1_null_on_optional_stripped():
    out, applied, errors = repair_tool_input(OPT, {"limit": None})
    assert "limit" not in out
    assert "limit: null-strip" in applied and errors == []


def test_r3_unwrap_placeholder():
    out, applied, errors = repair_tool_input(ARR, {"tags": {"value": ["a"]}})
    assert out == {"tags": ["a"]}
    assert "tags: unwrap-placeholder" in applied and errors == []


def test_missing_required_surfaces_error():
    out, applied, errors = repair_tool_input(REQ, {})
    assert errors and any("required" in e.lower() for e in errors)


def test_protected_content_field_never_coerced():
    sch = {
        "type": "object",
        "properties": {"content": {"type": "string"}, "tags": {"type": "array"}},
        "required": [],
    }
    # content looks like a JSON array but is a string field -> must stay verbatim,
    # while the neighbouring real array field is still repaired.
    out, applied, errors = repair_tool_input(sch, {"content": '["not","parsed"]', "tags": "x"})
    assert out["content"] == '["not","parsed"]'
    assert out["tags"] == ["x"]


def test_untyped_valid_field_untouched_when_another_field_errors():
    # A valid value for an UNTYPED field must NOT be unwrapped just because a
    # different (required) field is missing. (Regression: R3 over-reach.)
    schema = {
        "type": "object",
        "properties": {"meta": {"description": "free-form"}, "to": {"type": "string"}},
        "required": ["to"],
    }
    out, applied, errors = repair_tool_input(schema, {"meta": {"foo": "bar"}})
    assert out["meta"] == {"foo": "bar"}
    assert not applied
    assert errors  # 'to' still missing


def test_nullable_optional_not_stripped_when_another_field_errors():
    # A legitimately-null value (type allows "null") must NOT be stripped just
    # because a different field errors. (Regression: R1 over-reach.)
    schema = {
        "type": "object",
        "properties": {"opt": {"type": ["string", "null"]}, "to": {"type": "string"}},
        "required": ["to"],
    }
    out, applied, errors = repair_tool_input(schema, {"opt": None})
    assert "opt" in out and out["opt"] is None
    assert not applied
    assert errors  # 'to' still missing


def test_validation_error_message_matches_error_prefix_convention():
    # Part C returns "Tool Error: ..." for unrepairable schema errors. It MUST keep
    # matching the is_err / Whare-Wananga reactive-retry prefix set (mirrored here
    # from agent.py) so the model's self-correction still fires.
    _PREFIXES = ("error", "failed", "tool error", "security error", "exception", "❌")
    out, applied, errors = repair_tool_input(REQ, {})  # missing required 'to'
    assert errors
    msg = "Tool Error: invalid arguments for 'send_mail': " + "; ".join(errors)
    assert msg.lower().startswith(_PREFIXES)


def test_send_mail_string_attachment_is_wrapped_not_dropped():
    from vaf.tools.send_mail import SendMailTool
    schema = SendMailTool().parameters
    out, _applied, _errors = repair_tool_input(
        schema, {"to": "a@b.c", "subject": "s", "body": "b", "attachment_paths": "/x.pdf"}
    )
    assert out["attachment_paths"] == ["/x.pdf"]


def test_enum_violation_not_rendered_as_type_mismatch():
    # Live bug: tasks.0.status carried a VALID string that
    # violated an enum, but _localize rendered every field error with the type
    # wording -> "expects string, got str", which the model cannot repair from.
    schema = {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "enum": ["pending", "done"]},
                    },
                },
            },
        },
    }
    _out, _applied, errors = repair_tool_input(schema, {"tasks": [{"status": "in-progress"}]})
    assert errors, "enum violation must surface an error"
    assert "got str" not in errors[0], errors
    assert "is not one of" in errors[0], errors
    assert "tasks.0.status" in errors[0], errors


def test_real_type_mismatch_keeps_expects_got_wording():
    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
    }
    _out, _applied, errors = repair_tool_input(schema, {"count": "many"})
    assert errors and "expects integer, got str" in errors[0], errors


def test_r0_key_aliases_remap_to_canonical_names():
    """Live incident: a weak local model sent write_file
    {file_path, message} and the write was silently lost. R0 remaps declared
    aliases to the canonical property name before validation."""
    from vaf.core.tool_input_repair import repair_tool_input

    schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path"],
    }
    aliases = {"path": ["file_path", "filename"], "content": ["message", "text"]}
    rep, applied, errors = repair_tool_input(
        schema, {"file_path": "/tmp/x.html", "message": "<html>"}, aliases
    )
    assert rep == {"path": "/tmp/x.html", "content": "<html>"}
    assert errors == []
    assert "path: alias<-file_path" in applied
    assert "content: alias<-message" in applied


def test_r0_never_clobbers_a_supplied_canonical_key():
    from vaf.core.tool_input_repair import repair_tool_input

    schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    rep, applied, errors = repair_tool_input(
        schema, {"path": "real.txt", "file_path": "stray"}, {"path": ["file_path"]}
    )
    assert rep["path"] == "real.txt"  # canonical wins; alias not applied
    assert not any(a.startswith("path: alias") for a in applied)


def test_r0_alias_value_is_byte_identical_for_protected_content():
    from vaf.core.tool_input_repair import repair_tool_input

    schema = {
        "type": "object",
        "properties": {"content": {"type": "string"}},
        "required": ["content"],
    }
    payload = '["not","a","real","array"] <- must survive verbatim'
    rep, applied, errors = repair_tool_input(schema, {"message": payload}, {"content": ["message"]})
    assert rep["content"] == payload  # renamed, never coerced


def test_r0_ambiguous_aliases_are_left_alone():
    from vaf.core.tool_input_repair import repair_tool_input

    schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    # Two aliases present -> ambiguous -> do not guess.
    rep, applied, errors = repair_tool_input(
        schema, {"file_path": "a", "filename": "b"}, {"path": ["file_path", "filename"]}
    )
    assert not any(a.startswith("path: alias") for a in applied)
    assert errors  # still missing required 'path'


def test_write_file_aliases_live_and_off_the_model_schema():
    """The real WriteFileTool: input_aliases repair the incident args, and the
    model-facing parameters schema carries NO custom 'aliases' keyword (so a
    strict provider cannot reject the tool)."""
    from jsonschema import Draft202012Validator

    from vaf.core.tool_input_repair import repair_tool_input
    from vaf.tools.filesystem import WriteFileTool

    tool = WriteFileTool()
    rep, applied, errors = repair_tool_input(
        tool.parameters, {"file_path": "/tmp/x.html", "message": "<html>"},
        tool.input_aliases,
    )
    assert rep == {"path": "/tmp/x.html", "content": "<html>"}
    assert not errors

    # Schema is a valid JSON Schema and declares no 'aliases' anywhere.
    Draft202012Validator.check_schema(tool.parameters)
    import json
    assert '"aliases"' not in json.dumps(tool.parameters)


def test_write_file_file_content_alias_from_live_incident():
    """Live incident: a 4B model burned four write_file calls on
    file_content= before stumbling onto a mapped name - 44-step turn. The
    observed alias (and the obvious 'contents' variant) must repair."""
    from vaf.core.tool_input_repair import repair_tool_input
    from vaf.tools.filesystem import WriteFileTool

    tool = WriteFileTool()
    rep, applied, errors = repair_tool_input(
        tool.parameters, {"path": "wetter.html", "file_content": "<html>"},
        tool.input_aliases,
    )
    assert rep == {"path": "wetter.html", "content": "<html>"}
    assert not errors

    rep2, _, errors2 = repair_tool_input(
        tool.parameters, {"path": "wetter.html", "contents": "<html>"},
        tool.input_aliases,
    )
    assert rep2 == {"path": "wetter.html", "content": "<html>"}
    assert not errors2


def test_python_exec_task_and_script_aliases_from_live_incident():
    """Same incident: python_exec called with task= failed schema validation
    twice. task (observed) and script (the other common name for a code
    payload) must repair to code; a supplied canonical code= is never
    clobbered."""
    from vaf.core.tool_input_repair import repair_tool_input
    from vaf.tools.python_exec import PythonExecTool

    tool = PythonExecTool()
    for alias in ("task", "script"):
        rep, applied, errors = repair_tool_input(
            tool.parameters, {alias: "print(1)"}, tool.input_aliases,
        )
        assert rep == {"code": "print(1)"}, alias
        assert not errors, alias

    rep, _, _ = repair_tool_input(
        tool.parameters, {"code": "print(1)", "task": "IGNORED"}, tool.input_aliases,
    )
    assert rep.get("code") == "print(1)"  # canonical key wins, never clobbered


def test_repair_without_aliases_arg_is_unchanged():
    from vaf.core.tool_input_repair import repair_tool_input

    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    rep, applied, errors = repair_tool_input(schema, {"file_path": "x"})
    assert errors  # no aliases passed -> still missing 'path'
    assert not any(a.startswith("path: alias") for a in applied)
