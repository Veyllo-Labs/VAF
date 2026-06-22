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
