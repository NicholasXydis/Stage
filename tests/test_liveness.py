import asyncio
import json
import random
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from stage.domain import (
    CompanyVisit,
    Job,
    JobFilters,
    JobStatus,
    RateState,
    RoleCategory,
)
from stage.domain.liveness import Liveness, LivenessCheck
from stage.http import HostBudget, RatePosture
from stage.http import client as http_client
from stage.services import liveness
from stage.services.liveness import check_opened, check_still_open, listed_target, target_for
from stage.storage import SourceBatch, open_repository
from stage.storage.sqlite_repo import SqliteRepository

ANSWERS: dict[str, dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "liveness_answers.json").read_text(encoding="utf-8")
)
GH = "https://boards-api.greenhouse.io/v1/boards"
LEVER = "https://api.lever.co/v0/postings"
SR = "https://api.smartrecruiters.com/v1/companies"
WD = "https://abb.wd3.myworkdayjobs.com"
WD_JOB = "/job/Milpitas-California-USA/AI-Robotics-Intern--Fall-2026_JR00044846"
UUID = "da8cc185-70d0-4a8e-b484-9866c71b593e"
GH_BUCKET = "boards-api.greenhouse.io"
BUCKETS = (GH_BUCKET, "api.lever.co", "api.smartrecruiters.com", "workday")
UNPACED = {
    bucket: RatePosture(concurrency=4, min_interval_s=0.0, max_requests_per_run=100)
    for bucket in BUCKETS
}
FEEDS = frozenset({"simplify", "speedyapply", "vanshb03"})


def _answer(name: str, **headers: str) -> httpx.Response:
    found = ANSWERS[name]
    return httpx.Response(found["status"], json=found["body"], headers=headers)


_WAL = "https://wal.wd1.myworkdayjobs.com/WAB/job/Dallas-TX/Intern"


def _job(
    ident: str,
    url: str = "",
    *,
    source: str = "",
    seen: datetime | None = None,
    duplicate_of: str | None = None,
    description: str = "",
) -> Job:
    moment = seen or datetime.now(UTC) - timedelta(hours=1)
    return Job(
        id=ident,
        source=source or ident.split(":")[0],
        company="Acme",
        title_raw="Software Intern",
        title_normalized="software intern",
        apply_url_raw=url or f"https://example.com/{ident}",
        description=description,
        first_seen=moment,
        last_seen=moment,
        role=RoleCategory.SWE,
        term="summer-2027",
        duplicate_of=duplicate_of,
    )


GREENHOUSE = _job("greenhouse:acme:5268689007")
LEVER_JOB = _job(f"lever:cfsenergy:{UUID}", f"https://jobs.lever.co/cfsenergy/{UUID}")
SR_JOB = _job("smartrecruiters:abbvie:3743990016416916")
WORKDAY = _job(
    "workday:abb-external_career_page:jr00044846",
    f"https://abb.wd3.myworkdayjobs.com/external_career_page{WD_JOB}",
)


def _store(path: Path, *batches: SourceBatch) -> Path:
    repository = SqliteRepository.connect(path)
    for batch in batches:
        repository.apply_source_batch(batch)
    repository.close()
    return path


def _batch(source: str, *jobs: Job, at: datetime | None = None, **kwargs: Any) -> SourceBatch:
    started = at or datetime.now(UTC) - timedelta(hours=1)
    return SourceBatch(source=source, run_started_at=started, jobs=jobs, **kwargs)


def _rows(path: Path, sql: str, *params: object) -> list[tuple[Any, ...]]:
    connection = sqlite3.connect(path)
    try:
        return [tuple(row) for row in connection.execute(sql, params).fetchall()]
    finally:
        connection.close()


def _status(path: Path, ident: str) -> str:
    return str(_rows(path, "SELECT status FROM jobs WHERE id = ?", ident)[0][0])


def _checks(path: Path) -> dict[str, tuple[int, int]]:
    rows = _rows(path, "SELECT id, resolved, failed FROM detail_fetches WHERE id GLOB 'check:*'")
    return {str(ident): (int(resolved), int(failed)) for ident, resolved, failed in rows}


async def _verdict(
    job: Job,
    *,
    rate_state: dict[str, RateState] | None = None,
    budgets: dict[str, HostBudget] | None = None,
) -> tuple[Liveness | None, liveness.Probe]:
    target = target_for(job, FEEDS)
    assert target is not None
    probe = await liveness._probe(
        [(job, target)],
        rate_state=rate_state or {},
        budgets={} if budgets is None else budgets,
        postures=UNPACED,
        now=datetime.now(UTC),
        time_limit_s=10.0,
        rng=random.Random(0),
    )
    return (probe.checks[0].verdict if probe.checks else None), probe


@pytest.fixture(autouse=True)
def quick_retries(live_checks: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http_client, "wait_exponential_jitter", lambda **_: lambda _: 0)


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as mocked:
        yield mocked


@pytest.mark.parametrize(
    ("job", "posting", "board", "listed"),
    [
        (GREENHOUSE, f"{GH}/acme/jobs/5268689007", f"{GH}/acme", "greenhouse"),
        (LEVER_JOB, f"{LEVER}/cfsenergy/{UUID}", f"{LEVER}/cfsenergy", "lever"),
        (SR_JOB, f"{SR}/abbvie/postings/3743990016416916", f"{SR}/abbvie/postings", "sr"),
    ],
)
@pytest.mark.parametrize(
    ("answer", "board_answer", "expected"),
    [
        ("open", None, Liveness.OPEN),
        ("missing", "board", Liveness.GONE),
        ("missing", "board_missing", Liveness.UNKNOWN),
        ("missing", 503, Liveness.UNKNOWN),
        (403, None, Liveness.UNKNOWN),
        ("malformed", None, Liveness.UNKNOWN),
    ],
)
async def test_a_board_posting_is_gone_only_when_its_board_still_answers(
    router: respx.MockRouter,
    job: Job,
    posting: str,
    board: str,
    listed: str,
    answer: str | int,
    board_answer: str | int | None,
    expected: Liveness,
) -> None:
    names = {
        "greenhouse": ("greenhouse_job", "greenhouse_board", "greenhouse_board_missing"),
        "lever": ("lever_posting", "lever_board", "lever_board_missing"),
        "sr": ("smartrecruiters_posting", "smartrecruiters_board", "smartrecruiters_board_unknown"),
    }[listed]
    missing = {"greenhouse": "greenhouse_job_missing", "lever": "lever_posting_missing"}.get(
        listed, "smartrecruiters_posting_missing"
    )
    responses = {
        "open": _answer(names[0]),
        "missing": _answer(missing),
        "malformed": httpx.Response(200, json=["not", "a", "posting"]),
        403: httpx.Response(403, text="<html>denied</html>"),
    }
    asked = router.get(posting).mock(return_value=responses[answer])
    boards = {
        "board": _answer(names[1]),
        "board_missing": (
            httpx.Response(404, json={"error": "gone"}) if listed == "sr" else _answer(names[2])
        ),
        503: httpx.Response(503, headers={"Retry-After": "0"}),
    }
    checked = router.get(board).mock(
        return_value=boards[board_answer] if board_answer is not None else httpx.Response(500)
    )

    verdict, _ = await _verdict(job)

    assert verdict is expected
    assert asked.call_count == 1
    assert checked.called is (board_answer is not None)


async def test_a_smartrecruiters_company_with_no_postings_proves_nothing(
    router: respx.MockRouter,
) -> None:
    router.get(f"{SR}/abbvie/postings/3743990016416916").mock(
        return_value=_answer("smartrecruiters_posting_missing")
    )
    router.get(f"{SR}/abbvie/postings").mock(return_value=_answer("smartrecruiters_board_unknown"))

    verdict, _ = await _verdict(SR_JOB)

    assert verdict is Liveness.UNKNOWN


async def test_the_board_is_asked_once_per_check_session(router: respx.MockRouter) -> None:
    jobs = [_job(f"greenhouse:acme:{n}") for n in (11, 12, 13)]
    for job in jobs:
        router.get(f"{GH}/acme/jobs/{job.id.rsplit(':', 1)[1]}").mock(
            return_value=_answer("greenhouse_job_missing")
        )
    board = router.get(f"{GH}/acme").mock(return_value=_answer("greenhouse_board"))
    chosen = [(job, target) for job in jobs if (target := target_for(job, FEEDS)) is not None]

    probe = await liveness._probe(
        chosen, rate_state={}, budgets={}, postures=UNPACED, now=datetime.now(UTC), time_limit_s=5
    )

    assert [check.verdict for check in probe.checks] == [Liveness.GONE] * 3
    assert board.call_count == 1


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (_answer("workday_posting"), Liveness.OPEN),
        (_answer("workday_posting_removed"), Liveness.GONE),
        (_answer("workday_anchor_not_found"), Liveness.UNKNOWN),
        (httpx.Response(403, text="<html>Access denied</html>"), Liveness.UNKNOWN),
        (httpx.Response(200, json={"jobPostingInfo": {"posted": False}}), Liveness.UNKNOWN),
        (httpx.Response(200, text="<html>maintenance</html>"), Liveness.UNKNOWN),
    ],
)
async def test_workday_is_gone_only_on_its_own_removed_posting_answer(
    router: respx.MockRouter, response: httpx.Response, expected: Liveness
) -> None:
    asked = router.get(f"{WD}/wday/cxs/abb/external_career_page{WD_JOB}").mock(
        return_value=response
    )

    verdict, probe = await _verdict(WORKDAY)

    assert verdict is expected
    assert asked.call_count == 1
    assert [request.status for request in probe.requests] == [
        410 if expected is Liveness.GONE else response.status_code
    ]


async def test_removed_workday_postings_never_block_the_shared_bucket(
    router: respx.MockRouter,
) -> None:
    tenants = (("abb", "wd3"), ("kla", "wd1"), ("cibc", "wd3"))
    jobs = []
    for n, (tenant, dc) in enumerate(tenants):
        host = f"https://{tenant}.{dc}.myworkdayjobs.com"
        jobs.append(_job(f"simplify:feed:{n}", f"{host}/External/job/X/Intern_R{n}"))
        router.get(f"{host}/wday/cxs/{tenant}/External/job/X/Intern_R{n}").mock(
            return_value=_answer("workday_posting_removed")
        )
    budgets: dict[str, HostBudget] = {}
    chosen = [(job, target) for job in jobs if (target := target_for(job, FEEDS)) is not None]

    probe = await liveness._probe(
        chosen,
        rate_state={},
        budgets=budgets,
        postures=UNPACED,
        now=datetime.now(UTC),
        time_limit_s=5,
    )

    assert [check.verdict for check in probe.checks] == [Liveness.GONE] * 3
    assert budgets["workday"].deferred_s == 0
    tenants_seen = budgets["workday"].hosts
    assert {"kla.wd1.myworkdayjobs.com", "wd1.myworkdayjobs.com"} <= set(tenants_seen)
    assert not any(health.breaker.consecutive_failures for health in tenants_seen.values())
    assert probe.pushback == ()


EVERY_BOARD = [GREENHOUSE, LEVER_JOB, SR_JOB, WORKDAY]


@pytest.mark.parametrize("job", EVERY_BOARD)
async def test_a_429_is_unknown_and_its_retry_after_is_kept(
    router: respx.MockRouter, job: Job
) -> None:
    target = target_for(job, FEEDS)
    assert target is not None
    asked = router.get(target.url).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "120"})
    )

    verdict, probe = await _verdict(job)

    assert verdict is Liveness.UNKNOWN
    assert asked.call_count == 1, "a long Retry-After stops the bucket instead of retrying"
    tenant = [target.host] if target.bucket != target.host else []
    assert [state.bucket for state in probe.pushback] == [target.bucket, *tenant]
    assert probe.pushback[0].blocked_until is not None
    assert all(state.consecutive_failures == 1 for state in probe.pushback[1:]), (
        "a shared bucket's failure is counted on the tenant that failed"
    )


@pytest.mark.parametrize("job", EVERY_BOARD)
async def test_a_timeout_is_unknown_and_not_recorded(router: respx.MockRouter, job: Job) -> None:
    target = target_for(job, FEEDS)
    assert target is not None
    router.get(target.url).mock(side_effect=httpx.ReadTimeout("slow"))

    verdict, probe = await _verdict(job)

    assert verdict is None
    assert probe.checks == ()
    assert all(request.status is None for request in probe.requests)


async def test_a_blocked_bucket_sends_nothing(router: respx.MockRouter) -> None:
    now = datetime.now(UTC)
    asked = router.get(f"{GH}/acme/jobs/5268689007").mock(return_value=_answer("greenhouse_job"))
    blocked = RateState(
        bucket="boards-api.greenhouse.io",
        updated_at=now,
        blocked_until=now + timedelta(hours=1),
        reason="HTTP 429",
    )

    verdict, probe = await _verdict(GREENHOUSE, rate_state={blocked.bucket: blocked})

    assert verdict is None
    assert not asked.called
    assert probe.requests == () and probe.pushback == ()


async def test_a_spent_host_budget_sends_nothing(router: respx.MockRouter) -> None:
    asked = router.get(f"{GH}/acme/jobs/5268689007").mock(return_value=_answer("greenhouse_job"))
    spent = HostBudget(posture=UNPACED["boards-api.greenhouse.io"])
    spent.requests = spent.posture.max_requests_per_run

    verdict, _ = await _verdict(GREENHOUSE, budgets={"boards-api.greenhouse.io": spent})

    assert verdict is None
    assert not asked.called


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://job-boards.greenhouse.io/insomniac/jobs/6226611004",
            f"{GH}/insomniac/jobs/6226611004",
        ),
        (
            "https://boards.greenhouse.io/pathrobotics/jobs/8924241002?gh_jid=8924241002",
            f"{GH}/pathrobotics/jobs/8924241002",
        ),
        (
            "https://jobs.lever.co/palantir/da774f81-c98c-4dcf-9b9c-949fd7d90a13/apply",
            f"{LEVER}/palantir/da774f81-c98c-4dcf-9b9c-949fd7d90a13",
        ),
        (
            "https://jobs.smartrecruiters.com/BoschGroup/744000156311682-software-engineering"
            "-co-op-spring-2027?oga=true",
            f"{SR}/boschgroup/postings/744000156311682",
        ),
        (
            "https://pwc.wd3.myworkdayjobs.com/en-US/nonpublic_postings/job/IL-Rosemont/"
            "Software-Engineering---Intern_757092WD/apply",
            "https://pwc.wd3.myworkdayjobs.com/wday/cxs/pwc/nonpublic_postings/job/IL-Rosemont/"
            "Software-Engineering---Intern_757092WD",
        ),
        ("https://go.vansh.nyc/c7", None),
        ("https://jobs.ashbyhq.com/acme/0b0e2d5e-0000-4000-8000-000000000000", None),
        ("https://jobs.eu.lever.co/acme/da774f81-c98c-4dcf-9b9c-949fd7d90a13", None),
        ("https://boards.greenhouse.io/embed/job_app?token=8300829", None),
        ("https://acme.wd1.myworkdayjobs.com/External/job/../../wday/admin", None),
        ("https://acme.wd1.myworkdayjobs.com/External/details/Intern_R1", None),
        ("https://careers.acme.com/jobs/123?gh_jid=123", None),
    ],
)
def test_community_links_map_only_to_a_board_stage_can_ask(url: str, expected: str | None) -> None:
    target = listed_target(url)
    assert (target.url if target is not None else None) == expected


def test_only_feeds_and_the_four_boards_are_checkable() -> None:
    linked = "https://job-boards.greenhouse.io/acme/jobs/1"
    assert target_for(_job("simplify:feed:1", linked), FEEDS) is not None
    assert target_for(_job("custom_json:acme:1", linked), FEEDS) is None
    assert target_for(_job("ashby:acme:1"), FEEDS) is None
    assert target_for(_job("greenhouse:acme:not-a-number"), FEEDS) is None


def _queue_db(path: Path, now: datetime) -> Path:
    earlier = now - timedelta(hours=3)
    later = now - timedelta(hours=1)
    gh = "https://job-boards.greenhouse.io/acme/jobs/"
    _store(
        path,
        _batch(
            "greenhouse",
            _job("greenhouse:acme:1", seen=earlier),
            _job("greenhouse:acme:2", seen=earlier),
            at=earlier,
        ),
        _batch(
            "greenhouse",
            _job("greenhouse:acme:2", seen=later),
            at=later,
            visits=(CompanyVisit(board="greenhouse:acme", succeeded=True),),
        ),
        _batch("ashby", _job("ashby:acme:9", seen=earlier), at=earlier),
        _batch(
            "simplify",
            _job("simplify:feed:10", f"{gh}10", seen=later),
            _job("simplify:feed:11", f"{gh}11", seen=later),
            _job(
                "simplify:feed:12", "https://boards.greenhouse.io/embed/job_app?token=8", seen=later
            ),
            _job("simplify:feed:13", f"{gh}13", seen=later, duplicate_of="greenhouse:acme:2"),
            _job("simplify:feed:14", f"{gh}14", seen=now - timedelta(days=20)),
            _job("simplify:feed:15", f"{gh}15", seen=later),
            _job("vanshb03:feed:16", "https://go.vansh.nyc/c7", seen=later),
            at=later,
        ),
    )
    repository = SqliteRepository.connect(path)
    repository.record_checks(
        (
            LivenessCheck("simplify:feed:10", Liveness.OPEN, now - timedelta(hours=7)),
            LivenessCheck("simplify:feed:11", Liveness.UNKNOWN, now - timedelta(hours=2)),
        )
    )
    repository.close()
    return path


async def test_the_queue_takes_stale_board_rows_and_list_rows_with_board_links(
    db_path: Path,
) -> None:
    now = datetime.now(UTC)
    _queue_db(db_path, now)
    async with open_repository(db_path) as repository:
        queued = await repository.liveness_queue(
            now - timedelta(days=14),
            now - timedelta(hours=6),
            FEEDS,
            frozenset({"greenhouse", "lever", "smartrecruiters", "workday"}),
            liveness.URL_MARKS,
        )

    assert [job.id for job in queued] == [
        "greenhouse:acme:1",
        "simplify:feed:12",
        "simplify:feed:15",
        "simplify:feed:10",
    ]


async def test_a_sync_checks_its_queue_up_to_the_limit_and_closes_the_gone(
    db_path: Path, router: respx.MockRouter
) -> None:
    now = datetime.now(UTC)
    _queue_db(db_path, now)
    router.get(f"{GH}/acme/jobs/1").mock(return_value=_answer("greenhouse_job_missing"))
    router.get(f"{GH}/acme").mock(return_value=_answer("greenhouse_board"))
    router.get(f"{GH}/acme/jobs/15").mock(return_value=_answer("greenhouse_job"))
    router.get(f"{GH}/acme/jobs/10").mock(return_value=httpx.Response(500))

    async with open_repository(db_path) as repository:
        result = await check_still_open(
            repository, rate_state={}, budgets={}, postures=UNPACED, now=now, limit=2
        )
        first = _checks(db_path)
        again = await check_still_open(
            repository, rate_state={}, budgets={}, postures=UNPACED, now=now, limit=2
        )

    assert (result.checked, result.closed, result.unknown) == (2, 1, 0)
    assert {request.source for request in result.requests} == {"greenhouse"}
    assert _status(db_path, "greenhouse:acme:1") == "closed"
    assert first == {
        "check:greenhouse:acme:1": (0, 0),
        "check:simplify:feed:10": (1, 0),
        "check:simplify:feed:11": (0, 1),
        "check:simplify:feed:15": (1, 0),
    }
    assert (again.checked, again.closed, again.unknown) == (1, 0, 1)
    assert _checks(db_path)["check:simplify:feed:10"] == (0, 1)


async def test_a_gone_posting_closes_its_copies_and_a_list_never_brings_them_back(
    db_path: Path,
) -> None:
    now = datetime.now(UTC)
    copy = _job("simplify:feed:1", seen=now - timedelta(hours=2), duplicate_of="greenhouse:acme:1")
    _store(
        db_path,
        _batch("greenhouse", _job("greenhouse:acme:1", seen=now - timedelta(hours=2))),
        _batch("simplify", copy, at=now - timedelta(hours=2)),
    )
    async with open_repository(db_path) as repository:
        closed = await repository.record_checks(
            (LivenessCheck("greenhouse:acme:1", Liveness.GONE, now),)
        )
        await repository.apply_source_batch(
            _batch("simplify", replace(copy, duplicate_of=None), at=now + timedelta(minutes=5))
        )
        listed = await repository.list_jobs(JobFilters(status=JobStatus.OPEN))

    assert closed == ("greenhouse:acme:1", "simplify:feed:1")
    assert _status(db_path, "simplify:feed:1") == "closed"
    assert listed == []


async def test_a_gone_posting_leaves_a_copy_another_board_still_lists(db_path: Path) -> None:
    now = datetime.now(UTC)
    board_copy = _job(
        "ashby:acme:1", seen=now - timedelta(hours=2), duplicate_of="greenhouse:acme:1"
    )
    _store(
        db_path,
        _batch("greenhouse", _job("greenhouse:acme:1", seen=now - timedelta(hours=2))),
        _batch("ashby", board_copy, at=now - timedelta(hours=2)),
    )
    async with open_repository(db_path) as repository:
        closed = await repository.record_checks(
            (LivenessCheck("greenhouse:acme:1", Liveness.GONE, now),)
        )
        await repository.apply_source_batch(
            _batch(
                "ashby",
                replace(board_copy, last_seen=now + timedelta(minutes=5)),
                at=now + timedelta(minutes=5),
            )
        )

    assert closed == ("greenhouse:acme:1",)
    assert _status(db_path, "ashby:acme:1") == "open"


async def test_a_gone_posting_closes_only_list_copies_of_the_same_job_number(
    db_path: Path,
) -> None:
    now = datetime.now(UTC)
    earlier = now - timedelta(hours=2)
    main = _job("workday:wal:r14019", f"{_WAL}_R14019", seen=earlier)
    same = _job("simplify:wal:1", f"{_WAL}_R14019", seen=earlier, duplicate_of=main.id)
    other = _job("zshah101:wal:2", f"{_WAL}_R14020", seen=earlier, duplicate_of=main.id)
    _store(
        db_path,
        _batch("workday", main, at=earlier),
        _batch("simplify", same, at=earlier),
        _batch("zshah101", other, at=earlier),
    )
    async with open_repository(db_path) as repository:
        closed = await repository.record_checks((LivenessCheck(main.id, Liveness.GONE, now),))

    assert closed == (same.id, main.id)
    assert _status(db_path, other.id) == "open", "another job number was never checked"
    assert _rows(db_path, "SELECT duplicate_of FROM jobs WHERE id = ?", other.id) == [(None,)]


async def test_a_gone_check_makes_the_open_board_copy_the_main_at_once(db_path: Path) -> None:
    now = datetime.now(UTC)
    _store(
        db_path,
        _batch("greenhouse", _job("greenhouse:acme:1", seen=now - timedelta(hours=2))),
        _batch(
            "ashby",
            _job("ashby:acme:1", seen=now - timedelta(hours=2), duplicate_of="greenhouse:acme:1"),
            at=now - timedelta(hours=2),
        ),
    )
    async with open_repository(db_path) as repository:
        await repository.record_checks((LivenessCheck("greenhouse:acme:1", Liveness.GONE, now),))

    assert _rows(db_path, "SELECT duplicate_of FROM jobs WHERE id = 'ashby:acme:1'") == [(None,)]
    heir = "SELECT source FROM detail_fetches WHERE id = 'heir:greenhouse:acme:1'"
    assert _rows(db_path, heir) == [("ashby:acme:1",)], "tracking follows the posting"


@pytest.mark.parametrize(("minutes", "status"), [(-1, "closed"), (0, "closed"), (1, "open")])
async def test_a_board_read_reopens_a_checked_posting_only_if_it_started_after_the_check(
    db_path: Path, minutes: int, status: str
) -> None:
    now = datetime.now(UTC)
    job = _job("greenhouse:acme:1", seen=now - timedelta(hours=2))
    _store(db_path, _batch("greenhouse", job, at=now - timedelta(hours=2)))
    async with open_repository(db_path) as repository:
        await repository.record_checks((LivenessCheck(job.id, Liveness.GONE, now),))
        started = now + timedelta(minutes=minutes)
        await repository.apply_source_batch(
            _batch("greenhouse", replace(job, last_seen=started), at=started)
        )

    assert _status(db_path, job.id) == status


async def test_purge_forgets_the_checks_of_purged_postings(db_path: Path) -> None:
    now = datetime.now(UTC)
    _store(
        db_path,
        _batch("greenhouse", _job("greenhouse:acme:1"), _job("greenhouse:acme:2")),
    )
    async with open_repository(db_path) as repository:
        await repository.record_checks(
            (
                LivenessCheck("greenhouse:acme:1", Liveness.GONE, now),
                LivenessCheck("greenhouse:acme:2", Liveness.OPEN, now),
            )
        )
        early = await repository.purge(now)
        kept = _checks(db_path)
        purged = await repository.purge(now + timedelta(days=5))

    assert early.purged == 0 and set(kept) == {"check:greenhouse:acme:1", "check:greenhouse:acme:2"}
    assert purged.purged == 1
    assert _checks(db_path) == {"check:greenhouse:acme:2": (1, 0)}


async def test_checks_never_enter_the_description_queue(db_path: Path) -> None:
    now = datetime.now(UTC)
    job = replace(_job("smartrecruiters:acme:1"), term="unknown", role=RoleCategory.UNKNOWN)
    _store(db_path, _batch("smartrecruiters", job))
    async with open_repository(db_path) as repository:
        await repository.record_checks((LivenessCheck(job.id, Liveness.OPEN, now),))
        queued = await repository.detail_queue("smartrecruiters", 10)
        size = await repository.detail_queue_size("smartrecruiters")

    assert queued == [job.id] and size == 1


async def test_a_check_keeps_the_stored_rotation_cursor(db_path: Path) -> None:
    now = datetime.now(UTC)
    _store(
        db_path,
        _batch(
            "greenhouse",
            rate_state=(
                RateState(bucket="boards-api.greenhouse.io", updated_at=now, rotation_cursor="k"),
            ),
        ),
    )
    pushback = RateState(
        bucket="boards-api.greenhouse.io",
        updated_at=now,
        blocked_until=now + timedelta(minutes=2),
        consecutive_failures=1,
        reason="Retry-After: 120s",
    )
    async with open_repository(db_path) as repository:
        await repository.record_checks((), (pushback,))
        stored = (await repository.load_rate_state())["boards-api.greenhouse.io"]

    assert stored.rotation_cursor == "k"
    assert stored.blocked_until == pushback.blocked_until
    assert stored.reason == "Retry-After: 120s"


@pytest.fixture
def opened(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    import webbrowser

    events: list[str] = []

    def record(url: str, *_: object, **__: object) -> bool:
        events.append(f"browser {url}")
        return True

    monkeypatch.setattr(webbrowser, "open", record)
    monkeypatch.setattr(liveness, "open_postures", lambda: dict(UNPACED))
    _store(db_path, _batch("greenhouse", GREENHOUSE))
    return events


def _gone_routes(router: respx.MockRouter, events: list[str]) -> None:
    def answer(name: str) -> Any:
        def respond(request: httpx.Request) -> httpx.Response:
            events.append(f"http {request.url.path}")
            return _answer(name)

        return respond

    router.get(f"{GH}/acme/jobs/5268689007").mock(side_effect=answer("greenhouse_job_missing"))
    router.get(f"{GH}/acme").mock(side_effect=answer("greenhouse_board"))


def test_stage_open_launches_the_tab_before_it_asks_the_board(
    db_path: Path, opened: list[str], router: respx.MockRouter
) -> None:
    from typer.testing import CliRunner

    from stage.cli.app import app

    _gone_routes(router, opened)

    result = CliRunner().invoke(app, ["open", GREENHOUSE.id, "--db", str(db_path)])

    assert result.exit_code == 0, result.stdout
    assert opened[0].startswith("browser ")
    assert opened[1:] == ["http /v1/boards/acme/jobs/5268689007", "http /v1/boards/acme"]
    assert "Acme no longer lists Software Intern, so Stage marked it closed." in result.stdout
    assert _status(db_path, GREENHOUSE.id) == "closed"


def test_stage_open_print_asks_nothing(
    db_path: Path, opened: list[str], router: respx.MockRouter
) -> None:
    from typer.testing import CliRunner

    from stage.cli.app import app

    _gone_routes(router, opened)

    result = CliRunner().invoke(app, ["open", GREENHOUSE.id, "--print", "--db", str(db_path)])

    assert result.exit_code == 0 and opened == []
    assert _status(db_path, GREENHOUSE.id) == "open"


async def test_an_open_check_gives_up_quietly_while_another_writer_holds_the_database(
    db_path: Path, opened: list[str], router: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    _gone_routes(router, opened)
    monkeypatch.setattr(liveness, "OPEN_WAIT_MS", 50)
    holder = sqlite3.connect(db_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        result = await check_opened(db_path, [GREENHOUSE.id])
    finally:
        holder.rollback()
        holder.close()

    assert (result.checked, result.closed, result.unknown, result.gone) == (1, 0, 1, ())
    assert _status(db_path, GREENHOUSE.id) == "open"


async def test_opening_fifty_postings_queues_their_checks_under_the_host_limits(
    db_path: Path, router: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = [_job(f"greenhouse:acme:{n}") for n in range(100, 150)]
    _store(db_path, _batch("greenhouse", *jobs))
    narrow = RatePosture(concurrency=2, min_interval_s=0.0)
    monkeypatch.setattr(liveness, "open_postures", lambda: {**UNPACED, GH_BUCKET: narrow})
    flying = 0
    most = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal flying, most
        flying += 1
        most = max(most, flying)
        await asyncio.sleep(0.01)
        flying -= 1
        return _answer("greenhouse_job")

    router.get(url__regex=rf"^{GH}/acme/jobs/\d+$").mock(side_effect=respond)

    result = await check_opened(db_path, [job.id for job in jobs])

    assert (result.checked, result.unknown) == (50, 0)
    assert most == 2
    assert len(_checks(db_path)) == 50


async def test_the_time_limit_leaves_unasked_postings_unrecorded(
    db_path: Path, router: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = [_job(f"greenhouse:acme:{n}") for n in range(100, 110)]
    _store(db_path, _batch("greenhouse", *jobs))
    slow = RatePosture(concurrency=1, min_interval_s=5.0)
    monkeypatch.setattr(liveness, "open_postures", lambda: {**UNPACED, GH_BUCKET: slow})
    router.get(url__regex=rf"^{GH}/acme/jobs/\d+$").mock(return_value=_answer("greenhouse_job"))

    result = await check_opened(db_path, [job.id for job in jobs], time_limit_s=0.6)

    assert result.checked == 10 and result.unknown >= 9
    assert len(_checks(db_path)) == 10 - result.unknown


async def test_the_mcp_open_tool_says_which_postings_are_gone_and_writes_nothing(
    db_path: Path, opened: list[str], router: respx.MockRouter
) -> None:
    from mcp import Client
    from mcp.types import TextContent

    from stage.ai.server import build_server

    _gone_routes(router, opened)
    async with Client(build_server(db_path, actions=True)) as client:
        result = await client.call_tool("open_postings", {"ids": [GREENHOUSE.id]})

    block = result.content[0]
    assert isinstance(block, TextContent) and not result.is_error
    found = json.loads(block.text)
    assert found["no_longer_listed"] == [
        {"id": GREENHOUSE.id, "company": "Acme", "title": "Software Intern"}
    ]
    assert any("no longer list" in note for note in found["notes"])
    assert opened[0].startswith("browser ")
    assert _status(db_path, GREENHOUSE.id) == "open"
    assert _checks(db_path) == {}


async def test_the_tui_tells_you_in_the_background_when_an_opened_posting_is_gone(
    db_path: Path, opened: list[str], router: respx.MockRouter
) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    _gone_routes(router, opened)
    app = StageApp(db_path, "")
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        screen = app.screen
        assert isinstance(screen, PostingsScreen)
        told: list[str] = []
        app.notify = lambda message, **_: told.append(str(message))  # type: ignore[method-assign]
        await pilot.press("o")
        assert opened[0].startswith("browser "), "the tab opens before any request"
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert "Acme no longer lists Software Intern, so Stage marked it closed." in told
    assert _status(db_path, GREENHOUSE.id) == "closed"


async def test_the_applications_screen_checks_the_posting_it_opens(
    db_path: Path, opened: list[str], router: respx.MockRouter
) -> None:
    from stage.domain.tracker import ApplicationStatus
    from stage.services.tracker import record_status
    from stage.tui.app import StageApp
    from stage.tui.screens.applications import ApplicationsScreen

    _gone_routes(router, opened)
    async with open_repository(db_path) as repository:
        await record_status(repository, [GREENHOUSE.id], ApplicationStatus.SAVED)
    app = StageApp(db_path, "")
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        told: list[str] = []
        app.notify = lambda message, **_: told.append(str(message))  # type: ignore[method-assign]
        app.push_screen(ApplicationsScreen())
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.press("o")
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert any("no longer lists" in message for message in told)
    assert _status(db_path, GREENHOUSE.id) == "closed"


def test_open_postures_are_the_ones_a_sync_would_use() -> None:
    from stage.companies import load_companies
    from stage.services.sync import _bucket_postures, _group_by_adapter
    from stage.sources import get_feeds

    grouped, _ = _group_by_adapter(load_companies())
    expected = _bucket_postures(grouped, get_feeds())
    found = liveness.open_postures()

    assert found is not None
    assert {bucket: found[bucket] for bucket in BUCKETS} == {
        bucket: expected[bucket] for bucket in BUCKETS
    }


async def test_a_sync_without_postures_for_a_bucket_borrows_the_registry_ones(
    db_path: Path, router: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    _queue_db(db_path, now)
    asked: list[str] = []

    def registry() -> dict[str, RatePosture]:
        asked.append("registry")
        return dict(UNPACED)

    monkeypatch.setattr(liveness, "open_postures", registry)
    router.get(url__regex=rf"^{GH}/acme/jobs/\d+$").mock(return_value=_answer("greenhouse_job"))

    async with open_repository(db_path) as repository:
        result = await check_still_open(
            repository, rate_state={}, budgets={}, postures={}, now=now, limit=1
        )

    assert asked == ["registry"]
    assert (result.checked, result.closed, result.unknown) == (1, 0, 0)


async def test_nothing_is_asked_without_a_database(tmp_path: Path) -> None:
    assert await check_opened(tmp_path / "absent.db", [GREENHOUSE.id]) == liveness.LivenessResult()


def test_the_notice_names_one_posting_or_counts_several() -> None:
    other = replace(GREENHOUSE, company="Globex", title_raw="Data Intern")
    assert liveness.gone_notice([other]) == (
        "Globex no longer lists Data Intern, so Stage marked it closed."
    )
    assert liveness.gone_notice([GREENHOUSE, other]).startswith("2 of the postings you opened")


def test_a_board_that_drops_a_posting_closes_its_list_copies_for_good(tmp_path: Path) -> None:
    earlier = datetime.now(UTC) - timedelta(hours=3)
    board = _job("greenhouse:acme:1", seen=earlier)
    copy = _job("simplify:acme:abc", source="simplify", seen=earlier, duplicate_of=board.id)
    other = _job("lever:acme:2", seen=earlier, duplicate_of=board.id)
    db = _store(
        tmp_path / "s.db",
        _batch("greenhouse", board, at=earlier),
        _batch("simplify", copy, at=earlier),
        _batch("lever", other, at=earlier),
    )
    _store(
        db,
        _batch(
            "greenhouse",
            closable_boards=("greenhouse:acme",),
            list_sources=frozenset({"simplify"}),
        ),
    )
    assert _status(db, board.id) == "closed"
    assert _status(db, copy.id) == "closed", "a stale list copy must not outlive the board"
    assert _status(db, other.id) == "open", "another employer board decides for itself"

    _store(db, _batch("simplify", replace(copy, last_seen=datetime.now(UTC))))
    assert _status(db, copy.id) == "closed", "a list still carrying the link cannot reopen it"


def test_a_board_that_drops_a_posting_keeps_list_copies_of_another_job_number(
    tmp_path: Path,
) -> None:
    earlier = datetime.now(UTC) - timedelta(hours=3)
    board = _job("workday:wal:r14019", f"{_WAL}_R14019", seen=earlier)
    same = _job("simplify:wal:1", f"{_WAL}_R14019", seen=earlier, duplicate_of=board.id)
    other = _job("simplify:wal:2", f"{_WAL}_R14020", seen=earlier, duplicate_of=board.id)
    db = _store(
        tmp_path / "s.db",
        _batch("workday", board, at=earlier),
        _batch("simplify", same, other, at=earlier),
    )
    _store(
        db,
        _batch(
            "workday",
            closable_boards=("workday:wal",),
            list_sources=frozenset({"simplify"}),
        ),
    )

    assert (_status(db, board.id), _status(db, same.id)) == ("closed", "closed")
    assert _status(db, other.id) == "open", "another job number was never checked"
