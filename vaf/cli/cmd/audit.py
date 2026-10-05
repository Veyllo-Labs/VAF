# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf audit`: review a code change in any git repository, on this machine, with the model
VAF is configured for (vaf.core.code_audit; docs/agents/CODE_AUDIT.md).

Exit codes are a contract a script can rely on: 0 nothing at or above `--fail-on`, 1
findings (or a failed check in error mode), 2 the audit did not complete - a run that could
not review everything never exits 0.
"""
import contextlib
import os
import sys
from typing import List, Optional

import typer

from vaf.cli.ui import UI

app = typer.Typer(help="Review a code change: findings, proven, with a fix prompt each.")


def _scope(committed: bool, uncommitted: bool, files: bool) -> str:
    chosen = [name for name, on in (("committed", committed), ("uncommitted", uncommitted),
                                    ("files", files)) if on]
    if len(chosen) > 1:
        UI.error("Choose one of --committed, --uncommitted and --files.")
        raise typer.Exit(2)
    return chosen[0] if chosen else "changes"


@app.command("run")
def run(
    path: str = typer.Argument(".", help="The repository (or a directory inside it)"),
    base: Optional[str] = typer.Option(None, "--base", "-b",
                                       help="Compare against this branch or commit "
                                            "(default: the merge base with the upstream)"),
    committed: bool = typer.Option(False, "--committed", help="Only committed changes"),
    uncommitted: bool = typer.Option(False, "--uncommitted", help="Only uncommitted changes"),
    files: bool = typer.Option(False, "--files", help="Review whole files instead of a diff"),
    only: Optional[List[str]] = typer.Option(None, "--path", "-p",
                                             help="Limit to these paths (repeatable)"),
    untracked: bool = typer.Option(True, "--untracked/--no-untracked",
                                   help="Include new files git does not track yet"),
    profile: str = typer.Option("chill", "--profile",
                                help="chill: bugs, security and what matters; assertive: "
                                     "also style and small things"),
    fmt: str = typer.Option("text", "--format", "-f", help="text, json or prompt"),
    fail_on: str = typer.Option("minor", "--fail-on",
                                help="critical, major, minor or none: the lowest severity "
                                     "that makes the exit code 1"),
    max_files: int = typer.Option(60, "--max-files", help="Files reviewed at most"),
    no_llm: bool = typer.Option(False, "--no-llm",
                                help="Only the analyzers (secrets, ruff); the run then reports "
                                     "itself incomplete"),
    provider: Optional[str] = typer.Option(None, "--provider",
                                           help="Model provider (default: the configured one)"),
    model: Optional[str] = typer.Option(None, "--model", help="Model (default: configured)"),
    parallel: Optional[int] = typer.Option(None, "--parallel",
                                           help="Model calls at once (default: 4 for an API "
                                                "provider, 1 for the local server)"),
    verify_steps: Optional[int] = typer.Option(None, "--verify-steps",
                                               help="Searches and reads the verifier may make "
                                                    "in the repository per confirmed finding "
                                                    "(default 6); 0: first check only "
                                                    "(cheaper, more false findings)"),
) -> None:
    """Audit the change in PATH and print the findings."""
    from vaf.core.code_audit import VERIFY_STEPS, ask_via_complete, code_audit, parallel_for

    if fmt not in ("text", "json", "prompt"):
        UI.error("--format is text, json or prompt.")
        raise typer.Exit(2)
    if fail_on not in ("critical", "major", "minor", "none"):
        UI.error("--fail-on is critical, major, minor or none.")
        raise typer.Exit(2)
    scope = _scope(committed, uncommitted, files)
    ask = None if no_llm else ask_via_complete(provider=provider, model=model, caller="cli:audit")
    if fmt == "text":
        UI.info(f"Auditing {os.path.abspath(path)} ({scope}) ...")
    # stdout carries the report and nothing else, so `--format json` can be piped: progress
    # goes to stderr, and so does whatever the model lane prints while it runs (a provider
    # error is printed by the backend; measured, 70 such lines once made the JSON unreadable).
    with contextlib.redirect_stdout(sys.stderr):
        report = code_audit(os.path.abspath(path), scope=scope, base=base, paths=only or None,
                            include_untracked=untracked, profile=profile, ask=ask,
                            max_files=max_files, parallel=parallel or parallel_for(provider),
                            progress=lambda line: sys.stderr.write(f"  {line}\n"),
                            verify_steps=VERIFY_STEPS if verify_steps is None else verify_steps)
    if fmt == "json":
        sys.stdout.write(report.to_json() + "\n")
    elif fmt == "prompt":
        sys.stdout.write(report.to_prompt() + "\n")
    else:
        sys.stdout.write(report.to_text() + "\n")
    raise typer.Exit(report.exit_code(fail_on))


@app.command("show")
def show(path: str = typer.Argument(".", help="The repository")) -> None:
    """Print the last audit of this repository."""
    from vaf.core.code_audit import last_report

    text = last_report(os.path.abspath(path))
    if not text:
        UI.info("No audit of this repository yet: run `vaf audit run`.")
        raise typer.Exit(1)
    sys.stdout.write(text + "\n")


@app.command("dismiss")
def dismiss(
    finding_id: str = typer.Argument(..., help="The finding's id, as the report shows it"),
    reason: str = typer.Option(..., "--reason", "-r", help="Why it is not a problem"),
    path: str = typer.Option(".", "--repo", help="The repository"),
) -> None:
    """Record that a finding is not a problem; it is not reported again in this repository."""
    from vaf.core.code_audit import dismiss_finding

    if not dismiss_finding(os.path.abspath(path), finding_id.strip(), reason.strip()):
        UI.error("Not a git repository.")
        raise typer.Exit(2)
    UI.success(f"Dismissed {finding_id}: {reason.strip()}")
