import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.client.extension import ClientExtension, advertise
from mcp.server.apps import APP_MIME_TYPE, EXTENSION_ID
from mcp.types import TextContent, TextResourceContents

from stage.ai.server import ACTIONS, CARD, CARD_TOOLS, CATALOGUE, build_server
from stage.domain import Job, LocationBucket, RoleCategory, SyncOutcome, SyncRun
from stage.paths import card_path
from stage.storage.repository import SourceBatch
from stage.storage.sqlite_repo import SqliteRepository

NOW = datetime.now(UTC)
HTML = card_path().read_text(encoding="utf-8")
NAMES = {name for name, *_ in (*CATALOGUE, *ACTIONS)}
SPEC_METHODS = {
    "ui/initialize",
    "ui/notifications/initialized",
    "ui/notifications/tool-input",
    "ui/notifications/tool-input-partial",
    "ui/notifications/tool-result",
    "ui/notifications/tool-cancelled",
    "ui/notifications/host-context-changed",
    "ui/notifications/size-changed",
    "ui/open-link",
    "ui/request-display-mode",
    "ui/resource-teardown",
}
APPS = advertise(EXTENSION_ID, {"mimeTypes": [APP_MIME_TYPE]})


def _job(name: str, hours: int, url: str = "") -> Job:
    seen = NOW - timedelta(hours=hours)
    title = f"Software Intern {name}"
    return Job(
        id=f"greenhouse:acme:{name}",
        source="greenhouse",
        company="Acme",
        title_raw=title,
        title_normalized=title.lower(),
        apply_url_raw=url or f"https://example.com/{name}",
        description="",
        first_seen=seen,
        last_seen=seen,
        location_raw="Montreal, QC",
        location=LocationBucket.MONTREAL,
        term="summer-2027",
        role=RoleCategory.SWE,
    )


@pytest.fixture
def stored(tmp_path: Path) -> Path:
    path = tmp_path / "stage.db"
    repository = SqliteRepository.connect(path)
    jobs = [_job(name, hours) for hours, name in enumerate("abcdefg", start=1)]
    jobs.append(_job("trap", 9, url="javascript:alert(1)"))
    repository.apply_source_batch(
        SourceBatch(source="greenhouse", run_started_at=NOW, jobs=tuple(jobs))
    )
    repository.record_sync_run(
        SyncRun(started_at=NOW, finished_at=NOW, outcome=SyncOutcome.SUCCESS)
    )
    repository.close()
    return path


@pytest.fixture(params=["legacy", "auto"])
def mode(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@asynccontextmanager
async def _connected(
    database: Path,
    mode: str = "auto",
    *,
    actions: bool = False,
    extensions: list[ClientExtension] | None = None,
) -> AsyncIterator[Client]:
    server = build_server(database, actions=actions)
    async with Client(server, mode=mode, extensions=extensions) as client:
        yield client


def _meta(tool: Any) -> dict[str, Any]:
    found: dict[str, Any] = dict(tool.meta or {})
    return found


async def test_only_search_postings_shows_the_card(mode: str, tmp_path: Path) -> None:
    async with _connected(tmp_path / "absent.db", mode, actions=True) as client:
        listed = (await client.list_tools()).tools
    for tool in listed:
        meta = _meta(tool)
        if tool.name == "search_postings":
            assert meta["ui"]["resourceUri"] == CARD and meta["ui/resourceUri"] == CARD
        else:
            assert "resourceUri" not in meta.get("ui", {}) and "ui/resourceUri" not in meta


async def test_the_card_can_reach_only_the_tools_it_uses(tmp_path: Path) -> None:
    async with _connected(tmp_path / "absent.db", actions=True) as client:
        listed = (await client.list_tools()).tools
    both = ["model", "app"]
    reach = {tool.name: _meta(tool).get("ui", {}).get("visibility", both) for tool in listed}
    assert {name for name, seen in reach.items() if "app" in seen} == set(CARD_TOOLS)
    assert all("model" in seen for seen in reach.values())
    assert set(reach) == NAMES


def test_the_card_calls_only_the_tools_it_may() -> None:
    assert {name for name in NAMES if name in HTML} == set(CARD_TOOLS)
    assert set(CARD_TOOLS) == {"search_postings", "track_postings", "undo_tracking"}


async def test_the_card_is_one_resource_served_as_an_app(mode: str, stored: Path) -> None:
    async with _connected(stored, mode) as client:
        listed = (await client.list_resources()).resources
        read = await client.read_resource(CARD)
    assert [(item.uri, item.mime_type) for item in listed] == [(CARD, APP_MIME_TYPE)]
    [contents] = read.contents
    assert isinstance(contents, TextResourceContents)
    assert contents.mime_type == APP_MIME_TYPE and contents.text == HTML
    assert (contents.meta or {}).get("ui") == {"prefersBorder": True}


async def test_the_card_is_served_before_stage_has_a_database(tmp_path: Path) -> None:
    async with _connected(tmp_path / "absent.db") as client:
        read = await client.read_resource(CARD)
    [contents] = read.contents
    assert isinstance(contents, TextResourceContents) and contents.text == HTML
    assert not (tmp_path / "absent.db").exists()


async def test_apps_that_draw_cards_get_the_same_answer_as_apps_that_do_not(
    mode: str, stored: Path
) -> None:
    answers: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for extensions in (None, [APPS]):
        async with _connected(stored, mode, extensions=extensions) as client:
            result = await client.call_tool("search_postings", {"limit": 5})
        assert not result.is_error
        [block] = result.content
        assert isinstance(block, TextContent) and result.structured_content is not None
        said = json.loads(block.text)
        assert said == result.structured_content
        said.pop("last_sync")
        answers.append((said, result.meta or {}))
    assert answers[0] == answers[1]
    assert answers[0][0]["total"] == 8 and len(answers[0][0]["postings"]) == 5


async def test_a_search_answer_names_the_search_it_ran(stored: Path) -> None:
    async with _connected(stored, extensions=[APPS]) as client:
        result = await client.call_tool("search_postings", {"words": "intern", "limit": 5})
    assert (result.meta or {})["stage/search"] == {
        "words": "intern",
        "location": None,
        "role": None,
        "term": None,
        "language": None,
        "company": None,
        "mine": False,
        "untracked": False,
        "new": False,
        "days": None,
        "all_matches": False,
    }


def test_the_card_is_plain_ascii_without_comments() -> None:
    assert HTML.isascii()
    assert HTML.startswith("<!doctype html>\n")
    assert '<meta charset="utf-8">' in HTML
    for marker in ("<!--", "/*", "//"):
        assert marker not in HTML, marker


def test_the_card_loads_nothing_from_outside() -> None:
    lowered = HTML.lower()
    style = lowered.split("<style>")[1].split("</style>")[0]
    assert "url(" not in style and "@import" not in style and "@font-face" not in style
    for marker in (
        "src=",
        "href=",
        "<link",
        "<iframe",
        "<base",
        "<form",
        "<object",
        "<embed",
        "<img",
        "http-equiv",
        "fetch(",
        "xmlhttprequest",
        "websocket",
        "eventsource",
        "sendbeacon",
        "import(",
        "localstorage",
        "sessionstorage",
        "indexeddb",
        "document.cookie",
    ):
        assert marker not in lowered, marker


def test_the_card_never_turns_text_into_markup() -> None:
    for sink in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "Function(",
        'setAttribute("on',
        "srcdoc",
        "DOMParser",
        "createContextualFragment",
        "javascript:",
        "window.open",
        "window.location",
        "document.location",
        "top.location",
        ".href",
        ".style.cssText",
    ):
        assert sink not in HTML, sink


def test_the_card_listens_only_to_its_host() -> None:
    assert re.search(r"\.source !== window\.parent\)", HTML)
    assert "postMessage(" in HTML and HTML.count("postMessage(") == 1


def test_the_card_speaks_only_the_mcp_apps_protocol() -> None:
    used = set(re.findall(r'"(ui/[a-z/-]+)"', HTML))
    assert used <= SPEC_METHODS, used - SPEC_METHODS
    assert {
        "ui/initialize",
        "ui/notifications/initialized",
        "ui/notifications/tool-input",
        "ui/notifications/tool-result",
        "ui/notifications/host-context-changed",
        "ui/notifications/size-changed",
        "ui/open-link",
        "ui/request-display-mode",
        "ui/resource-teardown",
    } <= used


def test_the_card_opens_only_web_links() -> None:
    assert '"https:"' in HTML and '"http:"' in HTML
    assert HTML.count('"ui/open-link"') == 1


def test_the_card_stays_small() -> None:
    assert len(HTML.encode()) < 48_000
