from datetime import datetime
from typing import TYPE_CHECKING

from rich.console import Console

from stage.cli.render import error_cell, plain, summary

if TYPE_CHECKING:
    from stage.cli.schedule import ScheduleStatus

_FINISHED = {
    "success": ("completed successfully", "green"),
    "partial": ("completed with attention", "yellow"),
    "failure": ("failed", "red"),
}
_WORKING = {
    "rechecking": "checking stored postings",
    "syncing": "syncing",
    "discovering": "discovering",
    "checking": "checking health",
}


def render_schedule(console: Console, report: "ScheduleStatus") -> None:
    console.print(f"Scheduler: {report.backend} (per user)")
    for index, (action, enabled, installed) in enumerate(report.actions):
        state = "enabled" if enabled else "disabled"
        console.print(f"  {action.label}: {state}, {action.cadence} {action.when} local time")
        needs_update = index < len(report.needs_update) and report.needs_update[index]
        if enabled and (needs_update or not action.cadence.startswith(installed)):
            console.print(
                "    [yellow]needs a scheduler update (run stage schedule enable)[/yellow]"
            )
        stale = report.stale_interpreter[index] if index < len(report.stale_interpreter) else ""
        if enabled and stale:
            console.print(
                f"    [red]the Python it runs is gone: {summary(stale, 200)}[/red]. Its runs fail "
                "until you run stage schedule enable again."
            )
        render_scheduled_run(console, report.states[index] if index < len(report.states) else None)
    if report.lingering is False and any(enabled for _, enabled, _ in report.actions):
        import getpass

        console.print(
            "[yellow]Linux stops these timers when you log out.[/yellow] To keep them "
            f"running, run: sudo loginctl enable-linger {getpass.getuser()}"
        )
    console.print(plain(f"Logs: {report.log_dir}"), soft_wrap=True)


def render_scheduled_run(console: Console, run: dict[str, object] | None) -> None:
    if run is None:
        return
    phase = run.get("phase")
    if phase == "finished":
        outcome = run.get("outcome")
        message, colour = _FINISHED.get(
            outcome if isinstance(outcome, str) else "", ("finished", "default")
        )
        console.print(f"    [{colour}]{message}[/{colour}] {_time(run.get('finished_at'))}")
    elif phase == "offline":
        console.print(
            f"    [yellow]skipped, no internet connection[/yellow] {_time(run.get('finished_at'))}"
        )
    elif phase == "stopped":
        console.print(
            "    [yellow]skipped, the computer stopped it before it started[/yellow] "
            f"{_time(run.get('finished_at'))}"
        )
    elif phase == "blocked":
        error = error_cell(str(run.get("error", "")), 200)
        console.print(f"    [yellow]another run holds the lock[/yellow]: {error}")
    elif phase == "unresponsive":
        console.print(
            "    [yellow]run is unresponsive[/yellow], "
            f"last heartbeat {_time(run.get('heartbeat_at'))}"
        )
    elif phase == "waiting":
        console.print(
            f"    [cyan]waiting to start[/cyan], expected {_time(run.get('starts_after'))}"
        )
    elif isinstance(phase, str) and phase in _WORKING:
        console.print(f"    [cyan]{_WORKING[phase]}[/cyan], started {_time(run.get('started_at'))}")
    else:
        return
    progress = run.get("progress")
    if isinstance(progress, dict) and phase not in {
        "blocked",
        "offline",
        "stopped",
        "unresponsive",
    }:
        complete = progress.get("complete", 0)
        total = progress.get("total", 0)
        if complete or total:
            where = (progress.get("source"), progress.get("company"))
            detail = " / ".join(summary(str(value), 80) for value in where if value)
            suffix = f": {detail}" if detail else ""
            console.print(
                f"      progress: {summary(str(complete), 12)}/{summary(str(total), 12)}{suffix}"
            )
    if phase == "finished" and isinstance(run.get("exit_code"), int):
        console.print(f"      exit code: {run['exit_code']}")


def _time(value: object) -> str:
    if not isinstance(value, str):
        return "unknown time"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    except ValueError:
        return summary(value, 40)
