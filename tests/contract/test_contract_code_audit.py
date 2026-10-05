# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Contract: `vaf.code_audit` (docs/EMBEDDING.md, "Reviewing a code change").

What an embedder's own coding agent or CI step is built on: the model is a parameter
(`ask(messages, max_tokens) -> str`), a finding is reported only when it is verified, a
verified finding carries a fix prompt, and a run that could not review is never a clean
result - `status` and `exit_code()` say so. Wording is not pinned; shapes and outcomes are.
"""
import json
import shutil
import subprocess

import pytest

import vaf

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "c@example.org"],
                 ["config", "user.name", "C"]):
        subprocess.run(["git", *args], cwd=r, check=True, capture_output=True)
    (r / "m.py").write_text("def f(xs):\n    return xs[0]\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=r, check=True, capture_output=True)
    (r / "m.py").write_text("def f(xs):\n    return xs[len(xs)]\n", encoding="utf-8")
    return r


def _ask(messages, max_tokens):
    """One answer for every call, so the test keys on no prompt wording: the review reads the
    JSON object, the verification the first JSON array in it, which is the verdict list, and
    the deep check the object's own verdict."""
    return json.dumps({
        "verdict": "CONFIRMED", "confidence": 95, "reason": "xs[len(xs)] is past the end",
        "verdicts": [{"id": f"f{n}", "verdict": "CONFIRMED", "reason": "always out of range"}
                     for n in range(8)],
        "summary": "s", "files": {}, "effort": 1, "findings": [{
            "file": "m.py", "start_line": 2, "end_line": 2, "type": "issue", "severity": "major",
            "category": "correctness", "effort": "low", "title": "Index out of range",
            "explanation": "len(xs) is one past the end.", "suggestion": "xs[-1]",
            "evidence": "return xs[len(xs)]"}]})


def test_a_verified_finding_comes_with_its_fix_prompt(tmp_path):
    report = vaf.code_audit(str(_repo(tmp_path)), scope="uncommitted", ask=_ask, remember=False)
    assert isinstance(report, vaf.AuditReport) and report.status == "complete"
    [finding] = report.findings
    assert isinstance(finding, vaf.AuditFinding) and finding.verified
    assert (finding.file, finding.start_line, finding.severity) == ("m.py", 2, "major")
    assert "m.py" in report.fix_prompt() and report.exit_code() == 1


def test_without_a_model_the_run_is_not_complete(tmp_path):
    report = vaf.code_audit(str(_repo(tmp_path)), scope="uncommitted", ask=None, remember=False)
    assert report.status == "incomplete" and report.exit_code() == 2


def test_a_model_that_answers_nothing_usable_fails_the_run(tmp_path):
    report = vaf.code_audit(str(_repo(tmp_path)), scope="uncommitted",
                            ask=lambda m, n: "looks fine to me", remember=False)
    assert report.status == "failed" and report.findings == []


def test_it_never_raises_outside_a_repository(tmp_path):
    report = vaf.code_audit(str(tmp_path), ask=_ask)
    assert report.status == "failed"
