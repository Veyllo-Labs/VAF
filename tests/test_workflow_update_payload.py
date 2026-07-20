# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Nothing a subprocess sends to the browser may be dropped on the way through.

Every workflow event produced by a SUBPROCESS reaches the Web UI through one bottleneck:
POST /api/workflow/update, parsed into the WorkflowUpdate model and re-serialised for the
broadcast. A field the model does not declare is discarded there without a trace.

That is the CLAUDE.md Rule-2 field-forwarding trap, and it has now cost three fields. In
page.tsx it ate `diffs` and `activity`. Here it ate `success`: the model never declared it,
so every `workflow_done` from the separate workflow runner reached the browser without it,
and the panel - which reads `data.success ? 'completed' : 'failed'` - showed FAILED for a run
whose steps were all green and whose document had been written (live, 2026-07-20, verified in
the child's own event log: workflow_execute_end success=True). `line` was missing too, so a
mirrored output line from a subprocess arrived empty.

Two defences are pinned here: the model declares every field the producers actually send, and
it accepts unknown ones instead of dropping them.
"""
import ast
import re
from pathlib import Path

from vaf.core.web_server import WorkflowUpdate

_REPO = Path(__file__).resolve().parents[1]

# The subprocess producer: vaf/cli/cmd/workflow.py is the separate workflow runner, and its
# send_web_update() POSTs to /api/workflow/update, so every payload it builds passes through
# the model. In-process producers broadcast directly and never touch it.
_PRODUCERS = ("vaf/cli/cmd/workflow.py",)

# Event names that travel through this endpoint. Matching on the VALUE of "type" keeps the
# scan off nested objects that merely happen to carry a "type" key of their own (the UI step
# descriptors in workflow_start are {"id","name","type","status"}).
_EVENT_TYPES = {
    "workflow_start", "workflow_update", "workflow_output_stream", "workflow_done",
    "document_ready", "file_created",
}


def _source(rel: str) -> str:
    # Normalise line endings: git can check these files out with CRLF on the Windows CI
    # runner, and a pattern like ")\n" then finds nothing because a \r sits in between.
    # That is a real CI failure, not a hypothetical (2026-07-20).
    return (_REPO / rel).read_bytes().decode("utf-8").replace("\r\n", "\n")


def _payload_keys(rel: str) -> set:
    """Keys of every dict literal that builds one of the events listed above."""
    keys = set()
    for node in ast.walk(ast.parse(_source(rel))):
        if not isinstance(node, ast.Dict):
            continue
        literal = {}
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                literal[k.value] = v
        kind = literal.get("type")
        if isinstance(kind, ast.Constant) and kind.value in _EVENT_TYPES:
            keys |= set(literal)
    return keys


def test_the_model_declares_every_field_the_producers_send():
    declared = set(WorkflowUpdate.model_fields)
    missing = {}
    for rel in _PRODUCERS:
        undeclared = _payload_keys(rel) - declared
        if undeclared:
            missing[rel] = sorted(undeclared)
    assert not missing, (
        "These payload fields are not declared on WorkflowUpdate. They are only carried "
        "today because the model accepts extras; declare them so the payload stays typed "
        "and documented:\n" + "\n".join(f"  {k}: {v}" for k, v in missing.items())
    )


def test_an_undeclared_field_is_never_silently_dropped():
    """The belt behind the declaration. A future producer that adds a field must not be able
    to lose it just because nobody remembered to touch this model."""
    u = WorkflowUpdate(**{"type": "workflow_done", "sessionId": "s", "aFieldFromTheFuture": 42})
    assert u.model_dump(exclude_none=True).get("aFieldFromTheFuture") == 42


def test_workflow_done_keeps_its_verdict_in_both_directions():
    """THE regression. Both values matter: a dropped True showed a healthy run as FAILED,
    and a dropped False would be worse - a failed run reported as finished."""
    for verdict in (True, False):
        out = WorkflowUpdate(
            **{"type": "workflow_done", "sessionId": "s", "workflowId": "w",
               "success": verdict, "error": "" if verdict else "boom"}
        ).model_dump(exclude_none=True)
        assert out["success"] is verdict, "the run's verdict must survive the round trip"
        assert "error" in out


def test_a_mirrored_output_line_survives():
    out = WorkflowUpdate(
        **{"type": "workflow_output_stream", "sessionId": "s", "workflowId": "w",
           "line": "[Research] 1/8 searching sources..."}
    ).model_dump(exclude_none=True)
    assert out["line"].startswith("[Research]")


def test_the_endpoint_still_serialises_through_the_model():
    """If the endpoint ever stops going through the model, this guard is measuring nothing.
    Pin the shape it relies on."""
    src = _source("vaf/core/web_server.py")
    handler = src[src.index('@app.post("/api/workflow/update")'):]
    handler = handler[:handler.index("\n@app.")]
    assert "update: WorkflowUpdate" in handler
    assert re.search(r"update\.(dict|model_dump)\(", handler), (
        "the payload must be re-serialised from the model, which is what this guard checks"
    )
