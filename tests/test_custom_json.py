from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
import yaml

from stage.companies import RegistryError, load_companies, write_registry
from stage.domain import Company, CustomBoard, Platform
from stage.http import HttpClient, RatePosture, ValidatorCache
from stage.sources import adapter_for_platform, load_builtins
from stage.sources.base import PayloadValidationError
from stage.sources.custom_json import CustomJsonAdapter

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
URL = "https://jobs.example.test/api/openings"

PAYLOAD = {
    "data": {
        "positions": [
            {
                "reqId": "R-101",
                "name": "Stagiaire en développement logiciel",
                "office": {"city": "Montréal, QC"},
                "absolute_url": "https://jobs.example.test/apply/R-101",
                "content": "<p>Poste à <b>Montréal</b></p>",
            },
            {
                "reqId": "R-102",
                "name": "Data Science Intern",
                "office": {"city": "Toronto, ON"},
                "content": "",
            },
            {"reqId": "R-103", "office": {"city": "Nowhere"}},
        ]
    }
}

BOARD = CustomBoard(
    url=URL,
    jobs_path="data.positions",
    fields={
        "id": "reqId",
        "title": "name",
        "location": "office.city",
        "url": "absolute_url",
        "description": "content",
    },
    url_template="https://jobs.example.test/jobs/{id}",
)


def _company(board: CustomBoard = BOARD) -> Company:
    return Company(name="Acme", platform=Platform.CUSTOM_JSON, slug="acme", custom=board)


def _client() -> HttpClient:
    return HttpClient(
        allowed_hosts=frozenset({"jobs.example.test"}),
        posture=RatePosture(min_interval_s=0.0),
        cache=ValidatorCache(),
    )


@pytest.mark.asyncio
@respx.mock
async def test_a_mapped_payload_becomes_jobs() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json=PAYLOAD))
    adapter = CustomJsonAdapter()
    async with _client() as client:
        result = await adapter.fetch(_company(), client, NOW)

    assert [job.title_raw for job in result.jobs] == [
        "Stagiaire en développement logiciel",
        "Data Science Intern",
    ]
    first = result.jobs[0]
    assert first.id == "custom_json:acme:r-101"
    assert first.apply_url_raw == "https://jobs.example.test/apply/R-101"
    assert first.location_raw == "Montréal, QC"
    assert first.description == "Poste à Montréal"
    assert "<" not in first.description


@pytest.mark.asyncio
@respx.mock
async def test_a_row_without_a_title_is_dropped_and_costs_the_listing_its_authority() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json=PAYLOAD))
    async with _client() as client:
        result = await CustomJsonAdapter().fetch(_company(), client, NOW)

    assert len(result.jobs) == 2
    assert result.authoritative is False
    assert "failed validation" in result.degraded


@pytest.mark.asyncio
@respx.mock
async def test_a_url_template_fills_in_when_the_payload_has_no_link() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json=PAYLOAD))
    async with _client() as client:
        result = await CustomJsonAdapter().fetch(_company(), client, NOW)
    assert result.jobs[1].apply_url_raw == "https://jobs.example.test/jobs/R-102"


@pytest.mark.asyncio
@respx.mock
async def test_a_jobs_path_that_is_not_a_list_raises_and_captures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STAGE_CAPTURE_DIR", str(tmp_path))
    respx.get(URL).mock(return_value=httpx.Response(200, json={"data": {"positions": {}}}))
    async with _client() as client:
        with pytest.raises(PayloadValidationError, match="is not a list"):
            await CustomJsonAdapter().fetch(_company(), client, NOW)
    assert list(tmp_path.glob("custom_json-*.json"))


@pytest.mark.asyncio
@respx.mock
async def test_a_root_level_list_needs_no_jobs_path() -> None:
    flat = [{"reqId": "1", "name": "Intern"}]
    respx.get(URL).mock(return_value=httpx.Response(200, json=flat))
    board = CustomBoard(url=URL, fields={"id": "reqId", "title": "name"})
    async with _client() as client:
        result = await CustomJsonAdapter().fetch(_company(board), client, NOW)
    assert [job.title_raw for job in result.jobs] == ["Intern"]


def test_the_allowed_hosts_come_from_the_registry_rows() -> None:
    adapter = CustomJsonAdapter()
    assert adapter.hosts_for([_company()]) == frozenset({"jobs.example.test"})
    assert adapter.hosts_for([]) == frozenset()
    assert adapter.board_key(_company()) == "custom_json:acme"


def test_a_row_with_no_custom_block_fails_loudly_rather_than_silently() -> None:
    naked = Company(name="Acme", platform=Platform.CUSTOM_JSON, slug="acme")
    with pytest.raises(PayloadValidationError, match="needs a 'custom' block"):
        CustomJsonAdapter().plan(naked)


def test_the_platform_is_routable_now_that_the_adapter_exists() -> None:
    load_builtins()
    assert adapter_for_platform(Platform.CUSTOM_JSON) is not None


def _registry(tmp_path: Path, row: dict[str, object]) -> Path:
    target = tmp_path / "companies.yaml"
    target.write_text(yaml.safe_dump([row]), encoding="utf-8")
    return target


BASE_ROW: dict[str, object] = {"name": "Acme", "platform": "custom_json", "slug": "acme"}


def test_a_custom_row_round_trips_through_the_registry(tmp_path: Path) -> None:
    row = dict(
        BASE_ROW,
        custom={
            "url": URL,
            "jobs_path": "data.positions",
            "fields": {"id": "reqId", "title": "name", "location": "office.city"},
            "url_template": "https://jobs.example.test/jobs/{id}",
        },
    )
    target = _registry(tmp_path, row)
    loaded = load_companies(target)[0]
    assert loaded.custom is not None
    assert loaded.custom.url == URL
    assert loaded.custom.fields["title"] == "name"

    write_registry([loaded], target)
    again = load_companies(target)[0]
    assert again.custom == loaded.custom


@pytest.mark.parametrize(
    "url",
    (
        "http://jobs.example.test/api/openings",
        "https://localhost/api/openings",
        "https://localhost./api/openings",
        "https://127.0.0.1/api/openings",
        "https://2130706433/api/openings",
        "https://[::1]/api/openings",
        "https://user:password@jobs.example.test/api/openings",
    ),
)
def test_a_custom_row_cannot_target_an_internal_or_insecure_endpoint(
    tmp_path: Path, url: str
) -> None:
    row = dict(BASE_ROW, custom={"url": url, "fields": {"title": "name"}})
    with pytest.raises(RegistryError, match="public https"):
        load_companies(_registry(tmp_path, row))


BAD_ROWS = {
    "no custom block at all": BASE_ROW,
    "a url that is not a web address": dict(
        BASE_ROW, custom={"url": "javascript:alert(1)", "fields": {"title": "name"}}
    ),
    "no title mapping": dict(BASE_ROW, custom={"url": URL, "fields": {"id": "reqId"}}),
    "an unknown field name": dict(
        BASE_ROW, custom={"url": URL, "fields": {"title": "name", "salary": "pay"}}
    ),
    "fields that is not a mapping": dict(BASE_ROW, custom={"url": URL, "fields": ["name"]}),
    "an empty mapping value": dict(BASE_ROW, custom={"url": URL, "fields": {"title": "  "}}),
}


@pytest.mark.parametrize("label", list(BAD_ROWS))
def test_a_broken_custom_block_is_refused_at_load(tmp_path: Path, label: str) -> None:
    with pytest.raises(RegistryError):
        load_companies(_registry(tmp_path, BAD_ROWS[label]))


def test_a_custom_block_on_another_platform_is_refused(tmp_path: Path) -> None:
    row: dict[str, object] = {
        "name": "Acme",
        "platform": "greenhouse",
        "slug": "acme",
        "custom": {"url": URL, "fields": {"title": "name"}},
    }
    with pytest.raises(RegistryError, match="only belongs on platform custom_json"):
        load_companies(_registry(tmp_path, row))


def test_an_unrecognised_url_offers_a_custom_json_skeleton() -> None:
    from rich.console import Console

    from stage.cli.render import _show_custom_skeleton, registry_slug

    console = Console(width=120, no_color=True, force_terminal=False, record=True)
    _show_custom_skeleton(console, "https://jobs.rbc.com/ca/en/students", "RBC")
    rendered = console.export_text()
    assert "platform: custom_json" in rendered
    assert "slug: rbc" in rendered
    assert "enabled: false" in rendered
    assert "PASTE_THE_JSON_REQUEST_URL_HERE" in rendered
    assert registry_slug("Banque Nationale du Canada") == "banque-nationale-du-canada"


def test_a_url_that_is_not_a_web_address_gets_no_skeleton() -> None:
    from rich.console import Console

    from stage.cli.render import _show_custom_skeleton

    console = Console(width=120, no_color=True, force_terminal=False, record=True)
    _show_custom_skeleton(console, "javascript:alert(1)", "Nope")
    assert console.export_text().strip() == ""


HANDSHAKE_BOARD = CustomBoard(
    url="https://api.example.test/search",
    method="POST",
    handshake_url="https://board.example.test/home",
    token_pattern='"token":"([A-Za-z0-9._-]+)"',
    token_header="Authorization",
    token_prefix="Bearer ",
    jobs_path="data.rows",
    fields={"id": "reqId", "title": "name"},
)


def _handshake_client() -> HttpClient:
    return HttpClient(
        allowed_hosts=frozenset({"api.example.test", "board.example.test"}),
        posture=RatePosture(min_interval_s=0.0),
        cache=ValidatorCache(),
    )


@pytest.mark.asyncio
@respx.mock
async def test_a_handshake_token_is_sent_with_its_scheme_prefix() -> None:
    respx.get("https://board.example.test/home").mock(
        return_value=httpx.Response(200, text='var ctx={"token":"abc.def.ghi"};')
    )
    rows = {"data": {"rows": [{"reqId": "1", "name": "Intern"}]}}
    route = respx.post("https://api.example.test/search").mock(
        return_value=httpx.Response(200, json=rows)
    )
    adapter = CustomJsonAdapter()
    async with _handshake_client() as client:
        result = await adapter.fetch(_company(HANDSHAKE_BOARD), client, NOW)

    assert [job.title_raw for job in result.jobs] == ["Intern"]
    assert route.calls.last.request.headers["Authorization"] == "Bearer abc.def.ghi", (
        "a bearer token sent without its scheme is refused as unauthorized"
    )


@pytest.mark.asyncio
@respx.mock
async def test_a_handshake_without_a_prefix_sends_the_bare_token() -> None:
    board = CustomBoard(
        url=HANDSHAKE_BOARD.url,
        method="POST",
        handshake_url=HANDSHAKE_BOARD.handshake_url,
        token_pattern=HANDSHAKE_BOARD.token_pattern,
        token_header="x-csrf-token",
        jobs_path="data.rows",
        fields={"id": "reqId", "title": "name"},
    )
    respx.get("https://board.example.test/home").mock(
        return_value=httpx.Response(200, text='{"token":"plain-token"}')
    )
    rows = {"data": {"rows": [{"reqId": "1", "name": "Intern"}]}}
    route = respx.post("https://api.example.test/search").mock(
        return_value=httpx.Response(200, json=rows)
    )
    adapter = CustomJsonAdapter()
    async with _handshake_client() as client:
        await adapter.fetch(_company(board), client, NOW)

    assert route.calls.last.request.headers["x-csrf-token"] == "plain-token", (
        "the csrf boards that predate token_prefix must keep sending a bare token"
    )


def test_an_rss_value_arrives_with_its_entities_decoded() -> None:
    from stage.sources.custom_json import rss_items

    feed = (
        "<rss><channel><item><title>Property &amp; Tax Specialist</title>"
        "<link>https://board.test/job/Property-&amp;-Tax/9</link>"
        "<g:id>9</g:id></item></channel></rss>"
    )
    rows = rss_items(feed)

    assert rows[0]["title"] == "Property & Tax Specialist", "an entity reached the stored title"
    assert rows[0]["link"] == "https://board.test/job/Property-&-Tax/9", (
        "an unescaped entity in an apply url sends the user to a different address"
    )


def test_a_cdata_body_is_left_exactly_as_the_publisher_wrote_it() -> None:
    from stage.sources.custom_json import rss_items

    feed = "<rss><item><description><![CDATA[<p>Stage &amp; co-op</p>]]></description></item></rss>"

    assert rss_items(feed)[0]["description"] == "<p>Stage &amp; co-op</p>", (
        "cdata is already literal, so unescaping it twice corrupts the body"
    )


def test_a_mapped_field_holding_an_object_becomes_a_readable_location() -> None:
    from stage.sources.custom_json import _project

    board = CustomBoard(url="https://x.test/j", fields={"title": "name", "location": "locations"})
    place = {"city": "Montréal", "state": "Quebec", "country": "CA"}
    entry = {"name": "Intern", "locations": [place]}

    assert _project(entry, board)["location"] == "Montréal, Quebec, CA", (
        "an object location must flatten, or the resolver reads a python dict repr"
    )
