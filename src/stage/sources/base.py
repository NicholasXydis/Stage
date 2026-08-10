import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel, BeforeValidator, StringConstraints, ValidationError

from stage.domain import Company, Job, Platform, board_key
from stage.http import HttpClient
from stage.paths import capture_dir
from stage.sources.platforms import SlugRejectedError, safe_slug


class AdapterError(Exception):
    pass


class PayloadValidationError(AdapterError):
    pass


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
        f"{dropped} posting(s) failed validation and were dropped, raw rows "
        "captured; the listing is incomplete so it closes nothing"
    )


@dataclass(frozen=True, slots=True)
class FetchResult:
    jobs: tuple[Job, ...] = field(default_factory=tuple)
    not_modified: bool = False
    authoritative: bool = True
    degraded: str = ""
    stale_urls: tuple[str, ...] = field(default_factory=tuple)
    detail_fetches: tuple[object, ...] = field(default_factory=tuple)
    facets: tuple[object, ...] = field(default_factory=tuple)
    forgotten_facets: tuple[object, ...] = field(default_factory=tuple)


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

    def host_for(self, company: Company) -> str:
        return self.host_template.format(slug=safe_slug(company.slug))

    def url_for(self, company: Company) -> str:
        if self.host_template:
            return f"https://{self.host_for(company)}{self.path}"
        return self.base_url.format(slug=safe_slug(company.slug))

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
        facets: object = None,  # noqa: ARG002
        details: Sequence[str] = (),  # noqa: ARG002
    ) -> FetchResult:
        response = await client.get_json(self.url_for(company), params=self.params())
        if response.not_modified:
            return FetchResult(not_modified=True)
        return self.result(company, response.payload, now)

    def result(self, company: Company, payload: Any, now: datetime) -> FetchResult:
        rows, dropped = self.validate(company, payload)
        kept = [row for row in rows if self.keep(row)]
        return FetchResult(
            jobs=tuple(self.to_job(company, row, now) for row in kept),
            degraded=malformed_note(dropped),
            authoritative=not dropped,
        )

    def keep(self, row: Any) -> bool:  # noqa: ARG002
        return True

    def validate(self, company: Company, payload: Any) -> tuple[list[Any], int]:
        return validate_rows(
            self.row_model, self.rows(company, payload), source=self.name, slug=company.slug
        )

    def rows(self, company: Company, payload: Any) -> Sequence[Any]:
        if self.root_model is None:
            if not isinstance(payload, list):
                captured = capture_payload(self.name, company.slug, payload)
                raise PayloadValidationError(
                    f"{self.name}/{company.slug}: field '<root>' failed validation (expected "
                    f"a JSON list of postings); raw payload captured at {captured}"
                )
            return payload
        try:
            root = self.root_model.model_validate(payload)
        except ValidationError as exc:
            captured = capture_payload(self.name, company.slug, payload)
            first = exc.errors()[0]
            field_name = ".".join(str(part) for part in first["loc"]) or "<root>"
            raise PayloadValidationError(
                f"{self.name}/{company.slug}: field {field_name!r} failed validation "
                f"({first['msg']}); raw payload captured at {captured}"
            ) from exc
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
