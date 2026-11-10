import asyncio
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner, Result

from stage.cli.app import app
from stage.cli.schedule import _ACTIONS
from stage.companies import load_personal, write_registry
from stage.domain import (
    Company,
    Job,
    LocationBucket,
    Platform,
    PlatformCandidate,
    ProbeResult,
    ProbeVerdict,
)
from stage.domain.robots import robots_rules
from stage.http import RatePosture
from stage.paths import boards_path
from stage.services.discover import (
    ROBOTS_AGENT,
    ROBOTS_AT_ONCE,
    adopt_unregistered,
    plan_adoption,
    read_robots,
)
from stage.storage.repository import SourceBatch
from stage.storage.sqlite_repo import SqliteRepository

TODAY = date(2026, 11, 2)
WEEKLY = next(action.cli_arguments for action in _ACTIONS if action.key == "discover")
GREENHOUSE = "https://boards-api.greenhouse.io/robots.txt"
ACME = Company(name="Acme", platform=Platform.GREENHOUSE, slug="acme")
ZYX = Company(
    name="Zyx",
    platform=Platform.WORKDAY,
    slug="zyx",
    workday_tenant="zyx",
    workday_site="EarlyTalent",
    workday_dc="wd5",
)


@pytest.fixture(autouse=True)
def unpaced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "stage.services.discover.resolve",
        lambda *_: RatePosture(concurrency=8, min_interval_s=0.0, max_requests_per_run=500),
    )


def test_stage_reads_the_group_for_its_own_product_token() -> None:
    text = "User-agent: *\nDisallow: /\n\nUser-agent: Stage-CLI/1.2\nAllow: /\n"
    assert ROBOTS_AGENT == "stage-cli"
    assert robots_rules(text, ROBOTS_AGENT).allows("/v1/boards/acme/jobs")
    assert not robots_rules(text, "otherbot").allows("/v1/boards/acme/jobs")


def test_the_longest_rule_wins_and_allow_wins_a_tie() -> None:
    rules = robots_rules(
        "User-agent: *\nDisallow: /\nAllow: /api/\nDisallow: /api/private\nAllow: /tie\n"
        "Disallow: /tie\n",
        ROBOTS_AGENT,
    )
    assert rules.allows("/api/jobs")
    assert not rules.allows("/api/private/1")
    assert not rules.allows("/careers")
    assert rules.allows("/tie")


def test_wildcards_and_end_anchors_match_like_major_crawlers() -> None:
    rules = robots_rules(
        "User-agent: *\nDisallow: */JobBoardView\nDisallow: /*.json$\n", ROBOTS_AGENT
    )
    assert not rules.allows("/x/JobBoardView/1")
    assert not rules.allows("/feed.json")
    assert rules.allows("/feed.json?page=2")
    assert rules.allows("/careers")


def test_consecutive_agent_lines_share_one_group_and_comments_are_ignored() -> None:
    text = "User-agent: stage-cli\nUser-agent: *  # everyone\nDisallow: /private # no\n"
    rules = robots_rules(text, ROBOTS_AGENT)
    assert not rules.allows("/private/1")
    assert rules.allows("/public")


@pytest.mark.parametrize(
    ("line", "delay"),
    [("Crawl-delay: 2.5", 2.5), ("Crawl-delay: 1", 1.0), ("Crawl-delay: soon", None)],
)
def test_crawl_delay_reads_fractions_and_ignores_nonsense(line: str, delay: float | None) -> None:
    assert robots_rules(f"User-agent: *\n{line}\n", ROBOTS_AGENT).crawl_delay == delay


def test_an_empty_disallow_or_a_page_that_is_not_robots_txt_allows_everything() -> None:
    assert robots_rules("User-agent: *\nDisallow:\n", ROBOTS_AGENT).allows("/anything")
    assert robots_rules("<html><body>Not found</body></html>", ROBOTS_AGENT).allows("/a")


@respx.mock
async def test_robots_reads_wait_out_a_block_a_sync_stored() -> None:
    from datetime import timedelta

    from stage.domain import RateState
    from stage.services.discover import StoredLimits

    route = respx.get(GREENHOUSE).mock(return_value=httpx.Response(200, text="User-agent: *\n"))
    now = datetime.now(UTC)
    host = "boards-api.greenhouse.io"
    held = RateState(host, now, blocked_until=now + timedelta(hours=1), reason="429")

    await read_robots([ACME], limits=StoredLimits({host: held}, now))

    assert route.call_count == 0


def test_a_robots_txt_saved_with_a_byte_order_mark_still_counts() -> None:
    assert not robots_rules("\ufeffUser-agent: *\nDisallow: /\n", ROBOTS_AGENT).allows("/jobs")


@respx.mock
async def test_an_allowing_robots_txt_keeps_the_board_as_it_was() -> None:
    route = respx.get(GREENHOUSE).mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /embed/\n")
    )
    robots = await read_robots([ACME])
    kept, skipped = robots.screen([ACME], TODAY)
    assert (kept, skipped) == ([ACME], [])
    assert route.calls.last.request.headers["user-agent"].startswith("stage-cli/")


@respx.mock
async def test_a_board_disallowed_for_everyone_is_skipped_with_the_reason() -> None:
    respx.get(GREENHOUSE).mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /"))
    kept, skipped = (await read_robots([ACME])).screen([ACME], TODAY)
    assert kept == []
    assert skipped == [
        (ACME, "boards-api.greenhouse.io/robots.txt does not allow reading /v1/boards/acme/jobs")
    ]


@respx.mock
async def test_a_board_disallowed_only_for_stage_is_skipped() -> None:
    respx.get(GREENHOUSE).mock(
        return_value=httpx.Response(
            200, text="User-agent: stage-cli\nDisallow: /v1/\n\nUser-agent: *\nAllow: /\n"
        )
    )
    robots = await read_robots([ACME])
    assert "does not allow reading" in robots.refusal(ACME)
    assert robots_rules("User-agent: stage-cli\nDisallow: /v1/\n", "otherbot").allows("/v1/")


@respx.mock
async def test_a_workday_site_its_robots_txt_keeps_out_is_skipped() -> None:
    respx.get("https://zyx.wd5.myworkdayjobs.com/robots.txt").mock(
        return_value=httpx.Response(
            200, text="User-agent: *\nAllow: /External/\nDisallow: /EarlyTalent/\n"
        )
    )
    robots = await read_robots([ZYX])
    assert robots.refusal(ZYX).endswith("does not allow reading /EarlyTalent/")


@respx.mock
async def test_a_workday_rule_holds_whatever_case_the_site_is_written_in() -> None:
    respx.get("https://zyx.wd5.myworkdayjobs.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /EARLYTALENT/\n")
    )
    robots = await read_robots([ZYX])
    assert robots.refusal(ZYX).endswith("does not allow reading /EarlyTalent/")


@respx.mock
async def test_a_crawl_delay_of_two_seconds_or_more_makes_the_board_patient() -> None:
    respx.get(GREENHOUSE).mock(
        return_value=httpx.Response(200, text="User-agent: *\nCrawl-delay: 5\n")
    )
    kept, skipped = (await read_robots([ACME])).screen([ACME], TODAY)
    assert not skipped
    assert kept[0].rate_profile == "patient"
    assert (kept[0].notes or "").startswith("2026-11-02: boards-api.greenhouse.io/robots.txt")


@respx.mock
async def test_a_short_crawl_delay_changes_nothing() -> None:
    respx.get(GREENHOUSE).mock(
        return_value=httpx.Response(200, text="User-agent: *\nCrawl-delay: 1\n")
    )
    kept, _ = (await read_robots([ACME])).screen([ACME], TODAY)
    assert kept == [ACME]


@respx.mock
@pytest.mark.parametrize("status", [404, 401, 403])
async def test_a_missing_or_refused_robots_txt_allows(status: int) -> None:
    respx.get(GREENHOUSE).mock(
        return_value=httpx.Response(status, text="User-agent: *\nDisallow: /")
    )
    kept, skipped = (await read_robots([ACME])).screen([ACME], TODAY)
    assert (kept, skipped) == ([ACME], [])


@respx.mock
async def test_a_robots_txt_that_times_out_allows() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, text="User-agent: *\nDisallow: /")

    respx.get(GREENHOUSE).mock(side_effect=slow)
    robots = await asyncio.wait_for(read_robots([ACME], timeout=0.05), timeout=2)
    assert robots.refusal(ACME) == ""


@respx.mock
async def test_each_host_is_asked_once_however_many_boards_share_it() -> None:
    route = respx.get(GREENHOUSE).mock(return_value=httpx.Response(200, text="User-agent: *"))
    others = [
        Company(name=f"Co {index}", platform=Platform.GREENHOUSE, slug=f"co{index}")
        for index in range(5)
    ]
    await read_robots([ACME, *others])
    assert route.call_count == 1


@respx.mock
async def test_robots_files_are_read_two_at_a_time_and_each_host_once() -> None:
    flight = {"now": 0, "most": 0}

    async def answer(request: httpx.Request) -> httpx.Response:
        flight["now"] += 1
        flight["most"] = max(flight["most"], flight["now"])
        await asyncio.sleep(0.01)
        flight["now"] -= 1
        return httpx.Response(200, text="User-agent: *\nDisallow: /secret/\n")

    route = respx.get(url__regex=r"https://t\d\.wd5\.myworkdayjobs\.com/robots\.txt").mock(
        side_effect=answer
    )
    boards = [
        Company(
            name=f"T{index}",
            platform=Platform.WORKDAY,
            slug=f"t{index}",
            workday_tenant=f"t{index}",
            workday_site="External",
            workday_dc="wd5",
        )
        for index in range(6)
    ]
    robots = await read_robots([*boards, *boards])
    assert route.call_count == 6
    assert flight["most"] == ROBOTS_AT_ONCE == 2
    assert all(robots.refusal(board) == "" for board in boards)


async def test_a_board_stage_cannot_locate_asks_for_nothing() -> None:
    custom = Company(name="Custom", platform=Platform.CUSTOM_JSON, slug="custom")
    broken = Company(
        name="Broken",
        platform=Platform.WORKDAY,
        slug="broken",
        workday_tenant="broken",
        workday_site="has/slash",
        workday_dc="wd3",
    )
    with respx.mock(assert_all_called=False) as mock:
        robots = await read_robots([custom, broken])
    assert not mock.calls
    assert robots.refusal(custom) == robots.refusal(broken) == ""


def _matched(slug: str) -> tuple[str, ProbeResult]:
    return (
        slug.title(),
        ProbeResult(
            company=slug.title(),
            candidate=PlatformCandidate(Platform.GREENHOUSE, slug),
            verdict=ProbeVerdict.MATCH,
            url=f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
            board_name=slug.title(),
            job_count=4,
        ),
    )


@respx.mock
async def test_adoption_refuses_a_disallowed_board_and_slows_a_delayed_one() -> None:
    respx.get(GREENHOUSE).mock(
        return_value=httpx.Response(
            200, text="User-agent: *\nDisallow: /v1/boards/blocked\nCrawl-delay: 3\n"
        )
    )
    results = [_matched("blocked"), _matched("open")]

    report, robots = await plan_adoption([], results, today=TODAY, check_robots=True)

    assert [row.company.slug for row in report.adopted] == ["open"]
    assert report.adopted[0].company.rate_profile == "patient"
    assert report.refused == (
        (
            "Blocked",
            "greenhouse/blocked",
            "boards-api.greenhouse.io/robots.txt does not allow reading /v1/boards/blocked/jobs",
        ),
    )
    again = adopt_unregistered([], results, today=TODAY, robots=robots)
    assert again == report, "the write under the lock must decide exactly as the preview did"


async def test_adoption_without_apply_reads_no_robots_txt() -> None:
    with respx.mock(assert_all_called=False) as mock:
        report, _ = await plan_adoption([], [_matched("open")], today=TODAY)
    assert not mock.calls
    assert [row.company.slug for row in report.adopted] == ["open"]


@pytest.fixture
def packaged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "packaged.yaml"
    write_registry((Company(name="Coveo", platform=Platform.GREENHOUSE, slug="coveo"),), target)
    monkeypatch.delenv("STAGE_REGISTRY", raising=False)
    monkeypatch.setattr("stage.companies.registry_path", lambda: target)
    return target


def _seed(db: Path, name: str, url: str) -> None:
    now = datetime.now(UTC)
    repository = SqliteRepository.connect(db)
    repository.apply_source_batch(
        SourceBatch(
            source="simplify",
            run_started_at=now,
            jobs=(
                Job(
                    id="simplify:feed:1",
                    source="simplify",
                    company=name,
                    title_raw="Software Engineer Intern",
                    title_normalized="software engineer intern",
                    apply_url_raw=url,
                    description="",
                    first_seen=now,
                    last_seen=now,
                    location_raw="Montreal, QC",
                    location=LocationBucket.MONTREAL,
                ),
            ),
        )
    )
    repository.close()


def _board(slug: str, name: str) -> None:
    respx.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [{"id": 1}], "meta": {"total": 1}})
    )
    respx.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}").mock(
        return_value=httpx.Response(200, json={"name": name})
    )


def _run(*args: str) -> Result:
    return CliRunner().invoke(app, list(args))


@respx.mock
def test_the_weekly_adoption_reports_and_skips_a_board_robots_txt_keeps_out(
    packaged: Path, db_path: Path
) -> None:
    _seed(db_path, "Zyxwv Robotics", "https://job-boards.greenhouse.io/zyxwvrobotics/jobs/1")
    _board("zyxwvrobotics", "Zyxwv Robotics")
    respx.get(GREENHOUSE).mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /v1/boards/zyxwvrobotics")
    )

    result = _run(*WEEKLY, "--db", str(db_path))

    assert result.exit_code == 0, result.output
    said = " ".join(result.output.split())
    assert "0 adoptable" in said and "1 refused" in said
    assert "robots.txt does not allow reading /v1/boards/zyxwvrobotics/jobs" in said
    assert not boards_path().exists()


@respx.mock
def test_the_weekly_adoption_reads_a_slow_host_patiently(packaged: Path, db_path: Path) -> None:
    _seed(db_path, "Zyxwv Robotics", "https://job-boards.greenhouse.io/zyxwvrobotics/jobs/1")
    _board("zyxwvrobotics", "Zyxwv Robotics")
    respx.get(GREENHOUSE).mock(
        return_value=httpx.Response(200, text="User-agent: *\nCrawl-delay: 4")
    )

    result = _run(*WEEKLY, "--db", str(db_path))

    assert result.exit_code == 0, result.output
    assert [(row.slug, row.rate_profile) for row in load_personal().rows] == [
        ("zyxwvrobotics", "patient")
    ]


@respx.mock
def test_a_dry_run_asks_no_robots_txt(packaged: Path, db_path: Path) -> None:
    _seed(db_path, "Zyxwv Robotics", "https://job-boards.greenhouse.io/zyxwvrobotics/jobs/1")
    _board("zyxwvrobotics", "Zyxwv Robotics")
    robots = respx.get(GREENHOUSE).mock(return_value=httpx.Response(200, text="User-agent: *"))

    result = _run("discover", "--unregistered", "--direct-only", "--db", str(db_path))

    assert result.exit_code == 0, result.output
    assert not robots.called


@respx.mock
@pytest.mark.parametrize("by", ["name", "url"])
def test_discover_by_name_or_link_says_why_a_board_was_not_added(
    packaged: Path, db_path: Path, by: str
) -> None:
    _board("zyxwvrobotics", "Zyxwv Robotics")
    respx.get(url__startswith="https://boards-api.greenhouse.io/v1/boards/zyxwv").mock(
        return_value=httpx.Response(404)
    )
    respx.get(GREENHOUSE).mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /"))
    asked = (
        ("Zyxwv Robotics", "--platform", "greenhouse")
        if by == "name"
        else ("--url", "https://boards.greenhouse.io/zyxwvrobotics", "--name", "Zyxwv Robotics")
    )

    result = _run("discover", *asked, "--apply", "--db", str(db_path))

    assert result.exit_code == 0, result.output
    said = " ".join(result.output.split())
    assert "Zyxwv Robotics (greenhouse/zyxwvrobotics) was not added" in said
    assert "already has it" not in said
    assert not boards_path().exists()


@respx.mock
async def test_smartrecruiters_and_ukg_boards_follow_the_owner_decision_not_robots() -> None:
    hub = Company(name="Hub", platform=Platform.SMARTRECRUITERS, slug="Hub")
    respx.get(GREENHOUSE).mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /"))
    asked = respx.get("https://api.smartrecruiters.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /")
    )

    kept, skipped = (await read_robots([hub, ACME])).screen([hub, ACME], TODAY)

    assert [row.name for row in kept] == ["Hub"] and [row for row, _ in skipped] == [ACME]
    assert not asked.called, "robots.txt is never read for a host the owner chose to read"
