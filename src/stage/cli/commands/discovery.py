from collections.abc import AsyncIterator, Callable, Sequence
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, TextIO

import typer

from stage.cli.options import (
    WORD,
    DatabaseOption,
    InvalidOptionError,
    RegistryOption,
    _count,
    _database,
    _lock_path,
    _parse_enum,
    _print_failure,
    _refuse,
    _refusing,
    _require_enum,
    _warn_unread_boards,
    app,
    run_async,
)

if TYPE_CHECKING:
    from rich.console import Console

    from stage.companies import PersonalBoards
    from stage.domain import Company, DiscoveryEvent, PlatformCandidate, ProbeResult
    from stage.services.boards import PruneReport

    ProgressCallback = Callable[[DiscoveryEvent], None]


@app.command(
    help="Find the job-board platform a company uses", rich_help_panel="Registry and maintenance"
)
def discover(
    companies: Annotated[
        list[str] | None,
        typer.Argument(metavar="NAME...", click_type=WORD, help="Company names to probe"),
    ] = None,
    url: Annotated[
        str | None,
        typer.Option(
            "--url",
            metavar="URL",
            help="Extract the platform from a careers URL without fetching that URL",
        ),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option("--name", metavar="NAME", help="Company name to show with --url"),
    ] = None,
    platform: Annotated[
        list[str] | None,
        typer.Option(
            "--platform", metavar="PLATFORM", help="Probe only these platform adapters (repeatable)"
        ),
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option(
            "--exclude",
            metavar="NAME",
            help="Skip these platform adapters while probing (repeatable)",
        ),
    ] = None,
    only: Annotated[
        list[str] | None,
        typer.Option(
            "--company",
            metavar="NAME",
            help="With --verify, recheck only these registry companies (repeatable)",
        ),
    ] = None,
    expect_size: Annotated[
        str | None,
        typer.Option(
            "--expect-size",
            metavar="SIZE",
            help="Optional employer-size hint: startup, mid, large",
        ),
    ] = None,
    request_log: Annotated[
        Path | None,
        typer.Option(
            "--request-log",
            metavar="FILE",
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
            metavar="N",
            click_type=_count(1, 2000),
            help="Maximum unregistered company names to probe",
        ),
    ] = 40,
    show_all: Annotated[
        bool,
        typer.Option("--all", help="List every result instead of the first few"),
    ] = False,
    db: DatabaseOption = None,
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help=(
                "Write results to your board list, or to --registry when given: new boards "
                "from a name or --url, or what --verify and --unregistered found"
            ),
        ),
    ] = False,
    registry: RegistryOption = None,
) -> None:
    from contextlib import ExitStack
    from shlex import quote

    from stage.cli.logfile import open_request_log
    from stage.cli.render import plain, terminal
    from stage.cli.render.progress import board_text, registered_state, render_discovery
    from stage.cli.schedule_state import ScheduleStateWriter
    from stage.companies import RegistryError, load_boards, load_personal, personal_kept
    from stage.companies import load_companies as load_registry
    from stage.domain import (
        DiscoveryEvent,
        DiscoveryFinished,
        EmployerSize,
        Platform,
        PlatformProbed,
        ProbeResult,
        ProbeVerdict,
        UrlResolved,
    )
    from stage.domain.text import escape_markup
    from stage.paths import boards_path, registry_override
    from stage.runlock import AnotherRunInProgressError, single_run
    from stage.services.boards import ANSWERED
    from stage.services.discover import (
        NoMatchingCompanyError,
        discovered_row,
        registered_as,
        registered_board,
        unaddable,
    )

    console = terminal()
    scheduled_state = (
        ScheduleStateWriter.open(scheduled_progress, "discover")
        if scheduled_progress is not None
        else None
    )
    progress = scheduled_state.discovery_event if scheduled_state is not None else None

    if url is not None and not url.strip():
        _refuse(
            "[red]--url needs a careers page link[/red], such as "
            "[bold]--url https://jobs.lever.co/acme[/bold]."
        )
    if name is not None and not name.strip():
        _refuse("[red]--name needs the employer's name[/red], such as [bold]--name Acme[/bold].")
    if any(not company.strip() for company in companies or []):
        _refuse(
            "[red]Each company name needs at least one letter[/red], such as "
            "[bold]stage discover Acme[/bold]."
        )
    if url is not None and companies:
        _refuse("[red]Pass either company names or --url, not both.[/red]")
    if verify and unregistered:
        _refuse("[red]Pass either --verify or --unregistered, not both.[/red]")
    if url is not None and (verify or unregistered):
        flag = "--verify" if verify else "--unregistered"
        _refuse(f"[red]Pass either --url or {flag}, not both.[/red]")
    if direct_only and not unregistered:
        _refuse("[red]--direct-only requires --unregistered.[/red]")
    if adopt_unnamed and not direct_only:
        _refuse(
            "[red]--adopt-unnamed requires --direct-only[/red], since the token must come from "
            "an apply URL."
        )
    if only and not verify:
        _refuse(
            "[red]--company picks the registry rows that --verify rechecks.[/red] Add "
            "[bold]--verify[/bold], or pass the employer as a name: "
            "[bold]stage discover NAME[/bold]."
        )
    if only and any(not value.strip() for value in only):
        _refuse("[red]--company needs a value. Leave the flag out to recheck every row.[/red]")
    if apply and url is not None and name is None:
        _refuse(
            "[red]--apply with --url needs --name[/red], the employer's name. The slug in a "
            "link is a board token."
        )
    if platform and exclude:
        _refuse("[red]Pass either --platform or --exclude, not both.[/red]")
    if not verify and not unregistered and url is None and not companies:
        _refuse(
            "[red]Nothing to discover.[/red] Pass a company name, a careers page with "
            "[bold]--url[/bold], or [bold]--verify[/bold] to re-probe the registry."
        )

    with _refusing(InvalidOptionError):
        size = _parse_enum(expect_size, EmployerSize, "--expect-size")
        platforms = (
            [_require_enum(value, Platform, "--platform") for value in platform]
            if platform
            else None
        )
        excluded = (
            [_require_enum(value, Platform, "--exclude") for value in exclude] if exclude else None
        )

    async def run() -> object:
        from datetime import UTC, datetime

        from stage.domain.text import counted
        from stage.services.discover import probe_companies, resolve_careers_url

        today = datetime.now(UTC).date()
        with ExitStack() as stack:
            stream = (
                stack.enter_context(open_request_log(request_log))
                if request_log is not None
                else None
            )
            if verify:
                from stage.companies import update_registry
                from stage.services.discover import apply_verification, verify_registry

                results: list[ProbeResult] = []

                def collect(event: DiscoveryEvent) -> None:
                    if progress is not None:
                        progress(event)
                    if isinstance(event, PlatformProbed):
                        results.append(event.result)

                outcome = await render_discovery(
                    console,
                    verify_registry(
                        checked,
                        platforms=platforms,
                        excluded=excluded,
                        only=only,
                        board_checks=personal is not None,
                    ),
                    request_log=stream,
                    verifying=True,
                    progress=collect,
                )
                if apply and isinstance(outcome, DiscoveryFinished) and personal is not None:
                    await _prune_personal(console, personal, packaged, results, today=today, db=db)
                elif apply and isinstance(outcome, DiscoveryFinished):

                    def update(
                        existing: tuple[Any, ...],
                    ) -> tuple[tuple[Any, ...], Any]:
                        return apply_verification(existing, results, today)

                    target, report = update_registry(update, explicit)
                    console.print(
                        plain(
                            f"\napplied: {counted(report.verified, 'row')} verified, "
                            f"{report.disabled} disabled, written to {target}"
                        ),
                        soft_wrap=True,
                    )
                    if report.held:
                        console.print(
                            plain(
                                "Matched but left switched off, since their notes give another "
                                f"reason: {_names(report.held)}"
                            ),
                            soft_wrap=True,
                        )
                return isinstance(outcome, DiscoveryFinished) and any(
                    result.verdict in ANSWERED for result in results
                )
            if url is not None:

                async def once() -> AsyncIterator[DiscoveryEvent]:
                    yield resolve_careers_url(url)

                found: list[PlatformCandidate] = []

                def note(event: DiscoveryEvent) -> None:
                    if progress is not None:
                        progress(event)
                    if isinstance(event, UrlResolved):
                        found.append(event.candidate)

                resolved = await render_discovery(
                    console,
                    once(),
                    display_name=name,
                    progress=note,
                    known=known,
                    destination=destination,
                )
                rows: list[Company] = []
                for candidate in found:
                    reason = unaddable(candidate)
                    if reason:
                        console.print(plain(f"  Not addable: {reason}.", "yellow"))
                        resolved = resolved and not apply
                    elif registered_board(known, candidate) is None:
                        rows.append(discovered_row(name or candidate.slug, candidate, today))
                if name is not None:
                    await _offer(console, rows, apply=apply, explicit=explicit, packaged=packaged)
                return bool(resolved)

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
                    show_all=show_all,
                )
                if scheduled_state is not None:
                    scheduled_state.heartbeat()
                return adopted

            wanted: list[str] = []
            for asked in companies or []:
                rows_for = registered_as(known, asked)
                if not rows_for:
                    wanted.append(asked)
                    continue
                boards = ", ".join(
                    f"{board_text(row)} ({registered_state(row)})" for row in rows_for
                )
                console.print(
                    plain(
                        f"{asked} is already registered as {boards}. Recheck it with: "
                        f"stage discover --verify --company {quote(rows_for[0].name)}"
                    ),
                    soft_wrap=True,
                )
            if not wanted:
                return True
            matched: list[ProbeResult] = []

            def keep(event: DiscoveryEvent) -> None:
                if progress is not None:
                    progress(event)
                if isinstance(event, PlatformProbed) and event.result.verdict is ProbeVerdict.MATCH:
                    matched.append(event.result)

            events = probe_companies(wanted, platforms=platforms, excluded=excluded, size=size)
            resolved = await render_discovery(
                console, events, request_log=stream, progress=keep, known=known
            )
            best: dict[str, ProbeResult] = {}
            for result in matched:
                if registered_board(known, result.candidate) is not None:
                    continue
                held = best.get(result.company)
                if held is None or (result.job_count or 0) > (held.job_count or 0):
                    best[result.company] = result
            await _offer(
                console,
                [
                    discovered_row(result.company, result.candidate, today, result.board_name)
                    for result in best.values()
                ],
                apply=apply,
                explicit=explicit,
                packaged=packaged,
            )
            return bool(resolved)

    explicit = registry_override(registry)
    packaged: tuple[Any, ...] = ()
    known: tuple[Any, ...] = ()
    checked: list[Any] = []
    personal = None
    destination = str(explicit) if explicit is not None else f"your board list at {boards_path()}"
    if not verify and not unregistered:
        with _refusing(RegistryError):
            known, listed = load_boards(explicit)
            packaged = load_registry(explicit) if apply else ()
        if listed.problem and apply:
            _refuse(
                "[red]Your board list could not be read, so nothing was changed.[/red] "
                f"{escape_markup(listed.problem)}"
            )
        _warn_unread_boards(listed)
    if verify:
        from stage.sources.platforms import BOARD_PROBES

        with _refusing(RegistryError):
            packaged = load_registry(explicit)
        checked = list(packaged)
        if explicit is None:
            personal = load_personal()
            if personal.problem and apply:
                _refuse(
                    f"[red]Your board list could not be read, so nothing was changed.[/red] "
                    f"{escape_markup(personal.problem)}"
                )
            _warn_unread_boards(personal)
            checked = [
                *(row for row in packaged if row.platform not in BOARD_PROBES),
                *personal_kept(packaged, personal.rows),
            ]

    try:
        if url is not None:
            resolved = run_async(run())
        else:
            with single_run("discover", _lock_path(db)):
                resolved = run_async(run())
    except AnotherRunInProgressError as exc:
        if scheduled_state is not None:
            scheduled_state.blocked(str(exc))
        _print_failure(exc, soft_wrap=True)
        raise typer.Exit(code=2) from exc
    except (RegistryError, NoMatchingCompanyError) as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc
    raise typer.Exit(code=0 if resolved or unregistered else 1)


def _names(names: Sequence[str], shown: int = 5) -> str:
    listed = ", ".join(names[:shown])
    return f"{listed} and {len(names) - shown} more" if len(names) > shown else listed


async def _offer(
    console: "Console",
    rows: "Sequence[Company]",
    *,
    apply: bool,
    explicit: Path | None,
    packaged: "Sequence[Company]",
) -> None:
    from stage.cli.render import plain
    from stage.companies import (
        PERSONAL_LIMIT,
        board_identity,
        board_label,
        registry_entry_yaml,
        update_personal,
        update_registry,
    )
    from stage.paths import boards_path
    from stage.services.boards import room_left
    from stage.services.discover import read_robots

    if not rows:
        return
    where = str(explicit) if explicit is not None else f"your board list at {boards_path()}"
    if not apply:
        console.print(plain(f"\n--apply adds this to {where}:"), soft_wrap=True)
        for row in rows:
            for line in registry_entry_yaml(row).splitlines():
                console.print(plain(f"  {line}"), highlight=False, soft_wrap=True)
        return
    rows, skipped = (await read_robots(rows)).screen(rows)
    for row, reason in skipped:
        console.print(
            plain(f"\n{row.name} ({board_label(row)}) was not added: {reason}.", "yellow"),
            soft_wrap=True,
        )
    if not rows:
        return

    def fresh(existing: "Sequence[Company]") -> "list[Company]":
        taken = {board_identity(row) for row in existing}
        return [row for row in rows if board_identity(row) not in taken]

    def into_registry(
        existing: "tuple[Company, ...]",
    ) -> "tuple[list[Company], tuple[list[Company], int]]":
        added = fresh(existing)
        return [*existing, *added], (added, 0)

    def into_list(
        existing: "tuple[Company, ...]",
    ) -> "tuple[list[Company], tuple[list[Company], int]]":
        new = fresh((*packaged, *existing))
        added = new[: room_left(existing, packaged)]
        return [*existing, *added], (added, len(new) - len(added))

    if explicit is not None:
        target, (added, left_out) = update_registry(into_registry, explicit)
        where = str(target)
    else:
        target, (added, left_out) = update_personal(into_list)
        where = f"your board list at {target}"
    if added:
        console.print(
            plain(
                f"\nAdded {_names([row.name for row in added])} to {where}. "
                "stage sync reads it from now on."
            ),
            soft_wrap=True,
        )
    if left_out:
        console.print(
            plain(
                f"Your board list is full at {PERSONAL_LIMIT} boards, so {left_out:,} more "
                f"were left out. Remove some from {target} first."
            ),
            soft_wrap=True,
        )
    elif not added:
        console.print(plain(f"\n{where} already has it, so nothing changed."), soft_wrap=True)


async def _adopt_unregistered(
    console: "Console",
    *,
    registry: Path | None,
    db: Path | None,
    platforms: list[Any] | None,
    excluded: list[Any] | None,
    size: Any,
    limit: int,
    direct_only: bool,
    adopt_unnamed: bool,
    apply_rows: bool,
    today: date,
    stream: TextIO | None,
    progress: "ProgressCallback | None",
    show_all: bool = False,
) -> bool:
    from stage.cli.logfile import open_probe_journal, probe_journal_path
    from stage.cli.render import more, plain
    from stage.cli.render.progress import write_request_log
    from stage.companies import (
        PERSONAL_LIMIT,
        RegistryError,
        load_companies,
        load_personal,
        personal_kept,
        update_personal,
        update_registry,
    )
    from stage.domain import PlatformProbed, RequestLogged
    from stage.domain.text import counted
    from stage.paths import registry_override
    from stage.services.boards import room_left
    from stage.services.coverage import coverage
    from stage.services.discover import (
        adopt_unregistered,
        plan_adoption,
        probe_companies,
        select_unregistered,
        verify_registry,
    )
    from stage.storage import open_repository

    explicit = registry_override(registry)
    packaged = load_companies(explicit)
    personal = None if explicit is not None else load_personal(today=today)
    rows = packaged if personal is None else (*packaged, *personal_kept(packaged, personal.rows))
    if personal is not None and personal.problem:
        if apply_rows:
            raise RegistryError(
                f"your board list could not be read, so nothing was added and the file was left "
                f"as it is: {personal.problem}"
            )
        _warn_unread_boards(personal)
    if personal is not None and apply_rows and not room_left(personal.rows, packaged):
        console.print(
            plain(
                f"Your board list is full at {PERSONAL_LIMIT} boards, so nothing more is added. "
                f"Remove some from {personal.path} first."
            ),
            soft_wrap=True,
        )
        return False
    async with open_repository(_database(db)) as repository:
        report = await coverage(repository, rows, unregistered=True)
        ranked = [
            entry.company for entry in report.unregistered if entry.postings or not apply_rows
        ]
        apply_urls = await repository.company_apply_urls(ranked, kept_only=apply_rows)
    direct_rows, name_rows = select_unregistered(
        ranked,
        apply_urls,
        limit=limit,
        direct_only=direct_only,
        platforms=platforms,
        excluded=excluded,
    )
    direct = list(direct_rows)
    names = list(name_rows)
    if not names and not direct:
        console.print(plain("No unregistered employers to probe. Run stage sync first."))
        return False

    total = len(names) + len(direct)
    console.print(
        plain(
            f"Probing {counted(total, 'unregistered employer')}, "
            f"{len(direct)} from direct feed links."
        )
    )
    results: list[tuple[str, Any]] = []
    streams = []
    if direct:
        streams.append(
            verify_registry(direct, platforms=platforms, excluded=excluded, board_checks=True)
        )
    if names:
        streams.append(probe_companies(names, platforms=platforms, excluded=excluded, size=size))
    with open_probe_journal() as journal:
        for events in streams:
            async for event in events:
                if progress is not None:
                    progress(event)
                if isinstance(event, RequestLogged):
                    write_request_log(stream, event)
                if isinstance(event, PlatformProbed):
                    results.append((event.result.company, event.result))
                    journal(
                        {
                            "company": event.result.company,
                            "platform": event.result.candidate.platform.value,
                            "slug": event.result.candidate.slug,
                            "verdict": event.result.verdict.value,
                            "job_count": event.result.job_count,
                            "board_name": event.result.board_name,
                            "detail": event.result.detail,
                        }
                    )

    outcome, robots = await plan_adoption(
        rows, results, today=today, adopt_unnamed=adopt_unnamed, check_robots=apply_rows
    )
    console.print(
        plain(
            f"{outcome.probed} probed: {len(outcome.adopted)} adoptable "
            f"({counted(outcome.postings, 'posting')}), {len(outcome.review)} needing review, "
            f"{len(outcome.refused)} refused, {outcome.already_known} already known"
        )
    )
    adopted = outcome.adopted if show_all else outcome.adopted[:20]
    for row in adopted:
        console.print(plain(f"  + {row.company.name}: {counted(row.job_count, 'job')}"))
    more(console, len(outcome.adopted) - len(adopted))
    if outcome.review:
        console.print(
            plain("Boards with postings whose platform publishes no name. Decide these by hand:")
        )
        review = outcome.review if show_all else outcome.review[:40]
        for candidate in review:
            mark = "slug is distinctive" if candidate.distinctive else "slug is generic, check it"
            console.print(
                plain(
                    f"  ? {candidate.company} ({candidate.label}): "
                    f"{counted(candidate.job_count, 'job')}, {mark}"
                )
            )
        more(console, len(outcome.review) - len(review))
    refused = outcome.refused if show_all else outcome.refused[:10]
    for company, board, reason in refused:
        console.print(plain(f"  - {company} ({board}): {reason}"))
    more(console, len(outcome.refused) - len(refused))
    console.print(
        plain(f"Every probe result was journalled to {probe_journal_path()}"), soft_wrap=True
    )

    if not outcome.adopted:
        return False
    if not apply_rows:
        console.print(plain("Nothing written. Re-run with --apply to add these rows."))
        return True

    if personal is None:

        def update(existing: tuple[Any, ...]) -> tuple[list[Any], Any]:
            latest = adopt_unregistered(
                existing, results, today=today, adopt_unnamed=adopt_unnamed, robots=robots
            )
            return [*existing, *(row.company for row in latest.adopted)], latest

        target, applied = update_registry(update, explicit)
        console.print(
            plain(f"applied: {counted(len(applied.adopted), 'row')} added, written to {target}"),
            soft_wrap=True,
        )
        return True

    def adopt(existing: tuple[Any, ...]) -> tuple[list[Any], tuple[int, int]]:
        latest = adopt_unregistered(
            (*packaged, *existing),
            results,
            today=today,
            adopt_unnamed=adopt_unnamed,
            robots=robots,
        )
        taken = latest.adopted[: room_left(existing, packaged)]
        added = [row.company for row in taken]
        return [*existing, *added], (len(added), len(latest.adopted) - len(added))

    target, (added, left_out) = update_personal(adopt)
    console.print(
        plain(f"applied: {counted(added, 'board')} added to your board list at {target}"),
        soft_wrap=True,
    )
    if left_out:
        console.print(
            plain(
                f"Your board list is full at {PERSONAL_LIMIT} boards, so {left_out:,} more "
                "were left out."
            )
        )
    return True


async def _prune_personal(
    console: "Console",
    personal: "PersonalBoards",
    packaged: "Sequence[Company]",
    results: "Sequence[ProbeResult]",
    *,
    today: date,
    db: Path | None,
) -> None:
    from stage.cli.render import plain
    from stage.companies import personal_kept, update_personal
    from stage.domain.text import counted
    from stage.services.boards import BARREN_AFTER_DAYS, productive_boards, prune_personal
    from stage.services.discover import is_gone
    from stage.storage import open_repository

    shipped = {(row.name, row.platform, row.slug.lower()) for row in packaged}
    gone = sum(
        1
        for result in results
        if is_gone(result)
        and (result.company, result.candidate.platform, result.candidate.slug.lower()) in shipped
    )
    if gone:
        console.print(
            plain(
                f"\n{counted(gone, 'shipped board')} answered as gone. Stage does not rewrite "
                "its own list, so pass --registry PATH to change it."
            )
        )
    if not personal.rows:
        console.print(plain("No boards of your own to update."))
        return
    async with open_repository(_database(db)) as repository:
        productive = await productive_boards(repository, personal.rows)

    def prune(existing: "tuple[Company, ...]") -> "tuple[tuple[Company, ...], PruneReport]":
        own = personal_kept(packaged, existing)
        held = tuple(row for row in existing if row not in own)
        kept, report = prune_personal(own, results, today=today, productive=productive)
        return (*held, *kept), report

    target, report = update_personal(prune)
    console.print(
        plain(
            f"\napplied: {counted(report.verified, 'board')} verified, "
            f"{len(report.switched_off):,} switched off, {len(report.back_on):,} back on, "
            f"{len(report.removed) + len(report.barren):,} removed, written to {target}"
        ),
        soft_wrap=True,
    )
    for name in report.switched_off:
        console.print(plain(f"  - {name}: gone, removed if still gone next week"))
    for name in report.back_on:
        console.print(plain(f"  + {name}: answering again"))
    for name in report.removed:
        console.print(plain(f"  x {name}: gone two weeks running"))
    for name in report.barren:
        console.print(plain(f"  x {name}: no internship in {BARREN_AFTER_DAYS} days"))
