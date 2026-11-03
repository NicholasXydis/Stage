from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from stage.dedup import resolve_duplicates
from stage.domain import Job, LocationBucket, QuarantinedJob, RejectionReason
from stage.services.screening import normalize_batch
from stage.storage.repository import SourceBatch
from stage.storage.sqlite_repo import SqliteRepository

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
BOARD_URL = "https://job-boards.greenhouse.io/acme/jobs/8046795003"
DOCTORAL = "Pursuing Masters or PhD in Computer Science. Strong C++ skills."
OPEN_TO_ALL = "Pursuing a BS, MS, or PhD in Computer Science. Strong C++ skills."


def _board(description: str = DOCTORAL, when: datetime = NOW) -> Job:
    return Job(
        id="greenhouse:acme:8046795003",
        source="greenhouse",
        company="Acme Semiconductor",
        title_raw="Intern, Power-Thermal Simulation Software Engineer",
        title_normalized="intern power thermal simulation software engineer",
        apply_url_raw=BOARD_URL,
        description=description,
        location_raw="San Jose, CA",
        first_seen=when,
        last_seen=when,
    )


def _listed(identifier: str, title: str, url: str = BOARD_URL, when: datetime = NOW) -> Job:
    return Job(
        id=identifier,
        source="simplify",
        company="Acme",
        title_raw=title,
        title_normalized=title.lower(),
        apply_url_raw=url,
        description="",
        location_raw="San Jose, CA",
        first_seen=when,
        last_seen=when,
    )


COPY = "simplify:acme:copy"
OTHER = "simplify:acme:other"


def _copy(when: datetime = NOW) -> Job:
    return _listed(COPY, "Power-Thermal Simulation Software Engineer Intern", when=when)


def _other(when: datetime = NOW) -> Job:
    return _listed(
        OTHER, "Data Engineer Intern", "https://job-boards.greenhouse.io/acme/jobs/1", when
    )


def _screened(source: str, jobs: list[Job], when: datetime = NOW) -> SourceBatch:
    kept, rejected = normalize_batch(jobs)
    return SourceBatch(
        source=source,
        run_started_at=when,
        jobs=kept,
        quarantined=rejected,
        resolve_duplicates=resolve_duplicates,
    )


def _state(repository: SqliteRepository) -> tuple[dict[str, str | None], dict[str, str]]:
    jobs = {
        str(row["id"]): row["duplicate_of"]
        for row in repository._conn.execute("SELECT id, duplicate_of FROM jobs")
    }
    held = {
        str(row["id"]): str(row["matched_phrase"])
        for row in repository._conn.execute("SELECT id, matched_phrase FROM quarantine")
    }
    return jobs, held


def _linked(db_path: Path) -> SqliteRepository:
    repository = SqliteRepository.connect(db_path)
    repository.apply_source_batch(_screened("greenhouse", [_board(OPEN_TO_ALL)]))
    repository.apply_source_batch(_screened("simplify", [_copy(), _other()]))
    jobs, _ = _state(repository)
    assert jobs[COPY] == _board().id, "the fixture needs the list copy linked to its board copy"
    return repository


def test_a_list_copy_inherits_the_degree_rejection_of_its_board_copy(db_path: Path) -> None:
    repository = _linked(db_path)
    later = NOW + timedelta(hours=1)

    result = repository.apply_source_batch(
        _screened("greenhouse", [_board(DOCTORAL, later)], later)
    )
    jobs, held = _state(repository)

    assert COPY not in jobs, "a copy without a description must not be promoted and kept"
    assert held[COPY] == ("copy of greenhouse:acme:8046795003 rejected for pursuing masters or phd")
    assert result.quarantined == 2, "the copy counts as a rejection of this batch"
    repository.close()


def test_an_unrelated_list_posting_is_untouched(db_path: Path) -> None:
    repository = _linked(db_path)
    later = NOW + timedelta(hours=1)

    repository.apply_source_batch(_screened("greenhouse", [_board(DOCTORAL, later)], later))
    jobs, held = _state(repository)

    assert jobs[OTHER] is None
    assert OTHER not in held
    repository.close()


def test_a_later_accepted_board_copy_brings_the_list_copy_back(db_path: Path) -> None:
    repository = _linked(db_path)
    later, latest = NOW + timedelta(hours=1), NOW + timedelta(hours=2)
    repository.apply_source_batch(_screened("greenhouse", [_board(DOCTORAL, later)], later))

    repository.apply_source_batch(_screened("greenhouse", [_board(OPEN_TO_ALL, latest)], latest))
    jobs, held = _state(repository)

    assert jobs[COPY] == _board().id, "the copy returns as a duplicate of the accepted board copy"
    assert COPY not in held and _board().id not in held
    repository.close()


def test_a_list_copy_arriving_after_its_board_copy_was_rejected_is_rejected_too(
    db_path: Path,
) -> None:
    repository = SqliteRepository.connect(db_path)
    repository.apply_source_batch(_screened("greenhouse", [_board()]))

    repository.apply_source_batch(_screened("simplify", [_copy(), _other()]))
    jobs, held = _state(repository)

    assert COPY not in jobs and held[COPY].startswith("copy of greenhouse:acme:8046795003 ")
    assert OTHER in jobs

    again = NOW + timedelta(hours=1)
    repository.apply_source_batch(_screened("simplify", [_copy(again)], again))
    jobs, held = _state(repository)
    assert COPY not in jobs and COPY in held, "the next list read keeps the copy rejected"
    repository.close()


def test_a_copy_with_its_own_description_is_judged_on_its_own(db_path: Path) -> None:
    repository = SqliteRepository.connect(db_path)
    repository.apply_source_batch(_screened("greenhouse", [_board()]))
    described = replace(_copy(), description=OPEN_TO_ALL)

    repository.apply_source_batch(_screened("simplify", [described]))
    jobs, held = _state(repository)

    assert COPY in jobs and COPY not in held
    repository.close()


def test_a_better_ranked_copy_never_inherits_from_a_list_rejection(db_path: Path) -> None:
    repository = SqliteRepository.connect(db_path)
    rejected = QuarantinedJob(
        id=COPY,
        source="simplify",
        company="Acme",
        title_raw="Power-Thermal Simulation Software Engineer Intern (MS)",
        reason=RejectionReason.OUT_OF_SCOPE_DEGREE,
        first_seen=NOW,
        last_seen=NOW,
        apply_url_raw=BOARD_URL,
        location=LocationBucket.USA,
        matched_phrase="ms",
    )
    repository.apply_source_batch(
        SourceBatch(source="simplify", run_started_at=NOW, quarantined=(rejected,))
    )

    repository.apply_source_batch(_screened("greenhouse", [_board(description="")]))
    jobs, _ = _state(repository)

    assert _board().id in jobs, "a board copy is never rejected because a list copy was"
    repository.close()


def test_a_list_copy_stored_before_its_board_copy_is_rejected_with_it(db_path: Path) -> None:
    repository = SqliteRepository.connect(db_path)
    repository.apply_source_batch(_screened("simplify", [_copy(), _other()]))

    result = repository.apply_source_batch(_screened("greenhouse", [_board()]))
    jobs, held = _state(repository)
    repository.close()

    assert COPY not in jobs and held[COPY].startswith("copy of greenhouse:acme:8046795003 ")
    assert OTHER in jobs and result.quarantined == 2
