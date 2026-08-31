from typing import Annotated

import typer

from stage.cli.options import (
    WORD,
    JsonOption,
    _print_failure,
    _render_schedule,
    schedule_app,
)


@schedule_app.command(
    "enable", help="Create this user's six-hourly sync and weekly discovery schedule"
)
def schedule_enable(
    action: Annotated[
        list[str] | None,
        typer.Option(
            "--action",
            help="Schedule only these actions: sync, discover, verify; repeatable",
        ),
    ] = None,
) -> None:
    from stage.cli.render import terminal
    from stage.cli.schedule import ScheduleError, enable

    try:
        report = enable(action)
    except ScheduleError as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc
    _render_schedule(terminal(), report)


@schedule_app.command("status", help="Show automatic scheduling and scheduled-run progress")
def schedule_status(
    watch: Annotated[
        bool,
        typer.Option("--watch", help="Refresh while a scheduled run is active"),
    ] = False,
    as_json: JsonOption = False,
) -> None:
    import time

    from stage.cli.render import terminal
    from stage.cli.schedule import ScheduleError, status
    from stage.cli.schedule_state import HEARTBEAT_INTERVAL_SECONDS, is_active
    from stage.cli.serialize import emit, schedule_to_json

    if watch and as_json:
        _print_failure(ValueError("--watch and --json cannot be used together"))
        raise typer.Exit(code=2)
    console = terminal()
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
def schedule_disable(
    action: Annotated[
        list[str] | None,
        typer.Option(
            "--action",
            help="Remove only these actions: sync, discover, verify; repeatable",
        ),
    ] = None,
) -> None:
    from stage.cli.render import terminal
    from stage.cli.schedule import ScheduleError, disable

    try:
        report = disable(action)
    except ScheduleError as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc
    _render_schedule(terminal(), report)


@schedule_app.command(
    "notify", help="Post new postings to a Discord channel after each scheduled sync"
)
def schedule_notify(
    webhook: Annotated[
        str | None,
        typer.Argument(
            metavar="URL",
            click_type=WORD,
            help="Discord webhook URL, or omit to show the one in use",
        ),
    ] = None,
    clear: Annotated[bool, typer.Option("--clear", help="Stop posting to Discord")] = False,
) -> None:
    from stage.cli import notify
    from stage.cli.render import plain, terminal

    console = terminal()
    if clear:
        notify.forget()
        console.print(plain("Discord notifications are off."))
        return
    if webhook is None:
        stored = notify.read()
        if not stored:
            console.print(
                "[dim]No Discord webhook stored. Create one in a channel you manage "
                "(Edit Channel, Integrations, New Webhook) then run "
                "[bold]stage schedule notify URL[/bold].[/dim]"
            )
            return
        console.print(plain(f"Posting to {notify.redact(stored)}"))
        return
    try:
        notify.remember(webhook)
    except notify.NotifyError as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc
    console.print(
        plain("Saved. New postings will appear in that channel after each scheduled sync.")
    )
    console.print(
        "[dim]Scheduled syncs only run while this machine is awake. A missed run catches "
        "up once afterwards. For notifications around the clock, install Stage on a "
        "machine that stays on and run this there.[/dim]"
    )


@schedule_app.command("test-notify", help="Send a test message to the stored Discord webhook")
def schedule_test_notify() -> None:
    from stage.cli import notify
    from stage.cli.render import plain, terminal

    console = terminal()
    stored = notify.read()
    if not stored:
        console.print(
            "[yellow]No Discord webhook stored.[/yellow] Run "
            "[bold]stage schedule notify URL[/bold] first."
        )
        raise typer.Exit(code=2)
    payload = notify.compose(
        [
            notify.Posting(
                company="Stage",
                title="Test message",
                location="your machine",
                url="https://github.com/NicholasXydis/Stage",
            )
        ],
        total=1,
    )
    try:
        notify.post(stored, payload)
    except notify.NotifyError as exc:
        _print_failure(exc)
        raise typer.Exit(code=1) from exc
    console.print(plain(f"Sent to {notify.redact(stored)}"))
