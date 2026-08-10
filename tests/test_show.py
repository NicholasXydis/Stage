from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stage.cli.app import app
from stage.domain import Job, LocationBucket, RoleCategory
from stage.services.query import get_posting
from stage.storage import open_repository
from stage.storage.repository import SourceBatch
from stage.storage.sqlite_repo import SqliteRepository

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def _job(
    identifier: str,
    *,
    source: str = "greenhouse",
    title: str = "Stagiaire Développement",
    url: str = "https://boards.example.test/apply",
    duplicate_of: str | None = None,
) -> Job:
    return Job(
        id=identifier,
        source=source,
        company="Coveo Solutions",
        title_raw=title,
        title_normalized=title.lower(),
        title_canonical=title.lower(),
        apply_url_raw=url,
        description="Poste à Montréal.",
        first_seen=NOW,
        last_seen=NOW,
        location_raw="Montréal, QC",
        location=LocationBucket.MONTREAL,
        role=RoleCategory.SWE,
        term="summer-2027",
        duplicate_of=duplicate_of,
    )


@pytest.fixture
def seeded(db_path: Path) -> Path:
    repository = SqliteRepository.connect(db_path)
    repository.apply_source_batch(
        SourceBatch(
            source="greenhouse",
            run_started_at=NOW,
            jobs=(
                _job("greenhouse:coveo:1"),
                _job("simplify:feed:1", source="simplify", duplicate_of="greenhouse:coveo:1"),
                _job("greenhouse:coveo:2", url="javascript:alert(1)"),
            ),
        )
    )
    repository.close()
    return db_path


@pytest.mark.asyncio
async def test_a_posting_carries_the_rows_linked_to_it(seeded: Path) -> None:
    async with open_repository(seeded) as repository:
        detail = await get_posting(repository, "greenhouse:coveo:1")
        follower = await get_posting(repository, "simplify:feed:1")
        absent = await get_posting(repository, "greenhouse:coveo:404")

    assert detail is not None
    assert [job.id for job in detail.duplicates] == ["simplify:feed:1"]
    assert detail.canonical is None

    assert follower is not None
    assert follower.canonical is not None
    assert follower.canonical.id == "greenhouse:coveo:1"
    assert follower.duplicates == ()

    assert absent is None


def test_show_renders_a_posting_and_names_its_duplicate(seeded: Path) -> None:
    result = CliRunner().invoke(app, ["show", "greenhouse:coveo:1", "--db", str(seeded)])
    assert result.exit_code == 0, result.stdout
    assert "Coveo Solutions" in result.stdout
    assert "summer-2027" in result.stdout
    assert "Also published as" in result.stdout


def test_show_says_how_to_find_an_id_when_the_posting_is_absent(seeded: Path) -> None:
    result = CliRunner().invoke(app, ["show", "greenhouse:coveo:404", "--db", str(seeded)])
    assert result.exit_code == 1
    assert "stage list --json" in result.stdout


def test_open_prints_the_url_without_launching_a_browser(seeded: Path) -> None:
    result = CliRunner().invoke(app, ["open", "greenhouse:coveo:1", "--print", "--db", str(seeded)])
    assert result.exit_code == 0, result.stdout
    assert result.stdout.strip() == "https://boards.example.test/apply"


def test_open_refuses_a_scheme_that_is_not_http(seeded: Path) -> None:
    result = CliRunner().invoke(app, ["open", "greenhouse:coveo:2", "--print", "--db", str(seeded)])
    assert result.exit_code == 2
    assert "only a plain http or https address" in result.stdout


@pytest.mark.parametrize(
    "hostile",
    [
        "javascript:alert(1)",
        "file:///c:/windows/system32/calc.exe",
        "http:evil",
        "\x1b]8;;https://evil.example\x1b\\https://ok.example",
        "  java\tscript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
    ],
)
def test_open_refuses_every_shape_that_is_not_a_plain_web_address(
    db_path: Path, hostile: str
) -> None:
    repository = SqliteRepository.connect(db_path)
    repository.apply_source_batch(
        SourceBatch(
            source="greenhouse",
            run_started_at=NOW,
            jobs=(_job("greenhouse:coveo:9", url=hostile),),
        )
    )
    repository.close()

    result = CliRunner().invoke(
        app, ["open", "greenhouse:coveo:9", "--print", "--db", str(db_path)]
    )
    assert result.exit_code == 2, result.stdout
    assert "Refusing to open" in result.stdout


def test_open_launches_through_the_browser_module(
    seeded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import webbrowser

    opened: list[str] = []

    def record(url: str, new: int = 0, autoraise: bool = True) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(webbrowser, "open", record)
    result = CliRunner().invoke(app, ["open", "greenhouse:coveo:1", "--db", str(seeded)])
    assert result.exit_code == 0, result.stdout
    assert opened == ["https://boards.example.test/apply"]
