import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, fields
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from stage.cli.schedule import ScheduleStatus
    from stage.domain.preferences import Preferences
    from stage.services.boards import BoardListSummary
    from stage.services.canary import CanaryReport
    from stage.services.connect import AppHealth
    from stage.services.coverage import CoverageReport
    from stage.services.health import DoctorReport, StatsReport
    from stage.services.query import PostingDetail

from stage.domain import Job, QuarantinedJob, RateState, VisitState
from stage.domain.text import dump

ERROR_FIELDS = frozenset({"error", "last_error", "reason"})


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


def _posting_payload(
    detail: "PostingDetail", tracked: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "job": asdict(detail.job),
        "canonical": asdict(detail.canonical) if detail.canonical else None,
        "duplicates": [asdict(job) for job in detail.duplicates],
        "tracked": tracked,
    }


def tracked_only_payload(tracked: dict[str, Any]) -> dict[str, Any]:
    return {"job": None, "canonical": None, "duplicates": [], "tracked": tracked}


def quarantine_to_json(entries: Sequence[QuarantinedJob]) -> str:
    names = [field.name for field in fields(QuarantinedJob)]
    return dump([{name: getattr(entry, name) for name in names} for entry in entries])


def preferences_to_json(preferences: "Preferences | None", *, cleared: bool = False) -> str:
    if preferences is None:
        return dump({"cleared": cleared})
    return dump(
        {
            "locations": [location.value for location in preferences.locations],
            "roles": [role.value for role in preferences.roles],
            "terms": list(preferences.terms),
            "follow": [company.name for company in preferences.follow],
            "avoid": [company.name for company in preferences.avoid],
            "about": preferences.about,
        }
    )


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


def health_to_json(
    report: "DoctorReport",
    apps: "Sequence[AppHealth]" = (),
    boards: "BoardListSummary | None" = None,
) -> str:
    extra: dict[str, object] = {}
    if boards is not None:
        extra["your_boards"] = {
            "path": str(boards.path),
            "boards": boards.boards,
            "enabled": boards.enabled,
            "disabled": boards.disabled,
            "added_last_7_days": boards.recent,
            "shipped_with_stage": boards.shipped,
            "problem": boards.problem or None,
        }
    return _dump_plain(
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
            "ai_apps": [
                {
                    "app": app.key,
                    "name": app.label,
                    "status": app.status.value,
                    "detail": app.detail,
                    "actions": app.actions,
                }
                for app in apps
            ],
            **extra,
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
    return _dump_plain(
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


def schedule_to_json(report: "ScheduleStatus") -> str:
    return dump(
        {
            "backend": report.backend,
            "log_dir": str(report.log_dir),
            "actions": [
                {
                    "key": action.key,
                    "label": action.label,
                    "cadence": action.cadence,
                    "time": action.time,
                    "enabled": enabled,
                    "installed": installed,
                    "needs_update": (
                        report.needs_update[index] if index < len(report.needs_update) else False
                    ),
                    "stale_interpreter": (
                        report.stale_interpreter[index]
                        if index < len(report.stale_interpreter)
                        else ""
                    ),
                    "run": report.states[index] if index < len(report.states) else None,
                }
                for index, (action, enabled, installed) in enumerate(report.actions)
            ],
        }
    )


def canary_to_json(report: "CanaryReport") -> str:
    return dump(
        {
            "passed": report.passed,
            "timed_out": report.timed_out,
            "skipped_platforms": list(report.skipped_platforms),
            "probes": [
                {**asdict(probe), "failure": probe.is_failure, "empty": probe.is_empty}
                for probe in report.probes
            ],
        }
    )


def _plain_errors(value: object, plain: Callable[[str], str]) -> object:
    if isinstance(value, dict):
        return {
            key: plain(item)
            if key in ERROR_FIELDS and isinstance(item, str) and item
            else _plain_errors(item, plain)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_plain_errors(item, plain) for item in value]
    return value


def _dump_plain(payload: object) -> str:
    from stage.cli.render import error_text

    return dump(_plain_errors(payload, error_text))
