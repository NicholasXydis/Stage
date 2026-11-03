import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field

from stage.domain import (
    Company,
    CompanyDeferred,
    CompanyFailed,
    CompanyFinished,
    CompanyUnchanged,
    Platform,
)
from stage.domain.text import plain_error
from stage.storage import AsyncRepository

AKAMAI_PLATFORMS = frozenset({Platform.WORKDAY})


@dataclass(frozen=True, slots=True)
class BoardProbe:
    source: str
    company: str
    fetched: int = 0
    error: str = ""
    degraded: str = ""
    unchanged: bool = False
    unreachable: bool = False
    declared_empty: bool = False

    @property
    def note(self) -> str:
        return plain_error(self.error) if self.error else self.degraded

    @property
    def is_failure(self) -> bool:
        return bool(self.error) and not self.unreachable

    @property
    def is_unreachable(self) -> bool:
        return bool(self.error) and self.unreachable

    @property
    def is_empty(self) -> bool:
        return (
            not self.error and not self.unchanged and not self.declared_empty and self.fetched == 0
        )


@dataclass(frozen=True, slots=True)
class CanaryReport:
    probes: tuple[BoardProbe, ...] = field(default_factory=tuple)
    skipped_platforms: tuple[str, ...] = field(default_factory=tuple)
    timed_out: bool = False

    @property
    def failures(self) -> tuple[BoardProbe, ...]:
        return tuple(probe for probe in self.probes if probe.is_failure)

    @property
    def empties(self) -> tuple[BoardProbe, ...]:
        return tuple(probe for probe in self.probes if probe.is_empty)

    @property
    def unreachable(self) -> tuple[BoardProbe, ...]:
        return tuple(probe for probe in self.probes if probe.is_unreachable)

    @property
    def passed(self) -> bool:
        return not (self.failures or self.empties or self.timed_out)


def _probe_key(company: Company) -> tuple[str, str]:
    if company.platform is Platform.CUSTOM_JSON and company.custom is not None:
        return (company.platform.value, company.custom.fmt)
    return (company.platform.value, "")


def select_probes(
    companies: Sequence[Company], *, exclude: frozenset[Platform] = AKAMAI_PLATFORMS
) -> tuple[list[Company], tuple[str, ...]]:
    chosen: dict[tuple[str, str], Company] = {}
    for company in sorted(
        (entry for entry in companies if entry.enabled),
        key=lambda entry: entry.registry_key,
    ):
        if company.platform in exclude:
            continue
        chosen.setdefault(_probe_key(company), company)
    return (
        [chosen[key] for key in sorted(chosen)],
        tuple(sorted(platform.value for platform in exclude)),
    )


async def canary(
    repository: AsyncRepository,
    companies: Sequence[Company],
    *,
    exclude: frozenset[Platform] = AKAMAI_PLATFORMS,
    timeout: float | None = None,
) -> CanaryReport:
    selected, skipped = select_probes(companies, exclude=exclude)
    probes: dict[tuple[str, str], BoardProbe] = {}
    timed_out = False
    try:
        async with asyncio.timeout(timeout):
            await _probe(repository, selected, probes)
    except TimeoutError:
        timed_out = True
    return CanaryReport(
        probes=tuple(probes[key] for key in sorted(probes)),
        skipped_platforms=skipped,
        timed_out=timed_out,
    )


async def _probe(
    repository: AsyncRepository,
    selected: Sequence[Company],
    probes: dict[tuple[str, str], BoardProbe],
) -> None:
    from stage.services.sync import sync

    async for event in sync(
        repository, selected, close_orphans=False, still_open=False, force_refresh=True
    ):
        match event:
            case CompanyFinished():
                probe = BoardProbe(
                    event.source,
                    event.company,
                    fetched=event.fetched,
                    degraded=event.degraded,
                    declared_empty=event.declared_empty,
                )
            case CompanyFailed():
                probe = BoardProbe(
                    event.source, event.company, error=event.error, unreachable=event.unreachable
                )
            case CompanyDeferred():
                probe = BoardProbe(
                    event.source, event.company, error=event.reason, unreachable=True
                )
            case CompanyUnchanged():
                probe = BoardProbe(event.source, event.company, unchanged=True)
            case _:
                continue
        probes[(event.source, event.company)] = probe


__all__ = ["AKAMAI_PLATFORMS", "BoardProbe", "CanaryReport", "canary", "select_probes"]
