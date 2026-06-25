# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Security diagnostics commands."""

from __future__ import annotations

from typing import Dict, List

import typer

from vaf.cli.ui import UI
from vaf.core.security_misconfig import collect_security_findings

app = typer.Typer(help="Security diagnostics and hardening checks.")

_SEVERITY_ORDER: Dict[str, int] = {"high": 0, "medium": 1, "low": 2}
_SEVERITY_STYLE: Dict[str, str] = {"high": "red", "medium": "yellow", "low": "cyan"}


def _sort_findings(findings: List[Dict[str, str]]) -> List[Dict[str, str]]:
    return sorted(findings, key=lambda f: (_SEVERITY_ORDER.get(str(f.get("severity", "")).lower(), 99), str(f.get("code", ""))))


@app.command("doctor")
def doctor() -> None:
    """Run security misconfiguration checks (safe output, no secrets)."""
    findings = _sort_findings(collect_security_findings())

    UI.panel("VAF Security Doctor", style="bold yellow")
    if not findings:
        UI.success("No security misconfiguration findings.")
        return

    counts = {"high": 0, "medium": 0, "low": 0}
    for item in findings:
        sev = str(item.get("severity", "low")).lower()
        if sev in counts:
            counts[sev] += 1

    UI.warning(
        f"Findings: {len(findings)} total "
        f"(high={counts['high']}, medium={counts['medium']}, low={counts['low']})."
    )
    for item in findings:
        sev = str(item.get("severity", "low")).lower()
        sev_label = sev.upper()
        style = _SEVERITY_STYLE.get(sev, "white")
        code = str(item.get("code", "unknown"))
        msg = str(item.get("message", ""))
        UI.print(f"[{style}]- [{sev_label}] {code}[/{style}] {msg}")


__all__ = ["app", "doctor"]
