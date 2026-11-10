from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from stage.domain import Company, HttpValidator, Job, JobStatus, Platform, board_key, job_id
from stage.sources import get_adapter
from stage.sources.base import Adapter
from stage.storage import SourceBatch, open_repository

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def _job(ident: str, seen: datetime) -> Job:
    return Job(
        id=ident,
        source="greenhouse",
        company="Sony Interactive Entertainment",
        title_raw="Software Engineering Intern",
        title_normalized="software engineering intern",
        apply_url_raw="",
        description="",
        first_seen=seen,
        last_seen=seen,
    )


def test_two_boards_of_one_employer_have_different_board_keys() -> None:
    adapter = get_adapter("greenhouse")
    first = Company(
        name="Sony Interactive Entertainment", platform=Platform.GREENHOUSE, slug="siei"
    )
    second = Company(
        name="Sony Interactive Entertainment",
        platform=Platform.GREENHOUSE,
        slug="sonyinteractiveentertainmentglobal",
    )
    assert first.name == second.name
    assert adapter.board_key(first) != adapter.board_key(second)


def test_the_board_key_agrees_with_the_ids_the_adapter_produces() -> None:
    company = Company(name="Acme", platform=Platform.GREENHOUSE, slug="Acme_Corp")
    job = _job(f"{board_key('greenhouse', company.slug)}:12345", NOW)
    assert get_adapter("greenhouse").board_key(company) == job.board_key


def test_noncanonical_identifier_components_do_not_collapse_together() -> None:
    assert job_id("custom_json", "acme", "role/a") != job_id("custom_json", "acme", "role-a")
    assert job_id("simplify", "Coveo Solutions Inc.", "123") == "simplify:coveo-solutions-inc.:123"


async def test_one_board_failing_never_closes_its_twins_postings(db_path: Path) -> None:
    siei, global_board = "greenhouse:siei", "greenhouse:sonyinteractiveentertainmentglobal"
    later = NOW + timedelta(days=1)

    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=NOW,
                jobs=(_job(f"{siei}:1", NOW), _job(f"{global_board}:2", NOW)),
                closable_boards=(siei, global_board),
            )
        )

        result = await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=later,
                jobs=(_job(f"{siei}:1", later),),
                closable_boards=(siei,),
            )
        )

        assert result.closed == 0
        survivor = await repository.get_job(f"{global_board}:2")
        assert survivor is not None
        assert survivor.status is JobStatus.OPEN, (
            "a board that was never fetched cannot have taken its postings down"
        )


async def test_a_304_on_one_board_never_refreshes_its_twins_stale_rows(
    db_path: Path,
) -> None:
    siei, global_board = "greenhouse:siei", "greenhouse:sonyinteractiveentertainmentglobal"
    later = NOW + timedelta(days=1)

    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=NOW,
                jobs=(_job(f"{siei}:1", NOW), _job(f"{global_board}:2", NOW)),
            )
        )
        result = await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=later,
                unchanged_boards=(siei,),
            )
        )

    assert result.touched == 1, "only the board that answered 304 is refreshed"


async def test_a_board_token_containing_an_underscore_matches_only_itself(
    db_path: Path,
) -> None:
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=NOW,
                jobs=(_job("greenhouse:a_b:1", NOW), _job("greenhouse:axb:2", NOW)),
                closable_boards=("greenhouse:a_b", "greenhouse:axb"),
            )
        )
        result = await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=NOW + timedelta(days=1),
                jobs=(_job("greenhouse:a_b:1", NOW + timedelta(days=1)),),
                closable_boards=("greenhouse:a_b",),
            )
        )

    assert result.closed == 0, "`a_b` must not match `axb`"


def test_an_unresolvable_row_gets_a_key_of_its_own_that_matches_no_job() -> None:
    from stage.services.sync import _safe_board_key

    class _Broken:
        name = "workday"

        def board_key(self, company: Company) -> str:
            raise ValueError(f"{company.slug} is missing workday_site")

    adapter = _Broken()
    first = Company(name="A", platform=Platform.WORKDAY, slug="a", workday_tenant="a")
    second = Company(name="B", platform=Platform.WORKDAY, slug="b", workday_tenant="b")

    a = _safe_board_key(cast(Adapter, adapter), first)
    b = _safe_board_key(cast(Adapter, adapter), second)

    assert a != b, "a shared fallback key lets one broken row close another's postings"
    assert not a.startswith("workday:"), "it must match no real job id, so it closes nothing"


async def test_a_read_that_would_close_most_of_a_board_waits_for_a_second_read(
    db_path: Path,
) -> None:
    board = "greenhouse:siei"
    stored = tuple(_job(f"{board}:{index}", NOW) for index in range(10))
    first, second = NOW + timedelta(hours=1), NOW + timedelta(hours=2)

    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse", run_started_at=NOW, jobs=stored, closable_boards=(board,)
            )
        )
        held = await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=first,
                jobs=(_job(f"{board}:0", first), _job(f"{board}:1", first)),
                closable_boards=(board,),
            )
        )
        confirmed = await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=second,
                jobs=(_job(f"{board}:0", second), _job(f"{board}:1", second)),
                closable_boards=(board,),
            )
        )

    assert held.closed == 0, "eight of ten gone in one read waits for a second read"
    assert confirmed.closed == 8


async def test_a_held_close_forgets_the_boards_validator_so_a_304_cannot_keep_it(
    db_path: Path,
) -> None:
    board = "greenhouse:siei"
    url = "https://boards-api.greenhouse.io/v1/boards/siei/jobs?content=true"
    other = "https://boards-api.greenhouse.io/v1/boards/other/jobs?content=true"
    stored = tuple(_job(f"{board}:{index}", NOW) for index in range(10))
    later = NOW + timedelta(hours=1)

    def read(at: datetime, etag: str, jobs: tuple[Job, ...]) -> SourceBatch:
        return SourceBatch(
            source="greenhouse",
            run_started_at=at,
            jobs=jobs,
            closable_boards=(board,),
            board_urls=((board, (url,)),),
            validators=(
                HttpValidator(url=url, etag=etag, fetched_at=at),
                HttpValidator(url=other, etag=etag, fetched_at=at),
            ),
        )

    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(read(NOW, '"a"', stored))
        held = await repository.apply_source_batch(read(later, '"b"', stored[:2]))
        kept = await repository.load_validators("greenhouse")

    assert held.closed == 0
    assert url not in kept, "a 304 against the held read would never confirm the close"
    assert kept[other].etag == '"b"'


async def test_a_few_closes_go_through_at_once(db_path: Path) -> None:
    board = "greenhouse:siei"
    stored = tuple(_job(f"{board}:{index}", NOW) for index in range(10))
    later = NOW + timedelta(hours=1)

    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse", run_started_at=NOW, jobs=stored, closable_boards=(board,)
            )
        )
        result = await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=later,
                jobs=tuple(_job(f"{board}:{index}", later) for index in range(7)),
                closable_boards=(board,),
            )
        )

    assert result.closed == 3
