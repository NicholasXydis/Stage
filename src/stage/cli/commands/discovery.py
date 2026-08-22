from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from stage.cli.options import (
    DatabaseOption,
    InvalidOptionError,
    RegistryOption,
    _adopt_unregistered,
    _lock_path,
    _parse_enum,
    _print_failure,
    _require_enum,
    app,
    run_async,
)

if TYPE_CHECKING:
    pass


@app.command(help="Find the job-board platform a company uses")
def discover(
    companies: Annotated[
        list[str] | None,
        typer.Argument(help="Company names to probe"),
    ] = None,
    url: Annotated[
        str | None,
        typer.Option(
            "--url",
            help="Extract the platform from a careers URL without fetching that URL",
        ),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option("--name", help="Company name to show with --url"),
    ] = None,
    platform: Annotated[
        list[str] | None,
        typer.Option("--platform", help="Probe only these platform adapters; repeatable"),
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option(
            "--exclude",
            help="Skip these platform adapters while probing; repeatable",
        ),
    ] = None,
    only: Annotated[
        list[str] | None,
        typer.Option(
            "--company",
            help="With --verify, recheck only these registry companies; repeatable",
        ),
    ] = None,
    expect_size: Annotated[
        str | None,
        typer.Option(
            "--expect-size",
            help="Optional employer-size hint: startup, mid, large",
        ),
    ] = None,
    request_log: Annotated[
        Path | None,
        typer.Option(
            "--request-log",
            help="Write outbound HTTP requests to this JSON Lines file",
        ),
    ] = None,
    scheduled_progress: Annotated[
        Path | None,
        typer.Option("--scheduled-progress", hidden=True),
    ] = None,
    verify: Annotated[
        bool,
        typer.Option(
            "--verify",
            help="Recheck existing registry companies",
        ),
    ] = False,
    unregistered: Annotated[
        bool,
        typer.Option(
            "--unregistered",
            help="Probe companies found in feeds but missing from the registry",
        ),
    ] = False,
    direct_only: Annotated[
        bool,
        typer.Option(
            "--direct-only",
            help="With --unregistered, verify only ATS links already present in feed applications",
        ),
    ] = False,
    adopt_unnamed: Annotated[
        bool,
        typer.Option(
            "--adopt-unnamed",
            help="With --direct-only, also adopt boards whose platform publishes no board name",
        ),
    ] = False,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            min=1,
            max=2000,
            help="Maximum unregistered company names to probe",
        ),
    ] = 40,
    db: DatabaseOption = None,
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Write results to the registry; requires --verify or --unregistered",
        ),
    ] = False,
    registry: RegistryOption = None,
) -> None:
    from contextlib import ExitStack

    from rich.console import Console

    from stage.cli.logfile import open_request_log
    from stage.cli.render import failure, plain, render_discovery
    from stage.cli.runlock import AnotherRunInProgressError, single_run
    from stage.cli.schedule_state import ScheduleStateWriter
    from stage.companies import RegistryError
    from stage.companies import load_companies as load_registry
    from stage.domain import DiscoveryEvent, DiscoveryFinished, EmployerSize, Platform
    from stage.services.discover import NoMatchingCompanyError

    console = Console()
    scheduled_state = (
        ScheduleStateWriter.open(scheduled_progress, "discover")
        if scheduled_progress is not None
        else None
    )
    progress = scheduled_state.discovery_event if scheduled_state is not None else None

    if url is not None and companies:
        console.print("[red]Pass either company names or --url, not both.[/red]")
        raise typer.Exit(code=2)
    if verify and unregistered:
        console.print("[red]Pass either --verify or --unregistered, not both.[/red]")
        raise typer.Exit(code=2)
    if direct_only and not unregistered:
        console.print("[red]--direct-only requires --unregistered.[/red]")
        raise typer.Exit(code=2)
    if adopt_unnamed and not direct_only:
        console.print(
            "[red]--adopt-unnamed requires --direct-only[/red] — the token must come from an "
            "apply URL."
        )
        raise typer.Exit(code=2)
    if apply and not (verify or unregistered):
        console.print("[red]--apply only means something with --verify or --unregistered.[/red]")
        raise typer.Exit(code=2)
    if platform and exclude:
        console.print("[red]Pass either --platform or --exclude, not both.[/red]")
        raise typer.Exit(code=2)
    if not verify and not unregistered and url is None and not companies:
        console.print(
            "[red]Nothing to discover.[/red] Pass a company name, a careers page with "
            "[bold]--url[/bold], or [bold]--verify[/bold] to re-probe the registry."
        )
        raise typer.Exit(code=2)

    try:
        size = _parse_enum(expect_size, EmployerSize, "--expect-size")
        platforms = (
            [_require_enum(value, Platform, "--platform") for value in platform]
            if platform
            else None
        )
        excluded = (
            [_require_enum(value, Platform, "--exclude") for value in exclude] if exclude else None
        )
    except InvalidOptionError as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc

    async def run() -> object:
        from datetime import UTC, datetime

        from stage.services.discover import probe_companies, resolve_careers_url

        today = datetime.now(UTC).date()
        with ExitStack() as stack:
            stream = (
                stack.enter_context(open_request_log(request_log))
                if request_log is not None
                else None
            )
            if verify:
                from stage.companies import load_companies, update_registry
                from stage.services.discover import apply_verification, verify_registry

                rows = checked if checked is not None else load_companies(registry)
                outcome = await render_discovery(
                    console,
                    verify_registry(rows, platforms=platforms, excluded=excluded, only=only),
                    verified_on=today,
                    request_log=stream,
                    collect=True,
                    progress=progress,
                )
                if apply and isinstance(outcome, DiscoveryFinished):

                    def update(
                        existing: tuple[Any, ...],
                    ) -> tuple[tuple[Any, ...], tuple[int, int]]:
                        updated, ok, off = apply_verification(existing, outcome, today)
                        return updated, (ok, off)

                    target, (ok, off) = update_registry(update, registry)
                    console.print(
                        plain(
                            f"\napplied — {ok} row(s) verified, {off} disabled, written to {target}"
                        )
                    )
                return isinstance(outcome, DiscoveryFinished) and bool(outcome.matched)
            if url is not None:

                async def once() -> AsyncIterator[DiscoveryEvent]:
                    yield resolve_careers_url(url)

                return await render_discovery(console, once(), display_name=name, progress=progress)

            if unregistered:
                adopted = await _adopt_unregistered(
                    console,
                    registry=registry,
                    db=db,
                    platforms=platforms,
                    excluded=excluded,
                    size=size,
                    limit=limit,
                    direct_only=direct_only,
                    adopt_unnamed=adopt_unnamed,
                    apply_rows=apply,
                    today=today,
                    stream=stream,
                    progress=progress,
                )
                if scheduled_state is not None:
                    scheduled_state.heartbeat()
                return adopted

            events = probe_companies(
                companies or [], platforms=platforms, excluded=excluded, size=size
            )
            return await render_discovery(
                console, events, verified_on=today, request_log=stream, progress=progress
            )

    checked = None
    if verify:
        try:
            checked = load_registry(registry)
        except RegistryError as exc:
            console.print(failure(exc))
            raise typer.Exit(code=2) from exc

    try:
        if url is not None:
            resolved = run_async(run())
        else:
            with single_run("discover", _lock_path(db)):
                resolved = run_async(run())
    except AnotherRunInProgressError as exc:
        if scheduled_state is not None:
            scheduled_state.blocked(str(exc))
        console.print(failure(exc))
        raise typer.Exit(code=2) from exc
    except (RegistryError, NoMatchingCompanyError) as exc:
        console.print(failure(exc))
        raise typer.Exit(code=2) from exc
    raise typer.Exit(code=0 if resolved or unregistered else 1)
