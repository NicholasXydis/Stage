import asyncio
import json
import re
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from html import unescape
from typing import Any, ClassVar
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from bs4.element import Tag
from pydantic import BaseModel, ConfigDict

from stage.domain import Company, CustomBoard, Job, Platform, SourceSignals, board_key, job_id
from stage.http import HttpClient
from stage.http.client import HostBudgetExceededError
from stage.lexicon import fold, location_lexicon
from stage.sources import register
from stage.sources._text import collapse_whitespace, strip_html
from stage.sources.base import (
    BUILD_SLICE_ROWS,
    BUILD_SLICE_S,
    FetchResult,
    NonEmptyStr,
    PayloadValidationError,
    convert_rows,
    malformed_note,
    off_loop,
    parse_page,
    payload_error,
    sentences,
    validate_rows,
)
from stage.sources.platforms import dig, first_int

MAX_ROWS = 5000
MAX_EXTRACT_BYTES = 8 * 1024 * 1024
MAX_RSS_ITEMS = 20000
MAX_TOKEN_LEN = 4096
MAX_HTML_ROWS = 2000

MAX_PATH_SEGMENTS = 8
MAX_PAGES = 20
PAGE_CEILING = 200
_JSON_PARSE = 'JSON.parse("'
_UNCHANGED = object()
_NUMBER = re.compile(r"\d+")
_DECLARED_EMPTY = object()
_TOTAL_KEYS = (
    "total",
    "totalCount",
    "total_count",
    "totalResults",
    "totalHits",
    "totalJobs",
    "totalRecords",
    "totalElements",
    "recordsTotal",
)
_TOTAL_PARENTS = ("", "meta", "meta.pagination", "pagination", "paging")


class CustomListing(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: NonEmptyStr
    title: NonEmptyStr
    location: str = ""
    url: str = ""
    description: str = ""
    department: str = ""
    employment_type: str = ""
    category: str = ""


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _rows(payload: Any, board: CustomBoard) -> Any:
    return dig(payload, board.jobs_path) if board.jobs_path else payload


def _stated_total(payload: Any, board: CustomBoard) -> int | None:
    parents = dict.fromkeys((*_TOTAL_PARENTS, board.jobs_path.rpartition(".")[0]))
    paths = tuple(f"{parent}.{key}" if parent else key for parent in parents for key in _TOTAL_KEYS)
    total = first_int(payload, paths)
    return total if total is not None and total >= 0 else None


def _page_body(board: CustomBoard, index: int) -> dict[str, Any]:
    body = dict(board.body)
    if not board.paginated:
        return body
    parts = board.page_param.split(".")
    branch: dict[str, Any] = body
    for part in parts[:-1]:
        nested = branch.get(part)
        branch[part] = dict(nested) if isinstance(nested, Mapping) else {}
        branch = branch[part]
    branch[parts[-1]] = board.page_value(index)
    return body


def _page_url(board: CustomBoard, index: int) -> str:
    if not board.paginated or board.posts:
        return board.url
    parts = urlsplit(board.url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)]
    query = [(key, value) for key, value in query if key != board.page_param]
    query.append((board.page_param, str(board.page_value(index))))
    return urlunsplit(parts._replace(query=urlencode(query)))


def _js_string_object(text: str, quote: int) -> Any:
    limit = min(len(text), quote + MAX_EXTRACT_BYTES)
    index = quote + 1
    while index < limit:
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == '"':
            try:
                return json.loads(json.loads(text[quote : index + 1]))
            except ValueError:
                return None
        index += 1
    return None


def extract_object(text: str, marker: str) -> Any:
    start = text.find(marker)
    if start < 0:
        return None
    brace = text.find("{", start + len(marker))
    wrapped = text.find(_JSON_PARSE, start + len(marker))
    if wrapped >= 0 and (brace < 0 or wrapped < brace):
        return _js_string_object(text, wrapped + len(_JSON_PARSE) - 1)
    if brace < 0 or len(text) - brace > MAX_EXTRACT_BYTES:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(brace, min(len(text), brace + MAX_EXTRACT_BYTES)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[brace : index + 1])
                except ValueError:
                    return None
    return None


def _tag_text(item: str, start: int) -> tuple[str, str, int] | None:
    open_at = item.find("<", start)
    if open_at < 0:
        return None
    close_at = item.find(">", open_at)
    if close_at < 0:
        return None
    name = item[open_at + 1 : close_at].split()[0] if close_at > open_at + 1 else ""
    if not name or name.startswith(("/", "?", "!")):
        return "", "", close_at + 1
    end = item.find(f"</{name}>", close_at)
    if end < 0:
        return "", "", close_at + 1
    body = item[close_at + 1 : end]
    if body.startswith("<![CDATA["):
        return name, body[9:].removesuffix("]]>").strip(), end + len(name) + 3
    return name, unescape(body).strip(), end + len(name) + 3


def rss_items(text: str, tag: str = "item") -> list[dict[str, str]]:
    opening, closing = f"<{tag}>", f"</{tag}>"
    rows: list[dict[str, str]] = []
    cursor = 0
    while len(rows) < MAX_RSS_ITEMS:
        start = text.find(opening, cursor)
        if start < 0:
            break
        end = text.find(closing, start)
        if end < 0:
            break
        item = text[start + len(opening) : end]
        cursor = end + len(closing)
        row: dict[str, str] = {}
        at = 0
        while True:
            step = _tag_text(item, at)
            if step is None:
                break
            name, body, at = step
            if name and body:
                row.setdefault(name, body)
        if row:
            rows.append(row)
    return rows


def _job_postings(block: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(block)
    except ValueError:
        return []
    pending = [payload]
    found: list[dict[str, Any]] = []
    while pending and len(found) < MAX_HTML_ROWS:
        entry = pending.pop()
        if isinstance(entry, list):
            pending.extend(entry)
        elif isinstance(entry, dict):
            graph = entry.get("@graph")
            if isinstance(graph, list):
                pending.extend(graph)
            if entry.get("@type") == "JobPosting":
                found.append(entry)
    return found


def jsonld_rows(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor = 0
    while len(rows) < MAX_HTML_ROWS:
        start = text.find("<script", cursor)
        if start < 0:
            break
        opened = text.find(">", start)
        if opened < 0:
            break
        head = text[start:opened]
        closed = text.find("</script>", opened)
        if closed < 0:
            break
        if "application/ld+json" in head:
            rows.extend(_job_postings(text[opened + 1 : min(closed, opened + MAX_EXTRACT_BYTES)]))
        cursor = closed + 9
    return rows


def _segment_title(part: str) -> str:
    words = part.replace("-", " ").split()
    if not words:
        return ""
    lexicon = location_lexicon()
    titled = [word.title() for word in words]
    tail = fold(words[-1])
    if len(tail) == 2 and (tail in lexicon.usa_codes or tail in lexicon.canada_codes):
        titled[-1] = tail.upper()
    return " ".join(titled).strip()


def sitemap_rows(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    cursor = 0
    while len(rows) < MAX_RSS_ITEMS:
        start = text.find("<url>", cursor)
        if start < 0:
            break
        end = text.find("</url>", start)
        if end < 0:
            break
        entry = text[start + 5 : end]
        cursor = end + 6
        loc = ""
        at = 0
        while True:
            step = _tag_text(entry, at)
            if step is None:
                break
            name, body, at = step
            if name.endswith("loc") and body:
                loc = body
                break
        if not loc:
            continue
        tail = loc.rstrip("/").rsplit("/", 1)[-1]
        slug, _, ident = tail.rpartition("_")
        if not slug:
            slug, ident = tail, tail
        row = {
            "loc": loc,
            "id": ident,
            "slug": slug,
            "title": slug.replace("-", " ").strip().title(),
        }
        segments = [
            part for part in loc.split("?", 1)[0].split("#", 1)[0].rstrip("/").split("/") if part
        ]
        for offset, part in enumerate(reversed(segments[-MAX_PATH_SEGMENTS:]), start=1):
            row[f"path{offset}"] = part
            row[f"path{offset}_title"] = _segment_title(part)
        rows.append(row)
    return rows


def _fingerprint(page: list[Any], board: CustomBoard) -> set[str]:
    marks = set()
    for entry in page:
        projected = _project(entry, board)
        mark = projected.get("id") or projected.get("title", "")
        if mark:
            marks.add(mark)
    return marks


def _slug_parts(url: str) -> tuple[str, str]:
    tail = url.split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    slug, _, ident = tail.rpartition("_")
    if not slug:
        slug, ident = tail, tail
    return slug, ident


def _html_value(block: Tag, selector: str, *, base: str = "") -> str:
    mode = "text"
    attr = ""
    css = selector
    for token, kind in (
        ("::attr(", "attr"),
        ("::slug(", "slug"),
        ("::slugid(", "slugid"),
        ("::number(", "number"),
    ):
        if token in selector:
            css, _, rest = selector.partition(token)
            attr = rest.rstrip(")")
            mode = kind
            break
    if not css:
        target: Tag | None = block
    else:
        target = block.select_one(css)
    if target is None:
        return ""
    if mode == "text":
        return collapse_whitespace(target.get_text(" ", strip=True))
    raw = target.get(attr, "")
    value = " ".join(raw) if isinstance(raw, list) else str(raw)
    if not value:
        return ""
    if mode == "attr":
        return urljoin(base, value) if base else value
    if mode == "number":
        found = _NUMBER.search(value)
        return found.group(0) if found else ""
    slug, ident = _slug_parts(value)
    return ident if mode == "slugid" else slug.replace("-", " ").strip().title()


def html_rows(text: str, selector: str) -> list[Tag]:
    soup = BeautifulSoup(text, "html.parser")
    return soup.select(selector)[:MAX_HTML_ROWS]


def html_listings(
    text: str, selector: str, fields: dict[str, str], url: str
) -> list[dict[str, str]]:
    board = CustomBoard(url=url, fmt="html", row_selector=selector, fields=fields)
    return [_project(block, board) for block in html_rows(text, selector)]


def _scalar(value: Any) -> str:
    if isinstance(value, Mapping):
        parts = [str(item) for item in value.values() if isinstance(item, str | int | float)]
        return ", ".join(part for part in parts if part)
    return str(value)


def _project(entry: Any, board: CustomBoard) -> dict[str, str]:
    if board.html and isinstance(entry, dict):
        return entry
    projected: dict[str, str] = {}
    for name in (
        "title",
        "id",
        "location",
        "url",
        "description",
        "department",
        "employment_type",
        "category",
    ):
        path = board.mapped(name)
        if not path:
            continue
        if board.html and isinstance(entry, Tag):
            base = board.url if name == "url" else ""
            projected[name] = _html_value(entry, path, base=base)
            continue
        value = dig(entry, path)
        if isinstance(value, list):
            value = " / ".join(_scalar(item) for item in value if item)
        projected[name] = "" if value is None else _scalar(value)
    if board.html and not projected.get("title") and projected.get("url"):
        projected["title"] = _title_from_link(projected["url"])
    if not projected.get("id"):
        projected["id"] = projected.get("url") or projected.get("title", "")
    return projected


def _title_from_link(url: str) -> str:
    words = [part for part in urlsplit(url).path.split("/") if part and not part.isdigit()]
    return " ".join(unquote(words[-1]).replace("-", " ").replace("_", " ").split()) if words else ""


@register
class CustomJsonAdapter:
    name: ClassVar[str] = "custom_json"
    platform: ClassVar[Platform] = Platform.CUSTOM_JSON
    rate_profile: ClassVar[str] = "paginated"
    hosts: ClassVar[frozenset[str]] = frozenset()
    bucket_key: ClassVar[str] = ""
    detail_budget: ClassVar[int] = 0
    rotation_slice: ClassVar[int] = 0
    max_requests_per_company: ClassVar[int] = PAGE_CEILING

    def board_key(self, company: Company) -> str:
        return board_key(self.name, company.slug)

    def hosts_for(self, companies: Sequence[Company]) -> frozenset[str]:
        found = set()
        for company in companies:
            if company.custom is not None:
                for candidate in (company.custom.url, company.custom.handshake_url):
                    host = _host(candidate)
                    if host:
                        found.add(host)
        return frozenset(found)

    def plan(self, company: Company) -> tuple[str, ...]:
        board = self._board(company)
        return (_page_url(board, 0),)

    @staticmethod
    def _board(company: Company) -> CustomBoard:
        if company.custom is None:
            raise PayloadValidationError(
                f"The registry row for {company.name} uses platform custom_json but has no "
                "custom block. Add one with a url and a title field mapping."
            )
        return company.custom

    async def fetch(
        self,
        company: Company,
        client: HttpClient,
        now: datetime,
        facets: object = None,
        details: Sequence[str] = (),
    ) -> FetchResult:
        board = self._board(company)
        headers = {**board.headers, **await self._handshake(board, client)}
        entries: list[Any] = []
        truncated = False
        stale_page = False
        ignored_param = False
        unconfirmed_end = False
        stated: int | None = None
        seen: set[str] = set()
        pages = board.page_budget(MAX_PAGES, PAGE_CEILING)
        for index in range(pages):
            try:
                payload = await self._page(company, board, client, index, headers)
            except HostBudgetExceededError:
                if index == 0:
                    raise
                truncated = True
                break
            if payload is _UNCHANGED:
                if index == 0:
                    return FetchResult(not_modified=True)
                stale_page = True
                break
            if payload is _DECLARED_EMPTY:
                return FetchResult(
                    degraded="The board lists no vacancies right now, so this read closes nothing.",
                    authoritative=False,
                    declared_empty=True,
                )
            page = _rows(payload, board)
            if not isinstance(page, list):
                if index:
                    break
                where = (
                    f'at jobs_path "{board.jobs_path}"' if board.jobs_path else "at its top level"
                )
                raise payload_error(
                    self.name,
                    company.slug,
                    payload,
                    f"{self.name}/{company.slug} answered with no list of jobs {where}",
                )
            total = _stated_total(payload, board)
            stated = stated if total is None else total
            fingerprint = _fingerprint(page, board)
            if index and fingerprint and fingerprint <= seen:
                ignored_param = index == 1
                unconfirmed_end = not ignored_param and (stated is None or len(seen) < stated)
                break
            seen |= fingerprint
            entries.extend(page)
            if not board.paginated or len(page) < board.page_size:
                break
            if len(entries) > MAX_ROWS:
                truncated = True
                break
        else:
            truncated = board.paginated
        truncated = truncated or len(entries) > MAX_ROWS
        kept = entries[:MAX_ROWS]
        jobs, dropped = await self._sliced_jobs(company, board, kept, now)
        where = (
            f"at {len(seen):,} of the {stated:,} postings the board states"
            if stated is not None
            else "and the board states no total"
        )
        return FetchResult(
            jobs=tuple(jobs),
            degraded=sentences(
                malformed_note(dropped),
                f"Stage stopped at the {pages}-page or {MAX_ROWS:,}-row cap, so the listing "
                "is incomplete"
                if truncated
                else "",
                "A later page answered 304, so the walk ended on an unchanged page"
                if stale_page
                else "",
                f'The board ignores the "{board.page_param}" page setting (page 2 repeated '
                "page 1), so Stage stopped rather than loop"
                if ignored_param
                else "",
                f"A later page repeated an earlier one {where}, so this read closes nothing"
                if unconfirmed_end
                else "",
            ),
            authoritative=board.authoritative
            and not (dropped or truncated or stale_page or ignored_param or unconfirmed_end),
        )

    def _jobs(
        self, company: Company, board: CustomBoard, entries: Sequence[Any], now: datetime
    ) -> tuple[list[Job], int]:
        listings, dropped = validate_rows(
            CustomListing,
            [_project(entry, board) for entry in entries],
            source=self.name,
            slug=company.slug,
        )
        jobs, unconvertible = convert_rows(
            lambda listing: self._to_job(company, board, listing, now),
            listings,
            source=self.name,
            slug=company.slug,
        )
        return jobs, dropped + unconvertible

    async def _sliced_jobs(
        self, company: Company, board: CustomBoard, entries: Sequence[Any], now: datetime
    ) -> tuple[list[Job], int]:
        jobs: list[Job] = []
        dropped = 0
        since = time.perf_counter()
        for number, entry in enumerate(entries):
            if number % BUILD_SLICE_ROWS == 0 or time.perf_counter() - since > BUILD_SLICE_S:
                await asyncio.sleep(0)
                since = time.perf_counter()
            built, lost = self._jobs(company, board, [entry], now)
            jobs.extend(built)
            dropped += lost
        return jobs, dropped

    async def _page(
        self,
        company: Company,
        board: CustomBoard,
        client: HttpClient,
        index: int,
        headers: Mapping[str, str],
    ) -> Any:
        if board.rss:
            text = await client.get_text(_page_url(board, index), revalidate=index > 0)
            if text.not_modified:
                return _UNCHANGED
            rows = await off_loop(rss_items, text.text, board.item_tag)
            if not rows:
                raise self._drifted(company, text.text, "has no <item> elements in its feed")
            return rows
        if board.embedded:
            text = await client.get_text(_page_url(board, index), revalidate=index > 0)
            if text.not_modified:
                return _UNCHANGED
            payload = await off_loop(extract_object, text.text, board.extract)
            if payload is None:
                raise self._drifted(
                    company, text.text, f'has no "{board.extract}" object in the page'
                )
            return payload
        if board.jsonld:
            text = await client.get_text(_page_url(board, index), revalidate=index > 0)
            if text.not_modified:
                return _UNCHANGED
            rows = await off_loop(jsonld_rows, text.text)
            if not rows and index == 0:
                raise self._drifted(company, text.text, "has no JobPosting in any ld+json block")
            return rows
        if board.sitemap:
            text = await client.get_text(_page_url(board, index), revalidate=index > 0)
            if text.not_modified:
                return _UNCHANGED
            rows = await off_loop(sitemap_rows, text.text)
            if board.row_filter:
                keep = re.compile(board.row_filter)
                rows = [row for row in rows if keep.search(row.get("loc", ""))]
            if not rows:
                raise self._drifted(company, text.text, "has no <url> entries in its sitemap")
            return rows
        if board.html:
            text = await client.get_text(_page_url(board, index), revalidate=index > 0)
            if text.not_modified:
                return _UNCHANGED
            blocks = await parse_page(
                html_listings, text.text, board.row_selector, dict(board.fields), board.url
            )
            if not blocks and index == 0:
                if board.empty_marker and re.search(board.empty_marker, text.text):
                    return _DECLARED_EMPTY
                raise self._drifted(
                    company, text.text, f'matched no rows with the selector "{board.row_selector}"'
                )
            return blocks
        response = (
            await client.post_json(board.url, body=_page_body(board, index), extra_headers=headers)
            if board.posts
            else await client.get_json(_page_url(board, index), revalidate=index > 0)
        )
        return _UNCHANGED if response.not_modified else response.payload

    def _drifted(self, company: Company, page: str, problem: str) -> PayloadValidationError:
        return payload_error(
            self.name, company.slug, {"head": page[:4000]}, f"{self.name}/{company.slug} {problem}"
        )

    @staticmethod
    async def _handshake(board: CustomBoard, client: HttpClient) -> dict[str, str]:
        if not board.handshakes:
            return {}
        page = await client.get_text(board.handshake_url)
        found = re.search(board.token_pattern, page.text)
        if found is None or not found.groups():
            raise PayloadValidationError(
                f'Stage found no token matching "{board.token_pattern}" at '
                f"{board.handshake_url}, so it could not read the board"
            )
        token = found.group(1)[:MAX_TOKEN_LEN]
        parts = urlsplit(board.handshake_url)
        return {
            board.token_header: f"{board.token_prefix}{token}",
            "Referer": board.handshake_url,
            "Origin": f"{parts.scheme}://{parts.netloc}",
        }

    def _to_job(
        self, company: Company, board: CustomBoard, listing: CustomListing, now: datetime
    ) -> Job:
        apply_url = listing.url
        if not apply_url and board.url_template:
            apply_url = board.url_template.format(id=listing.id)
        title = collapse_whitespace(strip_html(listing.title)) or listing.title
        return Job(
            id=job_id(self.name, company.slug, listing.id),
            source=self.name,
            company=company.name,
            title_raw=title,
            title_normalized=collapse_whitespace(title),
            apply_url_raw=apply_url or board.url,
            description=collapse_whitespace(strip_html(listing.description)),
            location_raw=collapse_whitespace(strip_html(listing.location)),
            first_seen=now,
            last_seen=now,
            signals=SourceSignals(
                employment_type=listing.employment_type,
                category=listing.category or listing.department,
            ),
        )
