# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""YouTube Summary workflow template guards.

The template runs the caption recipe an agent composed ad-hoc over host_bash
fully inside the sandbox. These tests pin the properties that
make it safe: sandbox-only lane, honest no-subs/rate-limit behavior instead of
hallucination, armed step validation, and brace-safety of the embedded Python
code under the engine's {variable} substitution (a brace block containing a dot
is treated as a nested lookup and fails at runtime).
"""
import re

from vaf.workflows.engine import WorkflowEngine
from vaf.workflows.templates import get_template


def _tpl():
    t = get_template("youtube_summary")
    assert t, "youtube_summary template must load"
    return t


def test_template_loads_with_expected_steps():
    t = _tpl()
    assert [s["tool"] for s in t["steps"]] == ["python_sandbox", "coding_agent", "write_file"]
    assert t["defaults"]["filename"].endswith(".md")
    assert "video_url" in t["variables"]


def test_generation_step_has_validation_armed():
    # First built-in template with armed step validation - a script-as-artifact
    # must not pass as success again (Research & Code lesson).
    t = _tpl()
    assert t["steps"][1].get("validate") is True


def test_sandbox_step_uses_packages_not_host():
    t = _tpl()
    args = t["steps"][0]["args"]
    assert "yt-dlp" in args.get("packages", []), "yt-dlp must install into the sandbox, not the host"
    assert "skip_download=True" in args["code"], "never download the video itself"
    # Rate-limit lesson (live: 67s of 429s while the caption track existed): ONE
    # metadata call via the yt_dlp API, then the SIGNED caption URL - no
    # per-language subtitle FILE downloads.
    assert "extract_info" in args["code"]
    assert "automatic_captions" in args["code"]


def test_summary_step_forbids_self_fetching():
    # An agentic coder that receives a failure marker + the URL will otherwise
    # improvise its own transcript hunt (live: 6.2 minutes, 23 loops).
    t = _tpl()
    prompt = t["steps"][1]["input"]
    assert "ABSOLUT VERBOTEN" in prompt
    assert "task_done" in prompt


def test_honesty_markers_wired_through():
    t = _tpl()
    code = t["steps"][0]["args"]["code"]
    prompt = t["steps"][1]["input"]
    for marker in ("NO_SUBTITLES_AVAILABLE", "RATE_LIMITED_BY_YOUTUBE"):
        assert marker in code, f"sandbox step lost the {marker} marker"
        assert marker in prompt, f"summary step no longer handles {marker} honestly"
    assert "NIEMALS" in prompt


def test_no_dotted_brace_blocks_anywhere():
    # {foo.bar} triggers the engine's nested-lookup path and raises at runtime -
    # embedded Python code must never contain a brace block with a dot.
    t = _tpl()
    strings = [t["steps"][0]["args"]["code"], t["steps"][1]["input"],
               t["steps"][2]["args"]["path"], t["steps"][2]["args"]["content"]]
    for s in strings:
        bad = [m for m in re.findall(r"\{([^}]+)\}", s) if "." in m and "|" not in m]
        assert not bad, f"dotted brace block(s) would break engine substitution: {bad}"


def test_code_survives_variable_resolution():
    t = _tpl()
    eng = WorkflowEngine({})
    resolved = eng._resolve_template(
        t["steps"][0]["args"]["code"],
        {"video_url": "https://youtu.be/abc123"}, t.get("defaults"),
    )
    assert "https://youtu.be/abc123" in resolved
    assert "{video_url}" not in resolved
    # the cleaning regexes must survive substitution byte-identically
    assert 'r"<[^>]+>"' in resolved and 'r"\\s+"' in resolved
    compile(resolved, "<workflow-step>", "exec")  # must stay valid Python
