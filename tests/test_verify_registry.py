from collections.abc import AsyncIterator, Callable
from dataclasses import replace

import httpx
import pytest

from stage.domain import (
    Company,
    DiscoveryEvent,
    DiscoveryFinished,
    DiscoveryStarted,
    Platform,
    PlatformProbed,
    ProbeVerdict,
)
from stage.http import HttpClient, RatePosture, ValidatorCache
from stage.services.discover import ClientFactory, NoMatchingCompanyError, verify_registry

Handler = Callable[[httpx.Request], httpx.Response]

ROWS = (
    Company(name="Faire", platform=Platform.GREENHOUSE, slug="faire"),
    Company(name="Coveo", platform=Platform.LEVER, slug="coveo"),
)


def _client_factory(handler: Handler) -> ClientFactory:
    def factory(hosts: frozenset[str], posture: RatePosture) -> HttpClient:
        return HttpClient(
            allowed_hosts=hosts,
            posture=replace(posture, min_interval_s=0.0),
            cache=ValidatorCache(),
            transport=httpx.MockTransport(handler),
            jitter=False,
        )

    return factory


async def _collect(events: AsyncIterator[DiscoveryEvent]) -> list[DiscoveryEvent]:
    return [event async for event in events]


def _named(name: str) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": [{"id": 1}, {"id": 2}]})
        return httpx.Response(200, json={"name": name, "content": "", "jobs": [{"id": 1}]})

    return handler


def _probed(events: list[DiscoveryEvent]) -> list[PlatformProbed]:
    return [event for event in events if isinstance(event, PlatformProbed)]


async def test_a_registry_row_whose_board_still_names_it_verifies() -> None:
    events = await _collect(
        verify_registry(
            ROWS[:1],
            platforms=[Platform.GREENHOUSE],
            client_factory=_client_factory(_named("Faire")),
        )
    )
    started = events[0]
    assert isinstance(started, DiscoveryStarted), "verification must open with a start event"
    assert started.probes_planned == 1, "the planned count must match the selected rows"
    verdicts = [probe.result.verdict for probe in _probed(events)]
    assert verdicts == [ProbeVerdict.MATCH], "a board naming the employer must verify"
    finished = events[-1]
    assert isinstance(finished, DiscoveryFinished)
    assert len(finished.matched) == 1, "the match was not carried into the summary"


async def test_a_board_that_has_gone_missing_is_a_miss_not_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    events = await _collect(
        verify_registry(
            ROWS[:1], platforms=[Platform.GREENHOUSE], client_factory=_client_factory(handler)
        )
    )
    probes = _probed(events)
    assert [probe.result.verdict for probe in probes] == [ProbeVerdict.MISS], (
        "a 404 board is absent, which is different from a transport error"
    )
    finished = events[-1]
    assert isinstance(finished, DiscoveryFinished)
    assert finished.errors == 0, "an absent board must not be counted as an error"


async def test_a_transport_failure_is_an_error_not_an_absent_board() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    events = await _collect(
        verify_registry(
            ROWS[:1], platforms=[Platform.GREENHOUSE], client_factory=_client_factory(handler)
        )
    )
    assert [probe.result.verdict for probe in _probed(events)] == [ProbeVerdict.ERROR], (
        "a transport failure says nothing about whether the board exists"
    )


async def test_naming_a_company_that_holds_no_row_refuses_rather_than_verifying_nothing() -> None:
    with pytest.raises(NoMatchingCompanyError, match="Nowhere"):
        await _collect(
            verify_registry(ROWS, only=["Nowhere"], client_factory=_client_factory(_named("Faire")))
        )


async def test_an_excluded_platform_is_left_out_of_the_plan_entirely() -> None:
    events = await _collect(
        verify_registry(
            ROWS, excluded=[Platform.GREENHOUSE], client_factory=_client_factory(_named("Coveo"))
        )
    )
    started = events[0]
    assert isinstance(started, DiscoveryStarted)
    assert "greenhouse" not in started.platforms, "an excluded platform was still planned"
    assert started.companies == ("Coveo",), "the excluded row was still selected"


WORKDAY = Company(
    name="Zyx",
    platform=Platform.WORKDAY,
    slug="zyx",
    workday_tenant="zyx",
    workday_site="External",
    workday_dc="wd3",
)
ORACLE = Company(
    name="Qux",
    platform=Platform.ORACLE_CLOUD,
    slug="eeho",
    oracle_host="eeho.fa.us2.oraclecloud.com",
    oracle_site="CX_1",
)
JOBVITE = Company(name="Anthology", platform=Platform.JOBVITE, slug="anthology")
LISTED = (
    '<table class="jv-job-list"><tbody>'
    '<tr><td class="jv-job-list-name"><a href="/anthology/job/o1">Intern</a></td></tr>'
    '<tr><td class="jv-job-list-name"><a href="/anthology/job/o2">Co-op</a></td></tr>'
    "</tbody></table>"
)


def _boards(requests: list[httpx.Request]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host.endswith("myworkdayjobs.com"):
            return httpx.Response(200, json={"total": 7, "jobPostings": [{"title": "Intern"}]})
        if request.url.host.endswith("oraclecloud.com"):
            return httpx.Response(200, json={"items": [{"TotalJobsCount": 4}]})
        return httpx.Response(200, text=LISTED)

    return handler


async def test_workday_oracle_and_jobvite_boards_are_checked_with_one_request_each() -> None:
    requests: list[httpx.Request] = []
    events = await _collect(
        verify_registry(
            (WORKDAY, ORACLE, JOBVITE),
            client_factory=_client_factory(_boards(requests)),
            board_checks=True,
        )
    )

    results = {probe.result.company: probe.result for probe in _probed(events)}
    assert {name: result.job_count for name, result in results.items()} == {
        "Zyx": 7,
        "Qux": 4,
        "Anthology": 2,
    }
    assert {result.verdict for result in results.values()} == {ProbeVerdict.UNVERIFIED}
    assert results["Zyx"].candidate.workday_site == "External"
    assert results["Qux"].candidate.oracle_host == "eeho.fa.us2.oraclecloud.com"
    assert len(requests) == 3
    workday = next(item for item in requests if item.method == "POST")
    assert workday.url.path == "/wday/cxs/zyx/External/jobs"
    oracle = next(item for item in requests if "oraclecloud" in item.url.host)
    assert "siteNumber=CX_1" in oracle.url.params["finder"]


async def test_board_checks_stay_off_unless_asked() -> None:
    requests: list[httpx.Request] = []
    events = await _collect(
        verify_registry((WORKDAY, *ROWS[:1]), client_factory=_client_factory(_boards(requests)))
    )
    started = events[0]
    assert isinstance(started, DiscoveryStarted)
    assert started.companies == ("Faire",)


@pytest.mark.parametrize(
    ("page", "verdict"),
    [
        ('<p class="jv-text-center">There are no open jobs right now.</p>', ProbeVerdict.EMPTY),
        ("<html><body>Something else</body></html>", ProbeVerdict.MISS),
    ],
)
async def test_a_jobvite_page_without_a_listing_is_empty_or_unknown(
    page: str, verdict: ProbeVerdict
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page)

    events = await _collect(
        verify_registry((JOBVITE,), client_factory=_client_factory(handler), board_checks=True)
    )
    assert [probe.result.verdict for probe in _probed(events)] == [verdict]


async def test_a_gone_board_carries_its_status() -> None:
    from stage.services.discover import is_gone

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404 if "faire" in request.url.path else 403)

    events = await _collect(verify_registry(ROWS, client_factory=_client_factory(handler)))
    results = {probe.result.company: probe.result for probe in _probed(events)}
    assert results["Faire"].status == 404
    assert is_gone(results["Faire"])
    assert not is_gone(results["Coveo"])


async def test_a_board_with_unusable_coordinates_is_skipped() -> None:
    broken = Company(
        name="Broken",
        platform=Platform.WORKDAY,
        slug="broken",
        workday_tenant="broken",
        workday_site="has/slash",
        workday_dc="wd3",
    )
    events = await _collect(
        verify_registry((broken,), client_factory=_client_factory(_boards([])), board_checks=True)
    )
    started = events[0]
    assert isinstance(started, DiscoveryStarted)
    assert started.companies == ()


async def test_a_misspelt_company_is_offered_the_closest_registry_row() -> None:
    with pytest.raises(NoMatchingCompanyError) as caught:
        await _collect(
            verify_registry(ROWS, only=["Fiare"], client_factory=_client_factory(_named("Faire")))
        )
    assert str(caught.value) == 'No registry row is named "Fiare". Did you mean "Faire"?'


async def test_a_name_nothing_resembles_says_what_to_do() -> None:
    with pytest.raises(NoMatchingCompanyError) as caught:
        await _collect(
            verify_registry(ROWS, only=["Zzzzz"], client_factory=_client_factory(_named("Faire")))
        )
    assert "leave out --company" in str(caught.value)


async def test_a_failed_probe_explains_itself_without_a_class_name() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400)

    events = await _collect(
        verify_registry(
            ROWS[:1], platforms=[Platform.GREENHOUSE], client_factory=_client_factory(handler)
        )
    )
    [probe] = _probed(events)
    assert probe.result.verdict is ProbeVerdict.ERROR
    assert "returned 400" in probe.result.detail
    assert "Error" not in probe.result.detail
