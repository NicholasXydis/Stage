import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from stage.domain import RequestLogged, SyncFinished
from stage.http import client as http_client
from stage.services.sync import sync
from stage.storage import open_repository

ANSWERS: dict[str, dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "liveness_answers.json").read_text(encoding="utf-8")
)
GH = "https://boards-api.greenhouse.io/v1/boards"
SIMPLIFY = (
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/"
    "scripts/listings.json"
)
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _answer(name: str) -> httpx.Response:
    found = ANSWERS[name]
    return httpx.Response(found["status"], json=found["body"])


@pytest.fixture(autouse=True)
def quick_retries(live_checks: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http_client, "wait_exponential_jitter", lambda **_: lambda _: 0)


def _routes(router: respx.MockRouter) -> respx.Route:
    router.get(SIMPLIFY).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "gone-one",
                    "company_name": "Acme",
                    "title": "Software Engineering Intern",
                    "url": "https://boards.greenhouse.io/acme/jobs/5268689007",
                    "locations": ["Toronto, ON"],
                    "terms": ["Summer 2027"],
                }
            ],
        )
    )
    checked = router.get(f"{GH}/acme/jobs/5268689007").mock(
        return_value=_answer("greenhouse_job_missing")
    )
    router.get(f"{GH}/acme").mock(return_value=_answer("greenhouse_board"))
    router.route().mock(return_value=httpx.Response(404, json={}))
    return checked


async def _run(db: Path, sources: list[str] | None) -> tuple[SyncFinished, list[RequestLogged]]:
    events: list[object] = []
    async with open_repository(db) as repository:
        async for event in sync(repository, [], sources=sources, now_fn=lambda: NOW):
            events.append(event)
    finished = events[-1]
    assert isinstance(finished, SyncFinished)
    return finished, [event for event in events if isinstance(event, RequestLogged)]


async def test_a_full_sync_checks_a_lists_dead_link_and_closes_it(tmp_path: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        checked = _routes(router)
        finished, requests = await _run(tmp_path / "stage.db", None)

    assert checked.call_count == 1
    assert (finished.checked, finished.checked_gone) == (1, 1)
    assert finished.closed >= 1
    assert any(record.url.startswith(f"{GH}/acme/jobs/") for record in requests)
    async with open_repository(tmp_path / "stage.db") as repository:
        spent, _ = await repository.requests_since(NOW.replace(hour=0))
    assert spent.get("greenhouse", 0) >= 2, "checks count toward the host's daily allowance"


async def test_a_sync_of_chosen_sources_checks_nothing(tmp_path: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        checked = _routes(router)
        finished, _ = await _run(tmp_path / "stage.db", ["simplify"])

    assert checked.call_count == 0
    assert finished.checked == 0
