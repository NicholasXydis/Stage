import sys

import typer

from stage.cli.options import DatabaseOption, _database, app, run_async

PANEL = "AI apps"
STARTED_BY_HAND = (
    "stage mcp is started by an AI app, which talks to it through this terminal's input and "
    "output. Press Ctrl+C to stop."
)


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
