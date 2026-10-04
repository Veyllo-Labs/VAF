# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Code Audit (vaf/core/code_audit.py): scope from git, deterministic evidence, a review that
must quote the code, verification before anything is reported, and a completion contract a
failed run cannot pass. The model is a fake `ask`; each test names the mutation it catches.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from vaf.core import code_audit as ca

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          check=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@example.org")
    _git(r, "config", "user.name", "T")
    (r / "app.py").write_text("def total(items):\n    return sum(items)\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    return r


def _change(repo: Path, rel: str, text: str) -> None:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text, encoding="utf-8")


BUGGY = ("def total(items):\n"
         "    result = 0\n"
         "    for i in range(1, len(items)):\n"
         "        result += items[i]\n"
         "    return result\n")


class _Model:
    """A fake reviewer and verifier: answers the review with `findings`, the verification
    with `verdict` for every finding, and records what it was shown."""

    def __init__(self, findings, verdict="CONFIRMED", review_raw=None):
        self.findings, self.verdict, self.review_raw = findings, verdict, review_raw
        self.seen = []

    def __call__(self, messages, max_tokens):
        system, user = messages[0]["content"], messages[-1]["content"]
        self.seen.append(user)
        if "verify code review findings" in system:
            n = user.count("--- finding f")
            return json.dumps([{"id": f"f{i}", "verdict": self.verdict, "reason": "seen"}
                               for i in range(n)])
        if "evaluate repository checks" in system:
            return json.dumps([{"name": "has tests", "result": "failed", "reason": "none"}])
        if self.review_raw is not None:
            return self.review_raw
        return "Here is my review:\n```json\n" + json.dumps({
            "summary": "Rewrites total.", "files": {"app.py": "loop instead of sum"},
            "effort": 2, "findings": self.findings}) + "\n```"


OFF_BY_ONE = {"file": "app.py", "start_line": 1, "end_line": 1, "type": "issue",
              "severity": "major", "category": "correctness", "effort": "low",
              "title": "The loop skips the first item",
              "explanation": "range starts at 1, so items[0] is never added.",
              "suggestion": "for i in range(len(items)):",
              "evidence": "for i in range(1, len(items)):"}


def test_a_verified_finding_is_reported_where_its_quote_is(repo):
    """The model said line 1; the quote is on line 3. MUTATION: trust the model's line."""
    _change(repo, "app.py", BUGGY)
    report = ca.code_audit(str(repo), ask=_Model([OFF_BY_ONE]), remember=False)
    assert report.status == "complete", report.status_reason
    [f] = report.findings
    assert (f.file, f.start_line, f.verified, f.severity) == ("app.py", 3, True, "major")
    assert f.id and report.summary == "Rewrites total." and report.effort == 2


def test_a_quote_that_is_not_in_the_code_is_dropped(repo):
    """MUTATION: report a finding whose evidence the file does not contain."""
    _change(repo, "app.py", BUGGY)
    made_up = dict(OFF_BY_ONE, evidence="while True: pass")
    report = ca.code_audit(str(repo), ask=_Model([made_up]), remember=False)
    assert report.findings == [] and report.rejected == 1


def test_a_finding_the_verifier_rejects_is_dropped(repo):
    """MUTATION: skip the verification call."""
    _change(repo, "app.py", BUGGY)
    report = ca.code_audit(str(repo), ask=_Model([OFF_BY_ONE], verdict="REJECTED"),
                           remember=False)
    assert report.findings == [] and report.rejected == 1


def test_an_unconfirmed_finding_is_kept_apart_without_a_fix_prompt(repo):
    """The verifier gave no verdict: reported apart, never as something to change code for,
    and the run is not complete. MUTATION: count an unconfirmed finding as verified."""
    _change(repo, "app.py", BUGGY)
    report = ca.code_audit(str(repo), ask=_Model([OFF_BY_ONE], verdict="MAYBE"),
                           remember=False)
    assert report.findings == [] and len(report.unverified) == 1
    assert report.unverified[0].fix_prompt() == ""


def test_the_fix_prompt_keeps_the_findings_data(repo):
    """MUTATION: drop the untrusted-data preamble."""
    _change(repo, "app.py", BUGGY)
    report = ca.code_audit(str(repo), ask=_Model([OFF_BY_ONE]), remember=False)
    prompt = report.fix_prompt()
    assert prompt.startswith(ca.UNTRUSTED_PREAMBLE)
    assert "@app.py around lines 3-3" in prompt and "range(len(items))" in prompt


def test_an_unreadable_review_is_never_a_clean_result(repo):
    """MUTATION: treat a model answer without JSON as "no findings"."""
    _change(repo, "app.py", BUGGY)
    report = ca.code_audit(str(repo), ask=_Model([], review_raw="I looked, all fine."),
                           remember=False)
    assert report.status == "failed" and report.exit_code() == 2


def test_without_a_model_only_the_analyzers_run_and_it_says_so(repo):
    _change(repo, "app.py", BUGGY)
    report = ca.code_audit(str(repo), ask=None, remember=False)
    assert report.status == "incomplete" and report.exit_code("none") == 2


def test_a_hardcoded_key_is_a_critical_finding_without_any_model(repo):
    """MUTATION: drop the secret pass."""
    key = "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2"
    _change(repo, "cfg.py", f'API_KEY = "{key}"\n')
    report = ca.code_audit(str(repo), ask=None, remember=False)
    [f] = [x for x in report.findings if x.source == "secrets"]
    assert f.severity == "critical" and f.category == "security" and f.file == "cfg.py"
    assert key not in report.to_text() and key not in report.to_json()


def test_the_model_never_sees_a_credential(repo):
    """Neither from the change nor from a guideline file beside it. MUTATION: send the
    context unredacted, or the guidelines unredacted."""
    key = "sk-" + "Z9y8X7w6V5u4T3s2R1q0P9o8"
    rules_key = "sk-" + "Q1w2E3r4T5y6U7i8O9p0A1s2"
    _change(repo, "AGENTS.md", f'Use the staging key API_KEY = "{rules_key}" in tests.\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "rules")
    _change(repo, "cfg.py", f'API_KEY = "{key}"\nTIMEOUT = 3\n')
    model = _Model([])
    ca.code_audit(str(repo), ask=model, scope="uncommitted", remember=False)
    assert any("staging key" in s for s in model.seen)
    assert model.seen and not any(key in s or rules_key in s for s in model.seen)


@pytest.mark.skipif(shutil.which("ruff") is None and not (Path(__import__("sys").executable).parent / "ruff").exists(),
                    reason="needs ruff")
def test_ruff_reports_only_on_changed_lines(repo):
    """An undefined name on an unchanged line is not this change's finding. MUTATION: keep
    every ruff diagnostic."""
    _change(repo, "app.py", "def total(items):\n    return sum(items) + undefined_old\n")
    _git(repo, "commit", "-qam", "old bug")
    _change(repo, "app.py", "def total(items):\n    return sum(items) + undefined_old\n"
                            "\n\ndef avg(items):\n    return total(items) / count_new\n")
    report = ca.code_audit(str(repo), ask=None, scope="uncommitted", remember=False)
    ruff = [f for f in report.findings if f.source == "ruff"]
    assert [f.start_line for f in ruff] == [6] and "count_new" in ruff[0].title


def test_scope_committed_ignores_the_working_tree(repo):
    """MUTATION: diff against the working tree for "committed"."""
    _change(repo, "app.py", BUGGY)
    _git(repo, "commit", "-qam", "bug")
    _change(repo, "extra.py", "x = 1\n")
    report = ca.code_audit(str(repo), scope="committed", ask=None, remember=False)
    assert report.files_reviewed == ["app.py"]
    uncommitted = ca.code_audit(str(repo), scope="uncommitted", ask=None, remember=False)
    assert uncommitted.files_reviewed == ["extra.py"]


def test_generated_and_lock_files_are_skipped_and_listed(repo):
    """MUTATION: review everything the diff names."""
    _change(repo, "node_modules/lib/index.js", "x\n")
    _change(repo, "package-lock.json", "{}\n")
    _change(repo, "src/real.py", "y = 2\n")
    report = ca.code_audit(str(repo), scope="uncommitted", ask=None, remember=False)
    assert report.files_reviewed == ["src/real.py"]
    assert {p for p, _ in report.files_skipped} >= {"package-lock.json"}


def test_one_root_cause_in_two_places_is_one_finding(repo):
    """MUTATION: drop the deduplication."""
    _change(repo, "app.py", BUGGY + "\n\ndef again(items):\n    for i in range(1, len(items)):\n"
                                     "        pass\n")
    second = dict(OFF_BY_ONE, start_line=9, end_line=9)
    report = ca.code_audit(str(repo), ask=_Model([OFF_BY_ONE, second]), remember=False)
    reviewed = [f for f in report.findings if f.source == "review"]
    assert len(reviewed) == 1 and [loc[1] for loc in reviewed[0].locations] == [3, 9]


def test_the_profile_hides_nitpicks_unless_assertive(repo):
    _change(repo, "app.py", BUGGY)
    nit = dict(OFF_BY_ONE, type="nitpick", severity="trivial", title="Name result total",
               evidence="result = 0")
    chill = ca.code_audit(str(repo), ask=_Model([nit]), remember=False)
    assert chill.findings == [] and chill.hidden_by_profile == 1
    loud = ca.code_audit(str(repo), ask=_Model([nit]), profile="assertive", remember=False)
    assert len(loud.findings) == 1


def test_ids_are_stable_and_addressed_findings_are_noticed(repo):
    """MUTATION: drop the comparison with the last audit."""
    _change(repo, "app.py", BUGGY)
    first = ca.code_audit(str(repo), ask=_Model([OFF_BY_ONE]))
    again = ca.code_audit(str(repo), ask=_Model([OFF_BY_ONE]))
    assert first.findings[0].id == again.findings[0].id
    fixed = ca.code_audit(str(repo), ask=_Model([]))
    assert [a["id"] for a in fixed.addressed] == [first.findings[0].id]


def test_a_dismissed_finding_is_not_reported_again(repo):
    """MUTATION: ignore the dismissed list."""
    _change(repo, "app.py", BUGGY)
    first = ca.code_audit(str(repo), ask=_Model([OFF_BY_ONE]))
    assert ca.dismiss_finding(str(repo), first.findings[0].id, "intended: starts at 1")
    later = ca.code_audit(str(repo), ask=_Model([OFF_BY_ONE]))
    assert later.findings == [] and later.dismissed == 1


def test_a_failed_error_check_fails_the_run(repo):
    _change(repo, ".vaf/code-audit.json", json.dumps(
        {"checks": [{"name": "has tests", "instructions": "Every change adds a test.",
                     "mode": "error"}]}))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "config")
    _change(repo, "app.py", BUGGY)
    report = ca.code_audit(str(repo), ask=_Model([]), remember=False)
    assert [c.result for c in report.checks] == ["failed"] and report.exit_code() == 1


def test_not_a_repository_is_a_failed_audit(tmp_path):
    report = ca.code_audit(str(tmp_path), ask=None)
    assert report.status == "failed" and report.exit_code() == 2


def test_the_guidelines_of_the_changed_path_reach_the_model(repo):
    """AGENTS.md beside the changed file is part of the review. MUTATION: drop guidelines."""
    _change(repo, "src/AGENTS.md", "Never use floats for money.\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "rules")
    _change(repo, "src/pay.py", "price = 1.5\n")
    model = _Model([])
    ca.code_audit(str(repo), ask=model, remember=False)
    assert any("Never use floats for money." in s for s in model.seen)


def test_the_json_parser_survives_prose_and_fences():
    assert ca._json_from('noise {"a": [1, {"b": "}"}]} tail', "{") == {"a": [1, {"b": "}"}]}
    assert ca._json_from("```json\n[1, 2]\n```", "[") == [1, 2]
    assert ca._json_from("no json here", "{") is None
