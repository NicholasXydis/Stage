import asyncio
import multiprocessing
import pickle
import random
import socket
import subprocess
import sys
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from concurrent.futures.process import BrokenProcessPool
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from stage.domain import Company, Job, SourceFinished
from stage.http import RatePosture
from stage.services import screening
from stage.services import sync as sync_module
from stage.services.screening import CHUNK_SIZE, Screener, normalize_batch
from stage.services.sync import sync
from stage.storage import open_repository

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
ENDPOINT = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
TITLES = (
    "Software Engineering Intern",
    "Senior Staff Engineer",
    "Data Science Co-op, Summer 2027",
    "Marketing Intern",
    "Machine Learning Intern",
    "Accountant",
)
PLACES = ("Montréal, QC", "Toronto, ON", "Berlin, Germany", "Remote", "", "New York, NY")
BODY = (
    "We are looking for a software engineering intern to join our platform team. You will "
    "build services in Python and Go and ship features to customers. Requirements: pursuing "
    "a bachelor's degree in computer science and eligible to work in Canada. "
)


def _jobs(count: int, body: str = "") -> list[Job]:
    return [
        Job(
            id=f"greenhouse:acme:{n}",
            source="greenhouse",
            company="Acme",
            title_raw=TITLES[n % len(TITLES)],
            title_normalized=TITLES[n % len(TITLES)].lower(),
            apply_url_raw=f"https://boards.greenhouse.io/acme/jobs/{n}",
            description=body,
            location_raw=PLACES[n % len(PLACES)],
            first_seen=NOW,
            last_seen=NOW,
        )
        for n in range(count)
    ]


def _board(count: int) -> dict[str, Any]:
    return {
        "jobs": [
            {
                "id": 5_000_000 + n,
                "title": TITLES[n % len(TITLES)],
                "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{5_000_000 + n}",
                "updated_at": "2026-07-20T14:03:11-04:00",
                "location": {"name": PLACES[n % len(PLACES)] or "Remote"},
                "content": "",
                "departments": [],
                "metadata": [],
            }
            for n in range(count)
        ],
        "meta": {"total": count},
    }


async def _settled() -> bool:
    for _ in range(100):
        if not multiprocessing.active_children():
            return True
        await asyncio.sleep(0.1)
    return False


class _Refusing:
    calls = 0
    error: BaseException = OSError("spawn refused")

    def __init__(self, **_: object) -> None:
        pass

    def map(self, *_: object) -> list[object]:
        type(self).calls += 1
        raise type(self).error

    def shutdown(self, **_: object) -> None:
        pass


@pytest.fixture
def refusing(monkeypatch: pytest.MonkeyPatch) -> type[_Refusing]:
    monkeypatch.setattr(screening, "ProcessPoolExecutor", _Refusing)
    _Refusing.calls = 0
    return _Refusing


@pytest.fixture
def closings(monkeypatch: pytest.MonkeyPatch) -> list[Screener]:
    closed: list[Screener] = []
    original = Screener.close

    def recording(self: Screener) -> None:
        closed.append(self)
        original(self)

    monkeypatch.setattr(Screener, "close", recording)
    return closed


@pytest.fixture
def unpaced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sync_module,
        "resolve",
        lambda *_: RatePosture(concurrency=4, min_interval_s=0.0, max_requests_per_run=300),
    )


async def test_a_large_batch_screens_the_same_in_worker_processes() -> None:
    jobs = _jobs(CHUNK_SIZE + 250, BODY)
    screener = Screener()
    try:
        screened = await screener(jobs)
        assert multiprocessing.active_children()
    finally:
        screener.close()

    assert screened == normalize_batch(jobs)
    assert screened[0] and screened[1]
    assert [job.id for job in screened[0]] == sorted(
        (job.id for job in screened[0]), key=lambda value: int(value.rsplit(":", 1)[1])
    )
    assert not screener.broken
    assert await _settled()


async def test_a_small_batch_never_starts_a_process() -> None:
    before = multiprocessing.active_children()
    screener = Screener()
    screened = await screener(_jobs(10))
    assert multiprocessing.active_children() == before
    assert screened == normalize_batch(_jobs(10))
    screener.close()


@pytest.mark.parametrize(
    "error",
    [
        OSError("spawn refused"),
        BrokenProcessPool("died"),
        pickle.PicklingError(),
        ValueError("bad value(s) in fds_to_keep"),
        RuntimeError("cannot schedule new futures after shutdown"),
    ],
)
async def test_a_pool_that_fails_falls_back_to_a_thread_for_good(
    refusing: type[_Refusing], error: BaseException
) -> None:
    refusing.error = error
    jobs = _jobs(screening.POOL_THRESHOLD)
    screener = Screener()

    assert await screener(jobs) == normalize_batch(jobs)
    assert screener.broken
    assert await screener(jobs) == normalize_batch(jobs)
    assert refusing.calls == 1


class _InProcess:
    def __init__(self, **_: object) -> None:
        pass

    def map(self, function: Callable[[list[Job]], object], chunks: list[list[Job]]) -> list[object]:
        return [function(chunk) for chunk in chunks]

    def shutdown(self, **_: object) -> None:
        pass


async def test_a_bug_in_screening_is_not_hidden_by_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(_: list[Job]) -> object:
        raise ValueError("a real bug")

    monkeypatch.setattr(screening, "ProcessPoolExecutor", _InProcess)
    monkeypatch.setattr(screening, "normalize_batch", broken)
    with pytest.raises(ValueError, match="a real bug"):
        await Screener()(_jobs(screening.POOL_THRESHOLD))


def test_workers_start_when_stderr_has_no_file_descriptor() -> None:
    script = """
import asyncio, sys
from datetime import UTC, datetime
from stage.domain import Job
from stage.services.screening import Screener, normalize_batch

class Captured:
    def write(self, text): return len(text)
    def flush(self): pass
    def fileno(self): return -1

sys.stderr = Captured()
now = datetime(2026, 7, 31, tzinfo=UTC)
jobs = [
    Job(id=f"greenhouse:acme:{n}", source="greenhouse", company="Acme",
        title_raw="Software Engineering Intern", title_normalized="software engineering intern",
        apply_url_raw=f"https://example.com/{n}", description="", location_raw="Montreal, QC",
        first_seen=now, last_seen=now)
    for n in range(600)
]

async def main():
    screener = Screener()
    try:
        screened = await screener(jobs)
    finally:
        screener.close()
    print(screener.broken, screened == normalize_batch(jobs))

asyncio.run(main())
"""
    run = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120, check=False
    )
    assert run.stdout.split() == ["False", "True"], run.stderr[-400:]


def test_closing_is_safe_before_use_and_twice() -> None:
    screener = Screener()
    screener.close()
    screener.close()


def test_the_worker_count_never_exceeds_the_machine() -> None:
    assert 1 <= Screener(workers=64).workers <= max(1, multiprocessing.cpu_count())
    assert Screener(workers=0).workers == 1


@respx.mock
@pytest.mark.usefixtures("unpaced")
async def test_a_sync_screens_a_large_board_in_workers_and_closes_them(
    db_path: Path, acme: Company, closings: list[Screener]
) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=_board(600)))
    async with open_repository(db_path) as repository:
        events = [
            event
            async for event in sync(
                repository,
                [acme],
                sources=["greenhouse"],
                now_fn=lambda: NOW,
                rng=random.Random(1),
            )
        ]
    finished = [event for event in events if isinstance(event, SourceFinished)]
    assert len(finished) == 1
    assert finished[0].fetched == 600
    assert finished[0].added > 0
    assert len(closings) == 1
    assert await _settled()


@respx.mock
@pytest.mark.usefixtures("unpaced")
async def test_abandoning_a_sync_closes_its_workers(
    db_path: Path, acme: Company, closings: list[Screener]
) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=_board(600)))
    async with open_repository(db_path) as repository:
        stream = sync(
            repository, [acme], sources=["greenhouse"], now_fn=lambda: NOW, rng=random.Random(1)
        )
        assert isinstance(stream, AsyncGenerator)
        async for event in stream:
            if isinstance(event, SourceFinished):
                break
        assert multiprocessing.active_children()
        assert not closings
        await stream.aclose()
        assert len(closings) == 1
    assert await _settled()


@respx.mock
@pytest.mark.usefixtures("unpaced")
async def test_cancelling_a_running_sync_closes_its_workers(
    db_path: Path, acme: Company, closings: list[Screener]
) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=_board(600)))
    async with open_repository(db_path) as repository:

        async def consume() -> None:
            async for _ in sync(
                repository, [acme], sources=["greenhouse"], now_fn=lambda: NOW, rng=random.Random(1)
            ):
                pass

        task = asyncio.create_task(consume())
        for _ in range(200):
            if multiprocessing.active_children() or task.done():
                break
            await asyncio.sleep(0.01)
        assert multiprocessing.active_children()
        task.cancel()
        (stopped,) = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(stopped, asyncio.CancelledError)
        assert len(closings) == 1
    assert await _settled()


@respx.mock
@pytest.mark.usefixtures("unpaced")
async def test_a_tui_sync_screens_in_workers_despite_textuals_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, acme: Company, closings: list[Screener]
) -> None:
    from stage import companies as companies_module
    from stage.tui.app import StageApp
    from stage.tui.screens.sync import SyncScreen

    monkeypatch.setattr(companies_module, "load_companies", lambda *_: (acme,))
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=_board(600)))
    respx.route().mock(return_value=httpx.Response(404))

    app = StageApp(tmp_path / "tui.db", "")
    async with app.run_test(size=(120, 40)) as pilot:
        screen = SyncScreen()
        app.push_screen(screen)
        await pilot.pause()
        await pilot.press("s")
        for _ in range(1500):
            if closings:
                break
            await asyncio.sleep(0.02)
        await pilot.pause()
        summary = str(screen.query_one("#chips").render())

    assert len(closings) == 1
    assert not closings[0].broken
    assert "200 added" in summary
    assert await _settled()


async def _round_trips(work: Callable[[], Awaitable[object]]) -> float:
    stop = asyncio.Event()
    done = [0]

    async def bounce() -> None:
        left, right = socket.socketpair()
        reader, writer = await asyncio.open_connection(sock=left)
        echo_reader, echo_writer = await asyncio.open_connection(sock=right)
        while not stop.is_set():
            writer.write(b"ping\n")
            await writer.drain()
            await echo_reader.readline()
            echo_writer.write(b"pong\n")
            await echo_writer.drain()
            await reader.readline()
            done[0] += 1
        writer.close()
        echo_writer.close()

    bouncers = [asyncio.create_task(bounce()) for _ in range(10)]
    started = time.perf_counter()
    await work()
    took = time.perf_counter() - started
    stop.set()
    await asyncio.gather(*bouncers)
    return done[0] / took


@pytest.mark.serial
async def test_connections_keep_flowing_while_a_large_batch_screens() -> None:
    jobs = _jobs(1_200, BODY * 16)
    in_thread = await _round_trips(lambda: asyncio.to_thread(normalize_batch, jobs))
    screener = Screener()
    try:
        in_workers = await _round_trips(lambda: screener(jobs))
    finally:
        screener.close()
    assert in_workers > in_thread * 10
