import json
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from stage.cli import notify
from stage.domain import (
    FIRST_READ_PREFIX,
    Company,
    DetailFetch,
    Job,
    JobStatus,
    Platform,
    SyncFinished,
    SyncOutcome,
    SyncRun,
)
from stage.services.sync import sync
from stage.storage import open_repository
from stage.storage.repository import SourceBatch

WEBHOOK = "https://discord.com/api/webhooks/1/tok"
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
SIMPLIFY = (
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/"
    "scripts/listings.json"
)


def _job(identifier: str, moment: datetime) -> Job:
    return Job(
        id=identifier,
        source=identifier.split(":")[0],
        company="Acme",
        title_raw="Software Engineering Intern",
        title_normalized="software engineering intern",
        apply_url_raw=f"https://example.com/{identifier.rsplit(':', 1)[1]}",
        description="",
        first_seen=moment,
        last_seen=moment,
        location_raw="Montreal, QC",
        status=JobStatus.OPEN,
    )


class _Console:
    def print(self, *_: object, **__: object) -> None:
        return None


async def _seed(db: Path, jobs: tuple[Job, ...], first_reads: tuple[str, ...] = ()) -> None:
    now = datetime.now(UTC)
    async with open_repository(db) as repository:
        for minutes in (90, 30):
            await repository.record_sync_run(
                SyncRun(
                    started_at=now - timedelta(minutes=minutes + 5),
                    finished_at=now - timedelta(minutes=minutes),
                    outcome=SyncOutcome.SUCCESS,
                )
            )
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=now,
                jobs=jobs,
                detail_fetches=tuple(
                    DetailFetch(id=f"{FIRST_READ_PREFIX}{board}", resolved=True)
                    for board in first_reads
                ),
            )
        )


async def _announce(db: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from stage.cli.commands.pipeline import _announce_new_postings

    sent: list[dict[str, Any]] = []

    class _Response:
        status = 204

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_: object) -> None:
            return None

    def capture(request: Any, timeout: object = None) -> _Response:
        sent.append(json.loads(request.data.decode()))
        return _Response()

    monkeypatch.setattr(notify, "read", lambda *_, **__: WEBHOOK)
    monkeypatch.setattr(notify, "urlopen", capture)
    async with open_repository(db) as repository:
        await _announce_new_postings(repository, _Console())
    return sent


def test_an_old_sent_file_still_reads_and_keeps_its_place(tmp_path: Path) -> None:
    path = tmp_path / "sent.json"
    path.write_text(json.dumps({"until": NOW.isoformat(), "quiet": {}}), encoding="utf-8")
    assert notify.announced_until(path) == NOW
    notify.mark_announced(NOW + timedelta(hours=1), path)
    assert notify.announced_until(path) == NOW + timedelta(hours=1)


@pytest.mark.parametrize("body", ["", "not json", "[1, 2]", '{"until": "soon", "quiet": 4}'])
def test_a_damaged_sent_file_reads_as_nothing_sent(tmp_path: Path, body: str) -> None:
    path = tmp_path / "sent.json"
    path.write_text(body, encoding="utf-8")
    assert notify.announced_until(path) is None


def test_a_posting_is_backlog_only_up_to_its_boards_first_read() -> None:
    quiet = {"greenhouse:acme": NOW, "simplify": NOW}
    assert notify.is_backlog(_job("greenhouse:acme:1", NOW), quiet)
    assert not notify.is_backlog(_job("greenhouse:acme:2", NOW + timedelta(minutes=30)), quiet)
    assert notify.is_backlog(_job("simplify:acme:3", NOW - timedelta(minutes=1)), quiet)
    assert not notify.is_backlog(_job("lever:acme:4", NOW), quiet)


async def test_a_first_read_is_never_announced_but_news_beside_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "a.db"
    moment = datetime.now(UTC)
    await _seed(
        db,
        (_job("greenhouse:acme:1", moment), _job("greenhouse:other:2", moment)),
        first_reads=("greenhouse:acme",),
    )

    sent = await _announce(db, monkeypatch)

    assert len(sent) == 1
    description = sent[0]["embeds"][0]["description"]
    assert "example.com/2" in description and "example.com/1" not in description
    assert "1 new posting" in sent[0]["embeds"][0]["title"]


async def test_a_run_of_only_backlog_sends_nothing_and_moves_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "b.db"
    moment = datetime.now(UTC)
    await _seed(db, (_job("greenhouse:acme:1", moment),), first_reads=("greenhouse:acme",))

    assert await _announce(db, monkeypatch) == []
    until = notify.announced_until()
    assert until is not None and until > moment


@respx.mock
async def test_a_sync_names_the_boards_and_lists_it_read_for_the_first_time(
    tmp_path: Path,
) -> None:
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )
    respx.get(SIMPLIFY).mock(return_value=httpx.Response(200, json=[]))
    acme = [Company(name="Acme", platform=Platform.GREENHOUSE, slug="acme")]
    db = tmp_path / "c.db"

    finished: list[SyncFinished] = []

    async def run(moment: datetime) -> None:
        async with open_repository(db) as repository:
            async for event in sync(
                repository, acme, sources=["greenhouse", "simplify"], now_fn=lambda: moment
            ):
                if isinstance(event, SyncFinished):
                    finished.append(event)

    await run(NOW)
    await run(NOW + timedelta(hours=6))

    assert finished[0].first_reads == ("greenhouse:acme", "simplify")
    assert finished[1].first_reads == ()
    async with open_repository(db) as repository:
        assert await repository.first_reads() == {"greenhouse:acme": NOW, "simplify": NOW}


async def test_a_posting_announced_from_a_list_is_not_announced_again_from_its_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    db = tmp_path / "copies.db"
    listed_at = datetime.now(UTC) - timedelta(hours=2)
    copy = replace(_job("simplify:acme:abc", listed_at), source="simplify")
    async with open_repository(db) as repository:
        await repository.apply_source_batch(
            SourceBatch(source="simplify", run_started_at=listed_at, jobs=(copy,))
        )
    notify.mark_announced(listed_at + timedelta(minutes=1))
    board = _job("greenhouse:acme:1", datetime.now(UTC))
    await _seed(db, (board,))
    import sqlite3

    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("UPDATE jobs SET duplicate_of = ? WHERE id = ?", (board.id, copy.id))

    assert await _announce(db, monkeypatch) == []


async def test_a_corrected_clock_announces_only_what_the_run_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "clock.db"
    start = datetime.now(UTC) - timedelta(minutes=1)
    ahead = start + timedelta(days=7)
    async with open_repository(db) as repository:
        for when, names in ((ahead, ("seen", "future")), (start, ("seen", "found"))):
            await repository.apply_source_batch(
                SourceBatch(
                    source="greenhouse",
                    run_started_at=when,
                    jobs=tuple(_job(f"greenhouse:{name}:{name}", when) for name in names),
                )
            )
            await repository.record_sync_run(
                SyncRun(started_at=when, finished_at=when, outcome=SyncOutcome.SUCCESS)
            )
    notify.mark_announced(ahead + timedelta(microseconds=1))

    sent = await _announce(db, monkeypatch)

    assert [message["embeds"][0]["title"] for message in sent] == ["1 new posting"]
    assert "example.com/found" in sent[0]["embeds"][0]["description"]
    until = notify.announced_until()
    assert until is not None and until <= datetime.now(UTC)
    assert await _announce(db, monkeypatch) == []


async def test_a_first_read_stamped_ahead_of_the_clock_hides_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    db = tmp_path / "ahead.db"
    moment = datetime.now(UTC)
    await _seed(db, (_job("greenhouse:acme:1", moment),), first_reads=("greenhouse:acme",))
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute(
            "UPDATE detail_fetches SET fetched_at = ? WHERE id = ?",
            ((moment + timedelta(days=1)).isoformat(), f"{FIRST_READ_PREFIX}greenhouse:acme"),
        )

    assert len(await _announce(db, monkeypatch)) == 1


async def test_a_big_run_names_every_posting_across_follow_up_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "big.db"
    moment = datetime.now(UTC)
    await _seed(db, tuple(_job(f"greenhouse:acme:{index}", moment) for index in range(60)))

    sent = await _announce(db, monkeypatch)

    named = "".join(message["embeds"][0]["description"] for message in sent)
    assert 1 < len(sent) <= notify.MAX_MESSAGES
    assert all(f"example.com/{index})" in named for index in range(60))
    assert await _announce(db, monkeypatch) == [], "a second run must not repeat them"
