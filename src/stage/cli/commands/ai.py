import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from stage.cli.options import DatabaseOption, _database, app, run_async

if TYPE_CHECKING:
    from rich.console import Console

    from stage.services.connect import Outcome, ServerEntry, Target

PANEL = "AI apps"
STARTED_BY_HAND = (
    "stage mcp is started by an AI app, which talks to it through this terminal's input and "
    "output. Press Ctrl+C to stop."
)
NOTHING_FOUND = (
    "Stage found no AI app it can connect to on this computer. Install one, or run "
    "stage connect --status --every to see every app Stage knows."
)

AppsArgument = Annotated[
    list[str] | None,
    typer.Argument(
        metavar="APP...",
        help="Apps to change, such as claude-code, cursor or cline; leave out to choose",
        show_default=False,
    ),
]
EveryOption = Annotated[
    bool, typer.Option("--all", help="Every AI app Stage found on this computer")
]
DryRunOption = Annotated[
    bool, typer.Option("--dry-run", help="Show what would change without changing anything")
]


@app.command(
    help="Let AI apps search postings and keep your tracker over MCP", rich_help_panel=PANEL
)
def mcp(db: DatabaseOption = None) -> None:
    from stage.ai.server import serve

    if sys.stdin.isatty():
        typer.echo(STARTED_BY_HAND, err=True)
    try:
        run_async(serve(_database(db)))
    except KeyboardInterrupt:
        raise typer.Exit(code=130) from None


@app.command(help="Connect Stage to the AI apps on this computer", rich_help_panel=PANEL)
def connect(
    apps: AppsArgument = None,
    every: EveryOption = False,
    status: Annotated[
        bool, typer.Option("--status", help="Show which apps are connected and change nothing")
    ] = False,
    known: Annotated[
        bool, typer.Option("--every", help="With --status, list every app Stage knows")
    ] = False,
    dry_run: DryRunOption = False,
    db: DatabaseOption = None,
) -> None:
    from stage.cli.render import plain, terminal
    from stage.services.connect import Machine, server_entry, targets

    console = terminal()
    if known and not status:
        console.print(
            plain("--every lists apps with --status; it does not connect anything.", "red")
        )
        raise typer.Exit(code=2)
    entry = server_entry(database=db)
    machine = Machine.here()
    found = targets(machine, every=known and status)
    if status:
        _show_status(console, found, entry)
        return
    chosen = _choose(console, found, apps, every, entry, verb="connect")
    outcomes = [_connect_one(target, entry, dry_run) for target in chosen]
    _report(console, outcomes)
    if not dry_run and any(outcome.changed for outcome in outcomes):
        _self_test(console, entry)
    if any(outcome.status.value == "unreadable" for outcome in outcomes):
        raise typer.Exit(code=1)


@app.command(help="Remove Stage from AI apps", rich_help_panel=PANEL)
def disconnect(
    apps: AppsArgument = None,
    every: EveryOption = False,
    dry_run: DryRunOption = False,
    db: DatabaseOption = None,
) -> None:
    from stage.cli.render import terminal
    from stage.services.connect import Machine, server_entry, targets
    from stage.services.connect import disconnect as remove

    console = terminal()
    entry = server_entry(database=db)
    found = targets(Machine.here())
    chosen = _choose(console, found, apps, every, entry, verb="disconnect")
    outcomes = [remove(target, entry, dry_run=dry_run) for target in chosen]
    _report(console, outcomes)
    if any(outcome.status.value == "unreadable" for outcome in outcomes):
        raise typer.Exit(code=1)


def _connect_one(target: "Target", entry: "ServerEntry", dry_run: bool) -> "Outcome":
    from stage.services.connect import connect as attach

    outcome = attach(target, entry, dry_run=dry_run)
    if outcome.link and not dry_run:
        import webbrowser

        webbrowser.open(outcome.link)
    return outcome


def _choose(
    console: "Console",
    found: "tuple[Target, ...]",
    apps: list[str] | None,
    every: bool,
    entry: "ServerEntry",
    *,
    verb: str,
) -> "list[Target]":
    from stage.cli.render import plain

    if not found:
        console.print(plain(NOTHING_FOUND, "yellow"))
        raise typer.Exit(code=1)
    if every:
        return list(found)
    if apps:
        return _named(console, found, apps)
    _show_status(console, found, entry)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        console.print(
            plain(
                f"Name the apps to {verb}, such as stage {verb} {found[0].app.key}, or use --all.",
            )
        )
        raise typer.Exit(code=2)
    answer = typer.prompt(
        f"\nWhich apps should Stage {verb}? Numbers like 1 3, all, or Enter to cancel",
        default="",
        show_default=False,
    )
    return _picked(console, found, answer)


def _picked(console: "Console", found: "tuple[Target, ...]", answer: str) -> "list[Target]":
    from stage.cli.render import plain

    words = answer.replace(",", " ").split()
    if not words:
        raise typer.Exit(code=0)
    if [word.lower() for word in words] == ["all"]:
        return list(found)
    chosen: list[Target] = []
    for word in words:
        if not word.isdecimal() or not 1 <= int(word) <= len(found):
            console.print(plain(f"{word} is not one of the numbers above.", "red"))
            raise typer.Exit(code=2)
        target = found[int(word) - 1]
        if target not in chosen:
            chosen.append(target)
    return chosen


def _named(console: "Console", found: "tuple[Target, ...]", apps: list[str]) -> "list[Target]":
    from difflib import get_close_matches

    from stage.cli.render import plain
    from stage.services.ai_catalogue import APPS

    chosen: list[Target] = []
    for wanted in apps:
        name = _folded(wanted)
        matches = [target for target in found if name in _names(target)]
        if not matches:
            everything = {key for app in APPS for key in (app.key, _folded(app.name))}
            if name in everything:
                console.print(plain(f"Stage did not find {wanted} on this computer.", "red"))
            else:
                near = get_close_matches(name, sorted(everything), n=1)
                hint = f" Did you mean {near[0]}?" if near else ""
                console.print(plain(f"Stage does not know an app called {wanted}.{hint}", "red"))
            raise typer.Exit(code=2)
        chosen.extend(target for target in matches if target not in chosen)
    return chosen


def _names(target: "Target") -> set[str]:
    key = target.app.key
    names = {key, key.removesuffix("-legacy"), _folded(target.app.name)}
    names.update(_folded(part) for part in target.label.split(", "))
    if target.editor:
        names.add(f"{key.removesuffix('-legacy')}-{target.editor}")
    return names


def _folded(text: str) -> str:
    return "-".join(text.lower().replace("_", " ").split())


def _show_status(console: "Console", found: "tuple[Target, ...]", entry: "ServerEntry") -> None:
    from rich.table import Table

    from stage.cli.render import clipped, plain
    from stage.services.connect import read_status

    if not found:
        console.print(plain(NOTHING_FOUND, "yellow"))
        return
    table = Table(box=None, pad_edge=False, show_header=True, header_style="bold")
    table.add_column("#", justify="right", no_wrap=True)
    table.add_column("App", no_wrap=True)
    table.add_column("Stage", no_wrap=True)
    table.add_column("How", overflow="fold")
    for number, target in enumerate(found, start=1):
        reading = read_status(target, entry)
        table.add_row(
            str(number),
            clipped(target.label, 40),
            plain(_state(reading.status.value), _colour(reading.status.value)),
            plain(_how(target)),
        )
    console.print(table)


def _state(status: str) -> str:
    return "check in the app" if status == "unknown" else status


def _colour(status: str) -> str:
    return {
        "connected": "green",
        "outdated": "yellow",
        "broken": "red",
        "unreadable": "red",
    }.get(status, "")


def _how(target: "Target") -> str:
    reach = target.reach.value
    if reach == "command" and target.program:
        return f"runs {Path(target.program).name} mcp"
    if reach == "file" and target.settings is not None:
        return f"edits {_home(target.settings)}"
    if reach == "link":
        return "opens its install link"
    return "shows the steps"


def _home(path: Path) -> str:
    return _tilde(str(path))


def _tilde(text: str) -> str:
    home = str(Path.home())
    if len(home) < 2:
        return text
    return text.replace(home + "/", "~/").replace(home + "\\", "~\\")


def _report(console: "Console", outcomes: "list[Outcome]") -> None:
    from stage.cli.render import plain

    for outcome in outcomes:
        app = outcome.target.app
        style = (
            "green" if outcome.changed else ("red" if outcome.status.value == "unreadable" else "")
        )
        message = outcome.message
        if not message.startswith(("ran ", "would run ")):
            message = _tilde(message)
        console.print(plain(f"{outcome.target.label}: {message}", style))
        if outcome.backup is not None:
            console.print(plain(f"  The file as it was is kept at {_home(outcome.backup)}", "dim"))
        if outcome.changed and app.note:
            console.print(plain(f"  {app.note}", "dim"))
        if outcome.link:
            console.print(plain(f"  {outcome.link}", "dim"), soft_wrap=True)
        if outcome.instructions:
            for line in outcome.instructions.splitlines():
                console.print(plain(f"  {line}"), soft_wrap=True)


def _self_test(console: "Console", entry: "ServerEntry") -> None:
    from stage.cli.render import plain
    from stage.services.connect import self_test

    result = run_async(self_test(entry))
    style = "green" if result.passed else "red"
    console.print(plain(f"Self-test: {result.detail}", style))
    if not result.passed:
        raise typer.Exit(code=1)
