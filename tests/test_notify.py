import json
import re
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

from stage import paths
from stage.cli import notify


def test_a_discord_webhook_is_accepted() -> None:
    url = "https://discord.com/api/webhooks/123/token"

    assert notify.validate(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://discord.com/api/webhooks/1/x",
        "https://evil.com/api/webhooks/1/x",
        "https://discord.com/channels/1",
        "file:///etc/passwd",
    ],
)
def test_anything_else_is_refused(url: str) -> None:
    with pytest.raises(notify.NotifyError):
        notify.validate(url)


def test_the_token_never_appears_in_full() -> None:
    redacted = notify.redact("https://discord.com/api/webhooks/123/SUPERSECRETTOKEN")

    assert "SUPERSECRETTOKEN" not in redacted
    assert "discord.com" in redacted


def test_a_stored_webhook_round_trips(tmp_path: Path) -> None:
    target = tmp_path / "notify.json"
    url = "https://discord.com/api/webhooks/123/token"

    notify.remember(url, target)
    assert notify.read(target) == url

    notify.forget(target)
    assert notify.read(target) == ""


def test_a_stored_webhook_is_not_world_readable(tmp_path: Path) -> None:
    target = tmp_path / "notify.json"
    notify.remember("https://discord.com/api/webhooks/1/tok", target)

    assert target.stat().st_mode & 0o077 == 0


def test_a_missing_file_reads_as_no_webhook(tmp_path: Path) -> None:
    assert notify.read(tmp_path / "absent.json") == ""


def test_a_corrupt_file_reads_as_no_webhook(tmp_path: Path) -> None:
    target = tmp_path / "notify.json"
    target.write_text("{not json", encoding="utf-8")

    assert notify.read(target) == ""


def test_the_message_names_the_count_and_links_each_row() -> None:
    postings = [notify.Posting("Acme", "SWE Intern", "Montreal, QC", "https://e.com/a")]

    payload = notify.compose(postings, total=5)
    embed = payload["embeds"][0]  # type: ignore[index]

    assert embed["title"] == "5 new posting(s)"
    assert "https://e.com/a" in embed["description"]
    assert "4 more" in embed["description"]


def _many(count: int, title: str = "Software Engineering Intern") -> list[notify.Posting]:
    return [
        notify.Posting(f"Company {index}", title, "Montreal, QC", f"https://e.com/{index}")
        for index in range(count)
    ]


@pytest.mark.parametrize(
    "postings",
    [
        [],
        _many(1),
        _many(5),
        _many(50),
        _many(500),
        [notify.Posting("C", "T" * 5000, "L", "https://e.com/x")],
        [notify.Posting("C", "T", "L", "https://e.com/" + "u" * 3000)],
        [
            notify.Posting("C" * 200, "T" * 400, "L" * 200, "https://e.com/" + "u" * 300)
            for _ in range(40)
        ],
    ],
)
def test_the_message_always_fits_what_discord_accepts(
    postings: list[notify.Posting],
) -> None:
    embed = notify.compose(postings, total=max(len(postings), 1))["embeds"][0]  # type: ignore[index]

    assert len(embed["description"]) <= notify.DESCRIPTION_LIMIT
    assert len(embed["title"]) <= 256


def test_a_long_run_lists_what_fits_and_counts_the_rest() -> None:
    embed = notify.compose(_many(50), total=50)["embeds"][0]  # type: ignore[index]
    description = embed["description"]
    links = re.findall(r"\[([^\]]*)\]\(([^)]*)\)", description)

    assert 8 < len(links) <= notify.MAX_LISTED
    assert description.count("[") == description.count("]")
    assert description.splitlines()[-1].startswith("…and")
    assert "stage list --new" in description


def test_nothing_is_cut_in_the_middle_of_a_link() -> None:
    embed = notify.compose(_many(500), total=500)["embeds"][0]  # type: ignore[index]
    description = embed["description"]

    for line in description.splitlines():
        if line.startswith("["):
            assert line.endswith(")"), line


def test_a_run_that_fits_entirely_has_no_footer() -> None:
    embed = notify.compose(_many(3), total=3)["embeds"][0]  # type: ignore[index]

    assert "more. Run" not in embed["description"]


def test_at_least_one_posting_survives_however_large() -> None:
    huge = [notify.Posting("C", "T" * 9000, "L", "https://e.com/1")]

    embed = notify.compose(huge, total=1)["embeds"][0]  # type: ignore[index]

    assert embed["description"]
    assert len(embed["description"]) <= notify.DESCRIPTION_LIMIT


def test_a_title_cannot_break_out_of_its_own_link() -> None:
    hostile = [notify.Posting("Acme", "Intern [evil](http://bad)", "L", "https://ok.com/1")]

    line = notify.compose(hostile, total=1)["embeds"][0]["description"].splitlines()[0]  # type: ignore[index]

    assert line.startswith("[Intern (evil)(http://bad)]")
    assert line.endswith("(https://ok.com/1)")


def test_a_posting_without_a_url_still_appears() -> None:
    embed = notify.compose(  # type: ignore[index]
        [notify.Posting("Acme", "Intern", "Montreal, QC", "")], total=1
    )["embeds"][0]

    assert "Intern" in embed["description"]
    assert "Acme" in embed["description"]


def test_a_refused_webhook_is_never_posted_to() -> None:
    with pytest.raises(notify.NotifyError):
        notify.post("https://evil.com/api/webhooks/1/x", {"content": "hi"})


def _http_error(code: int, reason: str = "x") -> HTTPError:
    error = HTTPError("https://discord.com", code, reason, {}, None)  # type: ignore[arg-type]
    error.close()
    return error


def test_discord_errors_are_reported_without_a_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    from urllib.error import URLError

    def explode(request: object, timeout: object = None) -> None:
        raise URLError("no route to host")

    monkeypatch.setattr(notify, "urlopen", explode)

    with pytest.raises(notify.NotifyError, match="Could not reach Discord"):
        notify.post("https://discord.com/api/webhooks/1/tok", {"content": "hi"})


def test_a_deleted_webhook_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(request: object, timeout: object = None) -> None:
        raise _http_error(404, "Not Found")

    monkeypatch.setattr(notify, "urlopen", explode)

    with pytest.raises(notify.NotifyError, match="probably deleted"):
        notify.post("https://discord.com/api/webhooks/1/tok", {"content": "hi"})


def test_the_payload_is_json_discord_accepts(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict[str, object] = {}

    class _Response:
        status = 204

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_: object) -> None:
            return None

    def capture(request: object, timeout: object = None) -> _Response:
        sent["body"] = json.loads(request.data.decode())  # type: ignore[attr-defined]
        return _Response()

    monkeypatch.setattr(notify, "urlopen", capture)
    notify.post(
        "https://discord.com/api/webhooks/1/tok",
        notify.compose([notify.Posting("Acme", "Intern", "Remote", "https://e.com")], 1),
    )

    assert sent["body"]["username"] == "Stage"  # type: ignore[index]


async def _announce(
    db: Path, webhook: str, monkeypatch: pytest.MonkeyPatch
) -> list[dict[str, Any]]:
    from stage.cli.commands.pipeline import _announce_new_postings
    from stage.storage import open_repository

    sent: list[dict[str, Any]] = []

    class _Response:
        status = 204

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_: object) -> None:
            return None

    def capture(request: object, timeout: object = None) -> _Response:
        sent.append(json.loads(request.data.decode()))  # type: ignore[attr-defined]
        return _Response()

    class _Console:
        def print(self, *_: object, **__: object) -> None:
            return None

    monkeypatch.setattr(notify, "read", lambda *_, **__: webhook)
    monkeypatch.setattr(notify, "urlopen", capture)
    async with open_repository(db) as repository:
        await _announce_new_postings(repository, _Console())
    return sent


async def test_nothing_is_posted_without_a_webhook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert await _announce(tmp_path / "a.db", "", monkeypatch) == []


async def test_nothing_is_posted_before_a_second_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    webhook = "https://discord.com/api/webhooks/1/tok"

    assert await _announce(tmp_path / "b.db", webhook, monkeypatch) == []


async def test_a_new_posting_reaches_discord(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    from stage.domain import Job, JobStatus, SyncOutcome, SyncRun
    from stage.storage import open_repository
    from stage.storage.repository import SourceBatch

    db = tmp_path / "c.db"
    now = datetime.now(UTC)
    async with open_repository(db) as repository:
        for minutes in (90, 30):
            await repository.record_sync_run(
                SyncRun(
                    started_at=now - timedelta(minutes=minutes + 5),
                    finished_at=now - timedelta(minutes=minutes),
                    outcome=SyncOutcome.SUCCESS,
                )
            )
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=now,
                jobs=(
                    Job(
                        id="greenhouse:acme:1",
                        source="greenhouse",
                        company="Acme",
                        title_raw="Software Engineering Intern",
                        title_normalized="software engineering intern",
                        apply_url_raw="https://example.com/1",
                        description="",
                        first_seen=now,
                        last_seen=now,
                        location_raw="Montreal, QC",
                        status=JobStatus.OPEN,
                    ),
                ),
                closable_boards=("greenhouse:acme",),
            )
        )

    sent = await _announce(db, "https://discord.com/api/webhooks/1/tok", monkeypatch)

    assert len(sent) == 1
    assert "1 new posting(s)" in sent[0]["embeds"][0]["title"]
    assert "https://example.com/1" in sent[0]["embeds"][0]["description"]


def _invoke(args: list[str], home: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    from typer.testing import CliRunner

    from stage.cli.app import app

    monkeypatch.setattr(notify, "webhook_path", lambda: home / "notify.json")
    return CliRunner().invoke(app, args)


def test_the_command_stores_a_webhook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://discord.com/api/webhooks/123/TOKEN"

    result = _invoke(["schedule", "notify", url], tmp_path, monkeypatch)

    assert result.exit_code == 0  # type: ignore[attr-defined]
    assert notify.read(tmp_path / "notify.json") == url


def test_the_command_refuses_a_url_that_is_not_discord(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _invoke(
        ["schedule", "notify", "https://evil.example/api/webhooks/1/x"], tmp_path, monkeypatch
    )

    assert result.exit_code == 2  # type: ignore[attr-defined]
    assert not (tmp_path / "notify.json").exists()


def test_the_command_never_prints_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _invoke(
        ["schedule", "notify", "https://discord.com/api/webhooks/123/SUPERSECRET"],
        tmp_path,
        monkeypatch,
    )

    result = _invoke(["schedule", "notify"], tmp_path, monkeypatch)
    output = result.output  # type: ignore[attr-defined]

    assert "SUPERSECRET" not in output
    assert "discord.com" in output


def test_the_command_says_when_nothing_is_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _invoke(["schedule", "notify"], tmp_path, monkeypatch)

    assert result.exit_code == 0  # type: ignore[attr-defined]
    assert "No Discord webhook" in result.output  # type: ignore[attr-defined]


def test_clearing_removes_the_webhook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _invoke(["schedule", "notify", "https://discord.com/api/webhooks/1/tok"], tmp_path, monkeypatch)

    result = _invoke(["schedule", "notify", "--clear"], tmp_path, monkeypatch)

    assert result.exit_code == 0  # type: ignore[attr-defined]
    assert notify.read(tmp_path / "notify.json") == ""


def test_testing_without_a_webhook_explains_what_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _invoke(["schedule", "test-notify"], tmp_path, monkeypatch)

    assert result.exit_code == 2  # type: ignore[attr-defined]
    assert "stage schedule notify" in result.output  # type: ignore[attr-defined]


def test_a_test_message_reaches_the_webhook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[dict[str, Any]] = []

    class _Response:
        status = 204

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_: object) -> None:
            return None

    def capture(request: object, timeout: object = None) -> _Response:
        sent.append(json.loads(request.data.decode()))  # type: ignore[attr-defined]
        return _Response()

    _invoke(["schedule", "notify", "https://discord.com/api/webhooks/1/tok"], tmp_path, monkeypatch)
    monkeypatch.setattr(notify, "urlopen", capture)
    result = _invoke(["schedule", "test-notify"], tmp_path, monkeypatch)

    assert result.exit_code == 0  # type: ignore[attr-defined]
    assert sent
    assert "Test message" in sent[0]["embeds"][0]["description"]


def test_a_discord_outage_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib.error import URLError

    def explode(request: object, timeout: object = None) -> None:
        raise URLError("no route to host")

    _invoke(["schedule", "notify", "https://discord.com/api/webhooks/1/tok"], tmp_path, monkeypatch)
    monkeypatch.setattr(notify, "urlopen", explode)
    result = _invoke(["schedule", "test-notify"], tmp_path, monkeypatch)

    assert result.exit_code == 1  # type: ignore[attr-defined]
    assert "Could not reach Discord" in result.output  # type: ignore[attr-defined]


@pytest.mark.parametrize("code", [400, 401, 403, 404, 429, 500, 503])
def test_every_discord_status_is_reported_not_swallowed(
    code: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(request: object, timeout: object = None) -> None:
        raise _http_error(code)

    monkeypatch.setattr(notify, "urlopen", explode)

    with pytest.raises(notify.NotifyError):
        notify.post("https://discord.com/api/webhooks/1/tok", {"content": "hi"})


def test_rate_limiting_says_to_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(request: object, timeout: object = None) -> None:
        raise _http_error(429)

    monkeypatch.setattr(notify, "urlopen", explode)

    with pytest.raises(notify.NotifyError, match="rate limiting"):
        notify.post("https://discord.com/api/webhooks/1/tok", {"content": "hi"})


@pytest.mark.parametrize(
    "url",
    [
        "  https://discord.com/api/webhooks/1/tok  ",
        "https://discord.com/api/webhooks/1/tok\n",
        "\thttps://discord.com/api/webhooks/1/tok",
    ],
)
def test_surrounding_whitespace_is_trimmed_not_stored(url: str) -> None:
    assert notify.validate(url) == "https://discord.com/api/webhooks/1/tok"


def test_whitespace_inside_a_url_is_refused() -> None:
    with pytest.raises(notify.NotifyError, match="spaces or line breaks"):
        notify.validate("https://discord.com/api/webhooks/1/to k")


@pytest.mark.parametrize(
    "host", ["discord.com", "ptb.discord.com", "canary.discord.com", "discordapp.com"]
)
def test_every_discord_host_is_accepted(host: str) -> None:
    url = f"https://{host}/api/webhooks/1/tok"

    assert notify.validate(url) == url


def test_a_lookalike_host_is_refused() -> None:
    with pytest.raises(notify.NotifyError):
        notify.validate("https://discord.com.evil.example/api/webhooks/1/tok")


def test_a_webhook_is_never_world_readable_even_briefly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "notify.json"
    seen: list[int] = []
    real = paths.restrict_permissions

    def watch(path: Path) -> None:
        seen.append(path.stat().st_mode & 0o077)
        real(path)

    monkeypatch.setattr(notify, "restrict_permissions", watch)
    notify.remember("https://discord.com/api/webhooks/1/tok", target)

    assert seen == [0]


def test_a_rejected_url_leaves_a_stored_webhook_intact(tmp_path: Path) -> None:
    target = tmp_path / "notify.json"
    notify.remember("https://discord.com/api/webhooks/1/tok", target)

    with pytest.raises(notify.NotifyError):
        notify.remember("https://evil.example/api/webhooks/1/tok", target)

    assert notify.read(target) == "https://discord.com/api/webhooks/1/tok"


def _ranked(location: str, role: str, day: int = 1) -> Any:
    from dataclasses import dataclass
    from datetime import UTC, datetime

    from stage.domain import LocationBucket, RoleCategory

    @dataclass
    class Row:
        location: LocationBucket
        role: RoleCategory
        first_seen: datetime

    return Row(
        location=LocationBucket(location),
        role=RoleCategory(role),
        first_seen=datetime(2026, 9, day, tzinfo=UTC),
    )


def test_montreal_sorts_ahead_of_everywhere_else() -> None:
    rows = [
        _ranked("usa", "swe"),
        _ranked("canada", "swe"),
        _ranked("montreal", "general-cs"),
    ]

    order = [row.location.value for row in sorted(rows, key=notify.rank)]

    assert order == ["montreal", "canada", "usa"]


def test_software_sorts_ahead_of_quant_within_one_place() -> None:
    rows = [
        _ranked("montreal", "data"),
        _ranked("montreal", "quant"),
        _ranked("montreal", "swe"),
    ]

    order = [row.role.value for row in sorted(rows, key=notify.rank)]

    assert order == ["swe", "quant", "data"]


def test_place_outranks_discipline() -> None:
    rows = [_ranked("usa", "swe"), _ranked("montreal", "general-cs")]

    first = sorted(rows, key=notify.rank)[0]

    assert first.location.value == "montreal"


def test_the_newest_posting_wins_a_tie() -> None:
    rows = [_ranked("usa", "swe", day=1), _ranked("usa", "swe", day=5)]

    first = sorted(rows, key=notify.rank)[0]

    assert first.first_seen.day == 5


def test_an_unknown_place_or_role_still_ranks() -> None:
    rows = [_ranked("unknown", "unknown"), _ranked("montreal", "swe")]

    order = [row.location.value for row in sorted(rows, key=notify.rank)]

    assert order == ["montreal", "unknown"]


def test_the_search_debounce_stays_under_a_quarter_second() -> None:
    from stage.tui.state import DEBOUNCE_SECONDS

    assert 0.1 <= DEBOUNCE_SECONDS <= 0.25
