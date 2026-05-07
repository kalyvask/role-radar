"""CLI surface for the agent layer.

Adds three subcommands under `role-radar agent`:
- `triage <cv>`  : interactive review loop over the latest scored report
- `status`       : pipeline snapshot (counts by status, follow-ups due)
- `draft <cv>`   : draft outreach for a specific job by id or rank

Mirrors the existing CLI conventions (Typer + rich). The triage loop is
deliberately keystroke-driven so a job hunt session feels like a fast inbox
sweep, not a form-fill.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from role_radar.agent import Agent, FollowupCandidate, Match
from role_radar.config import load_settings
from role_radar.cv_parser import parse_cv
from role_radar.models import CVSignals, Job, ScoredJob
from role_radar.outreach import OutreachDraft, OutreachGenerationError
from role_radar.storage import ApplicationStatus, FeedbackAction, Storage
from role_radar.utils.logging import get_logger, setup_logging

logger = get_logger(__name__)
console = Console()

app = typer.Typer(
    name="agent",
    help="Stateful agent layer: triage matches, draft outreach, track pipeline.",
    no_args_is_help=True,
)


# ---- shared helpers ------------------------------------------------------


def _load_latest_scored_jobs(outputs_dir: Path) -> list[ScoredJob]:
    """Load and rehydrate ScoredJob objects from the latest report JSON.

    Reuses the existing rehydration logic from interview_prep.report_loader
    (so we stay in sync if the report shape changes), then wraps each Job
    with score and rank pulled from the same report entry.

    The detailed ScoreBreakdown isn't reconstructed here — the agent only
    reads `.score` and `.match_reasons` for classification, and the report
    serializes the breakdown as formatted strings, not numbers. If callers
    need numeric sub-scores, they should re-score from the source data.
    """
    from role_radar.interview_prep.report_loader import (
        find_latest_report,
        job_from_report_entry,
        load_report,
    )
    from role_radar.models import ScoreBreakdown

    report_path = find_latest_report(outputs_dir)
    if report_path is None:
        return []

    data = load_report(report_path)
    entries = data.get("jobs", [])

    scored_jobs: list[ScoredJob] = []
    for entry in entries:
        job = job_from_report_entry(entry)
        scored_jobs.append(
            ScoredJob(
                job=job,
                score=float(entry.get("score", 0)),
                score_breakdown=ScoreBreakdown(),
                match_reasons=entry.get("match_reasons", []),
                rank=int(entry.get("rank", 0)),
            )
        )

    return scored_jobs


def _format_match_header(m: Match, idx: int, total: int) -> str:
    sj = m.scored_job
    posted = (
        sj.job.posted_date.strftime("%Y-%m-%d")
        if sj.job.posted_date
        else "date unknown"
    )
    return (
        f"[bold cyan][{idx}/{total}][/bold cyan] "
        f"[bold]{sj.job.company}[/bold] — {sj.job.title}\n"
        f"  Score [green]{sj.score:.0f}/100[/green] · "
        f"{sj.job.location.format()} · posted {posted}\n"
        f"  {sj.job.apply_url}\n"
        f"  Match: {' · '.join(sj.match_reasons[:3]) if sj.match_reasons else '(no reasons)'}"
    )


def _load_optional_text(env_var: str, default_path: Optional[Path] = None) -> Optional[str]:
    """Load a Markdown text from env var path, then default path. Else None."""
    path_str = os.getenv(env_var)
    if path_str:
        p = Path(path_str)
        if p.exists():
            return p.read_text(encoding="utf-8")
    if default_path and default_path.exists():
        return default_path.read_text(encoding="utf-8")
    return None


# ---- triage --------------------------------------------------------------


@app.command()
def triage(
    cv: Path = typer.Argument(..., help="Path to your CV (PDF, DOCX, or TXT)"),
    threshold: float = typer.Option(
        70.0,
        "--threshold",
        help="Minimum score to surface a match for review (default 70).",
    ),
    show_borderline: bool = typer.Option(
        False,
        "--show-borderline",
        help="Also prompt on borderline matches (score 55-70).",
    ),
    candidate_name: Optional[str] = typer.Option(
        None, "--name", help="Candidate name for outreach drafts."
    ),
) -> None:
    """Walk through new matches and follow-ups in an interactive loop.

    Reads from the latest report in outputs/. Run `role-radar run` first to
    refresh the report, then use this for the human-in-the-loop pass.
    """
    settings = load_settings()
    settings.ensure_dirs()
    setup_logging(level=settings.log_level, format_type=settings.log_format)

    if not cv.exists():
        console.print(f"[red]Error:[/red] CV file not found: {cv}")
        raise typer.Exit(1)

    storage = Storage(settings.db_path)
    agent = Agent(
        storage,
        review_threshold=threshold,
        borderline_threshold=55.0 if show_borderline else 0.0,
    )

    name = candidate_name or os.environ.get("ROLE_RADAR_CANDIDATE_NAME") or "Candidate"

    # Load scored jobs from the latest report.
    scored_jobs = _load_latest_scored_jobs(settings.output_dir)
    if not scored_jobs:
        console.print(
            f"[red]No report found in {settings.output_dir}.[/red] "
            "Run `role-radar run <cv>` first."
        )
        storage.close()
        raise typer.Exit(1)

    console.print(
        Panel(
            f"[bold]CV:[/bold] {cv}\n"
            f"[bold]Threshold:[/bold] {threshold:.0f}/100\n"
            f"[bold]Borderline shown:[/bold] {show_borderline}\n"
            f"[bold]Scored jobs in latest report:[/bold] {len(scored_jobs)}",
            title="Agent Triage",
            border_style="blue",
        )
    )

    # Step 1: classify matches.
    matches = agent.fetch_new_matches(scored_jobs)
    must_review = [m for m in matches if m.category == "must_review"]
    borderline = [m for m in matches if m.category == "borderline"]
    queue = must_review + (borderline if show_borderline else [])

    new_count = sum(1 for m in queue if m.is_new)
    seen_count = len(queue) - new_count
    console.print(
        f"\n[bold]{len(queue)}[/bold] matches to review "
        f"({new_count} new, {seen_count} previously surfaced).\n"
    )

    if not queue:
        console.print("[dim]Nothing in the review queue.[/dim]")
    else:
        # 1-element holders so _walk_match can lazy-parse the CV once and
        # share the result across iterations without us threading state.
        cv_signals_holder: list[Optional[CVSignals]] = [None]
        cv_excerpt_holder: list[Optional[str]] = [None]

        for idx, match in enumerate(queue, 1):
            if _walk_match(
                match,
                idx=idx,
                total=len(queue),
                agent=agent,
                cv_path=cv,
                candidate_name=name,
                cv_signals_holder=cv_signals_holder,
                cv_excerpt_holder=cv_excerpt_holder,
                settings=settings,
            ):
                # User chose to quit
                break

    # Step 2: surface follow-ups.
    followups = agent.surface_followups()
    if followups:
        console.print(f"\n[bold]Follow-ups due ({len(followups)}):[/bold]")
        for f in followups:
            console.print(
                f"  - {f.company} — {f.title} "
                f"([yellow]{f.days_since_contact}d[/yellow] since contact, status: {f.status.value})"
            )
    else:
        console.print("\n[dim]No follow-ups due.[/dim]")

    # Step 3: pipeline summary.
    summary = agent.pipeline_summary()
    console.print(
        Panel(
            f"[bold]Active:[/bold] {summary['active']}    "
            f"[bold]Closed:[/bold] {summary['closed']}    "
            f"[bold]Total:[/bold] {summary['total']}\n\n"
            + "\n".join(
                f"  {status:20} {count}"
                for status, count in sorted(summary["by_status"].items())
            ),
            title="Pipeline",
            border_style="green",
        )
    )

    storage.close()


def _walk_match(
    match: Match,
    *,
    idx: int,
    total: int,
    agent: Agent,
    cv_path: Path,
    candidate_name: str,
    cv_signals_holder: list,
    cv_excerpt_holder: list,
    settings,
) -> bool:
    """Prompt the user for action on a single match.

    Returns True if user wants to quit the loop, False otherwise.
    `cv_signals_holder` / `cv_excerpt_holder` are 1-element lists used as
    mutable holders so we parse the CV at most once across the loop.
    """
    sj = match.scored_job
    console.print()
    console.print(_format_match_header(match, idx, total))
    if match.existing_status is not None:
        console.print(
            f"  [dim]existing status: {match.existing_status.value}[/dim]"
        )

    while True:
        action = Prompt.ask(
            "  [cyan]Action[/cyan]",
            choices=["s", "a", "d", "k", "n", "q"],
            default="k",
        )

        if action == "q":
            return True
        if action == "k":
            reason = Prompt.ask(
                "  Skip reason",
                choices=["role", "company", "location", "seniority", "other", ""],
                default="",
            ) or None
            agent.record_decision(
                sj.job.id,
                FeedbackAction.SKIPPED,
                reason=reason,
                company=sj.job.company,
                title=sj.job.title,
            )
            console.print("  [yellow]skipped[/yellow]")
            return False
        if action == "s":
            agent.record_decision(
                sj.job.id,
                FeedbackAction.SAVED,
                company=sj.job.company,
                title=sj.job.title,
            )
            console.print("  [green]saved[/green]")
            return False
        if action == "a":
            agent.record_decision(
                sj.job.id,
                FeedbackAction.APPLIED,
                company=sj.job.company,
                title=sj.job.title,
            )
            console.print("  [green]marked applied[/green]")
            return False
        if action == "n":
            note = Prompt.ask("  Note")
            agent.storage.upsert_application(
                sj.job.id,
                ApplicationStatus(
                    match.existing_status.value if match.existing_status else "new"
                ),
                notes=note,
            )
            agent.storage.record_event(
                sj.job.id, FeedbackAction.NOTED, reason=None,
            )
            console.print("  [dim]note saved[/dim]")
            # Stay on this match to allow another action.
            continue
        if action == "d":
            # Lazy-parse the CV for outreach.
            if cv_signals_holder[0] is None:
                with console.status("Parsing CV..."):
                    cv_signals_holder[0] = parse_cv(cv_path)
                    cv_excerpt_holder[0] = cv_signals_holder[0].raw_text

            try:
                with console.status("Drafting (Claude, ~30s)..."):
                    profile_md = _load_optional_text(
                        "ROLE_RADAR_OUTREACH_PROFILE",
                        default_path=settings.data_dir / "outreach_profile.md",
                    )
                    voice_md = _load_optional_text(
                        "ROLE_RADAR_OUTREACH_VOICE",
                        default_path=settings.data_dir / "outreach_voice.md",
                    )
                    draft, draft_id = agent.draft_outreach(
                        job=sj.job,
                        cv_signals=cv_signals_holder[0],
                        candidate_name=candidate_name,
                        cv_excerpt=cv_excerpt_holder[0],
                        candidate_profile_md=profile_md,
                        voice_notes_md=voice_md,
                    )
            except OutreachGenerationError as e:
                console.print(f"  [red]draft failed:[/red] {e}")
                continue

            console.print()
            console.print(
                Panel(
                    f"[bold]Subject:[/bold] {draft.subject}\n\n"
                    f"{draft.body}\n\n"
                    f"[dim]Self-rating: {draft.self_rating}/10 · "
                    f"saved as draft #{draft_id}[/dim]\n"
                    f"[dim]Rationale: {draft.rationale}[/dim]",
                    title="Outreach draft",
                    border_style="cyan",
                )
            )
            # Stay on this match — user may want to save / apply / next.
            continue


# ---- status --------------------------------------------------------------


@app.command()
def status(
    show_followups: bool = typer.Option(
        True,
        "--followups/--no-followups",
        help="Include follow-ups due in the output.",
    ),
    stale_days: int = typer.Option(
        14, "--stale-days", help="Threshold for follow-up suggestions."
    ),
) -> None:
    """Print a snapshot of the agent's pipeline state."""
    settings = load_settings()
    setup_logging(level=settings.log_level, format_type=settings.log_format)

    storage = Storage(settings.db_path)
    agent = Agent(storage, followup_stale_days=stale_days)

    summary = agent.pipeline_summary()
    counts = summary["by_status"]

    if summary["total"] == 0:
        console.print(
            "[dim]Pipeline is empty. Run `role-radar agent triage <cv>` to start.[/dim]"
        )
        storage.close()
        return

    table = Table(title="Pipeline by status")
    table.add_column("Status", style="cyan")
    table.add_column("Count", style="green", justify="right")
    for status_name in sorted(counts):
        table.add_row(status_name, str(counts[status_name]))
    console.print(table)
    console.print(
        f"\n[bold]Active:[/bold] {summary['active']}    "
        f"[bold]Closed:[/bold] {summary['closed']}    "
        f"[bold]Total:[/bold] {summary['total']}"
    )

    if show_followups:
        followups: list[FollowupCandidate] = agent.surface_followups()
        if followups:
            ftable = Table(title=f"Follow-ups due (>{stale_days}d quiet)")
            ftable.add_column("Company", width=22)
            ftable.add_column("Title", width=40)
            ftable.add_column("Status", width=18)
            ftable.add_column("Days", justify="right", width=6)
            for f in followups:
                ftable.add_row(
                    f.company,
                    f.title[:38] + "..." if len(f.title) > 40 else f.title,
                    f.status.value,
                    str(f.days_since_contact),
                )
            console.print()
            console.print(ftable)
        else:
            console.print("\n[dim]No follow-ups due.[/dim]")

    storage.close()


# ---- draft (single-job) --------------------------------------------------


@app.command()
def draft(
    cv: Path = typer.Argument(..., help="Path to your CV (PDF, DOCX, or TXT)"),
    job_id: Optional[str] = typer.Option(
        None, "--job-id", help="Job id from the latest report."
    ),
    rank: Optional[int] = typer.Option(
        None, "--rank", "-r", help="Rank in the latest report."
    ),
    candidate_name: Optional[str] = typer.Option(
        None, "--name", help="Candidate name for the sign-off."
    ),
    contact_name: Optional[str] = typer.Option(
        None, "--contact-name", help="Recipient's name (optional)."
    ),
    contact_role: Optional[str] = typer.Option(
        None, "--contact-role", help="Recipient's role, e.g. 'recruiter'."
    ),
) -> None:
    """Draft a cold outreach email for one specific job."""
    settings = load_settings()
    settings.ensure_dirs()
    setup_logging(level=settings.log_level, format_type=settings.log_format)

    selectors = [s for s in (job_id, rank) if s is not None]
    if len(selectors) != 1:
        console.print("[red]Error:[/red] specify exactly one of --job-id or --rank")
        raise typer.Exit(1)

    if not cv.exists():
        console.print(f"[red]Error:[/red] CV file not found: {cv}")
        raise typer.Exit(1)

    name = candidate_name or os.environ.get("ROLE_RADAR_CANDIDATE_NAME") or "Candidate"

    scored_jobs = _load_latest_scored_jobs(settings.output_dir)
    if not scored_jobs:
        console.print(
            f"[red]No report found in {settings.output_dir}.[/red] Run `role-radar run` first."
        )
        raise typer.Exit(1)

    target: Optional[ScoredJob] = None
    for sj in scored_jobs:
        if job_id and sj.job.id == job_id:
            target = sj
            break
        if rank is not None and sj.rank == rank:
            target = sj
            break

    if target is None:
        sel = job_id or f"rank {rank}"
        console.print(f"[red]No job in the latest report matches {sel}.[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]Parsing CV...[/bold]")
    cv_signals = parse_cv(cv)

    storage = Storage(settings.db_path)
    agent = Agent(storage)

    profile_md = _load_optional_text(
        "ROLE_RADAR_OUTREACH_PROFILE",
        default_path=settings.data_dir / "outreach_profile.md",
    )
    voice_md = _load_optional_text(
        "ROLE_RADAR_OUTREACH_VOICE",
        default_path=settings.data_dir / "outreach_voice.md",
    )

    try:
        with console.status("Drafting (Claude, ~30s)..."):
            draft, draft_id = agent.draft_outreach(
                job=target.job,
                cv_signals=cv_signals,
                candidate_name=name,
                contact_name=contact_name,
                contact_role=contact_role,
                cv_excerpt=cv_signals.raw_text,
                candidate_profile_md=profile_md,
                voice_notes_md=voice_md,
            )
    except OutreachGenerationError as e:
        console.print(f"[red]Draft failed:[/red] {e}")
        storage.close()
        raise typer.Exit(1)

    console.print(
        Panel(
            f"[bold]Subject:[/bold] {draft.subject}\n\n"
            f"{draft.body}\n\n"
            f"[dim]Self-rating: {draft.self_rating}/10 · "
            f"saved as draft #{draft_id}[/dim]\n"
            f"[dim]Rationale: {draft.rationale}[/dim]",
            title=f"Outreach draft for {target.job.company}",
            border_style="cyan",
        )
    )

    storage.close()
