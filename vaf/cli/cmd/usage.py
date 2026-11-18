# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""What this instance has consumed, and the one maintenance action on that record.

The web tab shows the same figures; this exists because the operator of a
headless install has no tab, and because `set-currency` rewrites the spend
ledger - an action nobody should have to reach into Python to perform.
"""
import typer

from vaf.cli.ui import UI

app = typer.Typer(help="Token usage and spend records.")


def _fmt(currencies: dict, legacy: float) -> str:
    """One amount, or a dash. Same rule the web tab follows: euros and dollars
    are not added, and an amount whose currency was never recorded is not
    quietly folded into one."""
    entries = [(c, v) for c, v in (currencies or {}).items() if v >= 0.005]
    if not entries:
        return f"~${legacy:.2f}"
    if len(entries) > 1 or entries[0][0] == "?":
        return "-"
    cur, val = entries[0]
    return f"~{'€' if cur == 'EUR' else '$'}{val:.2f}"


@app.command("show")
def show(days: int = typer.Option(30, "--days", "-d", help="Period to report on")) -> None:
    """Print totals, per account and per provider, for the last N days."""
    from vaf.core.cost import (LEDGER_KEEP_DAYS, PRICES_AS_OF, alias_rows_priced_as_unknown,
                               usage_totals)

    data = usage_totals(days=days)
    totals = data.get("totals") or {}
    UI.panel(
        f"Tokens: {totals.get('tokens', 0):,}\n"
        f"Requests: {totals.get('calls', 0):,}\n"
        f"Estimated cost: {_fmt(totals.get('currencies') or {}, float(totals.get('usd') or 0))}",
        title=f"Usage, last {data.get('days', days)} days", style="highlight")
    # A price without its date is a claim about today that was checked on some other day.
    UI.console.print(f"[dim]Estimated from list prices as of {PRICES_AS_OF}.[/dim]")
    # The same note the web tab and the export carry: a day with a call priced at
    # the unknown-model rate makes every total that includes it an upper bound.
    if totals.get("estimated_usd_incomplete"):
        UI.console.print("[dim]Some calls used a model that is not in the price list. Those are "
                         "priced at the expensive end, so the cost estimate is an upper bound.[/dim]")
    # Why a total can still be high after the alias got a price: its old rows were
    # booked at the unknown-model rate and are kept as recorded. Decided from the
    # rows themselves (an amount no call under the alias can reach today), so the
    # line goes away with the last of them.
    old_rows = alias_rows_priced_as_unknown(totals.get("providers") or {})
    if old_rows:
        keep = min(int(data.get("days", days) or days), LEDGER_KEEP_DAYS)
        # One paragraph that rich wraps to the terminal, and one break before the
        # last sentence: a hard break mid-sentence re-wrapped into orphan lines on
        # a terminal narrower than the line.
        UI.console.print(f"[dim]Amounts recorded under {', '.join(old_rows)} by earlier versions "
                         f"are kept as they were, not repriced. They leave this view once they "
                         f"are {keep} day{'' if keep == 1 else 's'} old.\n"
                         f"New calls are priced at the model that answered.[/dim]")

    if totals.get("estimated_tokens"):
        UI.event("Usage", f"{totals['estimated_tokens']:,} of those tokens are estimated "
                          f"({totals.get('no_usage_calls', 0)} calls reported no usage)", style="dim")

    for row in data.get("users") or []:
        UI.console.print(f"  {row.get('username', '?'):<20} {row.get('tokens', 0):>12,} tokens  "
                         f"{row.get('calls', 0):>6} calls  "
                         f"{_fmt(row.get('currencies') or {}, float(row.get('usd') or 0))}")
    for key, prov in sorted((totals.get("providers") or {}).items()):
        UI.console.print(f"  [dim]{key:<34}[/dim] {prov.get('tokens', 0):>12,} tokens  "
                         f"{prov.get('calls', 0):>6} calls  "
                         f"{_fmt(prov.get('currencies') or {}, float(prov.get('usd') or 0))}")


@app.command("set-currency")
def set_currency(
    currency: str = typer.Argument(..., help="EUR or USD"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation"),
) -> None:
    """Attribute amounts recorded before the currency was stored.

    The ledger cannot know what those were billed in - the field was called
    `usd` while a Veyllo call inside it was euros - so the operator states it
    once. Only unattributed amounts are touched, a backup is written first, and
    running it again does nothing.
    """
    from vaf.core.cost import stamp_legacy_currency, usage_totals

    cur = (currency or "").strip().upper()
    if cur not in {"EUR", "USD"}:
        UI.error("Currency must be EUR or USD.")
        raise typer.Exit(1)

    pending = float((usage_totals(days=10_000).get("totals") or {})
                    .get("currencies", {}).get("?", 0.0))
    if pending < 0.005:
        UI.success("Nothing to attribute: every recorded amount already has a currency.")
        return

    UI.panel(f"~{pending:.2f} has no recorded currency.\n\n"
             f"It will be attributed to {cur}. A .bak copy of each ledger is written first.",
             title="Attribute past amounts", style="warning")
    if not yes and not typer.confirm(f"Attribute these amounts to {cur}?"):
        UI.event("Usage", "Cancelled - nothing was changed.", style="dim")
        return

    result = stamp_legacy_currency(cur)
    if result.get("error"):
        UI.error(str(result["error"]))
        raise typer.Exit(1)
    UI.success(f"Attributed ~{result.get('amount', 0):.2f} to {cur} "
               f"across {result.get('days', 0)} days in {result.get('files', 0)} ledger(s).")
