import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as Late
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.types import CallToolResult, PromptReference, TextContent
from typer.testing import CliRunner

from stage.ai import tools as toolbox
from stage.ai.server import (
    ACTION_INSTRUCTIONS,
    ACTIONS,
    ASK_EVERY_TIME,
    CATALOGUE,
    INSTRUCTIONS,
    build_server,
)
from stage.cli.app import app
from stage.domain import Job, LocationBucket, RoleCategory, SyncOutcome, SyncRun
from stage.services import background
from stage.services.background import SyncStart, progress_path
from stage.storage.repository import SourceBatch
from stage.storage.sqlite_repo import SqliteRepository

GOLDEN = Path(__file__).parent / "golden" / "mcp_action_tools.json"
NOW = datetime.now(UTC)
ACTION_NAMES = [name for name, *_ in ACTIONS]
NAMES = [name for name, *_ in CATALOGUE]


def _id(name: str) -> str:
    return f"greenhouse:acme:{name}"


def _job(
    name: str,
    *,
    hours: int = 1,
    company: str = "Acme",
    title: str = "",
    location: LocationBucket = LocationBucket.MONTREAL,
    url: str = "",
    term: str = "summer-2027",
) -> Job:
    seen = NOW - timedelta(hours=hours)
    heading = title or f"Software Intern {name}"
    return Job(
        id=_id(name),
        source="greenhouse",
        company=company,
        title_raw=heading,
        title_normalized=heading.lower(),
        apply_url_raw=url or f"https://example.com/{name}",
        description="",
        first_seen=seen,
        last_seen=seen,
        location_raw="Montreal, QC" if location is LocationBucket.MONTREAL else "Toronto, ON",
        location=location,
        term=term,
        role=RoleCategory.SWE,
    )


def _seed(path: Path, jobs: list[Job]) -> Path:
    repository = SqliteRepository.connect(path)
    repository.apply_source_batch(
        SourceBatch(source="greenhouse", run_started_at=NOW, jobs=tuple(jobs))
    )
    repository.record_sync_run(
        SyncRun(started_at=NOW, finished_at=NOW, outcome=SyncOutcome.SUCCESS)
    )
    repository.close()
    return path


@pytest.fixture
def stored(tmp_path: Path) -> Path:
    return _seed(
        tmp_path / "stage.db",
        [
            _job("a", hours=1),
            _job("b", hours=2),
            _job("toronto", hours=3, location=LocationBucket.CANADA),
            _job("shop", hours=4, company="Shopify", title="Backend Developer Intern"),
            _job("shop2", hours=5, company="Shopify", title="Data Engineering Intern"),
            _job("jane", hours=6, company="Jane Street", term="fall-2026"),
            _job("trap", hours=7, company="Evil Corp", url="javascript:alert(1)"),
            _job("old", hours=24 * 20),
        ],
    )


@pytest.fixture
def exports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "exports"
    folder.mkdir()
    monkeypatch.setenv("STAGE_EXPORT_DIR", str(folder))
    return folder


@pytest.fixture
def browser(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    import webbrowser

    opened: list[str] = []

    def record(url: str, *_: object, **__: object) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(webbrowser, "open", record)
    return opened


@asynccontextmanager
async def _connected(database: Path, *, actions: bool = True) -> AsyncIterator[Client]:
    async with Client(build_server(database, actions=actions)) as client:
        yield client


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


async def _ok(client: Client, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    result = await client.call_tool(name, arguments or {})
    assert not result.is_error, _text(result)
    found: dict[str, Any] | None = result.structured_content
    assert found is not None
    assert json.loads(_text(result)) == found
    return found


async def _refused(client: Client, name: str, arguments: dict[str, Any]) -> str:
    result = await client.call_tool(name, arguments)
    assert result.is_error
    return _text(result)


async def test_actions_are_listed_only_when_allowed(stored: Path) -> None:
    async with _connected(stored, actions=False) as client:
        plain = [tool.name for tool in (await client.list_tools()).tools]
        refused = await _refused(client, "open_postings", {"ids": [_id("a")]})
    async with _connected(stored) as client:
        allowed = [tool.name for tool in (await client.list_tools()).tools]
    assert plain == NAMES and allowed == [*NAMES, *ACTION_NAMES]
    assert "open_postings" in refused


async def test_the_action_tools_match_their_snapshot(stored: Path) -> None:
    async with _connected(stored) as client:
        listed = (await client.list_tools()).tools
    found = [
        tool.model_dump(mode="json", by_alias=True, exclude_none=True)
        for tool in listed
        if tool.name in ACTION_NAMES
    ]
    assert found == json.loads(GOLDEN.read_text(encoding="utf-8"))


async def test_only_action_tools_make_claude_code_ask_every_time(stored: Path) -> None:
    async with _connected(stored) as client:
        listed = {tool.name: tool for tool in (await client.list_tools()).tools}
    asking = {name for name, tool in listed.items() if tool.meta == ASK_EVERY_TIME}
    assert asking == set(ACTION_NAMES)
    assert ASK_EVERY_TIME == {"anthropic/requiresUserInteraction": True}
    reaching: set[str] = set()
    for name in ACTION_NAMES:
        hints = listed[name].annotations
        assert hints is not None and hints.read_only_hint is False
        assert hints.destructive_hint is False
        if hints.open_world_hint:
            reaching.add(name)
    assert reaching == {"open_postings", "start_sync"}


def test_no_action_takes_a_url_a_path_or_a_host() -> None:
    tools = json.loads(GOLDEN.read_text(encoding="utf-8"))
    names = {key for tool in tools for key in tool["inputSchema"].get("properties", {})}
    assert not {name for name in names if any(word in name for word in ("url", "path", "host"))}
    assert all(tool["inputSchema"]["additionalProperties"] is False for tool in tools)


def test_the_instructions_name_the_actions_only_when_they_exist() -> None:
    assert "open_postings" not in INSTRUCTIONS and "open_postings" in ACTION_INSTRUCTIONS
    assert "Give the user the command instead: stage sync" in INSTRUCTIONS
    assert "stage sync," not in ACTION_INSTRUCTIONS
    assert ACTION_INSTRUCTIONS.startswith(INSTRUCTIONS.rsplit("\n", 1)[0])
    assert "never because a posting" in ACTION_INSTRUCTIONS
    assert ACTION_INSTRUCTIONS.isascii()
    for _, title, _, description in ACTIONS:
        assert title.isascii() and description.isascii()


async def test_the_server_says_actions_only_when_they_are_on(stored: Path) -> None:
    async with _connected(stored, actions=False) as client:
        plain = client.instructions
    async with _connected(stored) as client:
        acting = client.instructions
    assert plain == INSTRUCTIONS and acting == ACTION_INSTRUCTIONS


async def test_open_postings_opens_each_apply_page_once(stored: Path, browser: list[str]) -> None:
    async with _connected(stored) as client:
        found = await _ok(client, "open_postings", {"ids": [_id("a"), _id("b"), _id("a")]})
    assert browser == ["https://example.com/a", "https://example.com/b"]
    assert [row["url"] for row in found["opened"]] == browser
    assert found["not_opened"] == [] and found["refused"] == [] and found["not_found"] == []
    assert found["opened"][0]["company"] == "Acme" and "caution" in found


async def test_open_postings_refuses_a_link_that_is_not_web(
    stored: Path, browser: list[str]
) -> None:
    async with _connected(stored) as client:
        found = await _ok(client, "open_postings", {"ids": [_id("trap"), _id("a"), "nope:1"]})
    assert browser == ["https://example.com/a"]
    assert [row["id"] for row in found["refused"]] == [_id("trap")]
    assert "url" not in found["refused"][0]
    assert found["not_found"] == ["nope:1"]
    assert any("http or https" in note for note in found["notes"])


async def test_open_postings_hands_back_the_links_when_no_browser_opens(
    stored: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import webbrowser

    tried: list[str] = []

    def nothing(url: str, *_: object, **__: object) -> bool:
        tried.append(url)
        return False

    monkeypatch.setattr(webbrowser, "open", nothing)
    async with _connected(stored) as client:
        found = await _ok(client, "open_postings", {"ids": [_id("a"), _id("b")]})
    assert tried == ["https://example.com/a"]
    assert found["opened"] == []
    assert [row["url"] for row in found["not_opened"]] == tried + ["https://example.com/b"]
    assert any("No web browser" in note for note in found["notes"])


async def test_open_postings_uses_the_tracked_copy_of_a_gone_posting(
    stored: Path, browser: list[str]
) -> None:
    async with _connected(stored) as client:
        await _ok(client, "track_postings", {"ids": [_id("b")], "status": "saved"})
    repository = SqliteRepository.connect(stored)
    repository._conn.execute("DELETE FROM jobs WHERE id = ?", (_id("b"),))
    repository._conn.commit()
    repository.close()
    async with _connected(stored) as client:
        found = await _ok(client, "open_postings", {"ids": [_id("b")]})
    assert browser == ["https://example.com/b"] and found["opened"][0]["id"] == _id("b")


async def test_open_postings_takes_at_most_fifty(stored: Path, browser: list[str]) -> None:
    async with _connected(stored) as client:
        refused = await _refused(client, "open_postings", {"ids": [_id("a")] * 51})
        empty = await _refused(client, "open_postings", {"ids": []})
    assert "50" in refused and browser == []
    assert "at least 1" in empty


async def test_export_postings_writes_a_new_file_and_never_replaces_one(
    stored: Path, exports: Path
) -> None:
    async with _connected(stored) as client:
        first = await _ok(client, "export_postings", {})
        second = await _ok(client, "export_postings", {"file_type": "csv"})
        third = await _ok(client, "export_postings", {"file_type": "csv"})
    assert Path(first["path"]).parent == exports and first["path"] != second["path"]
    assert Path(second["path"]).name.endswith("-2.csv")
    assert Path(third["path"]).name.endswith("-3.csv") and len(list(exports.iterdir())) == 3
    assert first["count"] == 7 and first["window_days"] == 14 and first["file_type"] == "csv"
    rows = Path(first["path"]).read_text(encoding="utf-8-sig").splitlines()
    assert rows[0].startswith("company,title") and len(rows) == 8


@pytest.mark.parametrize("kind", ["csv", "md", "json", "pdf"])
async def test_export_postings_writes_every_format(stored: Path, exports: Path, kind: str) -> None:
    async with _connected(stored) as client:
        found = await _ok(client, "export_postings", {"file_type": kind, "company": "Shopify"})
    written = Path(found["path"])
    assert written.suffix == f".{kind}" and written.stat().st_size > 0 and found["count"] == 2
    if kind == "json":
        assert {job["company"] for job in json.loads(written.read_text(encoding="utf-8"))} == {
            "Shopify"
        }


async def test_export_postings_filters_like_search_postings(stored: Path, exports: Path) -> None:
    async with _connected(stored) as client:
        searched = await _ok(client, "search_postings", {"location": "canada", "days": 0})
        exported = await _ok(client, "export_postings", {"location": "canada", "days": 0})
        worded = await _ok(client, "export_postings", {"words": "intern", "file_type": "json"})
        everything = await _ok(client, "export_postings", {"days": 0, "file_type": "json"})
    assert exported["count"] == searched["total"] == 1
    assert worded["window_days"] is None and worded["count"] == 8
    assert everything["count"] == 8


async def test_export_postings_leaves_out_hidden_and_tracked_postings(
    stored: Path, exports: Path
) -> None:
    async with _connected(stored) as client:
        await _ok(client, "track_postings", {"ids": [_id("a")], "status": "hidden"})
        await _ok(client, "track_postings", {"ids": [_id("b")], "status": "saved"})
        kept = await _ok(client, "export_postings", {"file_type": "json"})
        untracked = await _ok(client, "export_postings", {"file_type": "json", "untracked": True})
    ids = {job["id"] for job in json.loads(Path(kept["path"]).read_text(encoding="utf-8"))}
    assert _id("a") not in ids and _id("b") in ids
    fewer = {job["id"] for job in json.loads(Path(untracked["path"]).read_text(encoding="utf-8"))}
    assert _id("b") not in fewer and len(fewer) == len(ids) - 1


async def test_export_postings_says_when_nothing_matched(stored: Path, exports: Path) -> None:
    async with _connected(stored) as client:
        found = await _ok(client, "export_postings", {"company": "Nobody Inc"})
    assert found["count"] == 0 and Path(found["path"]).exists()
    assert any("headers only" in note for note in found["notes"])


async def test_export_postings_refuses_a_folder_that_is_gone(
    stored: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STAGE_EXPORT_DIR", str(tmp_path / "missing"))
    async with _connected(stored) as client:
        refused = await _refused(client, "export_postings", {})
    assert "does not exist" in refused and "Nothing was exported" in refused
    assert not (tmp_path / "missing").exists()


async def test_export_postings_with_mine_needs_saved_preferences(
    stored: Path, exports: Path
) -> None:
    async with _connected(stored) as client:
        refused = await _refused(client, "export_postings", {"mine": True})
        await _ok(client, "set_preferences", {"locations": ["canada"]})
        mine = await _ok(client, "export_postings", {"mine": True, "file_type": "json"})
    assert "No preferences are saved" in refused and list(exports.iterdir()) == [Path(mine["path"])]
    assert mine["preferences"] and mine["count"] == 7


async def test_export_postings_without_a_database_says_to_sync(
    tmp_path: Path, exports: Path
) -> None:
    async with _connected(tmp_path / "absent.db") as client:
        refused = await _refused(client, "export_postings", {})
    assert "stage sync" in refused and list(exports.iterdir()) == []


async def test_export_postings_goes_to_downloads_without_a_setting(
    stored: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STAGE_EXPORT_DIR", raising=False)
    monkeypatch.setenv("USERPROFILE", os.environ["HOME"])
    downloads = Path.home() / "Downloads"
    assert str(downloads).startswith(os.environ["HOME"])
    downloads.mkdir(parents=True, exist_ok=True)
    async with _connected(stored) as client:
        found = await _ok(client, "export_postings", {})
    assert Path(found["path"]).parent == downloads


@pytest.mark.parametrize(
    ("launched", "started", "error"),
    [
        (SyncStart(True, "The sync started.", {"state": "running"}), True, False),
        (SyncStart(True, "The sync already finished.", {"state": "finished"}), True, False),
        (
            SyncStart(False, "A sync Stage started is already running.", {"state": "running"}),
            False,
            False,
        ),
        (
            SyncStart(False, "stage sync (pid 1) is already running", {"state": "blocked"}),
            False,
            False,
        ),
        (
            SyncStart(False, "bad registry", {"state": "failed", "error": "bad registry"}),
            False,
            True,
        ),
        (SyncStart(False, "Stage could not start a sync: denied."), False, True),
    ],
)
async def test_start_sync_reports_what_happened(
    stored: Path,
    monkeypatch: pytest.MonkeyPatch,
    launched: SyncStart,
    started: bool,
    error: bool,
) -> None:
    calls: list[Path] = []

    def fake(database: Path) -> SyncStart:
        calls.append(database)
        return launched

    monkeypatch.setattr(background, "start_sync", fake)
    async with _connected(stored) as client:
        result = await client.call_tool("start_sync", {})
    assert calls == [stored] and result.is_error is error
    if error:
        assert "No sync is running" in _text(result)
        return
    found = result.structured_content
    assert found is not None and found["started"] is started
    assert bool(found["notes"]) is started


async def test_start_sync_takes_no_arguments(stored: Path) -> None:
    async with _connected(stored) as client:
        refused = await _refused(client, "start_sync", {"source": "lever"})
    assert "Extra inputs are not permitted" in refused


async def test_database_status_shows_a_background_sync(stored: Path) -> None:
    progress_path(stored).write_text(
        json.dumps(
            {
                "phase": "syncing",
                "started_at": NOW.isoformat(),
                "updated_at": NOW.isoformat(),
                "progress": {"total": 900, "complete": 120, "warnings": 2},
            }
        ),
        encoding="utf-8",
    )
    async with _connected(stored, actions=False) as client:
        found = await _ok(client, "database_status")
    assert found["background_sync"]["state"] == "running"
    assert found["background_sync"]["employers_checked"] == 120
    assert found["background_sync"]["employers_total"] == 900
    progress_path(stored).unlink()
    async with _connected(stored, actions=False) as client:
        assert (await _ok(client, "database_status"))["background_sync"] is None


async def test_every_action_takes_null_for_an_optional_field(stored: Path, exports: Path) -> None:
    async with _connected(stored) as client:
        listed = {tool.name: tool for tool in (await client.list_tools()).tools}
        schema = listed["export_postings"].input_schema
        nulls = dict.fromkeys(schema["properties"])
        found = await _ok(client, "export_postings", nulls)
    assert found["file_type"] == "csv"


def test_stage_mcp_allow_actions_reaches_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    from stage.ai import server

    served: list[bool] = []

    async def serve(database: Path, *, actions: bool = False) -> None:
        served.append(actions)

    monkeypatch.setattr(server, "serve", serve)
    assert CliRunner().invoke(app, ["mcp", "--allow-actions"]).exit_code == 0
    assert CliRunner().invoke(app, ["mcp"]).exit_code == 0
    assert served == [True, False]


@pytest.mark.skipif(os.name == "nt", reason="BROWSER needs a POSIX script")
def test_a_noisy_browser_cannot_reach_the_json_rpc_stream(stored: Path, tmp_path: Path) -> None:
    log = tmp_path / "opened.log"
    script = tmp_path / "browser.sh"
    script.write_text(f'#!/bin/sh\necho "browser noise"\necho "$1" >> {log}\n', encoding="utf-8")
    script.chmod(0o755)
    requests: list[dict[str, Any]] = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "open_postings", "arguments": {"ids": [_id("a")]}},
        },
        {"jsonrpc": "2.0", "id": 3, "method": "prompts/list"},
    ]
    env = dict(os.environ, STAGE_DB=str(stored), BROWSER=str(script))
    with subprocess.Popen(
        [sys.executable, "-m", "stage", "mcp", "--allow-actions"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    ) as process:

        def exchange() -> tuple[list[dict[str, Any]], bytes]:
            assert process.stdin is not None and process.stdout is not None
            for request in requests:
                process.stdin.write(json.dumps(request).encode() + b"\n")
            process.stdin.flush()
            replies = [json.loads(process.stdout.readline()) for _ in range(3)]
            process.stdin.close()
            return replies, process.stdout.read()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(exchange)
            try:
                replies, rest = future.result(timeout=60)
            except Late:
                process.kill()
                raise
        assert process.wait(timeout=30) == 0 and rest == b""
    replies.sort(key=lambda reply: reply["id"])
    assert [reply["id"] for reply in replies] == [1, 2, 3]
    assert replies[1]["result"]["isError"] is False
    assert len(replies[2]["result"]["prompts"]) == 5
    assert log.read_text(encoding="utf-8").split() == ["https://example.com/a"]


def test_toolbox_exports_what_prompts_build_on() -> None:
    assert {"ChosenPlace", "ChosenRole", "CHOSEN_TERM"} <= set(toolbox.__all__)


async def test_prompts_and_autocomplete_exist_without_actions(stored: Path) -> None:
    async with _connected(stored, actions=False) as client:
        prompts = (await client.list_prompts()).prompts
        completed = await client.complete(
            PromptReference(type="ref/prompt", name="find_matches"), {"name": "role", "value": "s"}
        )
    assert len(prompts) == 5 and completed.completion.values == ["swe", "security", "general-cs"]


async def test_export_postings_still_exports_with_a_damaged_tracker(
    stored: Path, exports: Path
) -> None:
    from stage.services.tracker import LIST_PROBLEM, tracker_path

    path = tracker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a database " * 100)
    before = path.read_bytes()
    async with _connected(stored) as client:
        found = await _ok(client, "export_postings", {})
    assert found["warning"] == LIST_PROBLEM and found["count"] == 7
    assert path.read_bytes() == before


async def test_export_postings_reports_a_file_it_could_not_write(
    stored: Path, exports: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.services import export

    async def full(*_: object, **__: object) -> object:
        raise export.ExportError("could not write stage-export.csv (No space left on device)")

    monkeypatch.setattr(export, "export_jobs", full)
    async with _connected(stored) as client:
        refused = await _refused(client, "export_postings", {})
    assert "No space left on device" in refused and "Nothing was exported" in refused


async def test_a_pdf_export_keeps_the_app_log_quiet(
    stored: Path, exports: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    server = build_server(stored, actions=True)
    assert server.settings.log_level == "WARNING"
    assert logging.getLogger().level == logging.WARNING
    async with Client(server) as client:
        with caplog.at_level(logging.WARNING):
            await _ok(client, "export_postings", {"file_type": "pdf"})
    assert not [record for record in caplog.records if record.name.startswith("fontTools")]
