# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Auto-attach of previous step results to deliverable-building agent steps.

Live incident: a weak model authored a run_temp workflow (two web searches,
then coding_agent "erstelle ein HTML mit den Ergebnissen"), correctly NAMED the
step outputs but never referenced them via {placeholders} - so the engine's
template substitution had nothing to substitute, the coder received zero data,
and the strict factual-data policy made it render [DATA NOT FOUND] into every
field of an otherwise beautiful page.

Contract pinned here: when a coding_agent/document_writer/document_agent step's
TEMPLATE references no prior step output, the engine appends a bounded
"RESULTS FROM PREVIOUS WORKFLOW STEPS" digest of the actual results to the
step's instruction; templates that DO reference an output (all saved
templates) are never touched; caps bound the attachment.
"""
from vaf.workflows.engine import WorkflowEngine, WorkflowStep


class _SearchTool:
    def __init__(self, text):
        self._text = text

    def run(self, **kwargs):
        return self._text


class _CaptureCoder:
    """Stands in for coding_agent; records the task it receives."""

    def __init__(self):
        self.received = None

    def run(self, **kwargs):
        self.received = kwargs.get("task") or ""
        return "done"


BERLIN = "Wetter Berlin heute: 25 Grad, Gewitter, Regenrisiko 90 Prozent"
NY = "New York heute: Hitzewarnung, Luftqualitaet schlecht, AP: Midtown Hochhaus instabil"


def _run(steps, tools):
    engine = WorkflowEngine(tools=tools, callback=lambda *a, **k: None)
    return engine.execute(steps, variables={})


def test_digest_attached_when_template_has_no_placeholders():
    coder = _CaptureCoder()
    tools = {"web_search": _SearchTool(BERLIN), "web_search2": _SearchTool(NY),
             "coding_agent": coder}
    steps = [
        WorkflowStep(tool="web_search", input_template="suche wetter berlin",
                     output_name="weather_berlin"),
        WorkflowStep(tool="web_search2", input_template="suche new york",
                     output_name="ny_info"),
        # The incident shape: prose reference, no {placeholder} anywhere.
        WorkflowStep(tool="coding_agent",
                     input_template="Erstelle eine HTML-Seite mit den Ergebnissen der vorherigen Suchen",
                     output_name="html_result"),
    ]
    result = _run(steps, tools)
    assert result.success, result.error
    assert coder.received is not None
    assert "RESULTS FROM PREVIOUS WORKFLOW STEPS" in coder.received
    assert BERLIN in coder.received
    assert NY in coder.received
    assert "weather_berlin" in coder.received  # named, so the model can cite


def test_digest_not_attached_when_template_references_an_output():
    """Saved templates author real placeholders - substitution already carries
    the data, the digest must stay away."""
    coder = _CaptureCoder()
    tools = {"web_search": _SearchTool(BERLIN), "coding_agent": coder}
    steps = [
        WorkflowStep(tool="web_search", input_template="suche wetter berlin",
                     output_name="weather_berlin"),
        WorkflowStep(tool="coding_agent",
                     input_template="Erstelle eine HTML-Seite mit: {weather_berlin}",
                     output_name="html_result"),
    ]
    result = _run(steps, tools)
    assert result.success, result.error
    assert BERLIN in coder.received            # substitution did its job
    assert "RESULTS FROM PREVIOUS WORKFLOW STEPS" not in coder.received


def test_digest_only_for_task_consuming_agent_tools():
    """A second search step must not get the digest bolted on - and neither
    must research_agent, whose primary arg is a short topic QUERY (bolting
    result data onto it would pollute the search profile)."""
    captured = {}

    class _SpyingSearch:
        def run(self, **kwargs):
            captured.update(kwargs)
            return NY

    class _SpyingResearch:
        def run(self, **kwargs):
            captured.update(kwargs)
            return "report"

    tools = {"web_search": _SearchTool(BERLIN), "spy_search": _SpyingSearch(),
             "research_agent": _SpyingResearch()}
    steps = [
        WorkflowStep(tool="web_search", input_template="suche wetter berlin",
                     output_name="weather_berlin"),
        WorkflowStep(tool="spy_search", input_template="suche new york",
                     output_name="ny_info"),
        WorkflowStep(tool="research_agent", input_template="quantum computing",
                     output_name="report_out"),
    ]
    result = _run(steps, tools)
    assert result.success, result.error
    assert all("RESULTS FROM PREVIOUS WORKFLOW STEPS" not in str(v)
               for v in captured.values())


def test_digest_reaches_analyzer_agents_too():
    """The hole is not builder-specific: a librarian step saying "analyze the
    data from the previous step" in prose has the same missing-placeholder
    problem and gets the same digest."""
    captured = {}

    class _SpyingLibrarian:
        def run(self, **kwargs):
            captured.update(kwargs)
            return "analysis done"

    tools = {"web_search": _SearchTool(BERLIN), "librarian_agent": _SpyingLibrarian()}
    steps = [
        WorkflowStep(tool="web_search", input_template="suche wetter berlin",
                     output_name="weather_berlin"),
        WorkflowStep(tool="librarian_agent",
                     input_template="Analysiere die Daten aus dem vorherigen Schritt",
                     output_name="analysis"),
    ]
    result = _run(steps, tools)
    assert result.success, result.error
    assert "RESULTS FROM PREVIOUS WORKFLOW STEPS" in str(captured.get("task", ""))
    assert BERLIN in str(captured.get("task", ""))


def test_heavy_agent_steps_get_the_workflow_timeout_floor():
    """The generic 300s subagent cap killed a HEALTHY coder mid-loop at minute
    five (live incident: loop 9, linter green, streaming - SIGTERM). Inside a
    workflow, heavy agent steps get a generous worst-case floor; dead children
    are caught much earlier by heartbeat liveness. Other tools keep their
    normal budgets."""
    from vaf.workflows.engine import _workflow_step_timeout

    assert _workflow_step_timeout("coding_agent") >= 1800
    assert _workflow_step_timeout("research_agent") >= 1800
    assert _workflow_step_timeout("document_agent") >= 1800
    assert _workflow_step_timeout("web_search") <= 300      # generic tools unchanged
    assert _workflow_step_timeout("librarian_agent") <= 300  # fast-return budget kept


def test_digest_is_bounded():
    coder = _CaptureCoder()
    huge = "x" * 20000
    tools = {"web_search": _SearchTool(huge), "web_search2": _SearchTool(huge),
             "web_search3": _SearchTool(huge), "web_search4": _SearchTool(huge),
             "coding_agent": coder}
    steps = [
        WorkflowStep(tool=f"web_search{sfx}", input_template=f"suche {sfx}",
                     output_name=f"out{sfx}")
        for sfx in ("", "2", "3", "4")
    ] + [
        WorkflowStep(tool="coding_agent",
                     input_template="Erstelle ein HTML aus allen Ergebnissen",
                     output_name="html_result"),
    ]
    result = _run(steps, tools)
    assert result.success, result.error
    digest = coder.received.split("RESULTS FROM PREVIOUS WORKFLOW STEPS", 1)[-1]
    # 3000 per result / 9000 total, plus headers - never the raw 80000 chars.
    assert len(digest) < 11000
