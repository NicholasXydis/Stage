from typing import TYPE_CHECKING, Annotated

import typer

from stage.cli.options import (
    DatabaseOption,
    InvalidOptionError,
    JsonOption,
    RegistryOption,
    RepairOption,
    StaleDaysOption,
    _database,
    _print_failure,
    _require_enum,
    app,
    run_async,
)

if TYPE_CHECKING:
    pass


@app.command(help="Find registry gaps and unclassified companies seen in feeds")
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
    stale_days: StaleDaysOption = None,
    as_json: JsonOption = False,
    registry: RegistryOption = None,
    db: DatabaseOption = None,
) -> None:
    from datetime import UTC, datetime

    from rich.console import Console

    from stage.cli.render import render_coverage
    from stage.cli.serialize import coverage_to_json, emit
    from stage.companies import RegistryError, load_companies
    from stage.services.coverage import CoverageReport
    from stage.services.coverage import coverage as coverage_service
    from stage.storage import open_repository

    console = Console()
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
    )


@app.command(help="Record why a feed-seen employer is not directly synced")
def classify(
    company: Annotated[
        str, typer.Argument(help="Employer name from stage coverage --unregistered")
    ],
    disposition: Annotated[
        str | None,
        typer.Option(
            "--status",
            help="Required unless --clear: feed-only, unavailable, custom-json-candidate, "
            "adapter-candidate, or deferred",
        ),
    ] = None,
    note: Annotated[
        str | None,
        typer.Option("--note", help="Required unless --clear: evidence for this decision"),
    ] = None,
    url: Annotated[
        str | None, typer.Option("--url", help="Public careers or jobs endpoint")
    ] = None,
    clear: Annotated[bool, typer.Option("--clear", help="Remove this classification")] = False,
    db: DatabaseOption = None,
) -> None:
    from datetime import UTC, datetime

    from rich.console import Console

    from stage.domain import CoverageClassification, CoverageDisposition
    from stage.storage import open_repository

    console = Console()
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
        console.print("[red]Pass both --status and --note, or pass --clear.[/red]")
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


@app.command(help="Check database integrity, source health, and staleness")
def doctor(
    stale_days: StaleDaysOption = None,
    as_json: JsonOption = False,
    repair: RepairOption = False,
    db: DatabaseOption = None,
    registry: RegistryOption = None,
) -> None:
    from datetime import UTC, datetime

    from rich.console import Console

    from stage.cli.render import failure, render_doctor, render_repairs
    from stage.cli.serialize import emit, health_to_json
    from stage.companies import RegistryError, load_companies
    from stage.domain import IntegrityRepair
    from stage.services.health import DoctorReport
    from stage.services.health import doctor as run_doctor
    from stage.services.maintenance import repair_integrity
    from stage.storage import open_repository

    console = Console()
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
        render_doctor(console, report, now)
    if not report.is_healthy:
        raise typer.Exit(code=1)


@app.command(help="Show sync history and database totals")
def stats(
    runs: Annotated[
        int,
        typer.Option("--runs", min=1, max=200, help="How many recent syncs to show"),
    ] = 10,
    as_json: JsonOption = False,
    db: DatabaseOption = None,
) -> None:
    from datetime import UTC, datetime

    from rich.console import Console

    from stage.cli.render import render_stats
    from stage.cli.serialize import emit, stats_to_json
    from stage.services.health import StatsReport, statistics
    from stage.storage import open_repository

    console = Console()

    async def run() -> StatsReport:
        async with open_repository(_database(db)) as repository:
            return await statistics(repository, history=max(1, runs))

    report = run_async(run())
    if as_json:
        emit(stats_to_json(report))
    else:
        render_stats(console, report, datetime.now(UTC))


@app.command(help="Inspect source health, rate limits, and HTTP caches")
def sources(
    reset_rate_limit: Annotated[
        str | None,
        typer.Option(
            "--reset-rate-limit",
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
            "--clear-cache", help="Clear saved HTTP validators for one source to force a refetch"
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

    from rich.console import Console

    from stage.cli.render import (
        render_board_health,
        render_rate_state,
        render_source_health,
        render_workday_crawl_progress,
    )
    from stage.cli.serialize import emit, sources_to_json
    from stage.services.health import DoctorReport
    from stage.services.health import doctor as run_doctor
    from stage.services.maintenance import RateStateView, rate_state
    from stage.storage import open_repository

    console = Console()
    now = datetime.now(UTC)

    if reset_rate_limit is not None and reset_all_rate_limits:
        console.print(
            "[red]Pass either --reset-rate-limit <bucket> or --reset-all, not both.[/red]"
        )
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
    render_rate_state(console, states, now)
    if boards:
        console.print()
        render_board_health(console, report.sources, now)
