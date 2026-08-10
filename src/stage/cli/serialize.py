import sys
from collections.abc import Sequence
from dataclasses import asdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stage.services.canary import CanaryReport
    from stage.services.coverage import CoverageReport
    from stage.services.health import DoctorReport, StatsReport
    from stage.services.query import PostingDetail

from stage.domain import Job, QuarantinedJob
from stage.domain.text import dump


def emit(payload: str) -> None:
    sys.stdout.write(f"{payload}\n")


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
