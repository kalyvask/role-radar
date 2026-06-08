"""CLI surface for the connections (warm-intro) layer.

Adds three subcommands under `role-radar connections`:
- `import <csv>`  : import a LinkedIn connections export into the local DB
- `list`          : show imported connections (optionally filtered by company)
- `intros`        : surface Tier-1/Tier-2 warm intros for a company or for the
                    jobs in the latest report

Mirrors the existing CLI conventions (Typer + rich). All state is local: the
network lives in the same SQLite DB as jobs, which is gitignored.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from role_radar.config import load_settings
from role_radar.connections import (
    ConnectionsImportError,
    IntroTier,
    backers_for,
    build_matcher,
    parse_connections_csv,
)
from role_radar.connections.normalize import normalize_company
from role_radar.storage import Storage
from role_radar.utils.logging import get_logger, setup_logging

logger = get_logger(__name__)
console = Console()

app = typer.Typer(
    name="connections",
    help="Import your network and surface warm intros at target companies.",
    no_args_is_help=True,
)


@app.command("import")
def import_csv(
    csv_path: Path = typer.Argument(
        ..., help="Path to a LinkedIn 'Connections.csv' export (or similar)."
    ),
) -> None:
    """Import a connections export, replacing any previously imported network."""
    settings = load_settings()
    settings.ensure_dirs()
    setup_logging(level=settings.log_level, format_type=settings.log_format)

    if not csv_path.exists():
        console.print(f"[red]Error:[/red] file not found: {csv_path}")
        raise typer.Exit(1)

    try:
        connections = parse_connections_csv(csv_path)
    except ConnectionsImportError as e:
        console.print(f"[red]Import failed:[/red] {e}")
        raise typer.Exit(1)

    if not connections:
        console.print("[yellow]No usable connections found in that file.[/yellow]")
        raise typer.Exit(1)

    storage = Storage(settings.db_path)
    count = storage.replace_connections([c.to_row() for c in connections])

    # Top employers, to show the import landed sensibly.
    by_employer: dict[str, int] = defaultdict(int)
    for c in connections:
        if c.employer:
            by_employer[c.employer] += 1
    top = sorted(by_employer.items(), key=lambda kv: -kv[1])[:5]
    top_str = "\n".join(f"  {n:>4}  {emp}" for emp, n in top)

    storage.close()
    console.print(
        Panel(
            f"[green]Imported {count} connections[/green] from {csv_path.name}\n\n"
            f"[bold]Top employers in your network:[/bold]\n{top_str}\n\n"
            "[dim]Re-import any time to refresh. Stored locally only.[/dim]",
            title="Connections imported",
            border_style="green",
        )
    )


@app.command("list")
def list_connections(
    company: Optional[str] = typer.Option(
        None, "--company", "-c", help="Only show connections at this company."
    ),
    limit: int = typer.Option(40, "--limit", "-n", help="Max rows to show."),
) -> None:
    """List imported connections, optionally filtered by employer."""
    settings = load_settings()
    storage = Storage(settings.db_path)

    rows = storage.get_all_connections()
    storage.close()

    if not rows:
        console.print(
            "[dim]No connections imported. Run "
            "`role-radar connections import <csv>` first.[/dim]"
        )
        return

    if company:
        target = normalize_company(company)
        rows = [r for r in rows if (r.get("employer_norm") or "") == target]
        if not rows:
            console.print(f"[yellow]No connections found at '{company}'.[/yellow]")
            return

    table = Table(title=f"Connections ({len(rows)})")
    table.add_column("Name", width=26)
    table.add_column("Position", width=34)
    table.add_column("Company", width=24)
    table.add_column("Since", width=6, justify="right")
    for r in rows[:limit]:
        since = (r.get("connected_on") or "")[:4]
        table.add_row(
            r.get("full_name", ""),
            (r.get("position") or "")[:32],
            (r.get("employer") or "")[:22],
            since,
        )
    console.print(table)
    if len(rows) > limit:
        console.print(f"[dim](showing {limit} of {len(rows)})[/dim]")


def _print_intros_for_company(storage: Storage, matcher, company: str) -> int:
    """Print a detail view for one company. Returns the intro count."""
    intros = matcher.intros_for(company, backers_for(storage, company))
    if not intros:
        console.print(f"\n[bold]{company}[/bold] — [dim]no warm intros found[/dim]")
        return 0

    table = Table(title=f"Warm intros — {company}")
    table.add_column("Tier", width=12)
    table.add_column("Name", width=24)
    table.add_column("Position", width=30)
    table.add_column("Via / At", width=22)
    table.add_column("Strength", width=8, justify="right")
    for wi in intros:
        tier = "1 · company" if wi.tier is IntroTier.AT_COMPANY else "2 · investor"
        via = wi.via if wi.tier is IntroTier.AT_INVESTOR else (wi.connection.employer or company)
        table.add_row(
            tier,
            wi.connection.full_name,
            (wi.connection.position or "")[:28],
            (via or "")[:20],
            str(round(wi.strength)),
        )
    console.print(table)
    return len(intros)


@app.command()
def intros(
    company: Optional[str] = typer.Option(
        None, "--company", "-c", help="Show warm intros for this company."
    ),
    rank: Optional[int] = typer.Option(
        None, "--rank", "-r", help="Show intros for the job at this rank in the latest report."
    ),
    job_id: Optional[str] = typer.Option(
        None, "--job-id", help="Show intros for this job id from the latest report."
    ),
    top: Optional[int] = typer.Option(
        None, "--top", "-t", help="Scan the top N jobs in the latest report."
    ),
) -> None:
    """Surface Tier-1 (at company) and Tier-2 (at investor) warm intros.

    With --company, shows one company. Otherwise scans the latest report:
    --rank/--job-id for a single job, --top N for the first N, or no selector
    to summarize every company in the report that has at least one intro.
    """
    settings = load_settings()
    setup_logging(level=settings.log_level, format_type=settings.log_format)
    storage = Storage(settings.db_path)

    matcher = build_matcher(storage)
    if not matcher.connections:
        console.print(
            "[dim]No connections imported. Run "
            "`role-radar connections import <csv>` first.[/dim]"
        )
        storage.close()
        return

    # Single company path.
    if company:
        _print_intros_for_company(storage, matcher, company)
        storage.close()
        return

    # Report-driven path.
    from role_radar.interview_prep.report_loader import (
        find_latest_report,
        load_report,
    )

    report_path = find_latest_report(settings.output_dir)
    if report_path is None:
        console.print(
            f"[red]No report found in {settings.output_dir}.[/red] "
            "Run `role-radar run` first, or pass --company."
        )
        storage.close()
        raise typer.Exit(1)

    data = load_report(report_path)
    entries = data.get("jobs", [])

    # Resolve the target companies from the report.
    def _company_of(entry: dict) -> str:
        return entry.get("job", {}).get("company", "")

    selected: list[dict] = []
    if job_id is not None:
        selected = [e for e in entries if e.get("job", {}).get("id") == job_id]
    elif rank is not None:
        selected = [e for e in entries if e.get("rank") == rank]
    elif top is not None:
        selected = entries[:top]
    else:
        selected = entries

    companies: list[str] = []
    seen: set[str] = set()
    for e in selected:
        name = _company_of(e)
        key = normalize_company(name)
        if name and key not in seen:
            seen.add(key)
            companies.append(name)

    if not companies:
        console.print("[yellow]No matching jobs in the latest report.[/yellow]")
        storage.close()
        return

    # Detail view when a single job was selected; summary otherwise.
    if job_id is not None or rank is not None:
        for name in companies:
            _print_intros_for_company(storage, matcher, name)
        storage.close()
        return

    # Summary table across companies, then detail the ones that have intros.
    summary = Table(title=f"Warm intros across {len(companies)} companies in latest report")
    summary.add_column("Company", width=28)
    summary.add_column("Tier 1", width=8, justify="right")
    summary.add_column("Tier 2", width=8, justify="right")

    with_intros: list[str] = []
    for name in companies:
        wis = matcher.intros_for(name, backers_for(storage, name))
        t1 = sum(1 for wi in wis if wi.tier is IntroTier.AT_COMPANY)
        t2 = sum(1 for wi in wis if wi.tier is IntroTier.AT_INVESTOR)
        if t1 or t2:
            with_intros.append(name)
            summary.add_row(name, str(t1) if t1 else "·", str(t2) if t2 else "·")

    if not with_intros:
        console.print("[dim]No warm intros found across the latest report.[/dim]")
        storage.close()
        return

    console.print(summary)
    for name in with_intros:
        _print_intros_for_company(storage, matcher, name)

    storage.close()
