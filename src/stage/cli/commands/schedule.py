from typing import TYPE_CHECKING, Annotated

import typer

from stage.cli.options import (
    JsonOption,
    _print_failure,
    _render_schedule,
    schedule_app,
)

if TYPE_CHECKING:
    pass


@schedule_app.command(
    "enable", help="Create this user's six-hourly sync and weekly discovery schedule"
)
def schedule_enable() -> None:
    from rich.console import Console

    from stage.cli.schedule import ScheduleError, enable

    try:
        report = enable()
    except ScheduleError as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc
    _render_schedule(Console(), report)


@schedule_app.command("status", help="Show automatic scheduling and scheduled-run progress")
def schedule_status(
    watch: Annotated[
        bool,
        typer.Option("--watch", help="Refresh while a scheduled run is active"),
    ] = False,
    as_json: JsonOption = False,
) -> None:
    import time

    from rich.console import Console

    from stage.cli.schedule import ScheduleError, status
    from stage.cli.schedule_state import HEARTBEAT_INTERVAL_SECONDS, is_active
    from stage.cli.serialize import emit, schedule_to_json

    if watch and as_json:
        _print_failure(ValueError("--watch and --json cannot be used together"))
        raise typer.Exit(code=2)
    console = Console()
    try:
        while True:
            report = status()
            if as_json:
                emit(schedule_to_json(report))
            else:
                if watch:
                    console.clear()
                _render_schedule(console, report)
            if not watch or not any(is_active(state) for state in report.states):
                return
            time.sleep(HEARTBEAT_INTERVAL_SECONDS)
    except ScheduleError as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc


@schedule_app.command("disable", help="Remove this user's automatic schedule")
def schedule_disable() -> None:
    from rich.console import Console

    from stage.cli.schedule import ScheduleError, disable

    try:
        report = disable()
    except ScheduleError as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc
    _render_schedule(Console(), report)
