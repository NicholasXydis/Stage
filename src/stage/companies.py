import importlib
import os
import re
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

import yaml

from stage.domain import (
    KNOWN_FIELDS,
    REQUIRED_FIELDS,
    Company,
    CustomBoard,
    Platform,
    SourceOfRecord,
    public_https_url,
)
from stage.paths import registry_path
from stage.sources.platforms import SlugRejectedError, oracle_target


class RegistryError(Exception):
    pass


def _require_str(row: dict[str, Any], key: str, index: int) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(
            f"companies.yaml entry {index}: field {key!r} must be a non-empty string"
        )
    return value.strip()


def _optional_str(row: dict[str, Any], key: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RegistryError(f"companies.yaml: field {key!r} must be a string")
    return value.strip() or None


def _require_bool(row: dict[str, Any], key: str, index: int, *, default: bool) -> bool:
    value = row.get(key, default)
    if not isinstance(value, bool):
        raise RegistryError(
            f"companies.yaml entry {index}: {key!r} must be an unquoted true or false, "
            f"not {type(value).__name__} {value!r}"
        )
    return value


def _parse_date(row: dict[str, Any], key: str, index: int) -> date | None:
    value = row.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise RegistryError(f"companies.yaml entry {index}: field {key!r} must be a date")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise RegistryError(
                f"companies.yaml entry {index}: field {key!r} must be an ISO date like "
                f"2026-08-08, not {value!r}"
            ) from exc
    raise RegistryError(f"companies.yaml entry {index}: field {key!r} must be a date")


def _oracle_fields(
    row: dict[str, Any], index: int, platform: Platform
) -> tuple[str | None, str | None]:
    host = _optional_str(row, "oracle_host")
    site = _optional_str(row, "oracle_site")
    if platform is not Platform.ORACLE_CLOUD:
        if host is not None or site is not None:
            raise RegistryError(
                f"companies.yaml entry {index}: Oracle fields only belong on platform oracle_cloud"
            )
        return None, None
    if host is None or site is None:
        raise RegistryError(
            f"companies.yaml entry {index}: platform oracle_cloud needs oracle_host and oracle_site"
        )
    try:
        return oracle_target(host, site)
    except SlugRejectedError as exc:
        raise RegistryError(f"companies.yaml entry {index}: {exc}") from exc


def _custom_int(raw: dict[str, Any], name: str, index: int) -> int:
    value = raw.get(name, 0)
    if value in (None, ""):
        return 0
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RegistryError(
            f"companies.yaml entry {index}: custom.{name} must be a non-negative integer"
        )
    return value


def _custom_board(row: dict[str, Any], index: int, platform: Platform) -> CustomBoard | None:
    raw = row.get("custom")
    if raw is None:
        if platform is Platform.CUSTOM_JSON:
            raise RegistryError(
                f"companies.yaml entry {index}: platform custom_json needs a 'custom' block "
                "with url and a title field mapping"
            )
        return None
    if platform is not Platform.CUSTOM_JSON:
        raise RegistryError(
            f"companies.yaml entry {index}: a 'custom' block only belongs on platform custom_json"
        )
    if not isinstance(raw, dict):
        raise RegistryError(f"companies.yaml entry {index}: 'custom' must be a mapping")

    url = public_https_url(str(raw.get("url", "")))
    if url is None:
        raise RegistryError(
            f"companies.yaml entry {index}: custom.url must be a public https address "
            "without credentials"
        )
    mapping = raw.get("fields", {})
    if not isinstance(mapping, dict):
        raise RegistryError(f"companies.yaml entry {index}: custom.fields must be a mapping")
    unknown = sorted(set(mapping) - set(KNOWN_FIELDS))
    if unknown:
        raise RegistryError(
            f"companies.yaml entry {index}: custom.fields has unknown key(s) "
            f"{', '.join(unknown)}; known: {', '.join(KNOWN_FIELDS)}"
        )
    missing = [name for name in REQUIRED_FIELDS if not str(mapping.get(name, "")).strip()]
    if missing:
        raise RegistryError(
            f"companies.yaml entry {index}: custom.fields must map {', '.join(missing)}"
        )
    for key, value in mapping.items():
        if not isinstance(value, str) or not value.strip():
            raise RegistryError(
                f"companies.yaml entry {index}: custom.fields[{key!r}] must be a non-empty string"
            )
    method = str(raw.get("method", "GET") or "GET").upper()
    if method not in ("GET", "POST"):
        raise RegistryError(f"companies.yaml entry {index}: custom.method must be GET or POST")
    body = raw.get("body", {})
    if not isinstance(body, dict):
        raise RegistryError(f"companies.yaml entry {index}: custom.body must be a mapping")
    headers_raw = raw.get("headers", {})
    if not isinstance(headers_raw, dict):
        raise RegistryError(f"companies.yaml entry {index}: custom.headers must be a mapping")
    headers: dict[str, str] = {}
    for key, value in headers_raw.items():
        if not isinstance(value, str) or not value.strip():
            raise RegistryError(
                f"companies.yaml entry {index}: custom.headers[{key!r}] must be a non-empty string"
            )
        headers[str(key)] = value
    fmt = str(raw.get("format", "json") or "json").lower()
    if fmt not in ("json", "rss", "html", "sitemap"):
        raise RegistryError(
            f"companies.yaml entry {index}: custom.format must be json, rss, html or sitemap"
        )
    row_selector = str(raw.get("row_selector", "") or "")
    if fmt == "html" and not row_selector:
        raise RegistryError(
            f"companies.yaml entry {index}: custom.format html needs custom.row_selector"
        )
    extract = str(raw.get("extract", "") or "")
    handshake_url = ""
    if raw.get("handshake_url"):
        checked = public_https_url(str(raw["handshake_url"]))
        if checked is None:
            raise RegistryError(
                f"companies.yaml entry {index}: custom.handshake_url must be a public https address"
            )
        handshake_url = checked
    token_pattern = str(raw.get("token_pattern", "") or "")
    token_header = str(raw.get("token_header", "") or "")
    if handshake_url and not (token_pattern and token_header):
        raise RegistryError(
            f"companies.yaml entry {index}: custom.handshake_url needs token_pattern and "
            "token_header"
        )
    if token_pattern:
        try:
            re.compile(token_pattern)
        except re.error as exc:
            raise RegistryError(
                f"companies.yaml entry {index}: custom.token_pattern is not a valid regex: {exc}"
            ) from exc
    page_param = str(raw.get("page_param", "") or "")
    paging = {
        name: _custom_int(raw, name, index)
        for name in ("page_size", "page_start", "page_step", "max_pages")
    }
    if page_param and paging["page_size"] < 1:
        raise RegistryError(
            f"companies.yaml entry {index}: custom.page_param needs a positive custom.page_size"
        )
    return CustomBoard(
        url=url,
        method=method,
        fmt=fmt,
        row_selector=row_selector,
        headers=headers,
        extract=extract,
        handshake_url=handshake_url,
        token_pattern=token_pattern,
        token_header=token_header,
        body=body,
        page_param=page_param,
        page_size=paging["page_size"],
        page_start=paging["page_start"],
        page_step=paging["page_step"],
        max_pages=paging["max_pages"],
        jobs_path=str(raw.get("jobs_path", "") or ""),
        fields={key: value.strip() for key, value in mapping.items()},
        url_template=str(raw.get("url_template", "") or ""),
    )


def _company_from_row(row: dict[str, Any], index: int) -> Company:
    platform_value = _require_str(row, "platform", index)
    try:
        platform = Platform(platform_value)
    except ValueError as exc:
        raise RegistryError(
            f"companies.yaml entry {index}: unknown platform {platform_value!r}"
        ) from exc

    record_value = row.get("source_of_record", SourceOfRecord.MANUAL.value)
    try:
        source_of_record = SourceOfRecord(record_value)
    except ValueError as exc:
        raise RegistryError(
            f"companies.yaml entry {index}: unknown source_of_record {record_value!r}"
        ) from exc

    enabled = _require_bool(row, "enabled", index, default=True)
    name_gate_exempt = _require_bool(row, "name_gate_exempt", index, default=False)
    oracle_host, oracle_site = _oracle_fields(row, index, platform)

    return Company(
        name=_require_str(row, "name", index),
        platform=platform,
        slug=_require_str(row, "slug", index),
        enabled=enabled,
        rate_profile=_optional_str(row, "rate_profile"),
        last_verified=_parse_date(row, "last_verified", index),
        recheck_after=_parse_date(row, "recheck_after", index),
        paused_until=_parse_date(row, "paused_until", index),
        source_of_record=source_of_record,
        workday_tenant=_optional_str(row, "workday_tenant"),
        workday_site=_optional_str(row, "workday_site"),
        workday_dc=_optional_str(row, "workday_dc"),
        workday_facet=_optional_str(row, "workday_facet"),
        oracle_host=oracle_host,
        oracle_site=oracle_site,
        name_gate_exempt=name_gate_exempt,
        notes=_optional_str(row, "notes"),
        custom=_custom_board(row, index, platform),
    )


def board_identity(company: Company) -> tuple[Platform, str, str]:
    if company.platform is Platform.ORACLE_CLOUD:
        target = ":".join(part for part in (company.oracle_host, company.oracle_site) if part)
    else:
        target = company.workday_site or ""
    return (company.platform, company.slug.lower(), target.lower())


def board_label(company: Company) -> str:
    if company.workday_site:
        return f"{company.platform.value}/{company.slug}/{company.workday_site}"
    if company.oracle_host:
        return f"{company.platform.value}/{company.oracle_host}/{company.oracle_site or '?'}"
    return f"{company.platform.value}/{company.slug}"


def _registry_row(company: Company) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": company.name,
        "platform": company.platform.value,
        "slug": company.slug,
        "source_of_record": company.source_of_record.value,
    }
    if company.last_verified is not None:
        row["last_verified"] = company.last_verified
    if company.recheck_after is not None:
        row["recheck_after"] = company.recheck_after
    if company.paused_until is not None:
        row["paused_until"] = company.paused_until
    for key in (
        "workday_tenant",
        "workday_site",
        "workday_dc",
        "workday_facet",
        "oracle_host",
        "oracle_site",
    ):
        value = getattr(company, key)
        if value is not None:
            row[key] = value
    if company.rate_profile is not None:
        row["rate_profile"] = company.rate_profile
    if company.name_gate_exempt:
        row["name_gate_exempt"] = True
    if not company.enabled:
        row["enabled"] = False
    if company.notes:
        row["notes"] = company.notes
    if company.custom is not None:
        block: dict[str, Any] = {"url": company.custom.url}
        if company.custom.posts:
            block["method"] = "POST"
            if company.custom.body:
                block["body"] = dict(company.custom.body)
        if company.custom.headers:
            block["headers"] = dict(company.custom.headers)
        if company.custom.rss or company.custom.html or company.custom.sitemap:
            block["format"] = company.custom.fmt
        if company.custom.row_selector:
            block["row_selector"] = company.custom.row_selector
        if company.custom.extract:
            block["extract"] = company.custom.extract
        if company.custom.handshake_url:
            block["handshake_url"] = company.custom.handshake_url
            block["token_pattern"] = company.custom.token_pattern
            block["token_header"] = company.custom.token_header
        if company.custom.jobs_path:
            block["jobs_path"] = company.custom.jobs_path
        block["fields"] = dict(company.custom.fields)
        if company.custom.url_template:
            block["url_template"] = company.custom.url_template
        if company.custom.page_param:
            block["page_param"] = company.custom.page_param
            block["page_size"] = company.custom.page_size
            if company.custom.page_start:
                block["page_start"] = company.custom.page_start
            if company.custom.page_step:
                block["page_step"] = company.custom.page_step
            if company.custom.max_pages:
                block["max_pages"] = company.custom.max_pages
        row["custom"] = block
    return row


def registry_entry_yaml(company: Company) -> str:
    dumped = yaml.safe_dump(
        [_registry_row(company)], sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    return dumped.rstrip("\n")


def _registry_payload(companies: Sequence[Company]) -> str:
    ordered = sorted(companies, key=lambda item: item.name.lower())
    return yaml.safe_dump(
        [_registry_row(item) for item in ordered],
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )


@contextmanager
def _registry_lock(target: Path) -> Iterator[None]:
    lock_path = target.with_name(f".{target.name}.lock")
    try:
        lock = lock_path.open("a+b")
    except OSError as exc:
        raise RegistryError(f"{target} could not be locked: {exc.strerror or exc}") from exc
    with lock:
        try:
            if os.name == "nt":
                msvcrt = cast(Any, importlib.import_module("msvcrt"))

                lock.seek(0, os.SEEK_END)
                if lock.tell() == 0:
                    lock.write(b"0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            else:
                fcntl = cast(Any, importlib.import_module("fcntl"))
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            raise RegistryError(f"{target} could not be locked: {exc.strerror or exc}") from exc
        yield


def _write_registry(companies: Sequence[Company], target: Path) -> Path:
    payload = _registry_payload(companies)
    staged: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".partial",
            delete=False,
        ) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
            staged = Path(stream.name)
        staged.replace(target)
    except OSError as exc:
        if staged is not None:
            staged.unlink(missing_ok=True)
        raise RegistryError(f"{target} could not be written: {exc.strerror or exc}") from exc
    return target


def write_registry(companies: Sequence[Company], path: Path | None = None) -> Path:
    target = path or registry_path()
    with _registry_lock(target):
        return _write_registry(companies, target)


def load_companies(path: Path | None = None, today: date | None = None) -> tuple[Company, ...]:
    source = path or registry_path()
    moment = today or datetime.now(UTC).date()
    if not source.exists():
        raise RegistryError(f"registry not found at {source}")

    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RegistryError(f"{source} is not valid YAML: {exc}") from exc
    except OSError as exc:
        raise RegistryError(f"{source} could not be read: {exc.strerror or exc}") from exc
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise RegistryError("companies.yaml must contain a list of company entries")

    companies: list[Company] = []
    seen: set[tuple[Platform, str, str]] = set()
    for index, row in enumerate(raw, start=1):
        if not isinstance(row, dict):
            raise RegistryError(f"companies.yaml entry {index} is not a mapping")
        company = _company_from_row(row, index)
        key = board_identity(company)
        if key in seen:
            raise RegistryError(
                f"companies.yaml entry {index}: duplicate board {board_label(company)}"
            )
        seen.add(key)
        companies.append(_resume_if_due(company, moment))
    return tuple(companies)


def _resume_if_due(company: Company, today: date) -> Company:
    if company.paused_until is None or not company.pause_elapsed(today):
        return company
    return replace(company, enabled=True, paused_until=None)


def update_registry[T](
    update: Callable[[tuple[Company, ...]], tuple[Sequence[Company], T]],
    path: Path | None = None,
) -> tuple[Path, T]:
    target = path or registry_path()
    with _registry_lock(target):
        companies, result = update(load_companies(target))
        return _write_registry(companies, target), result
