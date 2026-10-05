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


def test_a_claim_about_the_redaction_placeholder_is_dropped(repo):
    """The redaction replaced a value in the code the model saw, and the model reported the
    placeholder as a bug. MUTATION: keep findings about "[redacted]"."""
    _change(repo, "auth.py", 'HINT = {"token_type_hint": "refresh_token"}\n')
    about_placeholder = dict(OFF_BY_ONE, file="auth.py", title="Hint sends the literal [redacted]",
                             explanation="The revocation request sends the redacted placeholder.",
                             evidence='HINT = {"token_type_hint": "[redacted]"}')
    model = _Model([about_placeholder])
    report = ca.code_audit(str(repo), scope="uncommitted", ask=model, remember=False)
    assert any("[redacted]" in s for s in model.seen)        # the model did see the placeholder
    assert report.findings == [] and report.unverified == [] and report.rejected == 1


def test_the_verifier_sees_what_the_quoted_code_calls(repo):
    """"This call can raise" is decided by the call's definition, which lives in another file.
    Measured: three verified majors rested on such a claim and were false. MUTATION: verify
    from the excerpt alone."""
    _change(repo, "helpers.py", "def load_items(path):\n    try:\n        return open(path).read().split()\n"
                                "    except OSError:\n        return []\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "helpers")
    _change(repo, "app.py", "from helpers import load_items\n\n\ndef count(path):\n"
                            "    items = load_items(path)\n    return len(items)\n")
    claim = dict(OFF_BY_ONE, title="load_items can raise and crash count",
                 explanation="An unreadable path raises OSError out of count.",
                 evidence="    items = load_items(path)")
    model = _Model([claim])
    ca.code_audit(str(repo), scope="uncommitted", ask=model, remember=False)
    verify = [s for s in model.seen if "--- finding f0 ---" in s]
    assert verify and "definition of load_items (helpers.py:1)" in verify[0]
    assert "except OSError" in verify[0]


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
    assert report.status == "incomplete"


def test_a_finding_the_verifier_skips_leaves_the_run_incomplete(repo):
    """The verifier answered, but not for this finding. MUTATION: read a missing id as an
    answer."""
    class _Skips(_Model):
        def __call__(self, messages, max_tokens):
            if "verify code review findings" in messages[0]["content"]:
                return "[]"
            return super().__call__(messages, max_tokens)

    _change(repo, "app.py", BUGGY)
    report = ca.code_audit(str(repo), ask=_Skips([OFF_BY_ONE]), remember=False)
    assert len(report.unverified) == 1 and report.status == "incomplete"


def test_a_file_with_a_non_ascii_name_is_reviewed_under_its_name(repo):
    """git quotes such a path with octal escapes by default, a name no file has.
    MUTATION: drop core.quotepath=false."""
    _change(repo, "äpfel.py", "def zähle(items):\n    return len(items) - 1\n")
    _git(repo, "add", "-A")
    _change(repo, "äpfel.py", "def zähle(items):\n    return len(items) - 2\n")
    report = ca.code_audit(str(repo), ask=None, remember=False)
    assert "äpfel.py" in report.files_reviewed, report.files_skipped


def test_a_line_starting_with_plus_plus_inside_a_hunk_is_an_added_line():
    """git writes an added "++i;" as "+++i;"; taken for a file header it was dropped and
    every later line number shifted. MUTATION: skip "+++" lines inside hunks again."""
    diff = ("diff --git a/a.c b/a.c\n--- a/a.c\n+++ b/a.c\n@@ -1,2 +1,4 @@\n"
            " int i;\n+++i;\n+--j;\n return;\n")
    assert ca._hunk_lines(diff) == [2, 3]


def test_a_file_over_the_budget_is_never_read(repo, monkeypatch):
    """MUTATION: read the file before asking the budget."""
    for name in ("a.py", "b.py", "c.py"):
        _change(repo, name, "x = 1\n")
    read = []
    real = ca._worktree_text
    monkeypatch.setattr(ca, "_worktree_text", lambda top, rel: read.append(rel) or real(top, rel))
    report = ca.code_audit(str(repo), scope="uncommitted", ask=None, remember=False, max_files=1)
    assert len(report.files_reviewed) == 1 and len(read) == 1, read
    assert sum(1 for _p, r in report.files_skipped if r.startswith("over the file budget")) == 2


def test_the_no_newline_marker_is_not_counted_as_a_line():
    """A hunk that adds after a line without a final newline. MUTATION: count the marker."""
    diff = ("@@ -1,2 +1,3 @@\n"
            " first\n"
            "-last\n"
            "\\ No newline at end of file\n"
            "+last\n"
            "+added\n")
    assert ca._hunk_lines(diff) == [2, 3]


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


@pytest.mark.skipif(shutil.which("ruff") is None and not (Path(__import__("sys").executable).parent / "ruff").exists(),
                    reason="needs ruff")
def test_ruff_leaves_test_data_and_fixture_imports_alone(repo):
    """Binding to 0.0.0.0 is a finding in a server and test data in a test; a fixture
    imported and taken as an argument is F811 to ruff and the pytest idiom to everyone else.
    MUTATION: report the bandit rules in test files."""
    _change(repo, "tests/test_net.py", 'HOST = "0.0.0.0"\n')
    _change(repo, "server.py", 'HOST = "0.0.0.0"\n')
    report = ca.code_audit(str(repo), ask=None, scope="uncommitted", remember=False)
    ruff = [f for f in report.findings if f.source == "ruff"]
    assert [(f.file, f.title.split(":")[0]) for f in ruff] == [("server.py", "S104")]
    # Not merged in as a second location of the same title either.
    assert all(not loc[0].startswith("tests/") for f in ruff for loc in f.locations)


@pytest.mark.skipif(shutil.which("ruff") is None and not (Path(__import__("sys").executable).parent / "ruff").exists(),
                    reason="needs ruff")
def test_a_heuristic_lint_rule_goes_through_the_verifier(repo):
    """S104 flags a pattern; whether it is a problem here is the verifier's call. Without a
    model it is still reported. MUTATION: keep heuristic ruff findings verified."""
    _change(repo, "server.py", 'HOST = "0.0.0.0"\n')
    alone = ca.code_audit(str(repo), ask=None, scope="uncommitted", remember=False)
    assert [f.title.split(":")[0] for f in alone.findings if f.source == "ruff"] == ["S104"]
    judged = ca.code_audit(str(repo), ask=_Model([], verdict="REJECTED"), scope="uncommitted",
                           remember=False)
    assert [f for f in judged.findings if f.source == "ruff"] == [] and judged.rejected == 1


def test_scope_committed_ignores_the_working_tree(repo):
    """MUTATION: diff against the working tree for "committed"."""
    _change(repo, "app.py", BUGGY)
    _git(repo, "commit", "-qam", "bug")
    _change(repo, "extra.py", "x = 1\n")
    report = ca.code_audit(str(repo), scope="committed", ask=None, remember=False)
    assert report.files_reviewed == ["app.py"]
    uncommitted = ca.code_audit(str(repo), scope="uncommitted", ask=None, remember=False)
    assert uncommitted.files_reviewed == ["extra.py"]


def test_scope_committed_reviews_what_head_holds(repo):
    """The committed file is edited again in the working tree: the review sees the commit.
    MUTATION: read the working-tree file for the committed scope."""
    _change(repo, "app.py", BUGGY)
    _git(repo, "commit", "-qam", "bug")
    _change(repo, "app.py", BUGGY + "\nWORKING_TREE_ONLY = 1\n")
    model = _Model([])
    ca.code_audit(str(repo), scope="committed", ask=model, remember=False)
    assert any("range(1, len(items))" in s for s in model.seen)
    assert not any("WORKING_TREE_ONLY" in s for s in model.seen)


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")


def test_a_symlink_out_of_the_repository_is_never_read(repo, tmp_path):
    """A tracked symlink to a file outside the repository (a key, another account's file) is
    skipped, and its content never reaches the model. MUTATION: follow the link."""
    secret = tmp_path / "outside.txt"
    secret.write_text("OUTSIDE_CONTENT_MARKER\n", encoding="utf-8")
    _symlink(repo / "notes.txt", secret)
    _symlink(repo / "AGENTS.md", secret)
    _change(repo, "app.py", BUGGY)
    model = _Model([])
    report = ca.code_audit(str(repo), scope="uncommitted", ask=model, remember=False)
    assert ("notes.txt", "points outside the repository") in report.files_skipped
    assert not any("OUTSIDE_CONTENT_MARKER" in s for s in model.seen)


def test_a_finding_in_a_file_git_does_not_track_is_dropped(repo, tmp_path):
    """The model may point outside the diff, but only at a tracked file of this repository:
    the change under review can ask for an ignored .env or an absolute path to be quoted.
    MUTATION: read whatever path the model names."""
    (repo / ".gitignore").write_text(".env\n", encoding="utf-8")
    (repo / ".env").write_text("DB_PASSWORD=ENV_FILE_MARKER\n", encoding="utf-8")
    outside = tmp_path / "elsewhere.py"
    outside.write_text("ABSOLUTE_PATH_MARKER = 1\n", encoding="utf-8")
    _change(repo, "app.py", BUGGY)
    asks = [dict(OFF_BY_ONE, file=".env", evidence="DB_PASSWORD=ENV_FILE_MARKER"),
            dict(OFF_BY_ONE, file=str(outside), evidence="ABSOLUTE_PATH_MARKER = 1"),
            dict(OFF_BY_ONE, file="../elsewhere.py", evidence="ABSOLUTE_PATH_MARKER = 1")]
    model = _Model(asks)
    report = ca.code_audit(str(repo), scope="uncommitted", ask=model, remember=False)
    assert report.findings == [] and report.rejected == 3
    shown = model.seen[1:]                       # everything after the review call
    assert not any("ENV_FILE_MARKER" in s or "ABSOLUTE_PATH_MARKER" in s for s in shown)


@pytest.mark.skipif(__import__("os").name == "nt", reason="a shell script as the hook")
def test_reading_a_change_starts_no_program_the_repository_configures(repo, tmp_path):
    """core.fsmonitor in the repository's own config names a program git would run on every
    diff. MUTATION: drop `-c core.fsmonitor=false`."""
    marker = tmp_path / "ran"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    hook.chmod(0o755)
    _git(repo, "config", "core.fsmonitor", str(hook))
    _change(repo, "app.py", BUGGY)
    ca.code_audit(str(repo), ask=None, remember=False)
    assert not marker.exists()


def test_generated_and_lock_files_are_skipped_and_listed(repo):
    """MUTATION: review everything the diff names."""
    _change(repo, "node_modules/lib/index.js", "x\n")
    _change(repo, "package-lock.json", "{}\n")
    _change(repo, "src/real.py", "y = 2\n")
    report = ca.code_audit(str(repo), scope="uncommitted", ask=None, remember=False)
    assert report.files_reviewed == ["src/real.py"]
    assert {p for p, _ in report.files_skipped} >= {"package-lock.json"}


def test_vafs_own_bookkeeping_in_a_project_is_not_reviewed(repo):
    """The coder keeps its task list under .vaf/ in the project; reviewing it fed the task
    text to the reviewer. MUTATION: drop the .vaf/ filter."""
    _change(repo, ".vaf/tasks.json", '{"task": "TASK_TEXT_MARKER"}\n')
    _change(repo, "app.py", BUGGY)
    model = _Model([])
    report = ca.code_audit(str(repo), scope="uncommitted", ask=model, remember=False)
    assert (".vaf/tasks.json", "VAF bookkeeping (.vaf/)") in report.files_skipped
    assert not any("TASK_TEXT_MARKER" in s for s in model.seen)


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


def test_the_same_problem_worded_differently_keeps_its_id(repo):
    """A model words one finding differently on every run; the id follows the code it
    quotes, so a dismissal holds and a loop recognises a finding it already worked on.
    MUTATION: build the id from the title."""
    _change(repo, "app.py", BUGGY)
    first = ca.code_audit(str(repo), ask=_Model([OFF_BY_ONE]), remember=False)
    reworded = dict(OFF_BY_ONE, title="Index 0 is never summed",
                    evidence="    for i in range(1, len(items)):")
    again = ca.code_audit(str(repo), ask=_Model([reworded]), remember=False)
    assert first.findings[0].id == again.findings[0].id


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


def test_a_check_without_a_verdict_leaves_the_run_incomplete(repo):
    """The answer named no verdict for the check: shown inconclusive, and the run did not
    check everything. MUTATION: read a missing verdict as an answer."""
    _change(repo, ".vaf/code-audit.json", json.dumps(
        {"checks": [{"name": "has tests", "instructions": "Every change adds a test."}]}))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "config")
    _change(repo, "app.py", BUGGY)

    class _NoVerdict(_Model):
        def __call__(self, messages, max_tokens):
            if "evaluate repository checks" in messages[0]["content"]:
                return "[]"
            return super().__call__(messages, max_tokens)

    report = ca.code_audit(str(repo), ask=_NoVerdict([]), remember=False)
    assert [c.result for c in report.checks] == ["inconclusive"]
    assert report.status == "incomplete" and "check" in report.status_reason


def test_what_the_analyzers_proved_survives_a_review_the_model_could_not_give(repo):
    """The model answered nothing usable: the run fails, but a hard-coded key the secret pass
    found is still reported. MUTATION: return as soon as no review was readable."""
    key = "sk-" + "M1n2B3v4C5x6Z7l8K9j0H1g2"
    _change(repo, "cfg.py", f'API_KEY = "{key}"\n')
    report = ca.code_audit(str(repo), scope="uncommitted", remember=False,
                           ask=_Model([], review_raw="no JSON here"))
    assert report.status == "failed"
    assert [f.source for f in report.findings] == ["secrets"]


def test_a_finding_that_is_only_hidden_or_unread_is_not_addressed(repo):
    """Gone from the list is not fixed: hidden by the profile, or in a file the model gave no
    readable answer for this time. MUTATION: call every finding not shown "addressed"."""
    _change(repo, "app.py", BUGGY)
    first = ca.code_audit(str(repo), ask=_Model([OFF_BY_ONE]))
    assert first.findings
    as_nitpick = dict(OFF_BY_ONE, type="nitpick")
    hidden = ca.code_audit(str(repo), ask=_Model([as_nitpick]))
    assert hidden.findings == [] and hidden.hidden_by_profile == 1 and hidden.addressed == []
    unread = ca.code_audit(str(repo), ask=_Model([], review_raw="nothing usable"))
    assert unread.addressed == [] and "app.py" not in unread.files_reviewed


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


def test_a_guideline_file_git_ignores_stays_private(repo):
    """A CLAUDE.md the repository ignores is someone's own notes, not the project's rules.
    MUTATION: read every guideline file on disk."""
    (repo / ".gitignore").write_text("CLAUDE.md\n", encoding="utf-8")
    (repo / "CLAUDE.md").write_text("PRIVATE_NOTES_MARKER\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("PROJECT_RULES_MARKER\n", encoding="utf-8")
    _change(repo, "app.py", BUGGY)
    model = _Model([])
    ca.code_audit(str(repo), scope="uncommitted", ask=model, remember=False)
    assert any("PROJECT_RULES_MARKER" in s for s in model.seen)
    assert not any("PRIVATE_NOTES_MARKER" in s for s in model.seen)


def test_a_small_context_budget_splits_the_review_and_says_where_it_is(repo):
    """A model with a small window gets one file per call; `progress` hears each step.
    MUTATION: ignore batch_chars."""
    filler = "".join(f"VALUE_{i} = {i}  # a line that makes the file long enough\n"
                     for i in range(60))
    _change(repo, "app.py", BUGGY + filler)
    _change(repo, "other.py", "def avg(items):\n    return sum(items) / len(items)\n" + filler)
    model, lines = _Model([]), []
    ca.code_audit(str(repo), scope="uncommitted", ask=model, remember=False,
                  batch_chars=4_000, progress=lines.append)
    reviews = [s for s in model.seen if "Profile: chill." in s]
    assert len(reviews) == 2
    assert lines[:3] == ["reviewing 2 file(s) in 2 batch(es)", "reviewed 1/2", "reviewed 2/2"]


class _OutOfRoom(_Model):
    """A reasoning model that runs out of room on more than one file at a time: its answer
    for a larger batch is nothing but thinking."""

    def __call__(self, messages, max_tokens):
        user = messages[-1]["content"]
        if "verify code review findings" not in messages[0]["content"] \
                and user.count("=== FILE ") > 1:
            self.seen.append(user)
            return "<think>so much to consider"
        return super().__call__(messages, max_tokens)


def test_an_unreadable_review_of_several_files_is_asked_again_in_halves(repo):
    """MUTATION: give up on the batch instead of splitting it."""
    _change(repo, "app.py", BUGGY)
    _change(repo, "other.py", "def avg(items):\n    return sum(items) / len(items)\n")
    model = _OutOfRoom([OFF_BY_ONE])
    report = ca.code_audit(str(repo), scope="uncommitted", ask=model, remember=False)
    assert report.status == "complete", report.status_reason
    assert [f.title for f in report.findings] == [OFF_BY_ONE["title"]]
    assert not [p for p, r in report.files_skipped if r == "no readable review"]


class _VerifierOutOfRoom(_Model):
    """A verifier that answers nothing (only thinking) for more than one finding at once."""

    def __call__(self, messages, max_tokens):
        if "verify code review findings" in messages[0]["content"] \
                and messages[-1]["content"].count("--- finding f") > 1:
            self.seen.append(messages[-1]["content"])
            return "<think>weighing every claim at once"
        return super().__call__(messages, max_tokens)


def test_an_unreadable_verification_of_several_findings_is_asked_again_in_halves(repo):
    """Measured: eight findings per verification call left 81 of 101 unanswered with a
    reasoning model. MUTATION: give up on the chunk instead of splitting it."""
    _change(repo, "app.py", BUGGY)
    second = dict(OFF_BY_ONE, title="Returns an accumulator nobody resets", category="stability",
                  start_line=5, end_line=5, evidence="    return result")
    report = ca.code_audit(str(repo), ask=_VerifierOutOfRoom([OFF_BY_ONE, second]),
                           remember=False)
    assert report.status == "complete", report.status_reason
    assert len(report.findings) == 2 and report.unverified == []


def test_a_change_too_large_for_one_answer_is_reviewed_in_parts(repo):
    """Three changes far apart in one long file, a budget that fits one at a time: every
    change is shown to the reviewer, none is cut off, and a problem in the last one is found.
    MUTATION: review the file whole and cut what does not fit."""
    base = "".join(f"value_{i} = {i}  # a long enough line of ordinary code\n" for i in range(600))
    _change(repo, "big.py", base)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "big")
    lines = base.splitlines(keepends=True)
    for n in (10, 300, 590):
        lines[n] = f"value_{n} = compute_{n}(value_{n - 1}) / 0  # changed\n"
    _change(repo, "big.py", "".join(lines))
    last = dict(OFF_BY_ONE, file="big.py", title="Division by zero in the last change",
                evidence="value_590 = compute_590(value_589) / 0  # changed")

    class _Reviewer(_Model):
        def __call__(self, messages, max_tokens):
            user = messages[-1]["content"]
            if "verify code review findings" not in messages[0]["content"]:
                self.seen.append(user)
                found = [last] if "compute_590" in user else []
                return json.dumps({"summary": "s", "files": {}, "effort": 1, "findings": found})
            return super().__call__(messages, max_tokens)

    model = _Reviewer([])
    report = ca.code_audit(str(repo), scope="uncommitted", ask=model, remember=False,
                           batch_chars=4_000)
    assert report.status == "complete", report.status_reason
    shown = "\n".join(model.seen)
    assert all(f"compute_{n}" in shown for n in (10, 300, 590))
    assert "(cut:" not in shown and "part 1/" in shown
    assert [f.start_line for f in report.findings if f.source == "review"] == [591]


def test_one_file_the_model_cannot_answer_for_is_asked_again_in_smaller_parts(repo):
    """A reasoning model that runs out of room on one file answers on half of it.
    MUTATION: give up on a single file instead of splitting it further."""
    base = "".join(f"value_{i} = {i}  # a long enough line of ordinary code\n" for i in range(600))
    _change(repo, "big.py", base)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "big")
    lines = base.splitlines(keepends=True)
    for n in (10, 590):
        lines[n] = f"value_{n} = {n} + 1  # changed\n"
    _change(repo, "big.py", "".join(lines))

    class _OnlyHalves(_Model):
        def __call__(self, messages, max_tokens):
            user = messages[-1]["content"]
            if "verify code review findings" not in messages[0]["content"] \
                    and "value_10 = 10 + 1" in user and "value_590 = 590 + 1" in user:
                self.seen.append(user)
                return "<think>too much at once"
            return super().__call__(messages, max_tokens)

    report = ca.code_audit(str(repo), scope="uncommitted", ask=_OnlyHalves([]), remember=False)
    assert report.status == "complete", report.status_reason


def test_one_file_the_model_cannot_review_leaves_the_run_incomplete(repo):
    _change(repo, "app.py", BUGGY)
    report = ca.code_audit(str(repo), scope="uncommitted", remember=False,
                           ask=_Model([], review_raw="<think>still thinking"))
    assert report.status == "failed" and ("app.py", "no readable review") in report.files_skipped


def test_parallel_sends_the_review_calls_at_once(repo):
    """Two batches, a model that answers only while both calls are open at the same time.
    MUTATION: ignore `parallel` and call one after the other."""
    import threading
    barrier = threading.Barrier(2, timeout=5)

    class _Together(_Model):
        def __call__(self, messages, max_tokens):
            if "verify code review findings" not in messages[0]["content"]:
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    return "no answer alone"
            return super().__call__(messages, max_tokens)

    filler = "".join(f"VALUE_{i} = {i}  # a line that makes the file long enough\n"
                     for i in range(60))
    _change(repo, "app.py", BUGGY + filler)
    _change(repo, "other.py", "def avg(items):\n    return sum(items) / len(items)\n" + filler)
    report = ca.code_audit(str(repo), scope="uncommitted", ask=_Together([]), remember=False,
                           batch_chars=4_000, parallel=2)
    assert report.status == "complete", report.status_reason


def test_stop_ends_the_audit_before_the_next_model_call(repo):
    """Stop pressed while the review runs: no verification call follows and the report says
    so. MUTATION: never ask should_stop."""
    _change(repo, "app.py", BUGGY)
    model = _Model([OFF_BY_ONE])
    stop = {"now": False}

    def ask(messages, max_tokens):
        stop["now"] = True                       # the person presses Stop during the review
        return model(messages, max_tokens)

    report = ca.code_audit(str(repo), ask=ask, remember=False, should_stop=lambda: stop["now"])
    assert (report.status, report.status_reason) == ("failed", "stopped before it finished")
    assert len(model.seen) == 1 and report.findings == []


def test_the_json_parser_survives_prose_and_fences():
    assert ca._json_from('noise {"a": [1, {"b": "}"}]} tail', "{") == {"a": [1, {"b": "}"}]}
    assert ca._json_from("```json\n[1, 2]\n```", "[") == [1, 2]
    assert ca._json_from("no json here", "{") is None
