from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from statistics import median

MIN_VOLUME_HISTORY = 3
VOLUME_DROP_RATIO = 0.5
STALE_AFTER_DAYS = 14
UNRECORDED_VOLUME = -1


class VolumeVerdict(StrEnum):
    HEALTHY = "healthy"
    DROPPED = "dropped"
    COLLAPSED = "collapsed"
    UNPROVEN = "unproven"


class VisitState(StrEnum):
    HEALTHY = "healthy"
    STALE = "stale"
    FAILING = "failing"


@dataclass(frozen=True, slots=True)
class VolumePoint:
    stored: int
    deferred: int = 0
    blocked: bool = False

    @property
    def is_evidence(self) -> bool:
        return self.stored > UNRECORDED_VOLUME and self.deferred == 0 and not self.blocked


@dataclass(frozen=True, slots=True)
class VolumeSignal:
    source: str
    verdict: VolumeVerdict
    latest: int
    baseline: float
    samples: int
    detail: str = ""

    @property
    def is_alert(self) -> bool:
        return self.verdict in (VolumeVerdict.DROPPED, VolumeVerdict.COLLAPSED)


def assess_volume(source: str, history: list[VolumePoint]) -> VolumeSignal:
    evidence = [point for point in history if point.is_evidence]
    if not evidence:
        return VolumeSignal(
            source=source,
            verdict=VolumeVerdict.UNPROVEN,
            latest=UNRECORDED_VOLUME,
            baseline=0.0,
            samples=0,
            detail=_unproven_reason(history),
        )

    latest = evidence[0].stored
    prior = [point.stored for point in evidence[1:]]
    if len(prior) < MIN_VOLUME_HISTORY:
        return VolumeSignal(
            source=source,
            verdict=VolumeVerdict.UNPROVEN,
            latest=latest,
            baseline=0.0,
            samples=len(prior),
            detail=f"{len(prior)} comparable run(s), {MIN_VOLUME_HISTORY} needed",
        )

    baseline = float(median(prior))
    if baseline <= 0:
        return VolumeSignal(
            source=source,
            verdict=VolumeVerdict.HEALTHY,
            latest=latest,
            baseline=baseline,
            samples=len(prior),
            detail="has never stored anything",
        )

    if latest == 0:
        return VolumeSignal(
            source=source,
            verdict=VolumeVerdict.COLLAPSED,
            latest=latest,
            baseline=baseline,
            samples=len(prior),
            detail=f"stores nothing after a median of {baseline:.0f} across {len(prior)} runs",
        )

    if latest < baseline * VOLUME_DROP_RATIO:
        return VolumeSignal(
            source=source,
            verdict=VolumeVerdict.DROPPED,
            latest=latest,
            baseline=baseline,
            samples=len(prior),
            detail=f"stores {latest} against a median of {baseline:.0f}",
        )

    return VolumeSignal(
        source=source,
        verdict=VolumeVerdict.HEALTHY,
        latest=latest,
        baseline=baseline,
        samples=len(prior),
    )


def _unproven_reason(history: list[VolumePoint]) -> str:
    if not history:
        return "no runs on record"
    if any(point.blocked for point in history):
        return "every run was blocked, which is a throttle not a drop"
    if any(point.deferred for point in history):
        return "every run deferred members, so rotation explains it"
    return "no run has recorded a stored count yet"


def classify_visit(
    last_success_at: datetime | None,
    consecutive_failures: int,
    now: datetime,
    stale_after_days: int = STALE_AFTER_DAYS,
) -> VisitState:
    if last_success_at is None:
        return VisitState.FAILING
    if consecutive_failures > 0:
        return VisitState.FAILING
    if now - last_success_at > timedelta(days=stale_after_days):
        return VisitState.STALE
    return VisitState.HEALTHY


@dataclass(frozen=True, slots=True)
class IntegrityRepair:
    check: str
    repaired: int
    detail: str = ""


@dataclass(frozen=True, slots=True)
class IntegrityFinding:
    check: str
    count: int
    detail: str

    @property
    def is_clean(self) -> bool:
        return self.count == 0


__all__ = [
    "MIN_VOLUME_HISTORY",
    "STALE_AFTER_DAYS",
    "UNRECORDED_VOLUME",
    "VOLUME_DROP_RATIO",
    "IntegrityFinding",
    "VisitState",
    "VolumePoint",
    "VolumeSignal",
    "VolumeVerdict",
    "assess_volume",
    "classify_visit",
]
