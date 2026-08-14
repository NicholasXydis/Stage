from dataclasses import dataclass

from stage.domain.enums import Platform, ProbeVerdict
from stage.domain.events import RequestLogged


@dataclass(frozen=True, slots=True)
class PlatformCandidate:
    platform: Platform
    slug: str
    workday_tenant: str | None = None
    workday_site: str | None = None
    workday_dc: str | None = None
    oracle_host: str | None = None
    oracle_site: str | None = None
    resolves_board: bool = True

    @property
    def label(self) -> str:
        if not self.resolves_board:
            return f"{self.platform.value}/{self.slug} (front end only — board unresolved)"
        if self.workday_tenant is not None:
            site = self.workday_site or "?"
            return f"{self.platform.value}/{self.workday_tenant}/{site}@{self.workday_dc or '?'}"
        if self.oracle_host is not None:
            return f"{self.platform.value}/{self.oracle_host}/{self.oracle_site or '?'}"
        return f"{self.platform.value}/{self.slug}"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    company: str
    candidate: PlatformCandidate
    verdict: ProbeVerdict
    url: str
    board_name: str = ""
    job_count: int | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class DiscoveryStarted:
    companies: tuple[str, ...]
    platforms: tuple[str, ...]
    probes_planned: int


@dataclass(frozen=True, slots=True)
class CandidateSkipped:
    company: str
    slug: str
    reason: str


@dataclass(frozen=True, slots=True)
class PlatformProbed:
    result: ProbeResult


@dataclass(frozen=True, slots=True)
class UrlResolved:
    url: str
    candidate: PlatformCandidate
    detail: str = ""


@dataclass(frozen=True, slots=True)
class UrlUnrecognized:
    url: str
    detail: str


@dataclass(frozen=True, slots=True)
class DiscoveryFinished:
    matched: tuple[ProbeResult, ...]
    unverified: tuple[ProbeResult, ...]
    rejected: tuple[ProbeResult, ...]
    missed: int
    errors: int
    requests: int
    elapsed_ms: float
    ceiling_hit: tuple[str, ...] = ()
    non_json: tuple[tuple[str, int], ...] = ()


DiscoveryEvent = (
    DiscoveryStarted
    | CandidateSkipped
    | PlatformProbed
    | RequestLogged
    | UrlResolved
    | UrlUnrecognized
    | DiscoveryFinished
)
