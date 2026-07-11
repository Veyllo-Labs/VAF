# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Workflow runner tool overlay + variable extraction guards.

Live incident (session yellow305153): @youtube_summary ran in the CLI workflow
subprocess, whose hand-maintained tool dict lacked python_sandbox -> the first
step failed with "Tool not found" although the same workflow worked through the
in-chat executor (which overlays the live agent registry). And the variable
extractor turned the video URL into filename="//www.youtube.com". These tests
pin the shared single-source overlay and the URL-blind extraction.
"""
import re

from vaf.workflows.templates import WORKFLOW_TEMPLATES
from vaf.workflows.tool_overlay import workflow_primitives


# ── the CI guard for the incident class ──────────────────────────────────────

def test_every_builtin_template_step_tool_is_constructible_headless():
    # The @workflow CLI subprocess has NO live agent registry: every tool any
    # built-in template names must come from the shared primitives builder.
    available = set(workflow_primitives())
    missing = {}
    for wf_id, tpl in WORKFLOW_TEMPLATES.items():
        for step in tpl.get("steps", []):
            tool = step.get("tool")
            if tool and tool not in available:
                missing.setdefault(wf_id, []).append(tool)
    assert not missing, (
        f"built-in templates name tools the headless runner cannot construct: {missing} "
        f"- add them to vaf/workflows/tool_overlay.py"
    )


def test_primitives_include_python_sandbox():
    assert "python_sandbox" in workflow_primitives()


def test_runners_share_the_single_source():
    # Both runners must build their registry from tool_overlay - a reborn
    # hand-maintained copy is exactly how the drift happened.
    from pathlib import Path
    import vaf.tools.workflow_executor as ex_mod
    import vaf.cli.cmd.workflow as cli_mod
    for mod in (ex_mod, cli_mod):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "workflow_primitives" in src, f"{mod.__name__} no longer uses the shared overlay"


# ── URL-blind variable extraction ────────────────────────────────────────────

def _extract(var_name: str, text: str):
    from vaf.workflows.selector import WorkflowSelector
    sel = WorkflowSelector.__new__(WorkflowSelector)
    return sel._extract_value(text, var_name, "")


def test_filename_extraction_ignores_urls():
    got = _extract("filename",
                   "kannst du mir das https://www.youtube.com/watch?v=WKQ2FD7rMN4 zusammenfassen?")
    assert got != "//www.youtube.com"
    assert got is None or "youtube" not in got, got


def test_filename_extraction_still_finds_real_names():
    got = _extract("filename", "speichere es als bericht_q3.md bitte")
    assert got == "bericht_q3.md"


def test_path_extraction_ignores_urls():
    got = _extract("file_path", "analysiere https://example.com/deep/path/page.html danach")
    assert got is None or "example.com" not in got, got


def test_url_variable_still_extracts_urls():
    got = _extract("video_url", "fasse https://youtu.be/abc123 zusammen")
    assert got == "https://youtu.be/abc123"


# ── TLS-aware subprocess -> backend routing ──────────────────────────────────

def test_internal_api_base_respects_tls_config(monkeypatch):
    import vaf.core.config as config_mod
    from vaf.core.web_interface import internal_api_base
    monkeypatch.setattr(config_mod.Config, "get",
                        staticmethod(lambda k, d=None: True if k == "local_network_tls_enabled" else d))
    assert internal_api_base() == "http://127.0.0.1:8005"
    monkeypatch.setattr(config_mod.Config, "get",
                        staticmethod(lambda k, d=None: False if k == "local_network_tls_enabled" else d))
    assert internal_api_base() == "http://127.0.0.1:8001"


def test_no_hardcoded_plain_http_8001_in_cli_senders():
    # With local_network_tls_enabled the public 8001 port speaks HTTPS: a
    # hardcoded plain-HTTP sender loses every event silently (live incident:
    # the @workflow subprocess's workflow_start never reached the UI, so the
    # SubAgent window showed instead of the Workflow Runtime panel).
    from pathlib import Path
    import vaf.cli.cmd.workflow as wf_mod
    import vaf.cli.cmd.run as run_mod
    for mod in (wf_mod, run_mod):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "http://127.0.0.1:8001" not in src, (
            f"{mod.__name__} hardcodes the public plain-HTTP port - "
            f"use vaf.core.web_interface.internal_api_base()"
        )
