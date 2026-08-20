from collections.abc import Iterable
from dataclasses import dataclass, replace
from types import MappingProxyType

CEILING_BACKSTOP = 2000
NORMAL_REFRESH_H = 4.0
CONSERVATIVE_REFRESH_H = 10.0


class UnknownProfileError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class RatePosture:
    concurrency: int = 3
    min_interval_s: float = 0.25
    max_requests_per_run: int = 300
    refresh_interval_h: float = NORMAL_REFRESH_H
    requests_per_board: int = 2

    def strictest(self, other: "RatePosture") -> "RatePosture":
        return RatePosture(
            concurrency=min(self.concurrency, other.concurrency),
            min_interval_s=max(self.min_interval_s, other.min_interval_s),
            max_requests_per_run=min(self.max_requests_per_run, other.max_requests_per_run),
            refresh_interval_h=max(self.refresh_interval_h, other.refresh_interval_h),
            requests_per_board=min(self.requests_per_board, other.requests_per_board),
        )

    def sized_for(self, boards: int, reserve: int = 0) -> "RatePosture":
        if boards < 1:
            return self
        derived = boards * self.requests_per_board + reserve
        return replace(
            self,
            max_requests_per_run=min(CEILING_BACKSTOP, max(self.max_requests_per_run, derived)),
        )


STANDARD = RatePosture(concurrency=3, min_interval_s=0.25, max_requests_per_run=300)
BROAD = RatePosture(concurrency=3, min_interval_s=0.25, max_requests_per_run=450)
MODERATE = RatePosture(concurrency=2, min_interval_s=0.4, max_requests_per_run=150)
PAGINATED = RatePosture(
    concurrency=2, min_interval_s=0.4, max_requests_per_run=250, requests_per_board=3
)
CONSERVATIVE = RatePosture(
    concurrency=1,
    min_interval_s=1.0,
    max_requests_per_run=80,
    refresh_interval_h=CONSERVATIVE_REFRESH_H,
    requests_per_board=1,
)
WORKDAY = RatePosture(
    concurrency=2, min_interval_s=1.5, max_requests_per_run=500, requests_per_board=2
)
FEEDS = RatePosture(
    concurrency=2, min_interval_s=0.5, max_requests_per_run=20, refresh_interval_h=0.0
)

JOBBANK = RatePosture(
    concurrency=1,
    min_interval_s=5.0,
    max_requests_per_run=120,
    refresh_interval_h=0.0,
    requests_per_board=1,
)

DISCOVERY = RatePosture(
    concurrency=1, min_interval_s=1.0, max_requests_per_run=60, refresh_interval_h=0.0
)

PROFILES: MappingProxyType[str, RatePosture] = MappingProxyType(
    {
        "standard": STANDARD,
        "broad": BROAD,
        "moderate": MODERATE,
        "paginated": PAGINATED,
        "conservative": CONSERVATIVE,
        "workday": WORKDAY,
        "feeds": FEEDS,
        "jobbank": JOBBANK,
        "discovery": DISCOVERY,
    }
)


def profile(name: str) -> RatePosture:
    try:
        return PROFILES[name]
    except KeyError as exc:
        known = ", ".join(sorted(PROFILES))
        raise UnknownProfileError(f"unknown rate profile {name!r} (known: {known})") from exc


def resolve(default: str, overrides: Iterable[str | None]) -> RatePosture:
    posture = profile(default)
    for name in overrides:
        if name is not None:
            posture = posture.strictest(profile(name))
    return posture
