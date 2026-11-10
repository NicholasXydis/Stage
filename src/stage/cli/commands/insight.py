from typing import Annotated

import typer

from stage.cli.options import (
    WORD,
    CompanyOption,
    DatabaseOption,
    InvalidOptionError,
    JsonOption,
    RegistryOption,
    RepairOption,
    StaleDaysOption,
    _count,
    _database,
    _one,
    _parse_enum,
    _print_error,
    _refuse,
    _refusing,
    _require_enum,
    _validated_source,
    _warn_unread_boards,
    _with_db,
    app,
    run_async,
)


@app.command(
    help="Find registry gaps and unclassified companies seen in feeds",
    rich_help_panel="Registry and maintenance",
)
def coverage(
    unregistered: Annotated[
        bool,
        typer.Option(
            "--unregistered",
            help="Show feed companies not in the registry and not already reviewed",
        ),
    ] = False,
    classified: Annotated[
        bool,
        typer.Option("--classified", help="Show researched feed-company classifications"),
    ] = False,
    contradictions: Annotated[
        bool,
        typer.Option(
            "--contradictions",
            help="Show review verdicts the registry or the clock now disagrees with",
        ),
    ] = False,
    show_all: Annotated[
        bool,
        typer.Option("--all", help="List every row instead of the first 30"),
    ] = False,
    stale_days: StaleDaysOption = None,
    as_json: JsonOption = False,
    registry: RegistryOption = None,
    db: DatabaseOption = None,
) -> None:
    from datetime import UTC, datetime

    from stage.cli.render import terminal
    from stage.cli.render.health import ROW_PREVIEW, render_coverage
    from stage.cli.serialize import coverage_to_json, emit
    from stage.companies import RegistryError, load_boards
    from stage.domain import STALE_AFTER_DAYS
    from stage.services.coverage import CoverageReport
    from stage.services.coverage import coverage as coverage_service
    from stage.storage import open_repository

    console = terminal()
    now = datetime.now(UTC)

    async def run() -> CoverageReport:
        companies, personal = load_boards(registry)
        _warn_unread_boards(personal)
        async with open_repository(_database(db)) as repository:
            return await coverage_service(
                repository,
                companies,
                now=now,
                unregistered=unregistered,
                stale_after_days=STALE_AFTER_DAYS if stale_days is None else stale_days,
            )

    with _refusing(RegistryError):
        report = run_async(run())

    if as_json:
        emit(coverage_to_json(report))
        return
    render_coverage(
        console,
        report,
        now,
        include_classified=classified,
        include_contradictions=contradictions,
        limit=None if show_all else ROW_PREVIEW,
    )


@app.command(
    help="Record why a feed-seen employer is not directly synced",
    rich_help_panel="Registry and maintenance",
)
def classify(
    company: Annotated[
        str,
        typer.Argument(
            metavar="NAME",
            click_type=WORD,
            help="Employer name from stage coverage --unregistered",
        ),
    ],
    disposition: Annotated[
        str | None,
        typer.Option(
            "--status",
            metavar="STATUS",
            help="Required unless --clear: feed-only, unavailable, custom-json-candidate, "
            "adapter-candidate, or deferred",
        ),
    ] = None,
    note: Annotated[
        str | None,
        typer.Option(
            "--note", metavar="TEXT", help="Required unless --clear: evidence for this decision"
        ),
    ] = None,
    url: Annotated[
        str | None, typer.Option("--url", metavar="URL", help="Public careers or jobs endpoint")
    ] = None,
    clear: Annotated[bool, typer.Option("--clear", help="Remove this classification")] = False,
    db: DatabaseOption = None,
) -> None:
    from datetime import UTC, datetime

    from stage.cli.render import plain, terminal
    from stage.domain import CoverageClassification, CoverageDisposition

    console = terminal()
    if not company.strip():
        _refuse(
            "[red]stage classify needs the employer's name[/red], as "
            "[bold]stage coverage --unregistered[/bold] lists it."
        )
    if clear:
        if disposition is not None or note is not None or url is not None:
            _refuse("[red]--clear cannot be combined with --status, --note, or --url.[/red]")
        with _refusing(ValueError):
            removed = _with_db(
                db, lambda repository: repository.clear_coverage_classification(company)
            )
        if not removed:
            _print_error(plain(f'No classification is recorded for "{company}".', "yellow"))
            raise typer.Exit(code=1)
        console.print("Classification removed.")
        return
    if disposition is None or note is None:
        options = ", ".join(item.value for item in CoverageDisposition)
        choices = f" --status takes one of: {options}." if disposition is None else ""
        _refuse(f"[red]Pass both --status and --note, or pass --clear.[/red]{choices}")
    if not note.strip():
        _refuse(
            "[red]--note needs the evidence for this decision[/red], such as "
            '--note "no careers page".'
        )
    with _refusing(InvalidOptionError, ValueError):
        entry = CoverageClassification(
            company=company,
            disposition=_require_enum(disposition, CoverageDisposition, "--status"),
            note=note,
            checked_on=datetime.now(UTC),
            url=url,
        )
        replaced = _with_db(db, lambda repository: repository.record_coverage_classification(entry))
    console.print("Classification updated." if replaced else "Classification recorded.")


@app.command(
    help="Check database integrity, source health, and staleness", rich_help_panel="Health"
)
def doctor(
    show_all: Annotated[
        bool,
        typer.Option("--all", help="List every board and row instead of the first 10"),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", help="Print each board error in full"),
    ] = False,
    stale_days: StaleDaysOption = None,
    as_json: JsonOption = False,
    repair: RepairOption = False,
    skip_file_check: Annotated[
        bool,
        typer.Option(
            "--skip-file-check",
            help="Skip reading the whole database file for damage, as the scheduled check does",
        ),
    ] = False,
    db: DatabaseOption = None,
    registry: RegistryOption = None,
) -> None:
    from datetime import UTC, datetime

    from stage.cli.render import terminal
    from stage.cli.render.health import (
        BOARD_PREVIEW,
        render_app_health,
        render_board_list,
        render_doctor,
        render_repairs,
    )
    from stage.cli.serialize import emit, health_to_json
    from stage.companies import RegistryError, load_companies, load_personal, personal_kept
    from stage.domain import STALE_AFTER_DAYS, IntegrityRepair
    from stage.services.boards import summarize
    from stage.services.connect import app_health
    from stage.services.health import DoctorReport
    from stage.services.health import doctor as run_doctor
    from stage.services.maintenance import repair_integrity
    from stage.storage import open_repository

    console = terminal()
    now = datetime.now(UTC)
    with _refusing(RegistryError):
        packaged = load_companies(registry)
    personal = load_personal()
    rows = (*packaged, *personal_kept(packaged, personal.rows))
    boards = summarize(personal, packaged, now.date())

    async def run() -> tuple[DoctorReport, tuple[IntegrityRepair, ...]]:
        async with open_repository(_database(db)) as repository:
            repairs = await repair_integrity(repository) if repair else ()
            report = await run_doctor(
                repository,
                now=now,
                stale_after_days=STALE_AFTER_DAYS if stale_days is None else stale_days,
                companies=rows,
                whole_file=not skip_file_check,
            )
            return report, repairs

    report, repairs = run_async(run())
    apps = app_health()
    if as_json:
        emit(health_to_json(report, apps, boards))
    else:
        render_repairs(console, repairs)
        render_doctor(
            console,
            report,
            now,
            limit=None if show_all else BOARD_PREVIEW,
            verbose=verbose,
            file_checked=not skip_file_check,
        )
        render_board_list(console, boards)
        render_app_health(console, apps)
        from stage.cli.schedule import advice

        for hint in advice():
            console.print(f"[yellow]{hint}[/yellow]")
    if not report.is_healthy:
        raise typer.Exit(code=1)


@app.command(help="Show sync history and database totals", rich_help_panel="Health")
def stats(
    show_all: Annotated[
        bool,
        typer.Option("--all", help="List every breakdown row instead of the first 10"),
    ] = False,
    runs: Annotated[
        int,
        typer.Option(
            "--runs", metavar="N", click_type=_count(1, 200), help="How many recent syncs to show"
        ),
    ] = 10,
    as_json: JsonOption = False,
    db: DatabaseOption = None,
) -> None:
    from datetime import UTC, datetime

    from stage.cli.render import terminal
    from stage.cli.render.health import BOARD_PREVIEW, render_stats
    from stage.cli.serialize import emit, stats_to_json
    from stage.services.health import statistics

    console = terminal()

    report = _with_db(db, lambda repository: statistics(repository, history=max(1, runs)))
    if as_json:
        emit(stats_to_json(report))
    else:
        render_stats(
            console,
            report,
            datetime.now(UTC),
            limit=None if show_all else BOARD_PREVIEW,
        )


@app.command(help="Inspect source health, rate limits, and HTTP caches", rich_help_panel="Health")
def sources(
    verbose: Annotated[
        bool,
        typer.Option("--verbose", help="Print each error and reason in full"),
    ] = False,
    reset_rate_limit: Annotated[
        str | None,
        typer.Option(
            "--reset-rate-limit",
            metavar="BUCKET",
            help="Reset stored rate-limit state for one host bucket",
        ),
    ] = None,
    reset_all_rate_limits: Annotated[
        bool,
        typer.Option(
            "--reset-all",
            help="Reset stored rate-limit state for every host bucket",
        ),
    ] = False,
    boards: Annotated[
        bool,
        typer.Option("--boards", help="Include every failing, stale, refusing or gone board"),
    ] = False,
    clear_cache: Annotated[
        str | None,
        typer.Option(
            "--clear-cache",
            metavar="SOURCE",
            help="Clear saved HTTP validators for one source to force a refetch",
        ),
    ] = None,
    clear_cache_all: Annotated[
        bool,
        typer.Option("--clear-cache-all", help="Clear saved HTTP validators for every source"),
    ] = False,
    stale_days: StaleDaysOption = None,
    as_json: JsonOption = False,
    db: DatabaseOption = None,
    registry: RegistryOption = None,
) -> None:
    from datetime import UTC, datetime
    from difflib import get_close_matches

    from stage.cli.render import plain, terminal
    from stage.cli.render.health import (
        render_board_health,
        render_rate_state,
        render_source_health,
        render_workday_crawl_progress,
    )
    from stage.cli.serialize import emit, sources_to_json
    from stage.companies import RegistryError, load_boards
    from stage.domain import STALE_AFTER_DAYS
    from stage.domain.text import counted, did_you_mean
    from stage.services.health import DoctorReport
    from stage.services.health import doctor as run_doctor
    from stage.services.maintenance import RateStateView, rate_state
    from stage.storage import open_repository

    console = terminal()
    now = datetime.now(UTC)

    if reset_rate_limit is not None and reset_all_rate_limits:
        _refuse("[red]Pass either --reset-rate-limit BUCKET or --reset-all, not both.[/red]")
    if clear_cache is not None and clear_cache_all:
        _refuse("[red]Pass either --clear-cache SOURCE or --clear-cache-all, not both.[/red]")
    with _refusing(InvalidOptionError, RegistryError):
        clear_cache = _validated_source(clear_cache)
        rows, _ = load_boards(registry)

    async def run() -> tuple[RateStateView, DoctorReport]:
        async with open_repository(_database(db)) as repository:
            view = await rate_state(
                repository,
                bucket=reset_rate_limit,
                clear_all=reset_all_rate_limits,
                clear_cache=clear_cache,
                clear_cache_all=clear_cache_all,
            )
            report = await run_doctor(
                repository,
                now=now,
                stale_after_days=STALE_AFTER_DAYS if stale_days is None else stale_days,
                companies=rows,
            )
            return view, report

    view, report = run_async(run())
    cleared, states = view.cleared, view.states

    if not as_json and (view.validators_cleared or clear_cache is not None or clear_cache_all):
        target = "every source" if clear_cache_all else f'source "{clear_cache}"'
        console.print(
            f"Cleared {counted(view.validators_cleared, 'saved HTTP validator')} for {target}."
        )
        console.print("[dim]The next sync will fully refetch those sources.[/dim]")

    if not as_json and (reset_rate_limit is not None or reset_all_rate_limits):
        target = (
            "every host bucket" if reset_all_rate_limits else f'host bucket "{reset_rate_limit}"'
        )
        if cleared:
            console.print(
                plain(f"Reset rate-limit state for {target} ({counted(cleared, 'row')}).")
            )
        elif reset_all_rate_limits:
            console.print(plain("No host bucket had stored rate state.", "yellow"))
        else:
            near = get_close_matches(
                reset_rate_limit or "", [state.bucket for state in states], n=1
            )
            hint = f" {did_you_mean(near[0])}" if near else ""
            console.print(plain(f"No stored rate state for {target}.{hint}", "yellow"))

    if as_json:
        emit(
            sources_to_json(
                report,
                states,
                include_boards=boards,
                rate_states_cleared=cleared,
                cache_validators_cleared=view.validators_cleared,
            )
        )
        return

    render_source_health(console, report.sources, report.stale_after_days)
    console.print()
    render_workday_crawl_progress(console, report.workday_crawls)
    console.print()
    render_rate_state(console, states, now, verbose=verbose)
    if boards:
        console.print()
        render_board_health(console, report.sources, now, verbose=verbose)


@app.command(help="Inspect rejected postings and why they were rejected", rich_help_panel="Health")
def quarantine(
    reason: Annotated[
        list[str] | None,
        typer.Option(
            "--reason",
            metavar="REASON",
            help=(
                "Filter by rejection reason: out-of-scope-location, not-an-internship, "
                "out-of-scope-degree, not-a-cs-role, unknown-cs-role"
            ),
        ),
    ] = None,
    source: Annotated[
        list[str] | None,
        typer.Option("--source", metavar="SOURCE", help="Filter by source adapter name"),
    ] = None,
    company: CompanyOption = None,
    show_all: Annotated[
        bool,
        typer.Option("--all", help="Show every match instead of the first page"),
    ] = False,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            metavar="N",
            click_type=_count(1),
            help="Maximum number of rejected postings to show (--all shows every match)",
        ),
    ] = 50,
    as_json: JsonOption = False,
    db: DatabaseOption = None,
) -> None:
    from stage.cli.render import terminal
    from stage.cli.render.health import render_quarantine
    from stage.cli.serialize import emit, quarantine_to_json
    from stage.domain import QuarantineFilters, RejectionReason
    from stage.services.quarantine import list_quarantined

    console = terminal()

    with _refusing(InvalidOptionError):
        filters = QuarantineFilters(
            reason=_parse_enum(_one(reason, "--reason", mine=False), RejectionReason, "--reason"),
            source=_validated_source(_one(source, "--source", mine=False)),
            company=_one(company, "--company", mine=False),
            limit=None if show_all else limit,
        )

    listing = _with_db(db, lambda repository: list_quarantined(repository, filters))

    if as_json:
        emit(quarantine_to_json(listing.entries))
        return

    render_quarantine(
        console,
        listing.entries,
        total_matching=listing.total_matching,
        reason_counts=listing.reason_counts,
        filtered=bool(filters.reason or filters.source or filters.company),
    )
