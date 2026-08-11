from typer.testing import CliRunner

from stage.cli.app import app

COMMANDS = (
    "sync",
    "list",
    "search",
    "show",
    "open",
    "export",
    "coverage",
    "purge",
    "rescreen",
    "doctor",
    "stats",
    "canary",
    "sources",
    "quarantine",
    "discover",
    "help",
)


def test_root_help_lists_every_command() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in COMMANDS:
        assert command in result.stdout


def test_every_command_has_help() -> None:
    runner = CliRunner()

    for command in COMMANDS:
        result = runner.invoke(app, [command, "--help"])

        assert result.exit_code == 0
        assert "Usage:" in result.stdout


def test_help_shows_common_workflows() -> None:
    result = CliRunner().invoke(app, ["help"])

    assert result.exit_code == 0
    assert "Start here:" in result.stdout
    assert "Common filters:" in result.stdout
    assert "Health and maintenance:" in result.stdout
    assert "stage search" in result.stdout
    assert "stage help COMMAND" in result.stdout


def test_help_shows_one_command() -> None:
    result = CliRunner().invoke(app, ["help", "sync"])

    assert result.exit_code == 0
    assert "Usage: root sync" in result.stdout
    assert "--dry-run" in result.stdout


def test_help_rejects_unknown_command() -> None:
    result = CliRunner().invoke(app, ["help", "missing"])

    assert result.exit_code == 2
    assert "Unknown command" in result.stderr
