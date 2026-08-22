from collections.abc import Sequence
from dataclasses import dataclass, field

from stage.domain import (
    Company,
    CompanyFailed,
    CompanyFinished,
    CompanyUnchanged,
    Platform,
)
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

    @property
    def is_failure(self) -> bool:
        return bool(self.error)

    @property
    def is_empty(self) -> bool:
        return not self.error and not self.unchanged and self.fetched == 0


@dataclass(frozen=True, slots=True)
class CanaryReport:
    probes: tuple[BoardProbe, ...] = field(default_factory=tuple)
    skipped_platforms: tuple[str, ...] = field(default_factory=tuple)

    @property
    def failures(self) -> tuple[BoardProbe, ...]:
        return tuple(probe for probe in self.probes if probe.is_failure)

    @property
    def empties(self) -> tuple[BoardProbe, ...]:
        return tuple(probe for probe in self.probes if probe.is_empty)

    @property
    def passed(self) -> bool:
        return not (self.failures or self.empties)


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
) -> CanaryReport:
    from stage.services.sync import sync

    selected, skipped = select_probes(companies, exclude=exclude)
    probes: dict[tuple[str, str], BoardProbe] = {}

    async for event in sync(repository, selected):
        if isinstance(event, CompanyFinished):
            probes[(event.source, event.company)] = BoardProbe(
                source=event.source,
                company=event.company,
                fetched=event.fetched,
                degraded=event.degraded,
            )
        elif isinstance(event, CompanyFailed):
            probes[(event.source, event.company)] = BoardProbe(
                source=event.source, company=event.company, error=event.error
            )
        elif isinstance(event, CompanyUnchanged):
            probes[(event.source, event.company)] = BoardProbe(
                source=event.source, company=event.company, unchanged=True
            )

    return CanaryReport(
        probes=tuple(probes[key] for key in sorted(probes)), skipped_platforms=skipped
    )


__all__ = ["AKAMAI_PLATFORMS", "BoardProbe", "CanaryReport", "canary", "select_probes"]
