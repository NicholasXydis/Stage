from json import loads
from pathlib import Path

from typer.testing import CliRunner

from stage.cli.app import app


def test_sources_rejects_conflicting_rate_limit_resets() -> None:
    result = CliRunner().invoke(
        app,
        ["sources", "--reset-rate-limit", "example.com", "--reset-all"],
    )

    assert result.exit_code == 2
    assert "not both" in result.stdout


def test_sources_rejects_conflicting_cache_resets() -> None:
    result = CliRunner().invoke(
        app,
        ["sources", "--clear-cache", "greenhouse", "--clear-cache-all"],
    )

    assert result.exit_code == 2
    assert "not both" in result.stdout


def test_sources_json_contains_every_report_section(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["sources", "--boards", "--json", "--db", str(tmp_path / "stage.db")],
    )

    assert result.exit_code == 0
    payload = loads(result.stdout)
    assert "sources" in payload
    assert "rate_states" in payload
    assert "boards" in payload


def test_purge_dry_run_reports_no_removal(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["purge", "--dry-run", "--db", str(tmp_path / "stage.db")])

    assert result.exit_code == 0
    assert "No postings removed" in result.stdout
