import sys
from collections.abc import Sequence
from dataclasses import asdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stage.services.canary import CanaryReport
    from stage.services.coverage import CoverageReport
    from stage.services.health import DoctorReport, StatsReport
    from stage.services.query import PostingDetail

from stage.domain import Job, QuarantinedJob, RateState, VisitState
from stage.domain.text import dump


def emit(payload: str) -> None:
    output = f"{payload}\n"
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        sys.stdout.write(output)
        return
    buffer.write(output.encode("utf-8"))


def fail(message: str) -> None:
    from stage.domain.text import sanitize

    sys.stderr.write(f"{sanitize(message)}\n")


def jobs_to_json(jobs: Sequence[Job]) -> str:
    return dump([asdict(job) for job in jobs])


def posting_to_json(detail: "PostingDetail") -> str:
    return dump(
        {
            "job": asdict(detail.job),
            "canonical": asdict(detail.canonical) if detail.canonical else None,
            "duplicates": [asdict(job) for job in detail.duplicates],
        }
    )


def quarantine_to_json(entries: Sequence[QuarantinedJob]) -> str:
    return dump([asdict(entry) for entry in entries])


def coverage_to_json(report: "CoverageReport") -> str:
    return dump(
        {
            "enabled": report.enabled,
            "disabled": report.disabled,
            "stale_after_days": report.stale_after_days,
            "rows": [asdict(row) for row in report.rows],
            "unregistered": [asdict(row) for row in report.unregistered],
            "classifications": [asdict(entry) for entry in report.classifications],
        }
    )


def health_to_json(report: "DoctorReport") -> str:
    return dump(
        {
            "schema_version": report.schema_version,
            "last_sync_at": report.last_sync_at,
            "never_synced": report.never_synced,
            "healthy": report.is_healthy,
            "warnings": report.warnings,
            "due_for_recheck": list(report.due_for_recheck),
            "workday_crawls": [asdict(crawl) for crawl in report.workday_crawls],
            "integrity": [asdict(finding) for finding in report.integrity],
            "blocks": [asdict(state) for state in report.blocks],
            "sources": [
                {
                    **asdict(source),
                    "cache_hit_ratio": source.cache_hit_ratio,
                    "success_rate": source.success_rate,
                }
                for source in report.sources
            ],
        }
    )


def sources_to_json(
    report: "DoctorReport",
    states: Sequence[RateState],
    *,
    include_boards: bool,
    rate_states_cleared: int,
    cache_validators_cleared: int,
) -> str:
    sources = []
    for source in report.sources:
        payload = asdict(source)
        payload.pop("boards")
        payload["cache_hit_ratio"] = source.cache_hit_ratio
        payload["success_rate"] = source.success_rate
        sources.append(payload)
    boards = (
        [
            asdict(board)
            for source in report.sources
            for board in source.boards
            if board.state is not VisitState.HEALTHY
        ]
        if include_boards
        else []
    )
    return dump(
        {
            "healthy": report.is_healthy,
            "stale_after_days": report.stale_after_days,
            "sources": sources,
            "rate_states": [asdict(state) for state in states],
            "boards": boards,
            "workday_crawls": [asdict(crawl) for crawl in report.workday_crawls],
            "rate_states_cleared": rate_states_cleared,
            "cache_validators_cleared": cache_validators_cleared,
        }
    )


def stats_to_json(report: "StatsReport") -> str:
    return dump(
        {
            "schema_version": report.schema_version,
            "total_jobs": report.total_jobs,
            "duplicates": report.duplicates,
            "tombstones": report.tombstones,
            "cached_urls": report.cached_urls,
            "quarantined": report.quarantined,
            "composition": report.composition,
            "runs": [asdict(run) for run in report.runs],
        }
    )


def canary_to_json(report: "CanaryReport") -> str:
    return dump(
        {
            "passed": report.passed,
            "skipped_platforms": list(report.skipped_platforms),
            "probes": [
                {**asdict(probe), "failure": probe.is_failure, "empty": probe.is_empty}
                for probe in report.probes
            ],
        }
    )
