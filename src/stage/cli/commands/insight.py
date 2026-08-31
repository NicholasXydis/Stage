from typing import Annotated

import typer

from stage.cli.options import (
    WORD,
    DatabaseOption,
    InvalidOptionError,
    JsonOption,
    RegistryOption,
    RepairOption,
    StaleDaysOption,
    _count,
    _database,
    _parse_enum,
    _print_failure,
    _require_enum,
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

    from stage.cli.render import ROW_PREVIEW, render_coverage, terminal
    from stage.cli.serialize import coverage_to_json, emit
    from stage.companies import RegistryError, load_companies
    from stage.services.coverage import CoverageReport
    from stage.services.coverage import coverage as coverage_service
    from stage.storage import open_repository

    console = terminal()
    now = datetime.now(UTC)

    async def run() -> CoverageReport:
        companies = load_companies(registry)
        async with open_repository(_database(db)) as repository:
            if stale_days is None:
                return await coverage_service(
                    repository,
                    companies,
                    now=now,
                    unregistered=unregistered,
                )
            return await coverage_service(
                repository,
                companies,
                now=now,
                unregistered=unregistered,
                stale_after_days=stale_days,
            )

    try:
        report = run_async(run())
    except RegistryError as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc

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

    from stage.cli.render import terminal
    from stage.domain import CoverageClassification, CoverageDisposition
    from stage.storage import open_repository

    console = terminal()
    if clear:
        if disposition is not None or note is not None or url is not None:
            console.print("[red]--clear cannot be combined with --status, --note, or --url.[/red]")
            raise typer.Exit(code=2)
        try:

            async def clear_entry() -> bool:
                async with open_repository(_database(db)) as repository:
                    return await repository.clear_coverage_classification(company)

            removed = run_async(clear_entry())
        except ValueError as exc:
            _print_failure(exc)
            raise typer.Exit(code=2) from exc
        if not removed:
            console.print("[yellow]No matching classification was recorded.[/yellow]")
            raise typer.Exit(code=1)
        console.print("Classification removed.")
        return
    if disposition is None or note is None:
        options = ", ".join(item.value for item in CoverageDisposition)
        console.print(
            f"[red]Pass both --status and --note, or pass --clear.[/red]\n"
            f"--status must be one of: {options}"
        )
        raise typer.Exit(code=2)
    try:
        entry = CoverageClassification(
            company=company,
            disposition=_require_enum(disposition, CoverageDisposition, "--status"),
            note=note,
            checked_on=datetime.now(UTC),
            url=url,
        )

        async def record_entry() -> bool:
            async with open_repository(_database(db)) as repository:
                return await repository.record_coverage_classification(entry)

        replaced = run_async(record_entry())
    except (InvalidOptionError, ValueError) as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc
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
    db: DatabaseOption = None,
    registry: RegistryOption = None,
) -> None:
    from datetime import UTC, datetime

    from stage.cli.render import (
        BOARD_PREVIEW,
        failure,
        render_doctor,
        render_repairs,
        terminal,
    )
    from stage.cli.serialize import emit, health_to_json
    from stage.companies import RegistryError, load_companies
    from stage.domain import IntegrityRepair
    from stage.services.health import DoctorReport
    from stage.services.health import doctor as run_doctor
    from stage.services.maintenance import repair_integrity
    from stage.storage import open_repository

    console = terminal()
    now = datetime.now(UTC)
    try:
        rows = load_companies(registry)
    except RegistryError as exc:
        console.print(failure(exc))
        raise typer.Exit(code=2) from exc

    async def run() -> tuple[DoctorReport, tuple[IntegrityRepair, ...]]:
        async with open_repository(_database(db)) as repository:
            repairs = await repair_integrity(repository) if repair else ()
            if stale_days is None:
                return await run_doctor(repository, now=now, companies=rows), repairs
            return (
                await run_doctor(repository, now=now, stale_after_days=stale_days, companies=rows),
                repairs,
            )

    report, repairs = run_async(run())
    if as_json:
        emit(health_to_json(report))
    else:
        render_repairs(console, repairs)
        render_doctor(
            console,
            report,
            now,
            limit=None if show_all else BOARD_PREVIEW,
            verbose=verbose,
        )
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

    from stage.cli.render import BOARD_PREVIEW, render_stats, terminal
    from stage.cli.serialize import emit, stats_to_json
    from stage.services.health import StatsReport, statistics
    from stage.storage import open_repository

    console = terminal()

    async def run() -> StatsReport:
        async with open_repository(_database(db)) as repository:
            return await statistics(repository, history=max(1, runs))

    report = run_async(run())
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
        typer.Option("--boards", help="Include every failing or stale board"),
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
) -> None:
    from datetime import UTC, datetime

    from stage.cli.render import (
        render_board_health,
        render_rate_state,
        render_source_health,
        render_workday_crawl_progress,
        terminal,
    )
    from stage.cli.serialize import emit, sources_to_json
    from stage.services.health import DoctorReport
    from stage.services.health import doctor as run_doctor
    from stage.services.maintenance import RateStateView, rate_state
    from stage.storage import open_repository

    console = terminal()
    now = datetime.now(UTC)

    if reset_rate_limit is not None and reset_all_rate_limits:
        console.print("[red]Pass either --reset-rate-limit BUCKET or --reset-all, not both.[/red]")
        raise typer.Exit(code=2)
    if clear_cache is not None and clear_cache_all:
        console.print(
            "[red]Pass either --clear-cache <source> or --clear-cache-all, not both.[/red]"
        )
        raise typer.Exit(code=2)

    async def run() -> tuple[RateStateView, DoctorReport]:
        async with open_repository(_database(db)) as repository:
            view = await rate_state(
                repository,
                bucket=reset_rate_limit,
                clear_all=reset_all_rate_limits,
                clear_cache=clear_cache,
                clear_cache_all=clear_cache_all,
            )
            report = (
                await run_doctor(repository, now=now)
                if stale_days is None
                else await run_doctor(repository, now=now, stale_after_days=stale_days)
            )
            return view, report

    view, report = run_async(run())
    cleared, states = view.cleared, view.states

    if view.validators_cleared or clear_cache is not None or clear_cache_all:
        target = "every source" if clear_cache_all else f"source {clear_cache!r}"
        console.print(f"Cleared {view.validators_cleared} saved HTTP validator(s) for {target}.")
        console.print("[dim]The next sync will fully refetch those sources.[/dim]")

    if reset_rate_limit is not None or reset_all_rate_limits:
        target = (
            "every host bucket" if reset_all_rate_limits else f"host bucket {reset_rate_limit!r}"
        )
        if cleared:
            console.print(f"Reset rate-limit state for {target} ({cleared} row(s)).")
        else:
            console.print(f"[yellow]No stored rate state for {target}.[/yellow]")

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
        str | None,
        typer.Option(
            "--reason",
            metavar="REASON",
            help=(
                "Filter by rejection reason: unknown-location, not-an-internship, "
                "out-of-scope-degree, not-a-cs-role, unknown-cs-role"
            ),
        ),
    ] = None,
    source: Annotated[
        str | None,
        typer.Option("--source", metavar="SOURCE", help="Filter by source adapter name"),
    ] = None,
    company: Annotated[
        str | None,
        typer.Option("--company", metavar="NAME", help="Filter by exact employer name"),
    ] = None,
    show_all: Annotated[
        bool,
        typer.Option("--all", help="Show every match instead of the first page"),
    ] = False,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            metavar="N",
            click_type=_count(1, None),
            help="Maximum number of rejected postings to show; --all shows every match",
        ),
    ] = 50,
    as_json: JsonOption = False,
    db: DatabaseOption = None,
) -> None:
    from stage.cli.render import render_quarantine, terminal
    from stage.cli.serialize import emit, quarantine_to_json
    from stage.domain import QuarantineFilters, RejectionReason
    from stage.services.quarantine import QuarantineListing, list_quarantined
    from stage.storage import open_repository

    console = terminal()

    try:
        filters = QuarantineFilters(
            reason=_parse_enum(reason, RejectionReason, "--reason"),
            source=source,
            company=company,
            limit=None if show_all else limit,
        )
    except InvalidOptionError as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc

    async def run() -> QuarantineListing:
        async with open_repository(_database(db)) as repository:
            return await list_quarantined(repository, filters)

    listing = run_async(run())

    if as_json:
        emit(quarantine_to_json(listing.entries))
        return

    render_quarantine(
        console,
        listing.entries,
        total_matching=listing.total_matching,
        reason_counts=listing.reason_counts,
    )
