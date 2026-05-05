"""Main CLI entry point for Role Radar."""

import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from role_radar import __version__
from role_radar.company_sources import (
    generate_ai_top20,
    generate_top_vcs_list,
    discover_portfolio_companies,
    TOP_VCS,
)
from role_radar.config import (
    Settings,
    Preferences,
    create_default_env_file,
    create_default_preferences_file,
    create_default_portfolios_csv,
    load_settings,
)
from role_radar.connectors import ConnectorRegistry
from role_radar.cv_parser import parse_cv
from role_radar.dedupe import deduplicate_jobs
from role_radar.emailer import EmailSender
from role_radar.models import Company, CompanyType, Job, RunSummary
from role_radar.reporting import ReportGenerator
from role_radar.scoring import filter_jobs, score_and_rank_jobs
from role_radar.storage import Storage
from role_radar.utils.http import HTTPClient
from role_radar.utils.logging import setup_logging, get_logger

app = typer.Typer(
    name="role-radar",
    help="Find PM roles at top AI companies and VC-backed startups",
    add_completion=False,
)
console = Console()


def version_callback(value: bool) -> None:
    if value:
        console.print(f"Role Radar v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-v",
        callback=version_callback,
        is_eager=True,
        help="Show version and exit",
    ),
) -> None:
    """Role Radar - Find PM roles at top AI companies and VC-backed startups."""
    pass


@app.command()
def init(
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing files"),
) -> None:
    """Initialize Role Radar configuration files."""
    base_dir = Path.cwd()

    files_created = []

    # Create .env file
    env_path = base_dir / ".env"
    if not env_path.exists() or force:
        create_default_env_file(env_path)
        files_created.append(str(env_path))

    # Create preferences.yaml
    prefs_path = base_dir / "preferences.yaml"
    if not prefs_path.exists() or force:
        create_default_preferences_file(prefs_path)
        files_created.append(str(prefs_path))

    # Create portfolios.csv
    portfolios_path = base_dir / "data" / "portfolios.csv"
    portfolios_path.parent.mkdir(parents=True, exist_ok=True)
    if not portfolios_path.exists() or force:
        create_default_portfolios_csv(portfolios_path)
        files_created.append(str(portfolios_path))

    # Create directories
    dirs = ["outputs", ".cache", "data"]
    for d in dirs:
        dir_path = base_dir / d
        dir_path.mkdir(parents=True, exist_ok=True)

    if files_created:
        console.print(Panel(
            "\n".join([
                "[green]Configuration files created:[/green]",
                *[f"  • {f}" for f in files_created],
                "",
                "[yellow]Next steps:[/yellow]",
                "  1. Edit .env and add your email credentials",
                "  2. Customize preferences.yaml for your job search",
                "  3. Optionally add companies to data/portfolios.csv",
                "  4. Run: role-radar run --cv your_cv.pdf",
            ]),
            title="Role Radar Initialized",
            border_style="green",
        ))
    else:
        console.print("[yellow]All configuration files already exist. Use --force to overwrite.[/yellow]")


@app.command()
def run(
    cv: Path = typer.Argument(..., help="Path to your CV (PDF, DOCX, or TXT)"),
    prefs: Optional[Path] = typer.Option(
        None,
        "--prefs", "-p",
        help="Path to preferences.yaml",
    ),
    send: bool = typer.Option(
        False,
        "--send", "-s",
        help="Send email (otherwise test mode)",
    ),
    daily: bool = typer.Option(
        False,
        "--daily",
        help="Daily run mode (send email if configured)",
    ),
    skip_fetch: bool = typer.Option(
        False,
        "--skip-fetch",
        help="Use cached jobs instead of fetching new ones",
    ),
    posted_within_days: Optional[int] = typer.Option(
        None,
        "--posted-within-days",
        help="Only include jobs posted within this many days (e.g. 7 for last week)",
    ),
) -> None:
    """Run Role Radar to find matching PM roles."""
    # Initialize
    settings = load_settings()
    settings.ensure_dirs()

    setup_logging(
        level=settings.log_level,
        format_type=settings.log_format,
    )
    logger = get_logger(__name__)

    # Load preferences
    prefs_path = prefs or Path.cwd() / "preferences.yaml"
    preferences = Preferences.from_yaml(prefs_path)

    # Create run summary
    run_id = str(uuid.uuid4())[:8]
    summary = RunSummary(
        run_id=run_id,
        started_at=datetime.utcnow(),
    )

    console.print(Panel(
        f"[bold]Run ID:[/bold] {run_id}\n"
        f"[bold]CV:[/bold] {cv}\n"
        f"[bold]Location:[/bold] {preferences.location}\n"
        f"[bold]Email:[/bold] {'Sending' if (send or daily) else 'Test mode'}",
        title="Role Radar Starting",
        border_style="blue",
    ))

    # Validate CV exists
    if not cv.exists():
        console.print(f"[red]Error: CV file not found: {cv}[/red]")
        raise typer.Exit(1)

    # Parse CV
    console.print("\n[bold]Parsing CV...[/bold]")
    cv_signals = parse_cv(cv)

    console.print(f"  - Found {len(cv_signals.skills)} skills")
    console.print(f"  - Found {len(cv_signals.domains)} domains")
    console.print(f"  - Inferred seniority: {cv_signals.inferred_seniority or 'Unknown'}")

    # Initialize components
    storage = Storage(settings.db_path)
    http_client = HTTPClient(
        user_agent=settings.user_agent,
        timeout=settings.request_timeout,
        requests_per_second=settings.rate_limit_requests_per_second,
        cache_dir=settings.cache_dir,
    )
    connector_registry = ConnectorRegistry(http_client)

    all_jobs: list[Job] = []

    if skip_fetch:
        # Use cached jobs
        console.print("\n[yellow]Using cached jobs...[/yellow]")
        all_jobs = storage.get_cached_jobs(max_age_hours=24)
        console.print(f"  - Found {len(all_jobs)} cached jobs")
    else:
        # Generate company lists
        console.print("\n[bold]Generating company lists...[/bold]")

        # AI Top 20
        ai_companies, ai_report = generate_ai_top20(
            output_path=settings.output_dir / "ai_top20_scoring.md"
        )
        console.print(f"  - AI Top 20: {len(ai_companies)} companies")

        # Save AI companies to storage
        for company in ai_companies:
            storage.save_company(company)

        # Top VCs and portfolio companies
        vc_list, vc_report = generate_top_vcs_list(
            output_path=settings.output_dir / "top_vcs_scoring.md"
        )
        console.print(f"  - Top VCs: {len(vc_list)} VCs")

        # Discover portfolio companies
        portfolios_path = settings.data_dir / "portfolios.csv"
        vc_backed_companies = discover_portfolio_companies(
            http_client=http_client,
            cache_dir=settings.cache_dir,
            csv_path=portfolios_path if portfolios_path.exists() else None,
            vcs=vc_list,
            scrape_portfolios=False,  # Disabled by default for reliability
        )
        console.print(f"  - VC-Backed: {len(vc_backed_companies)} companies")

        # Save VC-backed companies to storage
        for company in vc_backed_companies:
            storage.save_company(company)

        # Combine all companies
        all_companies = ai_companies + vc_backed_companies

        # Fetch jobs
        console.print("\n[bold]Fetching jobs...[/bold]")
        for i, company in enumerate(all_companies, 1):
            console.print(f"  [{i}/{len(all_companies)}] {company.name}...", end="")
            try:
                jobs = connector_registry.fetch_jobs(company)
                all_jobs.extend(jobs)
                storage.save_jobs(jobs)
                summary.companies_processed += 1
                console.print(f" {len(jobs)} jobs")
            except Exception as e:
                logger.error("company_fetch_error", company=company.name, error=str(e))
                summary.companies_with_errors += 1
                summary.errors.append(f"{company.name}: {str(e)}")
                console.print(" [red]error[/red]")

    summary.total_jobs_found = len(all_jobs)
    console.print(f"\n  - Total jobs found: {len(all_jobs)}")

    # Filter jobs
    console.print("\n[bold]Filtering jobs...[/bold]")
    filtered_jobs = filter_jobs(all_jobs, preferences, posted_within_days=posted_within_days)
    summary.jobs_after_filter = len(filtered_jobs)
    console.print(f"  - After filtering: {len(filtered_jobs)} jobs")

    # Deduplicate
    deduped_jobs = deduplicate_jobs(filtered_jobs)
    summary.jobs_after_dedupe = len(deduped_jobs)
    console.print(f"  - After deduplication: {len(deduped_jobs)} jobs")

    # Score and rank
    console.print("\n[bold]Scoring and ranking...[/bold]")
    scored_jobs = score_and_rank_jobs(
        deduped_jobs,
        cv_signals,
        preferences,
        max_results=preferences.max_roles_per_email,
    )
    summary.jobs_scored = len(scored_jobs)
    console.print(f"  - Top {len(scored_jobs)} jobs selected")

    if scored_jobs:
        # Show top 5 preview
        table = Table(title="Top 5 Matches")
        table.add_column("Rank", style="cyan", width=6)
        table.add_column("Score", style="green", width=8)
        table.add_column("Company", width=20)
        table.add_column("Title", width=40)
        table.add_column("Location", width=20)

        for sj in scored_jobs[:5]:
            table.add_row(
                str(sj.rank),
                f"{sj.score:.0f}/100",
                sj.job.company,
                sj.job.title[:38] + "..." if len(sj.job.title) > 40 else sj.job.title,
                sj.job.location.format()[:18],
            )

        console.print(table)

    # Generate reports
    console.print("\n[bold]Generating reports...[/bold]")
    report_generator = ReportGenerator(settings.output_dir)
    json_path, html_path = report_generator.save_reports(scored_jobs, summary)
    console.print(f"  - JSON: {json_path}")
    console.print(f"  - HTML: {html_path}")
    summary.report_path = str(html_path)

    # Send email
    email_config = settings.get_email_config()
    if not (send or daily):
        email_config.test_mode = True

    summary.jobs_in_email = len(scored_jobs)

    if scored_jobs:
        console.print("\n[bold]Sending email...[/bold]")
        emailer = EmailSender(email_config)
        success, html_body, text_body = emailer.send(scored_jobs)
        summary.email_sent = success

        if email_config.test_mode:
            # Save email to file
            email_html_path = settings.output_dir / f"email_{run_id}.html"
            email_html_path.write_text(html_body, encoding="utf-8")
            console.print(f"  - Email preview saved: {email_html_path}")
        elif success:
            console.print(f"  - Email sent to: {email_config.to_email}")
        else:
            console.print("[red]  - Failed to send email[/red]")

    # Finalize
    summary.completed_at = datetime.utcnow()
    storage.save_run_summary(summary)

    # Summary panel
    duration = (summary.completed_at - summary.started_at).total_seconds()
    console.print(Panel(
        f"[bold]Duration:[/bold] {duration:.1f}s\n"
        f"[bold]Jobs Found:[/bold] {summary.total_jobs_found}\n"
        f"[bold]Jobs Matched:[/bold] {summary.jobs_in_email}\n"
        f"[bold]Email:[/bold] {'Sent' if summary.email_sent else 'Test mode'}\n"
        f"[bold]Report:[/bold] {summary.report_path}",
        title="Run Complete",
        border_style="green",
    ))

    # Cleanup
    http_client.close()
    storage.close()


@app.command()
def companies(
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output file path"),
) -> None:
    """Show current AI top-20 and VC lists with company counts."""
    settings = load_settings()

    console.print("\n[bold]AI Top 20 Companies[/bold]")
    ai_companies, _ = generate_ai_top20()

    table = Table()
    table.add_column("Rank", style="cyan", width=6)
    table.add_column("Company", width=25)
    table.add_column("Category", width=15)
    table.add_column("Score", style="green", width=8)
    table.add_column("ATS", width=15)

    for i, company in enumerate(ai_companies, 1):
        table.add_row(
            str(i),
            company.name,
            company.ai_category.value if company.ai_category else "N/A",
            f"{company.ai_score:.0f}/100" if company.ai_score else "N/A",
            company.ats_type.value,
        )

    console.print(table)

    console.print("\n[bold]Top VCs[/bold]")
    vc_list, _ = generate_top_vcs_list()

    vc_table = Table()
    vc_table.add_column("Rank", style="cyan", width=6)
    vc_table.add_column("VC", width=30)
    vc_table.add_column("Stage Focus", width=12)
    vc_table.add_column("Notable Investments", width=40)

    for i, vc in enumerate(vc_list[:15], 1):
        notable = ", ".join(vc.notable_investments[:3])
        vc_table.add_row(
            str(i),
            vc.name,
            vc.stage_focus,
            notable,
        )

    console.print(vc_table)
    console.print(f"\n[dim](Showing top 15 of {len(vc_list)} VCs)[/dim]")


@app.command()
def ui(
    port: int = typer.Option(5000, "--port", "-p", help="Port to run the UI on"),
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Host to bind to"),
) -> None:
    """Launch the web UI for reviewing jobs and providing feedback."""
    from role_radar.web.app import run_server

    settings = load_settings()
    run_server(host=host, port=port, outputs_dir=settings.output_dir)


@app.command()
def prep(
    cv: Path = typer.Argument(..., help="Path to your CV (PDF, DOCX, or TXT)"),
    job_id: Optional[str] = typer.Option(
        None, "--job-id", help="Generate prep for a specific job ID from the latest report"
    ),
    rank: Optional[int] = typer.Option(
        None, "--rank", "-r", help="Generate prep for the job at this rank in the latest report"
    ),
    top: Optional[int] = typer.Option(
        None, "--top", "-t", help="Generate prep for the top N jobs from the latest report"
    ),
    report: Optional[Path] = typer.Option(
        None, "--report", help="Use a specific report JSON instead of the latest"
    ),
    candidate_name: Optional[str] = typer.Option(
        None, "--name", help="Candidate name for the doc header (defaults to env or 'Candidate')"
    ),
    no_docx: bool = typer.Option(
        False, "--no-docx", help="Skip DOCX rendering, only emit Markdown"
    ),
    review: bool = typer.Option(
        False, "--review", help="Run a second-pass critic on the generated doc and append findings"
    ),
    output_dir: Optional[Path] = typer.Option(
        None, "--output-dir", "-o", help="Where to write prep docs (default: outputs/prep/)"
    ),
) -> None:
    """Generate an LLM-powered interview prep doc for a job from the latest report.

    Examples:
        # Top match from the latest report
        role-radar prep cv.pdf --rank 1

        # Top 3 matches
        role-radar prep cv.pdf --top 3

        # Specific job
        role-radar prep cv.pdf --job-id cursor_66e67c2e
    """
    from role_radar.cv_parser import parse_cv
    from role_radar.interview_prep import (
        PrepGenerationError,
        generate_prep_for_job,
        render_review_markdown,
        review_prep_doc,
    )
    from role_radar.interview_prep.report_loader import (
        find_jobs_in_report,
        find_latest_report,
        load_report,
    )

    settings = load_settings()
    settings.ensure_dirs()
    setup_logging(level=settings.log_level, format_type=settings.log_format)

    # Need exactly one job selector
    selectors = [s for s in (job_id, rank, top) if s is not None]
    if len(selectors) != 1:
        console.print("[red]Error:[/red] specify exactly one of --job-id, --rank, or --top")
        raise typer.Exit(1)

    if not cv.exists():
        console.print(f"[red]Error:[/red] CV file not found: {cv}")
        raise typer.Exit(1)

    # Locate report
    report_path = report or find_latest_report(settings.output_dir)
    if report_path is None or not report_path.exists():
        console.print(
            f"[red]Error:[/red] no report found in {settings.output_dir}. Run `role-radar run` first."
        )
        raise typer.Exit(1)

    console.print(f"[dim]Using report: {report_path.name}[/dim]")
    report_data = load_report(report_path)
    matches = find_jobs_in_report(
        report_data, job_id=job_id, rank=rank, top_n=top
    )
    if not matches:
        sel = job_id or (f"rank {rank}" if rank else f"top {top}")
        console.print(f"[red]Error:[/red] no jobs in {report_path.name} match {sel}")
        raise typer.Exit(1)

    # Parse CV once
    console.print("\n[bold]Parsing CV...[/bold]")
    cv_signals = parse_cv(cv)
    console.print(
        f"  {len(cv_signals.skills)} skills · {len(cv_signals.domains)} domains · "
        f"seniority: {cv_signals.inferred_seniority or 'unknown'}"
    )

    name = candidate_name or os.environ.get("ROLE_RADAR_CANDIDATE_NAME") or "Candidate"
    out_dir = output_dir or (settings.output_dir / "prep")

    successes = 0
    failures = 0
    for entry_rank, job in matches:
        console.print(
            f"\n[bold]Generating prep ({entry_rank}/{len(matches)}):[/bold] "
            f"{job.company} — {job.title}"
        )
        try:
            with console.status("Calling Claude (this takes ~30-90s)...", spinner="dots"):
                prep_doc, md_path, docx_path = generate_prep_for_job(
                    job=job,
                    cv=cv_signals,
                    candidate_name=name,
                    output_dir=out_dir,
                    cv_excerpt=cv_signals.raw_text,
                    write_docx=not no_docx,
                )
            console.print(f"  [green]✓[/green] Markdown: {md_path}")
            if docx_path:
                console.print(f"  [green]✓[/green] DOCX:     {docx_path}")

            if review:
                with console.status("Running review pass...", spinner="dots"):
                    try:
                        report = review_prep_doc(md_path.read_text(encoding="utf-8"))
                        review_md = render_review_markdown(report)
                        with open(md_path, "a", encoding="utf-8") as f:
                            f.write(review_md)
                        console.print(
                            f"  [green]✓[/green] Review:   {report.overall_score}/10 · "
                            f"{len(report.findings)} findings · "
                            f"recommendation: [bold]{report.ship_recommendation}[/bold]"
                        )
                    except Exception as e:
                        console.print(f"  [yellow]![/yellow] Review failed: {e}")

            successes += 1
        except PrepGenerationError as e:
            console.print(f"  [red]✗[/red] Failed: {e}")
            failures += 1

    console.print(
        Panel(
            f"[bold]Generated:[/bold] {successes}\n"
            f"[bold]Failed:[/bold] {failures}\n"
            f"[bold]Output:[/bold] {out_dir}",
            title="Prep Complete",
            border_style="green" if failures == 0 else "yellow",
        )
    )


@app.command()
def debug(
    company: str = typer.Argument(..., help="Company name to debug"),
) -> None:
    """Debug job fetching for a specific company."""
    import json

    settings = load_settings()
    settings.ensure_dirs()

    setup_logging(level="DEBUG", format_type="text")

    # Find company in AI list or VC-backed
    ai_companies, _ = generate_ai_top20()
    target = None

    for c in ai_companies:
        if company.lower() in c.name.lower():
            target = c
            break

    if not target:
        console.print(f"[yellow]Company '{company}' not found in AI Top 20[/yellow]")
        console.print("[dim]Tip: Use the exact company name or a partial match[/dim]")
        raise typer.Exit(1)

    console.print(Panel(
        f"[bold]Company:[/bold] {target.name}\n"
        f"[bold]Type:[/bold] {target.company_type.value}\n"
        f"[bold]ATS:[/bold] {target.ats_type.value}\n"
        f"[bold]Identifier:[/bold] {target.ats_identifier}\n"
        f"[bold]Careers URL:[/bold] {target.careers_url}",
        title=f"Debugging: {target.name}",
        border_style="blue",
    ))

    http_client = HTTPClient(
        user_agent=settings.user_agent,
        timeout=settings.request_timeout,
        requests_per_second=settings.rate_limit_requests_per_second,
    )
    connector_registry = ConnectorRegistry(http_client)

    # Get raw data
    console.print("\n[bold]Fetching raw data...[/bold]")
    raw_data = connector_registry.get_raw_data(target)

    if raw_data:
        raw_path = settings.output_dir / f"debug_{target.slug}_raw.json"
        with open(raw_path, "w") as f:
            json.dump(raw_data, f, indent=2, default=str)
        console.print(f"  - Raw data saved: {raw_path}")

    # Fetch jobs
    console.print("\n[bold]Fetching jobs...[/bold]")
    jobs = connector_registry.fetch_jobs(target)

    console.print(f"  - Found {len(jobs)} jobs")

    if jobs:
        table = Table(title="Jobs Found")
        table.add_column("ID", width=15)
        table.add_column("Title", width=40)
        table.add_column("Location", width=25)
        table.add_column("Posted", width=12)

        for job in jobs[:20]:
            posted = job.posted_date.strftime("%Y-%m-%d") if job.posted_date else "N/A"
            table.add_row(
                job.external_id[:13] + "..." if len(job.external_id) > 15 else job.external_id,
                job.title[:38] + "..." if len(job.title) > 40 else job.title,
                job.location.format()[:23],
                posted,
            )

        console.print(table)

        if len(jobs) > 20:
            console.print(f"[dim](Showing 20 of {len(jobs)} jobs)[/dim]")

    http_client.close()


if __name__ == "__main__":
    app()
