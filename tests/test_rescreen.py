from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from stage.domain import (
    Job,
    JobStatus,
    LocationBucket,
    QuarantineFilters,
    RejectionReason,
    RoleCategory,
    SourceSignals,
)
from stage.services.maintenance import rescreen
from stage.storage import open_repository
from stage.storage.repository import SourceBatch
from stage.storage.sqlite_repo import SqliteRepository

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def _job(identifier: str, title: str, *, role: RoleCategory = RoleCategory.SWE) -> Job:
    return Job(
        id=identifier,
        source="workday",
        company="TD Bank",
        title_raw=title,
        title_normalized=title.lower(),
        apply_url_raw=f"https://boards.example.test/{identifier}",
        description="",
        first_seen=NOW,
        last_seen=NOW,
        location_raw="Montreal, QC",
        location=LocationBucket.MONTREAL,
        role=role,
        status=JobStatus.OPEN,
    )


@pytest.fixture
def seeded(db_path: Path) -> Path:
    repository = SqliteRepository.connect(db_path)
    repository.apply_source_batch(
        SourceBatch(
            source="workday",
            run_started_at=NOW,
            jobs=(
                _job("workday:td:1", "Personal Banking Associate Trainee"),
                _job("workday:td:2", "Software Engineer Intern"),
            ),
        )
    )
    repository.close()
    return db_path


@pytest.mark.asyncio
async def test_a_row_the_lexicon_now_rejects_is_moved_to_quarantine(seeded: Path) -> None:
    async with open_repository(seeded) as repository:
        result = await rescreen(repository, now=NOW)

    assert result.examined == 2
    assert result.quarantined == 1

    store = SqliteRepository.connect(seeded)
    kept = [row["id"] for row in store._conn.execute("SELECT id FROM jobs")]
    banished = [row["id"] for row in store._conn.execute("SELECT id FROM quarantine")]
    assert kept == ["workday:td:2"]
    assert banished == ["workday:td:1"]
    store.close()


@pytest.mark.asyncio
async def test_the_move_preserves_first_seen(seeded: Path) -> None:
    async with open_repository(seeded) as repository:
        await rescreen(repository, now=NOW.replace(year=2027))

    store = SqliteRepository.connect(seeded)
    row = store._conn.execute(
        "SELECT first_seen FROM quarantine WHERE id = 'workday:td:1'"
    ).fetchone()
    assert row["first_seen"].startswith("2026-08-08")
    store.close()


@pytest.mark.asyncio
async def test_a_second_pass_finds_nothing_left_to_do(seeded: Path) -> None:
    async with open_repository(seeded) as repository:
        await rescreen(repository, now=NOW)
        again = await rescreen(repository, now=NOW)
    assert again.quarantined == 0
    assert again.changed is False


@pytest.mark.asyncio
async def test_rescreen_restores_a_quarantined_title_that_the_lexicon_now_accepts(
    db_path: Path,
) -> None:
    from stage.classify.scope import Rejection, to_quarantined

    job = _job("workday:td:released", "Software Engineer Intern", role=RoleCategory.UNKNOWN)
    store = SqliteRepository.connect(db_path)
    store.apply_source_batch(
        SourceBatch(
            source="workday",
            run_started_at=NOW,
            quarantined=(
                to_quarantined(
                    job,
                    Rejection(
                        reason=RejectionReason.UNKNOWN_CS_ROLE,
                        matched_phrase="no matching CS role or source category",
                    ),
                ),
            ),
        )
    )
    store.close()

    async with open_repository(db_path) as repository:
        result = await rescreen(repository, now=NOW)

    assert result.released == 1
    store = SqliteRepository.connect(db_path)
    restored = store.get_job(job.id)
    assert restored is not None
    assert restored.role is RoleCategory.SWE
    assert store.list_quarantined(QuarantineFilters(limit=10)) == []
    store.close()


@pytest.mark.asyncio
async def test_rescreen_restores_a_quarantined_location_that_is_now_in_scope(db_path: Path) -> None:
    from dataclasses import replace

    from stage.classify.scope import Rejection, to_quarantined

    job = replace(
        _job("workday:td:tokyo", "Software Engineer Intern"),
        location_raw="Toronto, ON, Canada",
        location=LocationBucket.INTERNATIONAL,
    )
    store = SqliteRepository.connect(db_path)
    store.apply_source_batch(
        SourceBatch(
            source="workday",
            run_started_at=NOW,
            quarantined=(
                to_quarantined(
                    job,
                    Rejection(
                        reason=RejectionReason.OUT_OF_SCOPE_LOCATION,
                        matched_phrase="Toronto, ON, Canada",
                    ),
                ),
            ),
        )
    )
    store.close()

    async with open_repository(db_path) as repository:
        result = await rescreen(repository, now=NOW)

    assert result.released == 1
    store = SqliteRepository.connect(db_path)
    assert store.get_job(job.id) is not None
    store.close()


@pytest.mark.asyncio
async def test_an_empty_database_is_not_an_error(db_path: Path) -> None:
    async with open_repository(db_path) as repository:
        result = await rescreen(repository, now=NOW)
    assert (result.examined, result.quarantined) == (0, 0)


@pytest.mark.asyncio
async def test_rescreen_never_rewrites_a_stored_classification(db_path: Path) -> None:
    store = SqliteRepository.connect(db_path)
    store.apply_source_batch(
        SourceBatch(
            source="simplify",
            run_started_at=NOW,
            jobs=(
                replace(
                    _job("simplify:feed:1", "Summer Intern", role=RoleCategory.QUANT),
                    signals=SourceSignals(category="Quant"),
                ),
            ),
        )
    )
    store._conn.execute("UPDATE jobs SET term = 'summer-2027' WHERE id = 'simplify:feed:1'")
    store._conn.commit()
    store.close()

    async with open_repository(db_path) as repo:
        result = await rescreen(repo, now=NOW)

    assert result.quarantined == 0
    store = SqliteRepository.connect(db_path)
    row = store._conn.execute("SELECT term, role FROM jobs WHERE id = 'simplify:feed:1'").fetchone()
    assert (row["term"], row["role"]) == ("summer-2027", RoleCategory.QUANT.value), (
        "re-deriving term or role from a thin title would lose what the feed published"
    )
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("title", "stored", "description", "expected"),
    [
        ("Software Intern (Grad Year: Spring 2028)", "spring-2028", "", "unknown"),
        (
            "2027 Software Engineering Summer Intern",
            "spring-2029",
            "Grad date: Dec 2028 - Spring 2029",
            "summer-2027",
        ),
        ("Software Intern", "winter-2021", "Recruiting for winter 2021 is over.", "unknown"),
        ("Software Intern", "summer-2027", "", "summer-2027"),
        ("Winter Software Intern", "unknown", "Summer 2026 cohort", "unknown"),
        ("Winter Software Intern", "summer-2026", "Summer 2026 cohort", "unknown"),
    ],
)
async def test_rescreen_repairs_stored_terms_the_rules_now_read_differently(
    db_path: Path, title: str, stored: str, description: str, expected: str
) -> None:
    store = SqliteRepository.connect(db_path)
    store.apply_source_batch(
        SourceBatch(
            source="workday",
            run_started_at=NOW,
            jobs=(replace(_job("workday:td:9", title), description=description),),
        )
    )
    store._conn.execute("UPDATE jobs SET term = ? WHERE id = 'workday:td:9'", (stored,))
    store._conn.commit()
    store.close()

    async with open_repository(db_path) as repo:
        await rescreen(repo, now=NOW)

    store = SqliteRepository.connect(db_path)
    row = store._conn.execute("SELECT term FROM jobs WHERE id = 'workday:td:9'").fetchone()
    store.close()
    assert row["term"] == expected


@pytest.mark.asyncio
async def test_rescreen_recomputes_eligibility_and_language(db_path: Path) -> None:
    store = SqliteRepository.connect(db_path)
    job = replace(
        _job("workday:td:8", "Software Intern"),
        description="Candidates must be a U.S. citizen. Currently pursuing a bachelor's degree.",
    )
    store.apply_source_batch(SourceBatch(source="workday", run_started_at=NOW, jobs=(job,)))
    store._conn.execute(
        "UPDATE jobs SET work_auth_flag = 0, degree_requirement = 'unknown', language = 'unknown' "
        "WHERE id = 'workday:td:8'"
    )
    store._conn.commit()
    store.close()

    async with open_repository(db_path) as repo:
        await rescreen(repo, now=NOW)

    store = SqliteRepository.connect(db_path)
    row = store._conn.execute(
        "SELECT work_auth_flag, degree_requirement, language FROM jobs WHERE id = 'workday:td:8'"
    ).fetchone()
    store.close()
    assert row["work_auth_flag"] == 1
    assert row["degree_requirement"] != "unknown"
    assert row["language"] != "unknown"


def test_rescreen_is_reachable_from_the_command_line(seeded: Path) -> None:
    from typer.testing import CliRunner

    from stage.cli.app import app

    result = CliRunner().invoke(app, ["rescreen", "--db", str(seeded)])
    assert result.exit_code == 0, result.stdout
    assert "1 newly rejected" in result.stdout

    again = CliRunner().invoke(app, ["rescreen", "--db", str(seeded)])
    assert again.exit_code == 0, again.stdout
    assert "None changed." in again.stdout


@pytest.mark.asyncio
async def test_clearing_a_cached_validator_forces_the_next_fetch(db_path: Path) -> None:
    from stage.domain import HttpValidator
    from stage.services.maintenance import rate_state

    store = SqliteRepository.connect(db_path)
    store.apply_source_batch(
        SourceBatch(
            source="simplify",
            run_started_at=NOW,
            validators=(
                HttpValidator(
                    url="https://raw.example.test/listings.json", etag="abc", fetched_at=NOW
                ),
            ),
        )
    )
    store.close()

    async with open_repository(db_path) as repository:
        assert await repository.cached_url_count() == 1
        view = await rate_state(repository, clear_cache="simplify")
        assert view.validators_cleared == 1
        assert await repository.cached_url_count() == 0


async def test_rescreen_removes_a_stored_phd_internship_and_keeps_its_term_and_role(
    db_path: Path,
) -> None:
    from dataclasses import replace

    from stage.domain import JobFilters, QuarantineFilters, RejectionReason

    doctorate = replace(
        _job("workday:acme:1", "PhD Research Intern", role=RoleCategory.ML_AI),
        term="summer-2027",
    )
    ordinary = replace(_job("workday:acme:2", "Software Engineer Intern"), term="summer-2027")
    repository = SqliteRepository.connect(db_path)
    repository.apply_source_batch(
        SourceBatch(source="workday", run_started_at=NOW, jobs=(doctorate, ordinary))
    )
    repository.close()

    async with open_repository(db_path) as target:
        result = await rescreen(target, now=NOW)
        stored = {job.id: job for job in await target.list_jobs(JobFilters())}
        held = await target.list_quarantined(
            QuarantineFilters(reason=RejectionReason.OUT_OF_SCOPE_DEGREE)
        )

    assert result.quarantined == 1
    assert doctorate.id not in stored
    assert [entry.id for entry in held] == [doctorate.id]
    assert stored[ordinary.id].term == "summer-2027", "screening must not rewrite a term"
    assert stored[ordinary.id].role is RoleCategory.SWE, "screening must not rewrite a role"
    assert held[0].first_seen == NOW, "a move preserves first_seen"


async def test_rescreen_converges_when_quarantining_a_canonical_promotes_a_follower(
    db_path: Path,
) -> None:
    from dataclasses import replace

    from stage.domain import JobFilters, QuarantineFilters, RejectionReason

    canonical = _job("greenhouse:acme:1", "PhD Research Intern", role=RoleCategory.ML_AI)
    follower = replace(
        _job("simplify:acme:2", "Research Scientist Intern, PhD", role=RoleCategory.ML_AI),
        source="simplify",
        duplicate_of=canonical.id,
    )
    repository = SqliteRepository.connect(db_path)
    repository.apply_source_batch(
        SourceBatch(source="greenhouse", run_started_at=NOW, jobs=(canonical,))
    )
    repository.apply_source_batch(
        SourceBatch(source="simplify", run_started_at=NOW, jobs=(follower,))
    )
    repository._conn.execute(
        "UPDATE jobs SET duplicate_of = ? WHERE id = ?", (canonical.id, follower.id)
    )
    repository._conn.commit()
    repository.close()

    async with open_repository(db_path) as target:
        result = await rescreen(target, now=NOW)
        remaining = await target.list_jobs(JobFilters(status=None))
        held = await target.list_quarantined(
            QuarantineFilters(reason=RejectionReason.OUT_OF_SCOPE_DEGREE)
        )
        findings = await target.integrity_findings()

    assert result.quarantined == 2, (
        "a canonical rejected on its degree takes its description-less follower with it"
    )
    assert not remaining, "no PhD-restricted posting may survive as a promoted canonical"
    assert {entry.id for entry in held} == {canonical.id, follower.id}
    assert not [finding for finding in findings if finding.count], (
        "the move must leave no dangling duplicate link behind"
    )


@pytest.mark.asyncio
async def test_a_pass_that_cannot_read_every_row_says_so(
    seeded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.domain import JobFilters

    monkeypatch.setattr("stage.services.maintenance.RESCREEN_LIMIT", 1)
    async with open_repository(seeded) as repository:
        stored = len(await repository.list_jobs(JobFilters(status=None)))
        result = await rescreen(repository, now=NOW)

    assert stored > 1, "the fixture has to hold more rows than the cap, or nothing is skipped"
    assert result.total == stored
    assert result.examined == 1
    assert result.skipped == stored - 1, (
        "a capped pass reporting the full count is a silent truncation"
    )


@pytest.mark.asyncio
async def test_an_uncapped_pass_reports_nothing_skipped(seeded: Path) -> None:
    async with open_repository(seeded) as repository:
        result = await rescreen(repository, now=NOW)
    assert result.skipped == 0
    assert result.total == result.examined


@pytest.mark.asyncio
async def test_rescreen_quarantines_a_posting_now_seen_as_international(db_path: Path) -> None:
    from dataclasses import replace

    job = replace(
        _job("workday:td:1", "Software Engineer Intern"),
        location_raw="IN - Bangalore, India",
        location=LocationBucket.USA,
    )
    sync_repository = SqliteRepository.connect(db_path)
    sync_repository.apply_source_batch(
        SourceBatch(source="workday", run_started_at=NOW, jobs=(job,))
    )
    sync_repository.close()

    async with open_repository(db_path) as repository:
        result = await rescreen(repository, now=NOW)
        stored = await repository.get_job(job.id)

    assert result.updated == 0
    assert result.quarantined == 1
    assert stored is None


@pytest.mark.asyncio
async def test_rescreen_re_reads_a_quarantine_location_it_once_got_wrong(
    db_path: Path,
) -> None:
    from stage.classify.scope import Rejection, to_quarantined

    job = replace(
        _job("workday:bosch:erlangen", "Werkstudent Einkauf"),
        location_raw="Erlangen, BY, DE, 91058",
        location=LocationBucket.USA,
    )
    store = SqliteRepository.connect(db_path)
    store.apply_source_batch(
        SourceBatch(
            source="workday",
            run_started_at=NOW,
            quarantined=(
                to_quarantined(
                    job,
                    Rejection(
                        reason=RejectionReason.NOT_AN_INTERNSHIP,
                        matched_phrase="no internship marker in title",
                    ),
                ),
            ),
        )
    )
    store.close()

    async with open_repository(db_path) as repository:
        result = await rescreen(repository, now=NOW)
        entries = await repository.list_quarantined(QuarantineFilters(limit=10))

    assert result.relocated == 1, "a stale bucket on a non-internship row is still refreshed"
    assert entries[0].location is not LocationBucket.USA, "Bavaria is not Delaware"


@pytest.mark.asyncio
async def test_a_correct_quarantine_location_is_left_alone(db_path: Path) -> None:
    from stage.classify.scope import Rejection, to_quarantined

    job = replace(
        _job("workday:acme:boston", "Warehouse Associate"),
        location_raw="Boston, MA",
        location=LocationBucket.USA,
    )
    store = SqliteRepository.connect(db_path)
    store.apply_source_batch(
        SourceBatch(
            source="workday",
            run_started_at=NOW,
            quarantined=(
                to_quarantined(
                    job,
                    Rejection(
                        reason=RejectionReason.NOT_A_CS_ROLE, matched_phrase="warehouse associate"
                    ),
                ),
            ),
        )
    )
    store.close()

    async with open_repository(db_path) as repository:
        result = await rescreen(repository, now=NOW)

    assert result.relocated == 0, "a bucket that already agrees must not be rewritten"


@pytest.mark.parametrize("command", [["rescreen"], ["purge"]])
def test_maintenance_waits_for_a_running_sync_and_then_gives_up_plainly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    from typer.testing import CliRunner

    from stage.cli import app as cli_app
    from stage.cli.commands import pipeline
    from stage.runlock import run_lock_path, single_run

    database = tmp_path / "stage.db"
    monkeypatch.setattr(pipeline, "MAINTENANCE_WAIT_SECONDS", 0.0)

    with single_run("sync", run_lock_path(database)):
        result = CliRunner().invoke(cli_app.app, [*command, "--db", str(database)])

    said = " ".join(result.stderr.split())
    assert result.exit_code == 2
    assert f"run stage {command[0]} again when it finishes" in said.lower()
    assert not database.exists()


@pytest.mark.asyncio
async def test_a_quarantined_place_is_read_from_its_link_and_title_when_the_text_says_nothing(
    db_path: Path,
) -> None:
    from stage.classify.scope import Rejection, to_quarantined

    linked = replace(
        _job("workday:truist:1", "Teller"),
        location_raw="2 Locations",
        apply_url_raw="https://truist.wd1.myworkdayjobs.com/en-US/Careers/job/Charlotte-NC/Teller_R1",
        location=LocationBucket.UNKNOWN,
    )
    titled = replace(
        _job("workday:thales:2", "STAGE - Ingenieur logiciel F/H"),
        location_raw="Saint-Heand",
        location=LocationBucket.UNKNOWN,
    )
    rejection = Rejection(reason=RejectionReason.NOT_AN_INTERNSHIP, matched_phrase="x")
    store = SqliteRepository.connect(db_path)
    store.apply_source_batch(
        SourceBatch(
            source="workday",
            run_started_at=NOW,
            quarantined=tuple(to_quarantined(job, rejection) for job in (linked, titled)),
        )
    )
    store.close()

    async with open_repository(db_path) as repository:
        await rescreen(repository, now=NOW)
        entries = {
            entry.id: entry.location
            for entry in await repository.list_quarantined(QuarantineFilters(limit=10))
        }

    assert entries["workday:truist:1"] is LocationBucket.USA
    assert entries["workday:thales:2"] is LocationBucket.INTERNATIONAL


@pytest.mark.asyncio
async def test_rescreen_links_copies_an_older_stage_left_apart(db_path: Path) -> None:
    from stage.normalize import canonical_apply_url

    site = "https://kla.wd1.myworkdayjobs.com"
    board = replace(
        _job("workday:kla-ur:1", "Software Engineer Intern"),
        company="KLA",
        apply_url_raw=f"{site}/UR/job/Milpitas-CA/Software-Engineer-Intern_2642721",
    )
    listed = replace(
        board,
        id="simplify:kla:2",
        source="simplify",
        company="KLA Corporation",
        apply_url_raw=f"{site}/search/job/Milpitas-CA/Software-Engineer-Intern_2642721-1",
    )
    store = SqliteRepository.connect(db_path)
    for job in (board, listed):
        stored = replace(job, apply_url_canonical=job.apply_url_raw)
        store.apply_source_batch(SourceBatch(source=job.source, run_started_at=NOW, jobs=(stored,)))
    assert store.count_duplicates() == 0
    store.close()

    async with open_repository(db_path) as repository:
        result = await rescreen(repository, now=NOW)
        assert await repository.count_duplicates() == 1
        again = await rescreen(repository, now=NOW)

    assert canonical_apply_url(board.apply_url_raw) == canonical_apply_url(listed.apply_url_raw)
    assert result.relinked == 1 and again.relinked == 0


async def test_rescreen_re_reads_a_copy_that_is_already_linked(db_path: Path) -> None:
    board = replace(
        _job("greenhouse:bss:1", "Software Engineering Intern"),
        source="greenhouse",
        company="Benchmark Space Systems",
        location_raw="Burlington, VT",
        location=LocationBucket.USA,
    )
    copy = replace(
        board,
        id="speedyapply:bss:2",
        source="speedyapply",
        location=LocationBucket.CANADA,
        duplicate_of=board.id,
    )
    store = SqliteRepository.connect(db_path)
    for job in (board, copy):
        store.apply_source_batch(SourceBatch(source=job.source, run_started_at=NOW, jobs=(job,)))
    store.close()

    async with open_repository(db_path) as repository:
        await rescreen(repository, now=NOW)
        kept = await repository.get_job(copy.id)

    assert kept is not None and kept.location is LocationBucket.USA


async def test_rescreen_undoes_a_link_between_two_cities_an_older_stage_made(
    db_path: Path,
) -> None:
    first = replace(
        _job("greenhouse:ice:1", "Software Engineering Intern"),
        source="greenhouse",
        company="ICE",
        location_raw="Atlanta, GA",
        location=LocationBucket.USA,
    )
    second = replace(
        first,
        id="simplify:ice:2",
        source="simplify",
        location_raw="New York, NY",
        apply_url_raw="https://boards.example.test/ice-ny",
        duplicate_of=first.id,
    )
    store = SqliteRepository.connect(db_path)
    for job in (first, second):
        store.apply_source_batch(SourceBatch(source=job.source, run_started_at=NOW, jobs=(job,)))
    assert store.count_duplicates() == 1
    store.close()

    async with open_repository(db_path) as repository:
        await rescreen(repository, now=NOW)
        assert await repository.count_duplicates() == 0


async def test_a_database_from_an_older_stage_is_checked_once_on_its_first_sync(
    seeded: Path,
) -> None:
    from stage.services.maintenance import rescreen, upgrade_pending

    store = SqliteRepository.connect(seeded)
    store.mark_screened(0)
    store.close()

    async with open_repository(seeded) as repository:
        pending = await upgrade_pending(repository)
        first = await rescreen(repository, now=NOW)
        again = await upgrade_pending(repository)

    assert pending and first.quarantined == 1 and not again


def test_a_new_database_starts_out_screened_by_this_version(db_path: Path) -> None:
    from stage.paths import rules_stamp

    store = SqliteRepository.connect(db_path)
    try:
        assert store.screened_with() == rules_stamp()
    finally:
        store.close()


def test_a_changed_word_list_asks_for_a_new_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    from stage.paths import lexicon_dir, rules_stamp

    before = rules_stamp()
    copy = tmp_path / "lexicon"
    shutil.copytree(lexicon_dir(), copy)
    roles = copy / "roles.yaml"
    roles.write_text(roles.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    monkeypatch.setenv("STAGE_LEXICON", str(copy))

    assert rules_stamp() != before


@pytest.mark.asyncio
async def test_a_stored_term_over_before_the_posting_was_seen_is_cleared(db_path: Path) -> None:
    seen = datetime(2026, 11, 5, tzinfo=UTC)
    labelled = replace(_job("workday:td:3", "AI IT Intern"), term="summer-2026")
    named = replace(
        _job("workday:td:4", "Summer 2026 Software Intern"),
        term="summer-2026",
        source_posted_at=seen,
    )
    current = replace(_job("workday:td:5", "Software Intern"), term="summer-2027")
    store = SqliteRepository.connect(db_path)
    store.apply_source_batch(
        SourceBatch(
            source="workday",
            run_started_at=seen,
            jobs=tuple(
                replace(job, first_seen=seen, last_seen=seen) for job in (labelled, named, current)
            ),
        )
    )
    store.close()
    async with open_repository(db_path) as repository:
        await rescreen(repository, now=seen)

    store = SqliteRepository.connect(db_path)
    terms = dict(store._conn.execute("SELECT id, term FROM jobs").fetchall())
    store.close()
    assert terms == {
        "workday:td:3": "unknown",
        "workday:td:4": "summer-2026",
        "workday:td:5": "summer-2027",
    }, "only a term no title or description names is a stale label"
