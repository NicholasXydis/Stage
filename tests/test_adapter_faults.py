import random
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from stage.domain import (
    Company,
    CompanyDeferred,
    CompanyFailed,
    CompanyFinished,
    CustomBoard,
    Job,
    JobStatus,
    Platform,
    SourceFailed,
    SyncEvent,
    SyncFinished,
)
from stage.http import RatePosture
from stage.services import sync as sync_module
from stage.sources import adapter_for_platform, get_feeds
from stage.storage import AsyncRepository, SourceBatch, open_repository

WHEN = datetime(2026, 9, 14, 12, tzinfo=UTC)
CLASS_NAME = re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z]+)+(?:Error|Exception|Timeout)\b")
BOARDS = {
    "greenhouse": Company(name="Acme", platform=Platform.GREENHOUSE, slug="acme"),
    "lever": Company(name="Acme", platform=Platform.LEVER, slug="acme"),
    "ashby": Company(name="Acme", platform=Platform.ASHBY, slug="acme"),
    "workable": Company(name="Acme", platform=Platform.WORKABLE, slug="acme"),
    "bamboohr": Company(name="Acme", platform=Platform.BAMBOOHR, slug="acme"),
    "breezy": Company(name="Acme", platform=Platform.BREEZY, slug="acme"),
    "recruitee": Company(name="Acme", platform=Platform.RECRUITEE, slug="acme"),
    "collage": Company(name="Acme", platform=Platform.COLLAGE, slug="acme"),
    "smartrecruiters": Company(name="Acme", platform=Platform.SMARTRECRUITERS, slug="acme"),
    "jobvite": Company(name="Acme", platform=Platform.JOBVITE, slug="acme"),
    "oracle_cloud": Company(
        name="Acme",
        platform=Platform.ORACLE_CLOUD,
        slug="acme",
        oracle_host="acme.fa.us2.oraclecloud.com",
        oracle_site="CX_1",
    ),
    "workday": Company(
        name="Acme",
        platform=Platform.WORKDAY,
        slug="acme",
        workday_tenant="acme",
        workday_site="External",
        workday_dc="wd5",
    ),
    "custom_json": Company(
        name="Acme",
        platform=Platform.CUSTOM_JSON,
        slug="acme",
        custom=CustomBoard(
            url="https://careers.acme.example/api/jobs",
            jobs_path="jobs",
            fields={"id": "id", "title": "title"},
        ),
    ),
}
FEEDS = sorted(get_feeds())
ANSWERED = frozenset({"403", "404", "500", "network login"})
MULTI_FILE_LISTS = frozenset({"speedyapply", "vanshb03"})
SOURCES = sorted(BOARDS) + FEEDS


def _raise(kind: type[httpx.TransportError], message: str) -> Callable[[httpx.Request], None]:
    def fail(request: httpx.Request) -> None:
        raise kind(message, request=request)

    return fail


def _answer(response: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: response


FAULTS: dict[str, Callable[[httpx.Request], httpx.Response | None]] = {
    "read timeout": _raise(httpx.ReadTimeout, ""),
    "dns failure": _raise(httpx.ConnectError, "[Errno 8] nodename nor servname provided"),
    "dropped connection": _raise(httpx.RemoteProtocolError, "peer closed connection"),
    "403": _answer(httpx.Response(403, text="denied")),
    "404": _answer(httpx.Response(404, json={})),
    "429 for an hour": _answer(httpx.Response(429, headers={"Retry-After": "3600"})),
    "500": _answer(httpx.Response(500, text="oops")),
    "challenge page": _answer(
        httpx.Response(200, text="<html><body>Checking your browser</body></html>")
    ),
    "wrong shape": _answer(httpx.Response(200, json={"unexpected": True})),
    "over the size limit": _answer(
        httpx.Response(200, headers={"Content-Length": str(65 * 1024 * 1024)}, content=b"{}")
    ),
    "redirect off the list": _answer(
        httpx.Response(302, headers={"Location": "https://evil.example/login"})
    ),
    "network login": _answer(httpx.Response(511, text="<html>Sign in to Wi-Fi</html>")),
}


@pytest.fixture(autouse=True)
def instant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sync_module,
        "resolve",
        lambda *_: RatePosture(concurrency=2, min_interval_s=0.0, max_requests_per_run=400),
    )
    monkeypatch.setattr("stage.http.client.wait_exponential_jitter", lambda **_: lambda _state: 0.0)


def _registry(source: str) -> list[Company]:
    return [BOARDS[source]] if source in BOARDS else []


def _board(source: str) -> str:
    if source not in BOARDS:
        return source
    company = BOARDS[source]
    adapter = adapter_for_platform(company.platform)
    assert adapter is not None
    return adapter.board_key(company)


def _seeded(source: str) -> Job:
    return Job(
        id=f"{_board(source)}:seed",
        source=source,
        company="Acme",
        title_raw="Software Engineer Intern",
        title_normalized="software engineer intern",
        apply_url_raw="https://acme.example/jobs/seed",
        description="",
        location_raw="Montreal, QC",
        first_seen=WHEN - timedelta(days=1),
        last_seen=WHEN - timedelta(days=1),
    )


async def _seed(repository: AsyncRepository, source: str) -> None:
    await repository.apply_source_batch(
        SourceBatch(source=source, run_started_at=WHEN - timedelta(days=1), jobs=(_seeded(source),))
    )


async def _run(repository: AsyncRepository, source: str) -> list[SyncEvent]:
    return [
        event
        async for event in sync_module.sync(
            repository,
            _registry(source),
            sources=[source],
            now_fn=lambda: WHEN,
            rng=random.Random(3),
            force_refresh=True,
            close_orphans=False,
        )
    ]


def _said(events: list[SyncEvent]) -> list[str]:
    texts: list[str] = []
    for event in events:
        if isinstance(event, CompanyFailed | SourceFailed):
            texts.append(event.error)
        elif isinstance(event, CompanyDeferred):
            texts.append(event.reason)
        elif isinstance(event, CompanyFinished):
            texts.append(event.degraded)
    return [text for text in texts if text]


@pytest.mark.parametrize("fault", sorted(FAULTS))
@pytest.mark.parametrize("source", SOURCES)
async def test_a_failing_board_never_closes_what_it_listed_and_says_why_plainly(
    tmp_path: Path, source: str, fault: str
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(side_effect=FAULTS[fault])
        async with open_repository(tmp_path / "faults.db") as repository:
            await _seed(repository, source)
            events = await _run(repository, source)
            stored = await repository.get_job(_seeded(source).id)
            visits = [visit for visit in await repository.all_visits() if visit.source == source]

    finished = [event for event in events if isinstance(event, SyncFinished)]
    assert len(finished) == 1, "the sync must always finish"
    failed = [event for event in events if isinstance(event, CompanyFailed | SourceFailed)]
    assert failed or any(isinstance(event, CompanyDeferred) for event in events), fault
    if fault in ANSWERED and source not in MULTI_FILE_LISTS:
        assert all(event.unreachable for event in events if isinstance(event, CompanyFailed))
    assert stored is not None and stored.status is JobStatus.OPEN, "a failed read closed"
    for text in [*_said(events), *(visit.last_error for visit in visits)]:
        assert not CLASS_NAME.search(text), text
        assert ";" not in text, text


@pytest.mark.parametrize("source", sorted(BOARDS))
async def test_an_unreadable_shape_is_never_mistaken_for_an_empty_board(
    tmp_path: Path, source: str
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(side_effect=FAULTS["wrong shape"])
        async with open_repository(tmp_path / "shape.db") as repository:
            await _seed(repository, source)
            await _run(repository, source)
            stored = await repository.get_job(_seeded(source).id)

    assert stored is not None and stored.status is JobStatus.OPEN, source


@pytest.mark.parametrize("source", FEEDS)
async def test_an_empty_list_answer_is_a_change_of_shape_or_a_stated_empty(
    tmp_path: Path, source: str
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(
            side_effect=lambda _request: httpx.Response(200, text="", headers={"ETag": '"e"'})
        )
        async with open_repository(tmp_path / "empty.db") as repository:
            await _seed(repository, source)
            events = await _run(repository, source)
            stored = await repository.get_job(_seeded(source).id)

    assert any(isinstance(event, SyncFinished) for event in events)
    assert stored is not None and stored.status is JobStatus.OPEN, source
