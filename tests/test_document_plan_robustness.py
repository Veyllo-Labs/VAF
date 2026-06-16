"""Robustness of document_agent plan creation against weak-model output.

document_agent asks the configured (often small, local) model for a document plan.
Small models routinely emit trailing commas, get truncated by max_tokens, or wrap the
JSON in prose. These tests pin the repair + fallback chain so plan creation never hard
-fails with "Could not create document plan": lenient JSON repair, a coder-style plain
section-title list fallback, and a deterministic default as last resort.
"""
import json

from vaf.tools.document_agent import DocumentAgentTool


def _bare() -> DocumentAgentTool:
    """A tool shell without BaseTool.__init__ — the plan helpers need no setup."""
    return DocumentAgentTool.__new__(DocumentAgentTool)


def test_repair_trailing_comma():
    fixed = DocumentAgentTool._repair_json('{"a":1,"b":[1,2,],}')
    assert json.loads(fixed) == {"a": 1, "b": [1, 2]}


def test_repair_truncated_nested_closes_in_order():
    # Truncated mid-array-of-objects (classic max_tokens cut-off).
    truncated = '{"title":"X","sections":[{"title":"Intro","description":"a"},{"title":"Body"'
    fixed = DocumentAgentTool._repair_json(truncated)
    data = json.loads(fixed)
    assert data["title"] == "X"
    assert data["sections"][0]["title"] == "Intro"
    assert data["sections"][1]["title"] == "Body"


def test_repair_ignores_brackets_inside_strings():
    fixed = DocumentAgentTool._repair_json('{"note":"a [b] {c}","x":1')
    assert json.loads(fixed) == {"note": "a [b] {c}", "x": 1}


def test_extract_json_from_prose_and_codeblock():
    t = _bare()
    assert t._extract_json_from_response('Sure! ```json\n{"sections":[1]}\n```')["sections"] == [1]
    # truncated, wrapped in prose -> repaired
    got = t._extract_json_from_response('Here is the plan: {"title":"R","sections":[{"title":"A"}')
    assert got["title"] == "R" and got["sections"][0]["title"] == "A"


def test_infer_helpers():
    assert DocumentAgentTool._infer_format("Erstelle einen Bericht als PDF") == "pdf"
    assert DocumentAgentTool._infer_format("write a report") == "docx"
    assert DocumentAgentTool._infer_doc_type("Erstelle einen Arbeitsvertrag") == "contract"
    assert DocumentAgentTool._infer_doc_type("write a cover letter") == "letter"
    assert DocumentAgentTool._infer_title("Bitte erstelle mir einen Projektbericht für Q3").startswith("Projektbericht")


def test_sanitize_rejects_chain_of_thought_keeps_json():
    # JSON / structured output is preserved.
    assert DocumentAgentTool.sanitize_model_text('{"a":1}') == '{"a":1}'
    # <think> blocks and code fences stripped.
    assert DocumentAgentTool.sanitize_model_text('<think>plan...</think>```json\n{"a":1}\n```') == '{"a":1}'
    # Pure chain-of-thought is rejected (empty) so the caller's fallback fires.
    assert DocumentAgentTool.sanitize_model_text("Okay, the user wants a contract. Let me think...") == ""
    assert DocumentAgentTool.sanitize_model_text("Thinking Process: 1. Analyze the request") == ""


def test_section_lines_fallback_parses_plain_list():
    t = _bare()
    t.generate_text = lambda **kw: "1. Introduction\n- Background\n* Analysis\nConclusion\n"
    plan = t._plan_from_section_lines("write a report", "docx", "My Report")
    titles = [s["title"] for s in plan["sections"]]
    assert titles == ["Introduction", "Background", "Analysis", "Conclusion"]
    assert plan["filename"].endswith(".docx")


def test_create_plan_never_returns_none_even_on_garbage():
    t = _bare()
    t.generate_text = lambda **kw: "I cannot do that."   # never valid JSON nor a usable list
    plan = t._create_document_plan("Erstelle einen Arbeitsvertrag")
    assert plan is not None
    assert plan["document_type"] == "contract"
    assert len(plan["sections"]) >= 2
    assert plan["format"] == "docx"


def test_create_plan_format_from_task_not_model():
    t = _bare()
    # Model spuriously picks pdf; the task asks for no specific format -> default docx
    # (the format is decided by the task, not the model's whim). Filename follows.
    t.generate_text = lambda **kw: '{"document_type":"contract","title":"Kaufvertrag","format":"pdf","filename":"Kaufvertrag.pdf","sections":[{"title":"A","description":"a"},{"title":"B","description":"b"}]}'
    plan = t._create_document_plan("Erstelle einen Kaufvertrag für ein MacBook")
    assert plan["format"] == "docx"
    assert plan["filename"].endswith(".docx")


def test_create_plan_uses_json_when_valid():
    t = _bare()
    good = '{"document_type":"report","title":"R","format":"md","sections":[{"title":"A","description":"a"},{"title":"B","description":"b"}]}'
    t.generate_text = lambda **kw: good
    plan = t._create_document_plan("write something as markdown")
    assert plan["format"] == "md"
    assert [s["title"] for s in plan["sections"]] == ["A", "B"]
