import asyncio
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from stage.domain import (
    Company,
    CompanyDeferred,
    CompanyFailed,
    CompanyFinished,
    CompanyStarted,
    CustomBoard,
    Platform,
    RateState,
    SyncEvent,
    SyncFinished,
)
from stage.http import BucketBlockedError, HttpClient, RatePosture
from stage.services import sync as sync_module
from stage.storage import AsyncRepository, SourceBatch, open_repository

WHEN = datetime(2026, 9, 14, 12, tzinfo=UTC)
HOST = "boards-api.greenhouse.io"


@pytest.fixture(autouse=True)
def unpaced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sync_module,
        "resolve",
        lambda *_: RatePosture(concurrency=1, min_interval_s=0.0, max_requests_per_run=100),
    )


def _greenhouse(slug: str) -> Company:
    return Company(name=slug.title(), platform=Platform.GREENHOUSE, slug=slug)


def _bamboo(slug: str) -> Company:
    return Company(name=slug.title(), platform=Platform.BAMBOOHR, slug=slug)


async def _sync(
    repository: AsyncRepository, companies: list[Company], sources: list[str], when: datetime
) -> list[SyncEvent]:
    return [
        event
        async for event in sync_module.sync(
            repository,
            companies,
            sources=sources,
            now_fn=lambda: when,
            rng=random.Random(5),
            force_refresh=True,
            close_orphans=False,
        )
    ]


@respx.mock
async def test_a_cooldown_one_source_earns_holds_another_source_off_the_same_host(
    tmp_path: Path,
) -> None:
    calls = respx.route(host=HOST).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "3600"})
    )
    custom = Company(
        name="Wandb",
        platform=Platform.CUSTOM_JSON,
        slug="wandb",
        custom=CustomBoard(url=f"https://{HOST}/v1/boards/wandb/jobs", jobs_path="jobs"),
    )
    registry = [_greenhouse("acme"), _greenhouse("globex"), custom]

    async with open_repository(tmp_path / "shared.db") as repository:
        events = await _sync(repository, registry, ["greenhouse", "custom_json"], WHEN)
        stored = await repository.load_rate_state()

    failed = [event for event in events if isinstance(event, CompanyFailed)]
    deferred = [event for event in events if isinstance(event, CompanyDeferred)]
    assert calls.call_count == 1, "the second source kept asking after the host said wait"
    assert len(failed) == 1 and len(deferred) == 2
    sources = {event.source for event in failed} | {event.source for event in deferred}
    assert sources == {"greenhouse", "custom_json"}
    assert all("blocked" in event.reason for event in deferred)
    block = stored[HOST].blocked_until
    assert block is not None and block >= WHEN + timedelta(minutes=59)


@respx.mock
async def test_cancelling_a_sync_mid_board_leaves_no_task_and_writes_nothing(
    tmp_path: Path,
) -> None:
    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(3600)
        return httpx.Response(200, json={"jobs": []})

    respx.route(host=HOST).mock(side_effect=hang)
    started = asyncio.Event()

    async with open_repository(tmp_path / "cancel.db") as repository:

        async def consume() -> None:
            async for event in sync_module.sync(
                repository,
                [_greenhouse("acme"), _greenhouse("globex")],
                sources=["greenhouse"],
                now_fn=lambda: WHEN,
                force_refresh=True,
            ):
                if isinstance(event, CompanyStarted):
                    started.set()

        before = asyncio.all_tasks()
        task = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), timeout=10)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        leftover = asyncio.all_tasks() - before
        visits = await repository.all_visits()
        _, recorded = await repository.requests_since(WHEN - timedelta(days=1), WHEN)

        respx.route(host=HOST).mock(return_value=httpx.Response(200, json={"jobs": []}))
        again = await _sync(repository, [_greenhouse("acme")], ["greenhouse"], WHEN)

    assert not leftover, f"tasks outlived the cancelled sync: {leftover}"
    assert not visits and not recorded, "a cancelled sync was recorded as a run"
    assert any(isinstance(event, CompanyFinished) for event in again)


@respx.mock
async def test_a_block_that_expires_during_a_run_holds_until_the_next_run(
    tmp_path: Path,
) -> None:
    blocked = respx.get("https://pied.bamboohr.com/careers/list").mock(
        return_value=httpx.Response(200, json={"result": []})
    )
    respx.get("https://stark.bamboohr.com/careers/list").mock(
        return_value=httpx.Response(200, json={"result": []})
    )
    registry = [_bamboo("pied"), _bamboo("stark")]

    async with open_repository(tmp_path / "expiry.db") as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="bamboohr",
                run_started_at=WHEN - timedelta(hours=1),
                rate_state=(
                    RateState(
                        bucket="pied.bamboohr.com",
                        updated_at=WHEN - timedelta(hours=1),
                        blocked_until=WHEN + timedelta(microseconds=1),
                        consecutive_failures=1,
                        reason="pied.bamboohr.com returned 403 (forbidden)",
                    ),
                ),
            )
        )
        during = await _sync(repository, registry, ["bamboohr"], WHEN)
        held = blocked.call_count
        after = await _sync(repository, registry, ["bamboohr"], WHEN + timedelta(seconds=1))

    deferred = [event for event in during if isinstance(event, CompanyDeferred)]
    assert held == 0 and [event.company for event in deferred] == ["Pied"]
    assert any(isinstance(event, SyncFinished) for event in after)
    assert blocked.call_count == 1, "the expired block reopens with one request"


@respx.mock
async def test_a_blocked_tenant_that_was_never_asked_keeps_its_block_instead_of_a_longer_one() -> (
    None
):
    respx.get("https://stark.bamboohr.com/careers/list").mock(
        return_value=httpx.Response(200, json={"result": []})
    )
    stored = RateState(
        bucket="pied.bamboohr.com",
        updated_at=WHEN - timedelta(minutes=10),
        blocked_until=WHEN + timedelta(minutes=30),
        consecutive_failures=8,
        reason="pied.bamboohr.com returned 403 (forbidden)",
    )
    client = HttpClient(
        allowed_hosts=frozenset({"pied.bamboohr.com", "stark.bamboohr.com"}),
        posture=RatePosture(concurrency=1, min_interval_s=0.0, max_requests_per_run=10),
        bucket_key="bamboohr",
        rate_state={"pied.bamboohr.com": stored},
        now=WHEN,
        jitter=False,
    )
    async with client:
        with pytest.raises(BucketBlockedError):
            await client.get_json("https://pied.bamboohr.com/careers/list")
        await client.get_json("https://stark.bamboohr.com/careers/list")
        settled = {state.bucket: state for state in client.rate_state(WHEN)}

    assert "pied.bamboohr.com" not in settled, "a refused host had its block pushed later"
