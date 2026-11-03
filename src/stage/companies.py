import os
import re
import stat
import tempfile
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, override

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
from stage.lexicon import fold, parse_yaml
from stage.paths import boards_path, exclusive, registry_path
from stage.sources.platforms import SlugRejectedError, oracle_target


class RegistryError(Exception):
    pass


def _problem(index: int, message: str) -> RegistryError:
    return RegistryError(f"registry entry {index}: {message}")


def _unknown(index: int, key: str, value: object, kinds: Iterable[StrEnum]) -> RegistryError:
    from difflib import get_close_matches

    from stage.domain.text import did_you_mean

    choices = [kind.value for kind in kinds]
    near = get_close_matches(str(value).strip().lower(), choices, n=1, cutoff=0.6)
    hint = did_you_mean(near[0]) if near else f"Choose from: {', '.join(choices)}."
    return _problem(index, f'unknown {key} "{value}". {hint}')


def _required(row: dict[str, Any], key: str, index: int) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _problem(index, f"{key} must be a non-empty string")
    return value.strip()


def _text(row: dict[str, Any], key: str, index: int) -> str | None:
    value = row.get(key)
    if value is not None and not isinstance(value, str):
        raise _problem(index, f"{key} must be a string")
    return (value or "").strip() or None


def _flag(row: dict[str, Any], key: str, index: int, *, default: bool) -> bool:
    value = row.get(key, default)
    if not isinstance(value, bool):
        raise _problem(index, f'{key} must be an unquoted true or false, not "{value}"')
    return value


def _day(row: dict[str, Any], key: str, index: int) -> date | None:
    value = row.get(key)
    if value is None or isinstance(value, date):
        return value
    if isinstance(value, str):
        with suppress(ValueError):
            return date.fromisoformat(value)
    raise _problem(index, f'{key} must be a date like 2026-08-08, not "{value}"')


def _oracle_fields(
    row: dict[str, Any], index: int, platform: Platform
) -> tuple[str | None, str | None]:
    host = _text(row, "oracle_host", index)
    site = _text(row, "oracle_site", index)
    if platform is not Platform.ORACLE_CLOUD:
        if host is not None or site is not None or row.get("oracle_keyword") is not None:
            raise _problem(index, "Oracle fields only belong on platform oracle_cloud")
        return None, None
    if host is None or site is None:
        raise _problem(index, "platform oracle_cloud needs oracle_host and oracle_site")
    try:
        return oracle_target(host, site)
    except SlugRejectedError as exc:
        raise _problem(index, str(exc)) from exc


def _custom_text(raw: dict[str, Any], key: str, default: str = "") -> str:
    return str(raw.get(key, default) or default)


def _custom_count(raw: dict[str, Any], key: str, index: int) -> int:
    value = raw.get(key, 0)
    if value in (None, ""):
        return 0
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _problem(index, f"custom.{key} must be a whole number of 0 or more")
    return value


def _custom_pattern(raw: dict[str, Any], key: str, index: int) -> str:
    value = _custom_text(raw, key)
    if value:
        try:
            re.compile(value)
        except re.error as exc:
            raise _problem(index, f"custom.{key} is not a valid regex: {exc}") from exc
    return value


def _custom_strings(raw: dict[str, Any], key: str, index: int) -> dict[str, str]:
    mapping = raw.get(key, {})
    if not isinstance(mapping, dict):
        raise _problem(index, f"custom.{key} must be a mapping")
    for name, value in mapping.items():
        if not isinstance(value, str) or not value.strip():
            raise _problem(index, f"custom.{key}[{name!r}] must be a non-empty string")
    return {str(name): value for name, value in mapping.items()}


def _custom_board(row: dict[str, Any], index: int, platform: Platform) -> CustomBoard | None:
    raw = row.get("custom")
    if raw is None:
        if platform is Platform.CUSTOM_JSON:
            raise _problem(
                index,
                "platform custom_json needs a 'custom' block with url and a title field mapping",
            )
        return None
    if platform is not Platform.CUSTOM_JSON:
        raise _problem(index, "a 'custom' block only belongs on platform custom_json")
    if not isinstance(raw, dict):
        raise _problem(index, "'custom' must be a mapping")

    url = public_https_url(str(raw.get("url", "")))
    if url is None:
        raise _problem(index, "custom.url must be a public https address without credentials")
    mapping = _custom_strings(raw, "fields", index)
    unknown = sorted(set(mapping) - set(KNOWN_FIELDS))
    if unknown:
        raise _problem(
            index,
            f"custom.fields has unknown {'key' if len(unknown) == 1 else 'keys'} "
            f"{', '.join(unknown)}. Known keys: {', '.join(KNOWN_FIELDS)}",
        )
    missing = [name for name in REQUIRED_FIELDS if name not in mapping]
    if missing:
        raise _problem(index, f"custom.fields must map {', '.join(missing)}")
    method = _custom_text(raw, "method", "GET").upper()
    if method not in ("GET", "POST"):
        raise _problem(index, "custom.method must be GET or POST")
    body = raw.get("body", {})
    if not isinstance(body, dict):
        raise _problem(index, "custom.body must be a mapping")
    fmt = _custom_text(raw, "format", "json").lower()
    if fmt not in ("json", "rss", "html", "sitemap", "jsonld"):
        raise _problem(index, "custom.format must be json, rss, html, sitemap or jsonld")
    row_selector = _custom_text(raw, "row_selector")
    if fmt == "html" and not row_selector:
        raise _problem(index, "custom.format html needs custom.row_selector")
    handshake_url = ""
    if raw.get("handshake_url"):
        handshake_url = public_https_url(str(raw["handshake_url"])) or ""
        if not handshake_url:
            raise _problem(index, "custom.handshake_url must be a public https address")
    token_pattern = _custom_pattern(raw, "token_pattern", index)
    token_header = _custom_text(raw, "token_header")
    if handshake_url and not (token_pattern and token_header):
        raise _problem(index, "custom.handshake_url needs token_pattern and token_header")
    item_tag = _custom_text(raw, "item_tag", "item")
    if not re.fullmatch(r"[A-Za-z_][\w.:-]{0,63}", item_tag):
        raise _problem(index, "custom.item_tag must be an xml element name")
    page_param = _custom_text(raw, "page_param")
    paging = {
        name: _custom_count(raw, name, index)
        for name in ("page_size", "page_start", "page_step", "max_pages")
    }
    if page_param and paging["page_size"] < 1:
        raise _problem(index, "custom.page_param needs a positive custom.page_size")
    return CustomBoard(
        url=url,
        method=method,
        fmt=fmt,
        row_selector=row_selector,
        row_filter=_custom_pattern(raw, "row_filter", index),
        empty_marker=_custom_pattern(raw, "empty_marker", index),
        item_tag=item_tag,
        headers=_custom_strings(raw, "headers", index),
        extract=_custom_text(raw, "extract"),
        handshake_url=handshake_url,
        token_pattern=token_pattern,
        token_header=token_header,
        token_prefix=_custom_text(raw, "token_prefix"),
        body=body,
        authoritative=_flag(raw, "authoritative", index, default=True),
        page_param=page_param,
        jobs_path=_custom_text(raw, "jobs_path"),
        fields={key: value.strip() for key, value in mapping.items()},
        url_template=_custom_text(raw, "url_template"),
        **paging,
    )


def _company_from_row(row: dict[str, Any], index: int) -> Company:
    platform_value = _required(row, "platform", index)
    try:
        platform = Platform(platform_value)
    except ValueError as exc:
        raise _unknown(index, "platform", platform_value, Platform) from exc
    record_value = row.get("source_of_record", SourceOfRecord.MANUAL.value)
    try:
        source_of_record = SourceOfRecord(record_value)
    except ValueError as exc:
        raise _unknown(index, "source_of_record", record_value, SourceOfRecord) from exc
    oracle_host, oracle_site = _oracle_fields(row, index, platform)
    keyword = row.get("oracle_keyword")
    if keyword is not None and not isinstance(keyword, str):
        raise _problem(index, "oracle_keyword must be a string")
    return Company(
        name=_required(row, "name", index),
        platform=platform,
        slug=_required(row, "slug", index),
        enabled=_flag(row, "enabled", index, default=True),
        rate_profile=_text(row, "rate_profile", index),
        last_verified=_day(row, "last_verified", index),
        recheck_after=_day(row, "recheck_after", index),
        paused_until=_day(row, "paused_until", index),
        source_of_record=source_of_record,
        workday_tenant=_text(row, "workday_tenant", index),
        workday_site=_text(row, "workday_site", index),
        workday_dc=_text(row, "workday_dc", index),
        workday_facet=_text(row, "workday_facet", index),
        oracle_host=oracle_host,
        oracle_site=oracle_site,
        oracle_keyword=None if keyword is None else keyword.strip(),
        name_gate_exempt=_flag(row, "name_gate_exempt", index, default=False),
        notes=_text(row, "notes", index),
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


def _present(source: object, *keys: str) -> dict[str, Any]:
    return {key: value for key in keys if (value := getattr(source, key)) is not None}


def _filled(source: object, *keys: str) -> dict[str, Any]:
    return {key: value for key in keys if (value := getattr(source, key))}


def _registry_row(company: Company) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": company.name,
        "platform": company.platform.value,
        "slug": company.slug,
        "source_of_record": company.source_of_record.value,
        **_present(
            company,
            "last_verified",
            "recheck_after",
            "paused_until",
            "workday_tenant",
            "workday_site",
            "workday_dc",
            "workday_facet",
            "oracle_host",
            "oracle_site",
            "oracle_keyword",
            "rate_profile",
        ),
    }
    if company.name_gate_exempt:
        row["name_gate_exempt"] = True
    if not company.enabled:
        row["enabled"] = False
    row |= _filled(company, "notes")
    if company.custom is not None:
        row["custom"] = _custom_row(company.custom)
    return row


def _custom_row(custom: CustomBoard) -> dict[str, Any]:
    block: dict[str, Any] = {"url": custom.url}
    if custom.posts:
        block["method"] = "POST"
        if custom.body:
            block["body"] = dict(custom.body)
    if custom.headers:
        block["headers"] = dict(custom.headers)
    if custom.fmt != "json":
        block["format"] = custom.fmt
    if not custom.authoritative:
        block["authoritative"] = False
    block |= _filled(custom, "row_selector", "row_filter", "empty_marker")
    if custom.item_tag != "item":
        block["item_tag"] = custom.item_tag
    block |= _filled(custom, "extract")
    if custom.handshake_url:
        block |= _present(custom, "handshake_url", "token_pattern", "token_header")
        block |= _filled(custom, "token_prefix")
    block |= _filled(custom, "jobs_path")
    block["fields"] = dict(custom.fields)
    block |= _filled(custom, "url_template")
    if custom.page_param:
        block |= _present(custom, "page_param", "page_size")
        block |= _filled(custom, "page_start", "page_step", "max_pages")
    return block


def registry_entry_yaml(company: Company) -> str:
    dumped = yaml.safe_dump(
        [_registry_row(company)], sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    return dumped.rstrip("\n")


def _registry_payload(companies: Sequence[Company], *, quick: bool = False) -> str:
    ordered = sorted(companies, key=lambda item: item.name.lower())
    return yaml.dump(
        [_registry_row(item) for item in ordered],
        Dumper=_QuickNoAnchors if quick else _NoAnchors,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )


class _NoAnchors(yaml.SafeDumper):
    @override
    def ignore_aliases(self, data: Any) -> bool:
        return True


class _QuickNoAnchors(getattr(yaml, "CSafeDumper", yaml.SafeDumper)):  # type: ignore[misc]
    @override
    def ignore_aliases(self, data: Any) -> bool:
        return True


def _is_shard_dir(target: Path) -> bool:
    return target.suffix.lower() not in {".yaml", ".yml"}


def _shard_name(name: str) -> str:
    first = fold(name)[:1]
    return first if "a" <= first <= "z" else "other"


def _registry_files(source: Path) -> list[Path]:
    if source.is_dir():
        return sorted(path for path in source.glob("*.yaml") if not path.name.startswith("."))
    return [source] if source.exists() else []


def _rows_in(source: Path) -> list[Any]:
    try:
        raw = parse_yaml(source.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise RegistryError(f"{source} is not UTF-8 text. Save it as UTF-8") from exc
    except yaml.YAMLError as exc:
        raise RegistryError(f"{source} is not valid YAML: {exc}") from exc
    except OSError as exc:
        raise RegistryError(f"{source} could not be read: {exc.strerror or exc}") from exc
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RegistryError(f"{source.name} must contain a list of company entries")
    return raw


@contextmanager
def _registry_lock(target: Path) -> Iterator[None]:
    if _is_shard_dir(target):
        target.mkdir(parents=True, exist_ok=True)
        lock_path = target / ".registry.lock"
    else:
        lock_path = target.with_name(f".{target.name}.lock")
    with ExitStack() as stack:
        try:
            stack.enter_context(exclusive(lock_path))
        except OSError as exc:
            raise RegistryError(f"{target} could not be locked: {exc.strerror or exc}") from exc
        yield


def _write_registry(companies: Sequence[Company], target: Path) -> Path:
    if not _is_shard_dir(target):
        return _write_file(_registry_payload(companies), target)
    target.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[Company]] = {}
    for company in companies:
        grouped.setdefault(_shard_name(company.name), []).append(company)
    for shard, rows in sorted(grouped.items()):
        destination = target / f"{shard}.yaml"
        if not _unchanged(destination, rows):
            _write_file(_registry_payload(rows), destination)
    for stale in target.glob("*.yaml"):
        if stale.stem not in grouped:
            stale.unlink()
    return target


def _unchanged(destination: Path, companies: Sequence[Company]) -> bool:
    if not destination.exists():
        return False
    try:
        stored = _rows_in(destination)
    except RegistryError:
        return False
    ordered = sorted(stored, key=lambda row: str(row.get("name", "")).lower())
    quick = _registry_payload(companies, quick=True)
    return bool(ordered == parse_yaml(quick))


def _write_file(payload: str, target: Path, mode: int | None = None) -> Path:
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
            staged = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is None:
            mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
        staged.chmod(mode)
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
    files = _registry_files(source)
    if not files:
        raise RegistryError(f"registry not found at {source}. Check --registry or STAGE_REGISTRY")

    companies: list[Company] = []
    seen: set[tuple[Platform, str, str]] = set()
    for file in files:
        for index, row in enumerate(_rows_in(file), start=1):
            if not isinstance(row, dict):
                raise RegistryError(f"{file.name}: entry {index} is not a mapping of fields")
            try:
                company = _company_from_row(row, index)
            except RegistryError as exc:
                raise RegistryError(f"{file.name}: {exc}") from exc
            key = board_identity(company)
            if key in seen:
                raise RegistryError(
                    f"{file.name}: registry entry {index} is a duplicate board, "
                    f"{board_label(company)} is listed twice. Remove one of them"
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


PERSONAL_LIMIT = 500


@dataclass(frozen=True, slots=True)
class PersonalBoards:
    path: Path
    rows: tuple[Company, ...] = ()
    problem: str = ""


def _personal_rows(source: Path, today: date | None = None) -> tuple[Company, ...]:
    if source.is_dir():
        raise RegistryError(f"{source} is a folder. Your board list must be one YAML file")
    if not source.exists():
        return ()
    return load_companies(source, today)


def load_personal(path: Path | None = None, today: date | None = None) -> PersonalBoards:
    source = path or boards_path()
    try:
        return PersonalBoards(path=source, rows=_personal_rows(source, today))
    except RegistryError as exc:
        return PersonalBoards(path=source, problem=str(exc))


def personal_kept(packaged: Sequence[Company], personal: Sequence[Company]) -> tuple[Company, ...]:
    boards = {board_identity(row) for row in packaged}
    keys = {row.registry_key.lower() for row in packaged}
    return tuple(
        row
        for row in personal
        if board_identity(row) not in boards and row.registry_key.lower() not in keys
    )


def switched_off(packaged: Sequence[Company], personal: Sequence[Company]) -> tuple[Company, ...]:
    off = {board_identity(row) for row in personal if not row.enabled}
    return tuple(
        replace(row, enabled=False) if row.enabled and board_identity(row) in off else row
        for row in packaged
    )


def load_boards(path: Path | None = None) -> tuple[tuple[Company, ...], PersonalBoards]:
    packaged = load_companies(path)
    personal = load_personal()
    merged = switched_off(packaged, personal.rows)
    return (*merged, *personal_kept(packaged, personal.rows)), personal


def update_personal[T](
    update: Callable[[tuple[Company, ...]], tuple[Sequence[Company], T]],
    path: Path | None = None,
) -> tuple[Path, T]:
    target = path or boards_path()
    if target.is_symlink():
        target = target.resolve()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RegistryError(f"{target.parent} could not be created: {exc.strerror or exc}") from exc
    with _registry_lock(target):
        current = _personal_rows(target)
        rows, result = update(current)
        if tuple(rows) != current:
            if target.exists() and not os.access(target, os.W_OK):
                raise RegistryError(
                    f"{target} is read-only, so Stage left it as it was. Make it writable, "
                    "then try again"
                )
            _write_file(_registry_payload(rows), target, mode=0o600)
        return target, result
