from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from stage.domain import Company, CustomBoard, Platform
from stage.sources.custom_json import MAX_PAGES, CustomJsonAdapter, _page_url

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


class _Response:
    def __init__(self, payload: Any, not_modified: bool = False) -> None:
        self.payload = payload
        self.not_modified = not_modified


class _Client:
    def __init__(self, pages: list[Any]) -> None:
        self.pages = pages
        self.urls: list[str] = []

    async def get_json(self, url: str, *, revalidate: bool = False) -> _Response:
        self.urls.append(url)
        index = len(self.urls) - 1
        if index >= len(self.pages):
            return _Response({"positions": []})
        page = self.pages[index]
        if page is None:
            return _Response(None, not_modified=True)
        if isinstance(page, dict):
            return _Response(page)
        return _Response({"positions": page})


def _company(**kwargs: Any) -> Company:
    board = CustomBoard(
        url="https://boards.example.test/api/search?domain=x&start=0",
        jobs_path="positions",
        fields={"title": "name", "id": "id"},
        **kwargs,
    )
    return Company(name="Acme", platform=Platform.CUSTOM_JSON, slug="acme", custom=board)


def _rows(count: int, offset: int = 0) -> list[dict[str, Any]]:
    return [{"id": str(offset + i), "name": f"Intern {offset + i}"} for i in range(count)]


def test_a_board_without_paging_config_makes_one_request() -> None:
    company = _company()
    client = _Client([_rows(10), _rows(10)])
    result = __import__("asyncio").run(
        CustomJsonAdapter().fetch(company, client, NOW)  # type: ignore[arg-type]
    )

    assert len(client.urls) == 1, "an unpaginated board must not walk"
    assert len(result.jobs) == 10


def test_paging_walks_until_a_short_page_ends_it() -> None:
    company = _company(page_param="start", page_size=10)
    client = _Client([_rows(10), _rows(10, 10), _rows(4, 20)])
    result = __import__("asyncio").run(
        CustomJsonAdapter().fetch(company, client, NOW)  # type: ignore[arg-type]
    )

    assert len(client.urls) == 3, "the walk must continue while pages come back full"
    assert len(result.jobs) == 24, "every page's rows must be kept"
    assert result.authoritative, "a walk that reached the end may close what is missing"


def test_the_page_cap_makes_the_result_non_authoritative() -> None:
    company = _company(page_param="start", page_size=10)
    client = _Client([_rows(10, i * 10) for i in range(MAX_PAGES + 2)])
    result = __import__("asyncio").run(
        CustomJsonAdapter().fetch(company, client, NOW)  # type: ignore[arg-type]
    )

    assert len(client.urls) == MAX_PAGES
    assert not result.authoritative, "a truncated listing must never close what it did not see"


def test_a_304_on_a_later_page_does_not_end_the_list_authoritatively() -> None:
    company = _company(page_param="start", page_size=10)
    client = _Client([_rows(10), None])
    result = __import__("asyncio").run(
        CustomJsonAdapter().fetch(company, client, NOW)  # type: ignore[arg-type]
    )

    assert len(result.jobs) == 10
    assert not result.authoritative, "a later 304 says nothing about whether more pages exist"


def test_a_304_on_the_first_page_means_the_board_is_unchanged() -> None:
    company = _company(page_param="start", page_size=10)
    client = _Client([None])
    result = __import__("asyncio").run(
        CustomJsonAdapter().fetch(company, client, NOW)  # type: ignore[arg-type]
    )

    assert result.not_modified


@pytest.mark.parametrize(
    ("index", "expected"),
    [(0, "start=0"), (1, "start=10"), (3, "start=30")],
)
def test_the_page_parameter_is_replaced_not_appended(index: int, expected: str) -> None:
    board = CustomBoard(
        url="https://boards.example.test/api/search?domain=x&start=0",
        page_param="start",
        page_size=10,
        fields={"title": "name"},
    )
    url = _page_url(board, index)

    assert url.count("start=") == 1, "a repeated page parameter is ambiguous to the server"
    assert expected in url


def test_an_embedded_object_is_extracted_past_braces_inside_strings() -> None:
    from stage.sources.custom_json import extract_object

    page = 'var x=1; phApp.ddo = {"a": {"b": [1,2]}, "s": "} not the end {"}; more'
    assert extract_object(page, "phApp.ddo") == {"a": {"b": [1, 2]}, "s": "} not the end {"}
    assert extract_object(page, "absent") is None


def test_a_later_page_without_the_array_ends_the_walk_instead_of_failing() -> None:
    company = _company(page_param="start", page_size=10)
    client = _Client([_rows(10), {"totalJobs": 26}])
    result = __import__("asyncio").run(
        CustomJsonAdapter().fetch(company, client, NOW)  # type: ignore[arg-type]
    )

    assert len(result.jobs) == 10, "a later page that omits the array means the list ended"


def test_a_first_page_without_the_array_is_still_a_shape_error() -> None:
    from stage.sources.base import PayloadValidationError

    company = _company()
    client = _Client([{"totalJobs": 26}])
    with pytest.raises(PayloadValidationError):
        __import__("asyncio").run(
            CustomJsonAdapter().fetch(company, client, NOW)  # type: ignore[arg-type]
        )


def test_a_row_can_carry_request_headers_the_api_requires() -> None:
    from stage.domain import CustomBoard

    board = CustomBoard(url="https://x.example/api", headers={"Referer": "https://x.example/"})
    assert board.headers["Referer"] == "https://x.example/", "the header survives on the board"
    assert CustomBoard(url="https://x.example/api").headers == {}, "and defaults to none at all"


def test_registry_headers_round_trip(tmp_path: Path) -> None:
    from stage.companies import load_companies, write_registry
    from stage.domain import Company, CustomBoard, Platform

    target = tmp_path / "companies.yaml"
    board = CustomBoard(
        url="https://x.example/api",
        method="POST",
        fields={"title": "t"},
        headers={"Referer": "https://x.example/", "Origin": "https://x.example"},
    )
    row = Company(name="X", platform=Platform.CUSTOM_JSON, slug="x", custom=board)
    write_registry([row], target)
    back = load_companies(target)[0]
    assert back.custom is not None
    assert dict(back.custom.headers) == dict(board.headers), "headers survive the round trip"
