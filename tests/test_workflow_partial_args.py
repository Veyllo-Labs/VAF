# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A step's input must survive the presence of partial args.

Live incident: the model authored a run_temp workflow in exactly the shape the
tool schema teaches - the instruction in `input`, the extra parameters in
`args` ({"max_results": 3}) - and the engine's args path built the tool call
ONLY from args, silently dropping the input. web_search ran query-less and the
whole workflow failed with "Error: No query provided." although the author had
supplied everything.

Contract pinned here: when a step carries input + partial args, the resolved
input fills the tool's missing PRIMARY parameter; steps whose args already
carry the primary parameter (every saved template) are untouched and keep
their input as a display label.
"""
from vaf.workflows.engine import WorkflowEngine, WorkflowStep


class _Capture:
    def __init__(self, result="ok"):
        self.calls = []
        self._result = result

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return self._result


def _run(steps, tools):
    engine = WorkflowEngine(tools=tools, callback=lambda *a, **k: None)
    return engine.execute(steps, variables={})


def test_input_fills_the_missing_primary_parameter():
    search = _Capture("### results wetter")
    steps = [
        WorkflowStep(tool="web_search", input_template="Suche das aktuelle Wetter",
                     args_template={"max_results": 3}, output_name="weather"),
        WorkflowStep(tool="web_search", input_template="Suche die neuesten Nachrichten",
                     args_template={"max_results": 3}, output_name="news"),
    ]
    result = _run(steps, {"web_search": search})
    assert result.success, result.error
    assert search.calls[0]["query"] == "Suche das aktuelle Wetter"
    assert search.calls[0]["max_results"] == 3
    assert search.calls[0].get("deep") is False  # parity with the input-only path
    assert search.calls[1]["query"] == "Suche die neuesten Nachrichten"


def test_primary_already_in_args_keeps_input_as_label():
    """Saved-template shape: args carry the real query; input is a label and
    must NOT overwrite it."""
    search = _Capture()
    steps = [
        WorkflowStep(tool="web_search", input_template="{query}",
                     args_template={"query": "real query", "max_results": 5},
                     output_name="research"),
    ]
    result = _run(steps, {"web_search": search})
    assert result.success, result.error
    assert search.calls[0]["query"] == "real query"


def test_merged_input_resolves_placeholders():
    search = _Capture("wetterdaten")
    coder = _Capture("done")
    steps = [
        WorkflowStep(tool="web_search", input_template="wetter berlin",
                     args_template={"max_results": 3}, output_name="weather"),
        WorkflowStep(tool="coding_agent", input_template="Baue HTML aus: {weather}",
                     args_template={"project_path": "/tmp/x"}, output_name="html"),
    ]
    result = _run(steps, {"web_search": search, "coding_agent": coder})
    assert result.success, result.error
    assert "wetterdaten" in coder.calls[0]["task"]      # placeholder resolved
    assert coder.calls[0]["project_path"] == "/tmp/x"   # partial args kept
