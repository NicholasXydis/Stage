from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from stage.dedup import resolve_duplicates
from stage.domain import Job
from stage.services.screening import normalize_batch
from stage.storage.repository import SourceBatch
from stage.storage.sqlite_repo import SqliteRepository

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
URL = "https://job-boards.greenhouse.io/acme/jobs/8046795003"


def _posting(identifier: str, source: str, title: str, place: str) -> Job:
    return Job(
        id=identifier,
        source=source,
        company="Acme",
        title_raw=title,
        title_normalized=title.lower(),
        apply_url_raw=URL,
        description="",
        location_raw=place,
        first_seen=NOW,
        last_seen=NOW,
    )


BOARD = _posting("greenhouse:acme:8046795003", "greenhouse", "Software Engineer Intern", "")
COPY = _posting(
    "simplify:acme:1", "simplify", "Software Engineer Intern, Summer 2027", "Austin, TX"
)


def _store(
    repository: SqliteRepository, source: str, jobs: list[Job], when: datetime = NOW
) -> None:
    kept, rejected = normalize_batch(jobs)
    repository.apply_source_batch(
        SourceBatch(
            source=source,
            run_started_at=when,
            jobs=kept,
            quarantined=rejected,
            resolve_duplicates=resolve_duplicates,
        )
    )


def _main(repository: SqliteRepository) -> tuple[str, str, str]:
    row = repository._conn.execute(
        "SELECT term, location_raw, location FROM jobs WHERE id = ?", (BOARD.id,)
    ).fetchone()
    return str(row["term"]), str(row["location_raw"]), str(row["location"])


def test_a_posting_takes_the_term_and_place_its_copies_agree_on(db_path: Path) -> None:
    repository = SqliteRepository.connect(db_path)
    _store(repository, "greenhouse", [BOARD])
    _store(repository, "simplify", [COPY])
    later = NOW + timedelta(hours=1)
    _store(repository, "greenhouse", [replace(BOARD, last_seen=later)], later)
    main = _main(repository)
    repository.close()

    assert main == ("summer-2027", "Austin, TX", "usa"), "a later board read keeps them"


def test_a_remote_posting_keeps_its_own_place(db_path: Path) -> None:
    repository = SqliteRepository.connect(db_path)
    _store(repository, "greenhouse", [replace(BOARD, location_raw="Remote")])
    _store(repository, "simplify", [COPY])
    main = _main(repository)
    repository.close()

    assert main[1] == "Remote", "Remote is a place, not a gap"


def test_a_counted_place_waits_for_its_own_page(db_path: Path) -> None:
    repository = SqliteRepository.connect(db_path)
    _store(repository, "greenhouse", [replace(BOARD, location_raw="4 Locations")])
    _store(repository, "simplify", [COPY])
    main = _main(repository)
    repository.close()

    assert main[:2] == ("summer-2027", "4 Locations")


def test_copies_that_disagree_fill_nothing_once_the_board_is_read_again(db_path: Path) -> None:
    other = replace(
        COPY,
        id="speedyapply:acme:2",
        source="speedyapply",
        title_raw="Software Engineer Intern, Fall 2027",
        location_raw="Toronto, ON",
    )
    repository = SqliteRepository.connect(db_path)
    _store(repository, "greenhouse", [BOARD])
    _store(repository, "simplify", [COPY])
    _store(repository, "speedyapply", [other])
    later = NOW + timedelta(hours=1)
    _store(repository, "greenhouse", [replace(BOARD, last_seen=later)], later)
    main = _main(repository)
    repository.close()

    assert main == ("unknown", "", "unknown")


def test_a_copy_never_gives_a_term_that_ended_before_the_posting_went_up(db_path: Path) -> None:
    from stage.domain import SourceSignals

    board = replace(
        BOARD, location_raw="Austin, TX", source_posted_at=datetime(2026, 9, 24, tzinfo=UTC)
    )
    labelled = replace(
        COPY,
        title_raw="Software Engineer Intern",
        source_posted_at=datetime(2026, 7, 13, tzinfo=UTC),
        signals=SourceSignals(terms=("Summer 2026",)),
    )
    repository = SqliteRepository.connect(db_path)
    _store(repository, "greenhouse", [board])
    _store(repository, "simplify", [labelled])
    terms = dict(repository._conn.execute("SELECT id, term FROM jobs").fetchall())
    repository.close()

    assert terms[labelled.id] == "summer-2026"
    assert terms[BOARD.id] == "unknown", "summer 2026 was over before the board posted it"


def test_a_copy_never_gives_a_term_the_main_title_contradicts(db_path: Path) -> None:
    from stage.domain import SourceSignals

    board = replace(BOARD, title_raw="Software Engineering Intern - Winter Session")
    labelled = replace(
        COPY, title_raw="Software Engineer Intern", signals=SourceSignals(terms=("Fall 2026",))
    )
    repository = SqliteRepository.connect(db_path)
    _store(repository, "greenhouse", [board])
    _store(repository, "simplify", [labelled])
    terms = dict(repository._conn.execute("SELECT id, term FROM jobs").fetchall())
    repository.close()

    assert terms[labelled.id] == "fall-2026"
    assert terms[BOARD.id] == "unknown", "the board's own title says winter"


def test_a_copy_gives_a_term_the_main_title_agrees_with(db_path: Path) -> None:
    board = replace(BOARD, title_raw="Software Engineer Intern - Summer")
    repository = SqliteRepository.connect(db_path)
    _store(repository, "greenhouse", [board])
    _store(repository, "simplify", [COPY])
    terms = dict(repository._conn.execute("SELECT id, term FROM jobs").fetchall())
    repository.close()

    assert terms[BOARD.id] == "summer-2027"
