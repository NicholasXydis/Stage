import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from stage.domain import Job, JobStatus
from stage.http import HttpClient, profile
from stage.normalize import canonical_apply_url, is_tracker_url
from stage.services.sync import normalize_batch
from stage.sources import get_feeds, upcoming_season_year
from stage.sources.simplify import SimplifyFeed


def _client(feed: SimplifyFeed) -> HttpClient:
    return HttpClient(allowed_hosts=feed.hosts, posture=profile(feed.rate_profile), jitter=False)


def test_the_feed_self_registers_on_its_own_axis() -> None:
    feeds = get_feeds()
    assert "simplify" in feeds
    assert "simplify" not in {"greenhouse"}


def test_the_url_is_built_from_a_year_not_a_literal() -> None:
    feed = SimplifyFeed()
    early = feed.plan(datetime(2026, 3, 1, tzinfo=UTC))[0]
    late = feed.plan(datetime(2026, 9, 1, tzinfo=UTC))[0]
    assert early != late
    assert "2026" in early and "2027" in late

    body = Path(__file__).resolve().parents[1] / "src" / "stage" / "sources" / "simplify.py"
    template = body.read_text(encoding="utf-8")
    assert "{year}" in template, "the feed URL must be built from a year, never a literal"
    assert not re.search(r"Summer20\d\d-Internships", template), (
        "a hardcoded cycle returns an empty list rather than an error when the repo renames"
    )


def test_the_season_rolls_partway_through_the_preceding_year() -> None:
    assert upcoming_season_year(datetime(2026, 7, 31, tzinfo=UTC)) == 2026
    assert upcoming_season_year(datetime(2026, 8, 1, tzinfo=UTC)) == 2027


@respx.mock
async def test_listings_map_to_jobs_and_skip_inactive(run_time: datetime) -> None:
    feed = SimplifyFeed()
    respx.get(feed.plan(run_time)[0]).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "a1",
                    "company_name": "Acme",
                    "title": "Software Engineer Intern",
                    "url": "https://simplify.jobs/p/a1",
                    "locations": ["Montréal, QC"],
                    "active": True,
                    "is_visible": True,
                },
                {
                    "id": "a2",
                    "company_name": "Acme",
                    "title": "Closed Role",
                    "active": False,
                    "is_visible": True,
                },
            ],
        )
    )
    async with _client(feed) as client:
        result = await feed.fetch(client, run_time)

    assert [job.title_raw for job in result.jobs] == ["Software Engineer Intern"]
    assert result.jobs[0].source == "simplify"
    assert result.jobs[0].location_raw == "Montréal, QC"


@respx.mock
async def test_a_non_list_payload_is_a_shape_change_and_fails_loudly(
    run_time: datetime,
) -> None:
    feed = SimplifyFeed()
    respx.get(feed.plan(run_time)[0]).mock(return_value=httpx.Response(200, json={"listings": []}))
    from stage.sources.base import PayloadValidationError

    async with _client(feed) as client:
        with pytest.raises(PayloadValidationError, match="expected a JSON list"):
            await feed.fetch(client, run_time)


@respx.mock
async def test_a_bad_listing_is_dropped_and_the_feed_closes_nothing(
    run_time: datetime,
) -> None:
    feed = SimplifyFeed()
    respx.get(feed.plan(run_time)[0]).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "1", "company_name": "Acme", "title": "Intern"},
                {"company_name": "Acme", "title": "Intern"},
            ],
        )
    )

    async with _client(feed) as client:
        result = await feed.fetch(client, run_time)

    assert len(result.jobs) == 1
    assert "1 posting(s) failed validation" in result.degraded
    assert not result.authoritative


@pytest.mark.parametrize(
    "url",
    [
        "https://simplify.jobs/p/abc123",
        "https://trk.simplify.jobs/x",
        "https://click.appcast.io/z",
    ],
)
def test_tracker_urls_are_recognized_never_canonicalized(url: str) -> None:
    assert is_tracker_url(url)
    assert canonical_apply_url(url) == ""


def test_locale_segments_collapse_to_one_canonical_url() -> None:
    french = canonical_apply_url("https://acme.com/fr-CA/jobs/12345?src=x#top")
    english = canonical_apply_url("https://acme.com/en-CA/jobs/12345")
    assert french == english == "https://acme.com/jobs/12345"
    assert canonical_apply_url("https://acme.com/fr/jobs/12345") == "https://acme.com/jobs/12345"


@pytest.mark.parametrize("segment", ["it", "hr", "id", "us", "ca", "uk", "co", "no"])
def test_a_two_letter_segment_that_is_not_a_language_is_kept(segment: str) -> None:
    assert canonical_apply_url(f"https://acme.com/{segment}/jobs/12345") == (
        f"https://acme.com/{segment}/jobs/12345"
    ), f"/{segment}/ is a department or country, not a locale"


def test_a_real_ats_url_survives_canonicalization() -> None:
    assert (
        canonical_apply_url("https://boards.greenhouse.io/faire/jobs/7?gh_src=abc")
        == "https://boards.greenhouse.io/faire/jobs/7"
    )


def test_canonicalization_runs_at_ingestion(run_time: datetime) -> None:
    job = Job(
        id="x",
        source="simplify",
        company="Acme",
        title_raw="Software Engineer Intern",
        title_normalized="Software Engineer Intern",
        apply_url_raw="https://simplify.jobs/p/a1",
        description="",
        location_raw="Montreal, QC",
        first_seen=run_time,
        last_seen=run_time,
    )
    kept, _ = normalize_batch([job])
    assert kept[0].apply_url_canonical == ""
    direct, _ = normalize_batch([replace(job, apply_url_raw="https://acme.com/jobs/1/")])
    assert direct[0].apply_url_canonical == "https://acme.com/jobs/1"


async def test_a_feed_closes_its_own_postings_but_not_a_boards(
    db_path: Path, run_time: datetime
) -> None:
    from stage.storage import SourceBatch, open_repository

    def job(source: str, ident: str, seen: datetime) -> Job:
        return Job(
            id=f"{source}-{ident}",
            source=source,
            company="Acme",
            title_raw="Intern",
            title_normalized="Intern",
            apply_url_raw="",
            description="",
            first_seen=seen,
            last_seen=seen,
        )

    later = run_time + timedelta(days=1)
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="simplify",
                run_started_at=run_time,
                jobs=(job("simplify", "1", run_time),),
            )
        )
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=run_time,
                jobs=(job("greenhouse", "1", run_time),),
                closable_boards=("greenhouse:acme",),
            )
        )
        result = await repository.apply_source_batch(
            SourceBatch(
                source="simplify",
                run_started_at=later,
                jobs=(),
                closes_whole_source=True,
            )
        )
        assert result.closed == 1
        from_feed = await repository.get_job("simplify-1")
        from_board = await repository.get_job("greenhouse-1")

    assert from_feed is not None and from_feed.status is JobStatus.CLOSED
    assert from_board is not None and from_board.status is JobStatus.OPEN


class _FlakyFeed:
    name = "flaky"
    rate_profile = "feeds"
    hosts = frozenset({"raw.githubusercontent.com"})
    bucket_key = ""

    def __init__(self, outcome: str) -> None:
        self.outcome = outcome

    def season_year(self, now: datetime) -> int:
        return 2027

    def plan(self, now: datetime) -> tuple[str, ...]:
        return ("https://raw.githubusercontent.com/x/y/listings.json",)

    async def fetch(self, client: HttpClient, now: datetime):  # type: ignore[no-untyped-def]
        from stage.sources.base import FetchResult

        if self.outcome == "boom":
            raise RuntimeError("network blip")
        return FetchResult(jobs=(), degraded="partial page")


@pytest.mark.parametrize("outcome", ["boom", "degraded"])
async def test_a_partial_feed_run_never_closes_the_whole_source(
    db_path: Path, run_time: datetime, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    from stage.services import sync as sync_module
    from stage.storage import SourceBatch, open_repository

    monkeypatch.setattr(sync_module, "get_feeds", lambda: {"flaky": _FlakyFeed(outcome)})
    seeded = Job(
        id="flaky-1",
        source="flaky",
        company="Acme",
        title_raw="Intern",
        title_normalized="Intern",
        apply_url_raw="",
        description="",
        first_seen=run_time,
        last_seen=run_time,
    )
    later = run_time + timedelta(days=1)
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(source="flaky", run_started_at=run_time, jobs=(seeded,))
        )
        async for _ in sync_module.sync(repository, [], sources=["flaky"], now_fn=lambda: later):
            pass
        survivor = await repository.get_job("flaky-1")

    assert survivor is not None
    assert survivor.status is JobStatus.OPEN


@respx.mock
async def test_a_degraded_fetch_does_not_freeze_its_truncated_view(
    db_path: Path, run_time: datetime
) -> None:
    from stage.domain import Company, Platform
    from stage.services.sync import sync
    from stage.storage import open_repository

    big = "x" * 40
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        side_effect=[
            httpx.Response(200, content=b"[" + b"0" * (33 * 1024 * 1024) + b"]"),
            httpx.Response(
                200,
                json={"jobs": [{"id": 1, "title": big, "absolute_url": "https://a.co/1"}]},
                headers={"ETag": '"v1"'},
            ),
        ]
    )
    acme = Company(name="Acme", platform=Platform.GREENHOUSE, slug="acme")
    async with open_repository(db_path) as repository:
        async for _ in sync(repository, [acme], sources=["greenhouse"], now_fn=lambda: run_time):
            pass
        assert dict(await repository.load_validators("greenhouse")) == {}


@respx.mock
async def test_a_community_feed_missing_a_file_closes_nothing(run_time: datetime) -> None:
    from stage.sources.community_feeds import VanshFeed

    feed = VanshFeed()
    urls = feed.plan(run_time)
    respx.get(urls[0]).mock(
        return_value=httpx.Response(
            200,
            json=[{"id": "1", "company_name": "Acme", "title": "Intern", "active": True}],
        )
    )

    async with HttpClient(
        allowed_hosts=feed.hosts, posture=profile(feed.rate_profile), jitter=False
    ) as client:
        result = await feed.fetch(client, run_time)

    assert result.jobs
    assert result.authoritative, "a feed that read every file it planned is complete"


@respx.mock
async def test_a_community_feed_that_lost_a_file_is_not_authoritative(
    run_time: datetime,
) -> None:
    from stage.sources.community_feeds import _CommunityFeed
    from stage.sources.feed import register_feed

    @register_feed
    class _TwoFileFeed(_CommunityFeed):
        name = "vanshb03-twofile"
        templates = (
            "https://raw.githubusercontent.com/x/Summer{year}-Internships/dev/a.json",
            "https://raw.githubusercontent.com/x/Summer{year}-Internships/dev/b.json",
        )

    feed = _TwoFileFeed()
    first, second = feed.plan(run_time)
    respx.get(first).mock(
        return_value=httpx.Response(
            200, json=[{"id": "1", "company_name": "Acme", "title": "Intern"}]
        )
    )
    respx.get(second).mock(return_value=httpx.Response(500))

    async with HttpClient(
        allowed_hosts=feed.hosts, posture=profile(feed.rate_profile), jitter=False
    ) as client:
        result = await feed.fetch(client, run_time)

    assert len(result.jobs) == 1, "what did arrive is kept"
    assert "unavailable" in result.degraded
    assert not result.authoritative, (
        "a feed closes source-wide, so an incomplete read would close the missing file's postings"
    )
    assert second in result.stale_urls
