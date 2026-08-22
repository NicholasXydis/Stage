from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from stage.domain import (
    Company,
    CompanyFailed,
    CompanyFinished,
    CompanyUnchanged,
    Platform,
    SyncEvent,
)
from stage.services import canary as canary_module

EVENTS: tuple[SyncEvent, ...] = (
    CompanyFinished(source="greenhouse", company="Acme", fetched=12, elapsed_ms=1.0),
    CompanyFinished(
        source="lever", company="Beta", fetched=0, elapsed_ms=1.0, degraded="one page skipped"
    ),
    CompanyFailed(source="ashby", company="Gamma", error="HTTP 500", elapsed_ms=1.0),
    CompanyUnchanged(source="workable", company="Delta", elapsed_ms=1.0),
)


def _companies() -> list[Company]:
    return [
        Company(name="Acme", platform=Platform.GREENHOUSE, slug="acme"),
        Company(name="Beta", platform=Platform.LEVER, slug="beta"),
        Company(name="Gamma", platform=Platform.ASHBY, slug="gamma"),
        Company(name="Delta", platform=Platform.WORKABLE, slug="delta"),
    ]


def _fake_sync(events: Sequence[SyncEvent]) -> Any:
    async def sync(
        repository: Any, companies: Sequence[Company], **kwargs: Any
    ) -> AsyncIterator[SyncEvent]:
        for event in events:
            yield event

    return sync


@pytest.fixture(autouse=True)
def _stub_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("stage.services.sync.sync", _fake_sync(EVENTS))


async def test_every_board_outcome_becomes_its_own_probe() -> None:
    report = await canary_module.canary(None, _companies())  # type: ignore[arg-type]
    keyed = {(probe.source, probe.company): probe for probe in report.probes}
    assert len(keyed) == 4, "one probe per board is the whole sample"
    assert keyed[("greenhouse", "Acme")].fetched == 12, "the fetched count stopped being carried"
    assert keyed[("lever", "Beta")].degraded == "one page skipped", "the degraded note was lost"
    assert keyed[("ashby", "Gamma")].error == "HTTP 500", "the failure reason was lost"
    assert keyed[("workable", "Delta")].unchanged, "a 304 board was not recorded as unchanged"


async def test_an_unchanged_board_is_not_reported_as_an_empty_one() -> None:
    report = await canary_module.canary(None, _companies())  # type: ignore[arg-type]
    empties = {probe.company for probe in report.empties}
    assert "Delta" not in empties, "an unchanged board has postings we simply did not refetch"
    assert "Beta" in empties, "a board that answered with zero postings is empty"


async def test_probes_are_ordered_so_two_runs_read_the_same() -> None:
    first = await canary_module.canary(None, _companies())  # type: ignore[arg-type]
    second = await canary_module.canary(None, _companies())  # type: ignore[arg-type]
    keys = [(probe.source, probe.company) for probe in first.probes]
    assert keys == sorted(keys), "probe order must be stable, not arrival order"
    assert keys == [(probe.source, probe.company) for probe in second.probes]
