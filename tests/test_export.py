from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stage.domain import (
    ExportFormat,
    Job,
    JobFilters,
    Language,
    LocationBucket,
    RoleCategory,
)
from stage.paths import font_path
from stage.services.export import (
    COLUMNS,
    FORMULA_PREFIXES,
    ExportError,
    ExportResult,
    default_filename,
    export_jobs,
    render_csv,
    render_json,
    render_markdown,
    resolve_destination,
    write_pdf,
)
from stage.storage import open_repository
from stage.storage.repository import SourceBatch
from stage.storage.sqlite_repo import SqliteRepository

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def _job(identifier: str, title: str, *, company: str = "Coveo Solutions", age: int = 0) -> Job:
    return Job(
        id=identifier,
        source="greenhouse",
        company=company,
        title_raw=title,
        title_normalized=title.lower(),
        title_canonical=title.lower(),
        apply_url_raw=f"https://boards.example.test/{identifier}",
        description="Poste à Montréal",
        first_seen=NOW - timedelta(days=age),
        last_seen=NOW,
        location_raw="Montréal, QC",
        location=LocationBucket.MONTREAL,
        language=Language.FR,
        role=RoleCategory.SWE,
        term="summer-2027",
    )


FRENCH_TITLE = "Stagiaire en développement — cœur d’équipe"


def test_every_formula_prefix_is_guarded_and_plain_text_is_not() -> None:
    for marker in FORMULA_PREFIXES:
        rendered = render_csv([_job("greenhouse:acme:1", f"{marker}cmd 1+1")])
        title = rendered.splitlines()[1].split(",")[2]
        assert title.startswith(f"'{marker}"), title

    plain = render_csv([_job("greenhouse:acme:1", FRENCH_TITLE)])
    assert FRENCH_TITLE in plain
    assert "'" + FRENCH_TITLE not in plain


def test_leading_whitespace_cannot_smuggle_a_formula_past_the_guard() -> None:
    rendered = render_csv([_job("greenhouse:acme:1", "\t=1+1")])
    assert rendered.splitlines()[1].split(",")[2] == "'=1+1"


def test_csv_carries_a_bom_so_excel_reads_french(tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    target.write_text(render_csv([_job("greenhouse:acme:1", FRENCH_TITLE)]), encoding="utf-8-sig")
    assert target.read_bytes().startswith(b"\xef\xbb\xbf")
    assert FRENCH_TITLE in target.read_text(encoding="utf-8-sig")


def test_control_characters_never_reach_a_writer() -> None:
    spoof = _job("greenhouse:acme:1", "Intern\x1b[31mFAKE\x00\x08 role")
    for rendered in (
        render_csv([spoof]),
        render_json([spoof]),
        render_markdown([spoof], generated_at=NOW),
    ):
        assert "\x1b" not in rendered
        assert "\x00" not in rendered
        assert "FAKE" in rendered


def test_markdown_escapes_a_pipe_so_the_table_survives() -> None:
    rendered = render_markdown([_job("greenhouse:acme:1", "Data | Intern")], generated_at=NOW)
    row = rendered.splitlines()[-1]
    assert "Data \\| Intern" in row
    assert row.replace("\\|", "").count("|") == len(COLUMNS) + 1


def test_a_newline_in_a_field_never_breaks_a_row() -> None:
    rendered = render_markdown([_job("greenhouse:acme:1", "Intern\nSecond line")], generated_at=NOW)
    assert len(rendered.splitlines()) == 7
    assert "Intern Second line" in rendered


def test_the_pdf_embeds_a_font_that_covers_french(tmp_path: Path) -> None:
    assert font_path().exists()
    target = tmp_path / "out.pdf"
    write_pdf([_job("greenhouse:acme:1", FRENCH_TITLE)], target, generated_at=NOW)
    payload = target.read_bytes()
    assert payload.startswith(b"%PDF")
    assert b"/FontFile2" in payload


def test_resolve_destination_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    target.write_text("existing", encoding="utf-8")
    with pytest.raises(ExportError, match="--force"):
        resolve_destination(target, ExportFormat.CSV, NOW, False)
    assert resolve_destination(target, ExportFormat.CSV, NOW, True) == target.resolve()


def test_resolve_destination_names_the_file_inside_a_directory(tmp_path: Path) -> None:
    resolved = resolve_destination(tmp_path, ExportFormat.MD, NOW, False)
    assert resolved.name == default_filename(ExportFormat.MD, NOW) == "stage-export-20260808.md"


def test_resolve_destination_refuses_a_missing_parent(tmp_path: Path) -> None:
    with pytest.raises(ExportError, match="does not exist"):
        resolve_destination(tmp_path / "absent" / "out.csv", ExportFormat.CSV, NOW, False)


@pytest.mark.asyncio
async def test_export_respects_the_active_filters(db_path: Path, tmp_path: Path) -> None:
    repository = SqliteRepository.connect(db_path)
    repository.apply_source_batch(
        SourceBatch(
            source="greenhouse",
            run_started_at=NOW,
            jobs=(
                _job("greenhouse:acme:1", "Recent Intern"),
                _job("greenhouse:acme:2", "Old Intern", age=40),
                _job("greenhouse:acme:3", "Other Employer Intern", company="Behaviour"),
            ),
        )
    )
    repository.close()

    async with open_repository(db_path) as repo:
        windowed = await export_jobs(
            repo,
            JobFilters(limit=100),
            fmt=ExportFormat.CSV,
            destination=tmp_path / "window.csv",
            now=NOW,
        )
        filtered = await export_jobs(
            repo,
            JobFilters(company="Behaviour", limit=100),
            fmt=ExportFormat.JSON,
            destination=tmp_path / "one.json",
            window_days=None,
            now=NOW,
        )

    assert windowed.count == 2
    assert "Old Intern" not in windowed.path.read_text(encoding="utf-8-sig")
    assert filtered.count == 1
    assert "Other Employer Intern" in filtered.path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_a_truncating_limit_is_reported_rather_than_hidden(
    db_path: Path, tmp_path: Path
) -> None:
    repository = SqliteRepository.connect(db_path)
    repository.apply_source_batch(
        SourceBatch(
            source="greenhouse",
            run_started_at=NOW,
            jobs=tuple(_job(f"greenhouse:acme:{index}", f"Intern {index}") for index in range(5)),
        )
    )
    repository.close()

    async with open_repository(db_path) as repo:
        result = await export_jobs(
            repo,
            JobFilters(limit=2),
            fmt=ExportFormat.CSV,
            destination=tmp_path / "capped.csv",
            now=NOW,
        )
    assert (result.count, result.total_matching) == (2, 5)


def test_export_is_reachable_from_the_command_line(db_path: Path, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from stage.cli.app import app

    repository = SqliteRepository.connect(db_path)
    repository.apply_source_batch(
        SourceBatch(
            source="greenhouse",
            run_started_at=NOW,
            jobs=(_job("greenhouse:acme:1", FRENCH_TITLE),),
        )
    )
    repository.close()

    runner = CliRunner()
    target = tmp_path / "overwrite.csv"
    for fmt in ExportFormat:
        written = tmp_path / f"cli.{fmt.value}"
        result = runner.invoke(
            app,
            ["export", "--format", fmt.value, "--all", "--out", str(written), "--db", str(db_path)],
        )
        assert result.exit_code == 0, result.stdout
        assert written.exists()

    repeated = runner.invoke(
        app, ["export", "--format", "csv", "--out", str(target), "--db", str(db_path)]
    )
    assert repeated.exit_code == 0, repeated.stdout
    blocked = runner.invoke(
        app, ["export", "--format", "csv", "--out", str(target), "--db", str(db_path)]
    )
    assert blocked.exit_code == 2
    assert "--force" in blocked.stdout

    unknown = runner.invoke(app, ["export", "--format", "xlsx", "--db", str(db_path)])
    assert unknown.exit_code == 2
    assert "csv, json, md, pdf" in unknown.stdout


def test_a_backslash_before_a_pipe_cannot_break_the_markdown_table() -> None:
    cases = (
        ("A|B", "A" + chr(92) + "|B"),
        ("A" + chr(92) + "|B", "A" + chr(92) * 2 + chr(92) + "|B"),
        ("back" + chr(92) + "slash", "back" + chr(92) * 2 + "slash"),
    )
    for title, expected in cases:
        row = render_markdown([_job("greenhouse:acme:1", title)], generated_at=NOW).splitlines()[-1]
        cell = row.split(" | ")[2]
        assert cell == expected, (title, cell)
        bare = row.replace(chr(92) * 2, "").replace(chr(92) + "|", "")
        assert bare.count("|") == len(COLUMNS) + 1, (title, row)


def test_an_unbounded_field_cannot_break_the_pdf_renderer(tmp_path: Path) -> None:
    from stage.services.export import PDF_CELL_LIMIT

    target = tmp_path / "long.pdf"
    write_pdf([_job("greenhouse:acme:1", "z" * 10_000)], target, generated_at=NOW)
    assert target.read_bytes().startswith(b"%PDF")
    assert PDF_CELL_LIMIT < 10_000


def test_a_pdf_the_renderer_refuses_becomes_an_actionable_export_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import stage.services.export as export_module

    def explode(*_: object, **__: object) -> None:
        raise ValueError("The row with index 1 is too high")

    monkeypatch.setattr(export_module, "_render_pdf", explode)
    with pytest.raises(ExportError, match="csv, json or md"):
        write_pdf([_job("greenhouse:acme:1", "Intern")], tmp_path / "x.pdf", generated_at=NOW)


def test_a_glyph_the_font_cannot_draw_is_reported_rather_than_dropped_silently(
    tmp_path: Path,
) -> None:
    cjk = _job("greenhouse:acme:1", "実習生 Intern", company="株式会社")
    notes = write_pdf([cjk], tmp_path / "cjk.pdf", generated_at=NOW)
    assert notes, "a PDF that lost characters must say so"
    assert "missing the following glyphs" in notes[0]


def test_a_latin_export_reports_nothing(tmp_path: Path) -> None:
    assert (
        write_pdf([_job("greenhouse:acme:1", FRENCH_TITLE)], tmp_path / "fr.pdf", generated_at=NOW)
        == ()
    )


def test_a_failed_write_is_reported_and_leaves_no_partial_file(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import stage.services.export as export_module

    repository = SqliteRepository.connect(db_path)
    repository.apply_source_batch(
        SourceBatch(
            source="greenhouse",
            run_started_at=NOW,
            jobs=(_job("greenhouse:acme:1", "Intern"),),
        )
    )
    repository.close()

    def refuse(*_: object, **__: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(export_module, "render_csv", refuse)
    target = tmp_path / "full.csv"

    async def run() -> None:
        async with open_repository(db_path) as repo:
            await export_jobs(
                repo, JobFilters(limit=10), fmt=ExportFormat.CSV, destination=target, now=NOW
            )

    import asyncio

    with pytest.raises(ExportError, match="No space left on device"):
        asyncio.run(run())
    assert not target.exists()
    assert not list(tmp_path.glob("*.partial"))


def test_an_unwritable_destination_becomes_an_export_error(tmp_path: Path) -> None:
    from stage.services.export import _write_text

    with pytest.raises(ExportError, match="could not write"):
        _write_text(tmp_path, "payload")


def test_the_export_path_is_never_parsed_as_markup() -> None:
    import io

    from rich.console import Console

    from stage.cli.render import render_export

    hostile = ExportResult(
        path=Path("[link=https://evil.example]click[/link].csv"),
        fmt=ExportFormat.CSV,
        count=1,
        total_matching=1,
    )

    buffer = io.StringIO()
    render_export(
        Console(file=buffer, force_terminal=True, width=200, legacy_windows=False), hostile
    )
    assert "\x1b]8;" not in buffer.getvalue()

    recorder = Console(width=200, no_color=True, force_terminal=False, record=True)
    render_export(recorder, hostile)
    assert str(hostile.path) in recorder.export_text()
