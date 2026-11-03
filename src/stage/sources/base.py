import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Annotated, Any, ClassVar, Concatenate, Protocol, cast, runtime_checkable

from pydantic import BaseModel, BeforeValidator, StringConstraints, ValidationError

from stage.domain import Company, Job, Platform, WorkdayCrawlStep, board_key
from stage.domain.text import counted
from stage.http import (
    HttpClient,
    JsonResponse,
    RequestRefusedError,
    TextResponse,
    failure_text,
)
from stage.paths import capture_dir
from stage.sources.platforms import SlugRejectedError, safe_slug

THREAD_BUILD_BYTES = 1024 * 1024
PROCESS_PARSE_CHARS = 128 * 1024
BUILD_SLICE_ROWS = 10
BUILD_SLICE_S = 0.01
_BUILDER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stage-build")
type PageParser = Callable[[Callable[[], Any]], Awaitable[Any]]
_PARSER: ContextVar[PageParser | None] = ContextVar("stage_page_parser", default=None)


async def off_loop[**P, R](function: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_BUILDER, partial(function, *args, **kwargs))


def parse_pages_with(parser: PageParser | None) -> None:
    _PARSER.set(parser)


async def parse_page[**P, R](
    function: Callable[Concatenate[str, P], R], text: str, *args: P.args, **kwargs: P.kwargs
) -> R:
    parser = _PARSER.get()
    if parser is None or len(text) < PROCESS_PARSE_CHARS:
        return await off_loop(function, text, *args, **kwargs)
    return cast(R, await parser(partial(function, text, *args, **kwargs)))


class AdapterError(Exception):
    pass


class PayloadValidationError(AdapterError):
    def __init__(self, message: str, captured: str = "") -> None:
        super().__init__(message)
        self.captured = captured


NullableStr = Annotated[str, BeforeValidator(lambda value: "" if value is None else value)]
NonEmptyStr = Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]
NullableBool = Annotated[bool, BeforeValidator(lambda value: False if value is None else value)]


def validate_rows[ModelT: BaseModel](
    model: type[ModelT], rows: Sequence[Any], *, source: str, slug: str
) -> tuple[list[ModelT], int]:
    kept: list[ModelT] = []
    dropped = 0
    for row in rows:
        try:
            kept.append(model.model_validate(row))
        except ValidationError:
            dropped += 1
            capture_payload(f"{source}-posting", slug, row)
    return kept, dropped


def convert_rows[ModelT: BaseModel](
    build: Callable[[ModelT], Job], rows: Sequence[ModelT], *, source: str, slug: str
) -> tuple[list[Job], int]:
    kept: list[Job] = []
    dropped = 0
    for row in rows:
        try:
            kept.append(build(row))
        except (ValueError, OverflowError, OSError):
            dropped += 1
            capture_payload(f"{source}-posting", slug, row.model_dump(mode="json"))
    return kept, dropped


def malformed_note(dropped: int) -> str:
    if not dropped:
        return ""
    return (
        f"Stage dropped {counted(dropped, 'posting')} that failed validation and saved "
        "the raw rows, so this read closes nothing."
    )


def sentences(*notes: str) -> str:
    return " ".join(_sentence(note) for note in notes if note.strip())


def _sentence(note: str) -> str:
    text = note.strip()
    text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else f"{text}."


def validation_problem(exc: ValidationError) -> str:
    first = exc.errors()[0]
    field_name = ".".join(str(part) for part in first["loc"]) or "top-level"
    if first["type"] == "missing":
        return f'no "{field_name}" field'
    return f'an invalid "{field_name}" field ({str(first["msg"]).lower()})'


def parsed[ModelT: BaseModel](
    model: type[ModelT], payload: Any, *, source: str, slug: str, board: str
) -> ModelT:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        problem = f"{board} answered with {validation_problem(exc)}"
        raise payload_error(source, slug, payload, problem) from exc


def payload_error(source: str, slug: str, payload: Any, problem: str) -> PayloadValidationError:
    captured = capture_payload(source, slug, payload)
    return PayloadValidationError(f"{problem}, and Stage saved the answer to {captured}", captured)


async def read_files[ResponseT: JsonResponse | TextResponse](
    name: str,
    urls: Sequence[str],
    fetch: Callable[[str], Awaitable[ResponseT]],
    parse: Callable[[str, ResponseT], tuple[Sequence[Job], int]],
    *,
    unique: bool = False,
) -> "FetchResult":
    jobs: list[Job] = []
    modified = False
    failures: list[Exception] = []
    stale: list[str] = []
    malformed = 0
    for url in urls:
        try:
            response = await fetch(url)
        except Exception as exc:
            failures.append(exc)
            stale.append(url)
            continue
        if response.not_modified:
            continue
        modified = True
        found, dropped = parse(url, response)
        malformed += dropped
        jobs.extend(found)
    if failures and not jobs:
        if all(isinstance(exc, RequestRefusedError) for exc in failures):
            raise failures[0]
        said = ", ".join(dict.fromkeys(failure_text(exc) for exc in failures))
        raise PayloadValidationError(f"Every {name} file failed: {said}")
    if not modified and not failures:
        return FetchResult(not_modified=True)
    kept = tuple({job.id: job for job in jobs}.values()) if unique else tuple(jobs)
    return FetchResult(
        jobs=kept,
        degraded=sentences(
            f"{len(failures)} of {counted(len(urls), 'file')} unavailable" if failures else "",
            malformed_note(malformed),
        ),
        authoritative=not (failures or malformed),
        stale_urls=tuple(stale),
    )


@dataclass(frozen=True, slots=True)
class FetchResult:
    jobs: tuple[Job, ...] = field(default_factory=tuple)
    not_modified: bool = False
    authoritative: bool = True
    degraded: str = ""
    declared_empty: bool = False
    stale_urls: tuple[str, ...] = field(default_factory=tuple)
    detail_fetches: tuple[object, ...] = field(default_factory=tuple)
    facets: tuple[object, ...] = field(default_factory=tuple)
    forgotten_facets: tuple[object, ...] = field(default_factory=tuple)
    workday_crawl: WorkdayCrawlStep | None = None


@runtime_checkable
class Adapter(Protocol):
    name: ClassVar[str]
    platform: ClassVar[Platform]
    rate_profile: ClassVar[str]
    hosts: ClassVar[frozenset[str]]

    bucket_key: ClassVar[str]

    detail_budget: ClassVar[int]

    rotation_slice: ClassVar[int]

    max_requests_per_company: ClassVar[int]

    def board_key(self, company: Company) -> str:
        pass

    def hosts_for(self, companies: Sequence[Company]) -> frozenset[str]:
        pass

    def plan(self, company: Company) -> tuple[str, ...]:
        pass

    async def fetch(
        self,
        company: Company,
        client: HttpClient,
        now: datetime,
        facets: Any = None,
        details: Sequence[str] = (),
    ) -> FetchResult:
        pass


class BoardAdapter:
    name: ClassVar[str]
    platform: ClassVar[Platform]
    rate_profile: ClassVar[str]
    hosts: ClassVar[frozenset[str]] = frozenset()
    bucket_key: ClassVar[str] = ""
    detail_budget: ClassVar[int]
    rotation_slice: ClassVar[int] = 0
    max_requests_per_company: ClassVar[int]

    row_model: ClassVar[type[BaseModel]]
    root_model: ClassVar[type[BaseModel] | None] = None
    rows_field: ClassVar[str] = ""
    base_url: ClassVar[str] = ""
    host_template: ClassVar[str] = ""
    path: ClassVar[str] = ""
    query: ClassVar[tuple[tuple[str, str], ...]] = ()
    slug_validator: ClassVar[Callable[[str], str]] = safe_slug

    def host_for(self, company: Company) -> str:
        return self.host_template.format(slug=type(self).slug_validator(company.slug))

    def url_for(self, company: Company) -> str:
        if self.host_template:
            return f"https://{self.host_for(company)}{self.path}"
        return self.base_url.format(slug=type(self).slug_validator(company.slug))

    def hosts_for(self, companies: Sequence[Company]) -> frozenset[str]:
        if not self.host_template:
            return self.hosts
        allowed: set[str] = set()
        for company in companies:
            try:
                allowed.add(self.host_for(company))
            except SlugRejectedError:
                continue
        return frozenset(allowed)

    def board_key(self, company: Company) -> str:
        return board_key(self.name, company.slug)

    def plan(self, company: Company) -> tuple[str, ...]:
        url = self.url_for(company)
        if self.query:
            url = f"{url}?" + "&".join(f"{key}={value}" for key, value in self.query)
        return (url,)

    def params(self) -> dict[str, str] | None:
        return dict(self.query) or None

    async def fetch(
        self,
        company: Company,
        client: HttpClient,
        now: datetime,
        facets: object = None,
        details: Sequence[str] = (),
    ) -> FetchResult:
        response = await client.get_json(self.url_for(company), params=self.params())
        if response.not_modified:
            return FetchResult(not_modified=True)
        return await self.built(company, response, now)

    async def built(self, company: Company, response: JsonResponse, now: datetime) -> FetchResult:
        if response.size < THREAD_BUILD_BYTES:
            return self.result(company, response.payload, now)
        return await off_loop(self.result, company, response.payload, now)

    def result(self, company: Company, payload: Any, now: datetime) -> FetchResult:
        rows, dropped = self.validate(company, payload)
        kept = [row for row in rows if self.keep(row)]
        return FetchResult(
            jobs=tuple(self.to_job(company, row, now) for row in kept),
            degraded=malformed_note(dropped),
            authoritative=not dropped,
        )

    def keep(self, row: Any) -> bool:
        return True

    def validate(self, company: Company, payload: Any) -> tuple[list[Any], int]:
        return validate_rows(
            self.row_model, self.rows(company, payload), source=self.name, slug=company.slug
        )

    def rows(self, company: Company, payload: Any) -> Sequence[Any]:
        if self.root_model is None:
            if not isinstance(payload, list):
                raise payload_error(
                    self.name,
                    company.slug,
                    payload,
                    f"{self.name}/{company.slug} answered with something other than "
                    "a JSON list of postings",
                )
            return payload
        root = parsed(
            self.root_model,
            payload,
            source=self.name,
            slug=company.slug,
            board=f"{self.name}/{company.slug}",
        )
        listed = getattr(root, self.rows_field)
        return listed if isinstance(listed, list) else []

    def to_job(self, company: Company, row: Any, now: datetime) -> Job:
        raise NotImplementedError


def capture_payload(source: str, slug: str, payload: Any) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    target = capture_dir() / f"{source}-{slug}-{stamp}.json"
    target.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    return str(target)
