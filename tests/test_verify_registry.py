from collections.abc import AsyncIterator, Callable

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
            posture=posture,
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
