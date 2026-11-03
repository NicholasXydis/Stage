from datetime import UTC, datetime
from typing import Annotated

import typer

from stage.cli.options import (
    WORD,
    JsonOption,
    _refuse,
    _refusing,
    schedule_app,
)


@schedule_app.command(
    "enable", help="Create this user's half-hourly sync and weekly discovery schedule"
)
def schedule_enable(
    action: Annotated[
        list[str] | None,
        typer.Option(
            "--action",
            metavar="ACTION",
            help="Schedule only these actions: sync, discover, verify (repeatable)",
        ),
    ] = None,
    every: Annotated[
        str | None,
        typer.Option(
            "--every",
            metavar="TIME",
            help="How often the sync runs: 30m (default), 1h, 2h, 3h, 4h, 6h, 8h or 12h",
        ),
    ] = None,
) -> None:
    from stage.cli.render import terminal
    from stage.cli.render.schedule import render_schedule
    from stage.cli.schedule import ScheduleError, enable, parse_every

    with _refusing(ScheduleError):
        report = enable(action, None if every is None else parse_every(every))
    render_schedule(terminal(), report)


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
    from stage.cli.render.schedule import render_schedule
    from stage.cli.schedule import ScheduleError, status
    from stage.cli.schedule_state import HEARTBEAT_INTERVAL_SECONDS, is_active
    from stage.cli.serialize import emit, schedule_to_json

    if watch and as_json:
        _refuse("[red]Pass either --watch or --json, not both.[/red]")
    console = terminal()
    with _refusing(ScheduleError):
        while True:
            report = status()
            if as_json:
                emit(schedule_to_json(report))
            else:
                if watch:
                    console.clear()
                render_schedule(console, report)
            if not watch or not any(is_active(state) for state in report.states):
                return
            time.sleep(HEARTBEAT_INTERVAL_SECONDS)


@schedule_app.command("disable", help="Remove this user's automatic schedule")
def schedule_disable(
    action: Annotated[
        list[str] | None,
        typer.Option(
            "--action",
            metavar="ACTION",
            help="Remove only these actions: sync, discover, verify (repeatable)",
        ),
    ] = None,
) -> None:
    from stage.cli.render import terminal
    from stage.cli.render.schedule import render_schedule
    from stage.cli.schedule import ScheduleError, disable

    with _refusing(ScheduleError):
        report = disable(action)
    render_schedule(terminal(), report)


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
    if clear and webhook is not None:
        _refuse("[red]Pass a webhook URL or --clear, not both.[/red]")
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
    with _refusing(notify.NotifyError):
        notify.remember(webhook)
    notify.mark_announced(datetime.now(UTC))
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
        _refuse(
            "[yellow]No Discord webhook stored.[/yellow] Run "
            "[bold]stage schedule notify URL[/bold] first."
        )
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
    with _refusing(notify.NotifyError, code=1):
        notify.post(stored, payload)
    console.print(plain(f"Sent to {notify.redact(stored)}"))
