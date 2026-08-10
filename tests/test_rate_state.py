from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from stage.domain import (
    BLOCK_CAP_S,
    BREAKER_THRESHOLD,
    Company,
    Platform,
    RateState,
    SourceBlocked,
    SyncFinished,
    SyncOutcome,
    block_duration,
    blocked,
    decay,
)
from stage.http import BucketBlockedError, HostBudget, HttpClient, RatePosture, profile
from stage.http.client import MAX_INTERVAL_S
from stage.storage import SourceBatch, open_repository

WORKDAY_BASELINE = 1.5


def _state(bucket: str = "workday", **kwargs: object) -> RateState:
    return RateState(bucket=bucket, updated_at=datetime(2026, 8, 3, 12, 0, tzinfo=UTC), **kwargs)  # type: ignore[arg-type]


def test_a_block_expires_rather_than_standing_forever() -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    state = _state(blocked_until=now + timedelta(minutes=5))
    assert state.is_blocked(now)
    assert state.is_blocked(now + timedelta(minutes=4, seconds=59))
    assert not state.is_blocked(now + timedelta(minutes=5))
    assert state.blocks_remaining_s(now) == pytest.approx(300.0)
    assert state.blocks_remaining_s(now + timedelta(hours=1)) == 0.0


def test_no_reachable_path_stores_a_block_longer_than_the_cap() -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    absurd = RateState(bucket="workday", updated_at=now, blocked_until=now + timedelta(days=400))
    assert absurd.blocked_until == now + timedelta(seconds=BLOCK_CAP_S)

    escalated = blocked(_state(), now=now, consecutive_failures=99, reason="429")
    assert escalated.blocked_until is not None
    assert (escalated.blocked_until - now).total_seconds() <= BLOCK_CAP_S


def test_block_duration_escalates_but_never_past_the_cap() -> None:
    at_threshold = block_duration(BREAKER_THRESHOLD)
    assert at_threshold == 300.0
    assert block_duration(BREAKER_THRESHOLD + 1) == 600.0
    assert block_duration(BREAKER_THRESHOLD + 2) == 1200.0
    assert block_duration(BREAKER_THRESHOLD + 40) == BLOCK_CAP_S
    assert at_threshold > 60.0, "a block shorter than a coffee break does not survive a re-run"


def test_a_tightened_interval_decays_toward_baseline_and_eventually_clears() -> None:
    start = 8.0
    assert start > WORKDAY_BASELINE

    first = decay(start, WORKDAY_BASELINE)
    assert first is not None
    assert first < start, "one clean run must move the override, not merely not raise it"

    override: float | None = start
    seen: list[float] = [start]
    for _ in range(20):
        if override is None:
            break
        override = decay(override, WORKDAY_BASELINE)
        if override is not None:
            seen.append(override)

    assert override is None, "decay must reach the configured rate, not asymptote above it"
    assert seen == sorted(seen, reverse=True), "every step must move toward baseline"
    assert len(seen) <= 12, "decay must not take so many runs that it is decay in name only"


def test_decay_clears_rather_than_leaving_a_floor_just_above_baseline() -> None:
    assert decay(WORKDAY_BASELINE * 1.01, WORKDAY_BASELINE) is None
    assert decay(WORKDAY_BASELINE, WORKDAY_BASELINE) is None


def test_a_clean_run_hands_back_a_smaller_override_than_it_was_seeded_with() -> None:
    posture = RatePosture(concurrency=2, min_interval_s=WORKDAY_BASELINE, max_requests_per_run=120)
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    seeded = _state(min_interval_override=8.0)

    budget = HostBudget(posture=posture, seed=seeded)
    assert budget.min_interval_s == 8.0, "a stored tightening must apply to this run"
    budget.requests = 4

    settled = budget.settle("workday", now)
    assert settled.min_interval_override is not None
    assert settled.min_interval_override < 8.0
    assert settled.blocked_until is None


def test_a_run_that_was_throttled_persists_the_tightening_instead_of_decaying_it() -> None:
    posture = RatePosture(concurrency=2, min_interval_s=WORKDAY_BASELINE, max_requests_per_run=120)
    budget = HostBudget(posture=posture)
    budget.requests = 4
    budget.tighten(2.0, rejected=True)

    settled = budget.settle("workday", datetime(2026, 8, 3, 12, 0, tzinfo=UTC))
    assert settled.min_interval_override == pytest.approx(3.0)


def test_a_latency_tightening_still_decays_so_the_interval_cannot_ratchet() -> None:
    posture = RatePosture(concurrency=2, min_interval_s=WORKDAY_BASELINE, max_requests_per_run=120)
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    override = WORKDAY_BASELINE

    for run in range(6):
        budget = HostBudget(posture=posture, seed=_state(min_interval_override=override))
        budget.requests = 40
        budget.tighten(1.5)
        settled = budget.settle("workday", now)
        if settled.min_interval_override is None:
            break
        assert settled.min_interval_override < MAX_INTERVAL_S, f"run {run} hit the ceiling"
        override = settled.min_interval_override

    assert override <= WORKDAY_BASELINE * 4, f"ratcheted to {override}s"


def test_a_run_that_never_reached_the_bucket_writes_nothing_for_it() -> None:
    client = HttpClient(allowed_hosts=frozenset({"example.test"}), jitter=False)
    client._budget_for("example.test")
    assert client.rate_state() == ()


def test_a_tripped_breaker_settles_into_a_block_with_the_failure_recorded() -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    budget = HostBudget(posture=profile("workday"))
    budget.requests = 6
    budget.last_error = "HTTP 429"
    for _ in range(BREAKER_THRESHOLD):
        budget.breaker.record_failure()

    settled = budget.settle("workday", now)
    assert settled.blocked_until is not None
    assert settled.blocked_until > now
    assert settled.consecutive_failures == BREAKER_THRESHOLD
    assert settled.reason == "HTTP 429"


def test_stored_failures_carry_over_so_a_bucket_gets_no_fresh_five_tries() -> None:
    budget = HostBudget(
        posture=profile("workday"), seed=_state(consecutive_failures=BREAKER_THRESHOLD - 1)
    )
    budget.breaker.record_failure()
    assert not budget.breaker.allows(), "escalation must survive process exit, not restart"


def test_workday_tenants_on_different_hostnames_share_one_bucket() -> None:
    hosts = frozenset({"acme.wd1.myworkdayjobs.com", "globex.wd3.myworkdayjobs.com"})
    client = HttpClient(allowed_hosts=hosts, posture=profile("workday"), bucket_key="workday")

    assert {client.bucket_for(host) for host in hosts} == {"workday"}
    first = client._budget_for(client.bucket_for("acme.wd1.myworkdayjobs.com"))
    second = client._budget_for(client.bucket_for("globex.wd3.myworkdayjobs.com"))
    assert first is second, "one budget means one ceiling and one stride across tenants"


def test_an_adapter_without_a_bucket_key_still_gets_one_bucket_per_host() -> None:
    client = HttpClient(allowed_hosts=frozenset({"a.test", "b.test"}))
    assert client.bucket_for("a.test") == "a.test"
    assert client._budget_for("a.test") is not client._budget_for("b.test")


@respx.mock
async def test_the_client_refuses_a_blocked_bucket_even_if_a_caller_forgets_to_check() -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    route = respx.get("https://acme.wd1.myworkdayjobs.com/jobs").mock(
        return_value=httpx.Response(200, json={})
    )
    async with HttpClient(
        allowed_hosts=frozenset({"acme.wd1.myworkdayjobs.com"}),
        posture=profile("workday"),
        bucket_key="workday",
        rate_state={"workday": _state(blocked_until=now + timedelta(hours=1), reason="HTTP 429")},
        now=now,
        jitter=False,
    ) as client:
        with pytest.raises(BucketBlockedError, match="workday"):
            await client.get_json("https://acme.wd1.myworkdayjobs.com/jobs")

    assert not route.called, "re-probing during an active throttle is what makes it durable"


async def test_rate_state_survives_the_process_and_keeps_its_cap(db_path: Path) -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="workday",
                run_started_at=now,
                rate_state=(
                    RateState(
                        bucket="workday",
                        updated_at=now,
                        blocked_until=now + timedelta(days=99),
                        min_interval_override=4.5,
                        consecutive_failures=6,
                        reason="HTTP 429",
                        rotation_cursor="globex",
                    ),
                ),
            )
        )

    async with open_repository(db_path) as repository:
        stored = await repository.load_rate_state()

    state = stored["workday"]
    assert state.min_interval_override == pytest.approx(4.5)
    assert state.consecutive_failures == 6
    assert state.rotation_cursor == "globex"
    assert state.blocked_until == now + timedelta(seconds=BLOCK_CAP_S)


async def test_clearing_drops_the_block_and_keeps_the_rotation_cursor(db_path: Path) -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="workday",
                run_started_at=now,
                rate_state=(
                    RateState(
                        bucket="workday",
                        updated_at=now,
                        blocked_until=now + timedelta(hours=6),
                        min_interval_override=4.5,
                        consecutive_failures=6,
                        reason="HTTP 429",
                        rotation_cursor="globex",
                    ),
                ),
            )
        )
        assert await repository.clear_rate_state("workday") == 1
        cleared = (await repository.load_rate_state())["workday"]

    assert cleared.blocked_until is None
    assert cleared.min_interval_override is None
    assert cleared.consecutive_failures == 0
    assert cleared.reason == ""
    assert cleared.rotation_cursor == "globex"


async def test_clearing_an_unknown_bucket_reports_nothing_rather_than_pretending(
    db_path: Path,
) -> None:
    async with open_repository(db_path) as repository:
        assert await repository.clear_rate_state("nobody") == 0


class _NeverCalledAdapter:
    name = "greenhouse"
    platform = Platform.GREENHOUSE
    rate_profile = "standard"
    hosts = frozenset({"boards-api.greenhouse.io"})
    bucket_key = ""

    max_requests_per_company = 1
    detail_budget = 0

    def hosts_for(self, companies: object) -> frozenset[str]:
        return self.hosts

    def plan(self, company: Company) -> tuple[str, ...]:
        return ("https://boards-api.greenhouse.io/v1/boards/acme/jobs",)

    async def fetch(self, company: Company, client: HttpClient, now: datetime) -> object:
        raise AssertionError("a blocked source must not be fetched")


@respx.mock
async def test_a_blocked_source_names_itself_and_when_it_clears(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, acme: Company
) -> None:
    from stage.services import sync as sync_module

    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    adapter = _NeverCalledAdapter()
    monkeypatch.setattr(sync_module, "adapter_for_platform", lambda _: adapter)
    monkeypatch.setattr(sync_module, "get_feeds", dict)

    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=now,
                rate_state=(
                    RateState(
                        bucket="boards-api.greenhouse.io",
                        updated_at=now,
                        blocked_until=now + timedelta(hours=2),
                        consecutive_failures=5,
                        reason="HTTP 429",
                    ),
                ),
            )
        )
        events = [event async for event in sync_module.sync(repository, [acme], now_fn=lambda: now)]

    announced = [event for event in events if isinstance(event, SourceBlocked)]
    assert len(announced) == 1
    assert announced[0].source == "greenhouse"
    assert announced[0].bucket == "boards-api.greenhouse.io"
    assert announced[0].blocked_until == now + timedelta(hours=2)
    assert announced[0].remaining_s == pytest.approx(7200.0)
    assert announced[0].reason == "HTTP 429"

    finished = [event for event in events if isinstance(event, SyncFinished)]
    assert finished[-1].outcome is not SyncOutcome.SUCCESS, (
        "a run that skipped a source is not a successful run"
    )


@respx.mock
async def test_an_expired_block_lets_the_source_run_again(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, acme: Company, run_time: datetime
) -> None:
    from stage.services import sync as sync_module

    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )
    monkeypatch.setattr(sync_module, "get_feeds", dict)

    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=run_time,
                rate_state=(
                    RateState(
                        bucket="boards-api.greenhouse.io",
                        updated_at=run_time,
                        blocked_until=now - timedelta(minutes=1),
                        consecutive_failures=5,
                        reason="HTTP 429",
                    ),
                ),
            )
        )
        events = [event async for event in sync_module.sync(repository, [acme], now_fn=lambda: now)]

    assert not [event for event in events if isinstance(event, SourceBlocked)]


def test_the_clear_is_reachable_from_the_command_line(db_path: Path) -> None:
    from typer.testing import CliRunner

    from stage.cli.app import app
    from stage.storage.sqlite_repo import SqliteRepository

    now = datetime.now(UTC)
    repository = SqliteRepository.connect(db_path)
    repository.apply_source_batch(
        SourceBatch(
            source="workday",
            run_started_at=now,
            rate_state=(
                RateState(
                    bucket="workday",
                    updated_at=now,
                    blocked_until=now + timedelta(days=400),
                    min_interval_override=4.5,
                    consecutive_failures=6,
                    reason="HTTP 429",
                    rotation_cursor="globex",
                ),
            ),
        )
    )
    repository.close()

    runner = CliRunner()
    listed = runner.invoke(app, ["sources", "--db", str(db_path)])
    assert listed.exit_code == 0, listed.stdout
    assert "workday" in listed.stdout
    assert "blocked" in listed.stdout

    cleared = runner.invoke(app, ["sources", "--clear", "workday", "--db", str(db_path)])
    assert cleared.exit_code == 0, cleared.stdout
    assert "Cleared rate state" in cleared.stdout

    repository = SqliteRepository.connect(db_path)
    state = repository.load_rate_state()["workday"]
    repository.close()
    assert state.blocked_until is None
    assert state.rotation_cursor == "globex"


def test_one_bucket_paces_as_well_as_counts_across_its_hosts() -> None:
    hosts = frozenset({"acme.wd1.myworkdayjobs.com", "globex.wd3.myworkdayjobs.com"})
    client = HttpClient(allowed_hosts=hosts, posture=profile("workday"), bucket_key="workday")

    first = client._budget_for(client.bucket_for("acme.wd1.myworkdayjobs.com"))
    second = client._budget_for(client.bucket_for("globex.wd3.myworkdayjobs.com"))

    assert first is second
    assert first.semaphore is second.semaphore
    assert first.stride == pytest.approx(profile("workday").min_interval_s / 2)
    assert len(client._budgets) == 1, "one bucket, one budget, one of everything in it"

    first.min_interval_s = 9.0
    assert second.stride == pytest.approx(4.5), "a tightening on one tenant paces them all"


async def test_a_long_retry_after_becomes_a_block_instead_of_a_sixty_second_nap() -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    budget = HostBudget(posture=profile("workday"))
    budget.requests = 1
    budget.defer_for(3600.0, "Retry-After: 3600s")

    settled = budget.settle("workday", now)
    assert settled.blocked_until == now + timedelta(hours=1)
    assert settled.reason == "Retry-After: 3600s"
    assert settled.is_blocked(now + timedelta(minutes=59))
    assert not settled.is_blocked(now + timedelta(minutes=61))


async def test_a_stale_writer_cannot_shorten_a_block_it_never_saw(db_path: Path) -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="workday",
                run_started_at=now,
                rate_state=(
                    RateState(
                        bucket="workday",
                        updated_at=now,
                        blocked_until=now + timedelta(hours=6),
                        reason="Retry-After: 21600s",
                    ),
                ),
            )
        )
        await repository.apply_source_batch(
            SourceBatch(
                source="workday-detail",
                run_started_at=now,
                rate_state=(RateState(bucket="workday", updated_at=now),),
            )
        )
        state = (await repository.load_rate_state())["workday"]

    assert state.blocked_until == now + timedelta(hours=6)
    assert state.reason == "Retry-After: 21600s"


async def test_the_merge_still_lets_decay_lower_an_override_and_success_reset_failures(
    db_path: Path,
) -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="workday",
                run_started_at=now,
                rate_state=(
                    RateState(
                        bucket="workday",
                        updated_at=now,
                        min_interval_override=8.0,
                        consecutive_failures=4,
                    ),
                ),
            )
        )
        await repository.apply_source_batch(
            SourceBatch(
                source="workday",
                run_started_at=now,
                rate_state=(
                    RateState(
                        bucket="workday",
                        updated_at=now,
                        min_interval_override=5.6,
                        consecutive_failures=0,
                    ),
                ),
            )
        )
        state = (await repository.load_rate_state())["workday"]

    assert state.min_interval_override == pytest.approx(5.6)
    assert state.consecutive_failures == 0


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("3600", 3600.0),
        ("-5", 0.0),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),
        ("not-a-number", None),
    ],
)
def test_retry_after_is_read_in_both_legal_forms(header: str, expected: float | None) -> None:
    response = httpx.Response(429, headers={"Retry-After": header})
    parsed = HttpClient._retry_after_raw(response)
    if expected is None and header == "not-a-number":
        assert parsed is None
    elif header.startswith("Wed"):
        assert parsed is not None and parsed >= 0.0
    else:
        assert parsed == pytest.approx(expected)


def test_a_past_retry_after_date_clamps_to_zero_rather_than_going_negative() -> None:
    response = httpx.Response(429, headers={"Retry-After": "Mon, 01 Jan 2001 00:00:00 GMT"})
    assert HttpClient._retry_after_raw(response) == 0.0


def test_a_retry_after_block_passes_through_the_twenty_four_hour_cap() -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    budget = HostBudget(posture=profile("workday"))
    budget.requests = 1
    budget.defer_for(7 * 24 * 3600.0, "Retry-After: 604800s")

    settled = budget.settle("workday", now)
    assert settled.blocked_until == now + timedelta(seconds=BLOCK_CAP_S)


def test_a_short_retry_after_cannot_shorten_a_block_the_failures_already_justify() -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    budget = HostBudget(posture=profile("workday"))
    budget.requests = 6
    budget.last_error = "HTTP 429"
    for _ in range(BREAKER_THRESHOLD):
        budget.breaker.record_failure()
    budget.defer_for(30.0, "Retry-After: 30s")

    settled = budget.settle("workday", now)
    assert settled.blocked_until is not None
    assert (settled.blocked_until - now).total_seconds() == pytest.approx(
        block_duration(BREAKER_THRESHOLD)
    )
    assert settled.reason == "HTTP 429"


def test_a_long_retry_after_still_wins_over_a_shorter_escalation() -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    budget = HostBudget(posture=profile("workday"))
    budget.requests = 6
    for _ in range(BREAKER_THRESHOLD):
        budget.breaker.record_failure()
    budget.defer_for(7200.0, "Retry-After: 7200s")

    settled = budget.settle("workday", now)
    assert settled.blocked_until == now + timedelta(hours=2)
    assert settled.reason == "Retry-After: 7200s"


def test_an_expired_block_renders_distinctly_from_a_live_one(db_path: Path) -> None:
    from datetime import timedelta

    from rich.console import Console

    from stage.cli.render import render_rate_state

    now = datetime.now(UTC)
    expired = RateState(
        bucket="workday",
        updated_at=now - timedelta(hours=3),
        blocked_until=now - timedelta(hours=1),
        reason="HTTP 429",
    )
    live = RateState(
        bucket="greenhouse",
        updated_at=now,
        blocked_until=now + timedelta(hours=1),
        reason="HTTP 403",
    )

    console = Console(record=True, width=200)
    render_rate_state(console, [expired, live], now)
    output = console.export_text()

    assert "block expired" in output, "an expired block must not read as clear"
    assert "blocked" in output
    assert not expired.is_blocked(now)
    assert live.is_blocked(now)


def test_no_number_of_latency_tightenings_can_raise_the_persisted_interval() -> None:
    posture = RatePosture(concurrency=3, min_interval_s=0.25, max_requests_per_run=300)
    now = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    override: float | None = 0.25

    for run in range(8):
        seed = _state(min_interval_override=override) if override is not None else None
        budget = HostBudget(posture=posture, seed=seed)
        budget.requests = 94
        for _ in range(6):
            budget.tighten(1.5)

        assert budget.min_interval_s > budget.seeded_interval_s, "paced within the run"

        settled = budget.settle("boards-api.greenhouse.io", now)
        override = settled.min_interval_override
        if override is None:
            break
        assert override <= 0.25, f"run {run} ratcheted to {override}s above the seed"

    assert override is None, "a never-rejected host must return to baseline"


def test_a_rejection_still_outlives_the_run_that_saw_it() -> None:
    posture = RatePosture(concurrency=3, min_interval_s=0.25, max_requests_per_run=300)
    budget = HostBudget(posture=posture)
    budget.requests = 10
    budget.tighten(1.5)
    budget.tighten(2.0, rejected=True)

    settled = budget.settle("boards-api.greenhouse.io", datetime(2026, 8, 7, tzinfo=UTC))
    assert settled.min_interval_override == pytest.approx(0.75), "a rejection persists"
