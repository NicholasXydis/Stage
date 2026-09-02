import io
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from rich.console import Console

from stage.cli.render import (
    render_board_health,
    render_canary,
    render_doctor,
    render_rate_state,
    render_repairs,
    render_stats,
    render_workday_crawl_progress,
)
from stage.domain import (
    IntegrityFinding,
    IntegrityRepair,
    RateState,
    VisitState,
    VolumeVerdict,
)
from stage.domain.health import VolumeSignal
from stage.domain.workday import WorkdayCrawl
from stage.services.health import BoardHealth, DoctorReport, SourceHealth, StatsReport

if TYPE_CHECKING:
    from stage.services.canary import BoardProbe

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _paint(render: Callable[..., None], *args: Any, width: int = 100, **kwargs: Any) -> str:
    buffer = io.StringIO()
    render(Console(file=buffer, width=width, emoji=False), *args, **kwargs)
    return buffer.getvalue()


def _volume(verdict: VolumeVerdict = VolumeVerdict.UNPROVEN) -> VolumeSignal:
    return VolumeSignal(
        source="greenhouse",
        verdict=verdict,
        latest=10,
        baseline=10.0,
        samples=3,
        detail="volume dropped sharply",
    )


def _board(state: VisitState, error: str = "") -> BoardHealth:
    return BoardHealth(
        source="greenhouse",
        board="greenhouse:acme",
        label="Acme",
        state=state,
        last_success_at=None if state is VisitState.FAILING else NOW,
        consecutive_failures=4 if state is VisitState.FAILING else 0,
        last_error=error,
    )


def _source(**overrides: object) -> SourceHealth:
    defaults: dict[str, object] = {
        "source": "greenhouse",
        "stored": 12,
        "volume": _volume(),
        "requests": 30,
        "not_modified": 4,
        "latency_p50_ms": 120.0,
        "latency_p95_ms": 900.0,
        "errors": 0,
        "tightenings": 0,
        "deferred": 0,
        "blocked": False,
    }
    defaults.update(overrides)
    return SourceHealth(**defaults)  # type: ignore[arg-type]


def _report(**overrides: object) -> DoctorReport:
    defaults: dict[str, object] = {
        "schema_version": 1,
        "last_sync_at": NOW - timedelta(hours=2),
        "integrity": (),
        "sources": (_source(),),
        "blocks": (),
        "never_synced": False,
    }
    defaults.update(overrides)
    return DoctorReport(**defaults)  # type: ignore[arg-type]


def test_a_healthy_report_says_so() -> None:
    output = _paint(render_doctor, _report(), NOW)

    assert "Healthy" in output


def test_integrity_problems_are_named() -> None:
    finding = IntegrityFinding(check="orphan rows", count=3, detail="3 rows without a parent")

    output = _paint(render_doctor, _report(integrity=(finding,)), NOW)

    assert "orphan rows" in output


def test_a_never_synced_database_explains_itself() -> None:
    output = _paint(render_doctor, _report(never_synced=True, last_sync_at=None), NOW)

    assert "sync" in output


def test_a_blocked_bucket_is_reported_with_its_reason() -> None:
    blocked = RateState(
        bucket="boards.greenhouse.io",
        consecutive_failures=5,
        reason="429 for every request",
        updated_at=NOW,
        blocked_until=NOW + timedelta(hours=1),
    )

    output = _paint(render_doctor, _report(blocks=(blocked,)), NOW)

    assert "boards.greenhouse.io" in output


def test_failing_boards_are_listed_with_a_next_step() -> None:
    report = _report(sources=(_source(boards=(_board(VisitState.FAILING, "404"),)),))

    output = _paint(render_doctor, report, NOW)

    assert "Acme" in output


def test_a_volume_alert_names_the_source() -> None:
    report = _report(sources=(_source(volume=_volume(VolumeVerdict.COLLAPSED)),))

    output = _paint(render_doctor, report, NOW)

    assert "greenhouse" in output


def test_rows_due_for_recheck_are_listed() -> None:
    output = _paint(render_doctor, _report(due_for_recheck=("Acme: stale note",)), NOW)

    assert "Acme" in output


def test_workday_crawl_progress_shows_the_cursor() -> None:
    crawl = WorkdayCrawl(
        board="workday:acme-careers",
        next_offset=120,
        total=800,
        facet_parameter="",
        facet_ids=(),
    )

    output = _paint(render_workday_crawl_progress, (crawl,))

    assert "120" in output


def test_workday_crawl_progress_handles_nothing_in_flight() -> None:
    assert _paint(render_workday_crawl_progress, ()) != ""


def test_board_health_lists_unhealthy_boards_only() -> None:
    sources = (_source(boards=(_board(VisitState.FAILING, "boom"), _board(VisitState.HEALTHY))),)

    output = _paint(render_board_health, sources, NOW)

    assert "Acme" in output


def test_rate_state_with_nothing_recorded_says_so() -> None:
    output = _paint(render_rate_state, (), NOW)

    assert "No rate state" in output


def test_a_tightened_bucket_is_shown() -> None:
    state = RateState(
        bucket="api.lever.co",
        consecutive_failures=2,
        reason="slow responses",
        updated_at=NOW,
        min_interval_override=2.5,
    )

    output = _paint(render_rate_state, (state,), NOW)

    assert "api.lever.co" in output


def test_repairs_are_reported_when_any_ran() -> None:
    repair = IntegrityRepair(check="orphans", repaired=2, detail="relinked")

    output = _paint(render_repairs, (repair,))

    assert "orphans" in output


def test_no_repairs_prints_nothing() -> None:
    assert _paint(render_repairs, ()) == ""


def _stats(**overrides: object) -> StatsReport:
    defaults: dict[str, object] = {
        "runs": (),
        "composition": {"source": {"greenhouse": 10, "lever": 5}},
        "total_jobs": 15,
        "duplicates": 2,
        "quarantined": {"not-an-internship": 40},
        "tombstones": 0,
        "cached_urls": 7,
        "schema_version": 1,
    }
    defaults.update(overrides)
    return StatsReport(**defaults)  # type: ignore[arg-type]


def test_stats_reports_the_totals() -> None:
    output = _paint(render_stats, _stats(), NOW)

    assert "15" in output


def test_stats_breakdown_is_capped_and_says_so() -> None:
    wide = {"source": {f"src{index}": index for index in range(30)}}

    output = _paint(render_stats, _stats(composition=wide), NOW, limit=5)

    assert "--all" in output


def test_stats_without_a_limit_lists_every_row() -> None:
    wide = {"source": {f"src{index}": index for index in range(30)}}

    output = _paint(render_stats, _stats(composition=wide), NOW, limit=None)

    assert "src29" in output


@pytest.mark.parametrize("width", [50, 80, 120, 200])
def test_doctor_never_overflows_the_terminal(width: int) -> None:
    report = _report(sources=(_source(boards=(_board(VisitState.FAILING, "e" * 200),)),))

    output = _paint(render_doctor, report, NOW, width=width)

    assert max(len(line.rstrip()) for line in output.split("\n")) <= width


def test_verbose_keeps_the_whole_board_error() -> None:
    error = "a very long failure message that would normally be shortened for the table " * 3
    report = _report(sources=(_source(boards=(_board(VisitState.FAILING, error),)),))

    brief = _paint(render_doctor, report, NOW, width=200)
    full = _paint(render_doctor, report, NOW, width=200, verbose=True)

    assert len(full) > len(brief)


def _probe(**overrides: object) -> "BoardProbe":
    from stage.services.canary import BoardProbe

    defaults: dict[str, object] = {
        "source": "greenhouse",
        "company": "Acme",
        "fetched": 12,
        "error": "",
        "degraded": "",
        "unchanged": False,
        "unreachable": False,
    }
    defaults.update(overrides)
    return BoardProbe(**defaults)  # type: ignore[arg-type]


def test_a_passing_canary_reports_success() -> None:
    from stage.services.canary import CanaryReport

    output = _paint(render_canary, CanaryReport(probes=(_probe(),), skipped_platforms=()))

    assert "greenhouse" in output


def test_a_canary_failure_names_the_board() -> None:
    from stage.services.canary import CanaryReport

    report = CanaryReport(
        probes=(_probe(fetched=0, error="selector matched no rows"),),
        skipped_platforms=(),
    )

    output = _paint(render_canary, report)

    assert "Acme" in output


def test_an_unreachable_board_is_blamed_on_the_publisher() -> None:
    from stage.services.canary import CanaryReport

    report = CanaryReport(
        probes=(_probe(fetched=0, error="timeout", unreachable=True),),
        skipped_platforms=(),
    )

    output = _paint(render_canary, report)

    assert "their server" in output


def test_skipped_platforms_are_listed() -> None:
    from stage.services.canary import CanaryReport

    report = CanaryReport(probes=(_probe(),), skipped_platforms=("workday",))

    output = _paint(render_canary, report)

    assert "workday" in output


def test_a_canary_note_is_kept_whole_when_verbose() -> None:
    from stage.services.canary import CanaryReport

    note = "a long degradation note that the table would otherwise shorten " * 3
    report = CanaryReport(probes=(_probe(degraded=note),), skipped_platforms=())

    brief = _paint(render_canary, report, width=200)
    full = _paint(render_canary, report, width=200, verbose=True)

    assert len(full) > len(brief)
    assert note.strip() not in " ".join(brief.split())
    assert note.strip() in " ".join(full.split())


async def test_sync_lines_carry_a_running_count() -> None:
    import io
    from collections.abc import AsyncIterator
    from datetime import UTC, datetime

    from rich.console import Console

    from stage.cli.render import render_sync
    from stage.domain import (
        CompanyFinished,
        SyncEvent,
        SyncFinished,
        SyncOutcome,
        SyncStarted,
    )

    now = datetime.now(UTC)

    async def events() -> "AsyncIterator[SyncEvent]":
        yield SyncStarted(sources=("greenhouse",), companies=3, started_at=now)
        for name in ("Acme", "Globex", "Initech"):
            yield CompanyFinished(
                source="greenhouse", company=name, fetched=1, elapsed_ms=10.0, degraded=""
            )
        yield SyncFinished(
            outcome=SyncOutcome.SUCCESS,
            dry_run=False,
            added=3,
            updated=0,
            closed=0,
            failed_sources=(),
            elapsed_ms=30.0,
        )

    buffer = io.StringIO()
    await render_sync(Console(file=buffer, width=200, emoji=False), events())
    rendered = buffer.getvalue()

    assert "1/3" in rendered
    assert "3/3" in rendered


def test_the_banner_follows_the_terminal_foreground() -> None:
    import io
    import re

    from rich.console import Console

    from stage.cli.render import splash

    buffer = io.StringIO()
    console = Console(
        file=buffer, force_terminal=True, color_system="truecolor", width=80, emoji=False
    )
    splash(console)
    painted = buffer.getvalue()

    assert not re.search(r"38;2;\d+;\d+;\d+", painted)
    assert "\x1b[1;39m" in painted


def test_the_banner_accent_is_the_terminal_default() -> None:
    from stage.banner import ACCENT

    assert ACCENT == "default"
