from collections.abc import Iterable
from dataclasses import dataclass
from types import MappingProxyType


class UnknownProfileError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class RatePosture:
    concurrency: int = 3
    min_interval_s: float = 0.25
    max_requests_per_run: int = 300

    def strictest(self, other: "RatePosture") -> "RatePosture":
        return RatePosture(
            concurrency=min(self.concurrency, other.concurrency),
            min_interval_s=max(self.min_interval_s, other.min_interval_s),
            max_requests_per_run=min(self.max_requests_per_run, other.max_requests_per_run),
        )


STANDARD = RatePosture(concurrency=3, min_interval_s=0.25, max_requests_per_run=300)
MODERATE = RatePosture(concurrency=2, min_interval_s=0.4, max_requests_per_run=150)
CONSERVATIVE = RatePosture(concurrency=1, min_interval_s=1.0, max_requests_per_run=80)
WORKDAY = RatePosture(concurrency=2, min_interval_s=1.5, max_requests_per_run=120)
FEEDS = RatePosture(concurrency=2, min_interval_s=0.5, max_requests_per_run=20)

DISCOVERY = RatePosture(concurrency=1, min_interval_s=1.0, max_requests_per_run=60)

PROFILES: MappingProxyType[str, RatePosture] = MappingProxyType(
    {
        "standard": STANDARD,
        "moderate": MODERATE,
        "conservative": CONSERVATIVE,
        "workday": WORKDAY,
        "feeds": FEEDS,
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
