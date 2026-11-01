import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stage.cli.app import app
from stage.domain import Job, LocationBucket

NOW = datetime.now(UTC)
COMMANDS = [
    ["list"],
    ["list", "--all", "--last", "30"],
    ["list", "--location", "unknown"],
    ["search", "intern"],
    ["show", "1"],
    ["show", "2"],
    ["show", "greenhouse:nope:1"],
    ["save", "1", "--note", "call back"],
    ["tracker"],
    ["tracker", "--stats"],
    ["untrack", "1"],
    ["stats"],
    ["doctor"],
    ["sources"],
    ["quarantine"],
    ["prefs", "--show"],
    ["list", "--mine"],
    ["schedule", "status"],
    ["purge", "--dry-run"],
    ["rescreen"],
    ["discover"],
    ["sync", "--source", "lever", "--exclude", "ashby"],
    ["list", "--role", "nope"],
    ["export", "--format", "xlsx"],
    ["open", "1-99"],
]


def _job(index: int, *, title: str, location: str) -> Job:
    seen = NOW - timedelta(hours=index)
    return Job(
        id=f"greenhouse:acme:{index}",
        source="greenhouse",
        company="A very long employer name that will not fit in any column at all",
        title_raw=title,
        title_normalized=title.lower(),
        apply_url_raw=f"https://example.com/jobs/{index}",
        description="First line.\n\nSecond line that goes on and on. " * 20,
        first_seen=seen,
        last_seen=seen,
        location_raw=location,
        location=LocationBucket.MONTREAL if location else LocationBucket.UNKNOWN,
        term="summer-2027",
    )


@pytest.fixture
def seeded() -> None:
    from stage.storage.repository import SourceBatch
    from stage.storage.sqlite_repo import SqliteRepository

    repository = SqliteRepository.connect(Path(os.environ["STAGE_DB"]))
    repository.apply_source_batch(
        SourceBatch(
            source="greenhouse",
            run_started_at=NOW,
            jobs=(
                _job(1, title="Software Engineering Intern, Platform " * 4, location="Montreal"),
                _job(2, title="Data Intern", location=""),
            ),
        )
    )
    repository.close()


@pytest.mark.usefixtures("seeded")
@pytest.mark.parametrize("width", ["50", "120"])
@pytest.mark.parametrize("command", COMMANDS, ids=" ".join)
def test_every_command_prints_only_ascii(command: list[str], width: str) -> None:
    CliRunner().invoke(app, ["list"], env={"COLUMNS": width})
    result = CliRunner().invoke(app, command, env={"COLUMNS": width})

    said = result.stdout + result.stderr
    assert result.exception is None or isinstance(result.exception, SystemExit), said
    assert said.isascii(), sorted({char for char in said if not char.isascii()})
