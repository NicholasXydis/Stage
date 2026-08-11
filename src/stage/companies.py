from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from stage.domain import (
    KNOWN_FIELDS,
    REQUIRED_FIELDS,
    Company,
    CustomBoard,
    Platform,
    Priority,
    SourceOfRecord,
    web_url,
)
from stage.paths import registry_path


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

    url = web_url(str(raw.get("url", "")))
    if url is None:
        raise RegistryError(
            f"companies.yaml entry {index}: custom.url must be a plain http or https address"
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
    return CustomBoard(
        url=url,
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

    priority_value = row.get("priority", Priority.NORMAL.value)
    try:
        priority = Priority(priority_value)
    except ValueError as exc:
        raise RegistryError(
            f"companies.yaml entry {index}: unknown priority {priority_value!r}"
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

    return Company(
        name=_require_str(row, "name", index),
        platform=platform,
        slug=_require_str(row, "slug", index),
        priority=priority,
        enabled=enabled,
        rate_profile=_optional_str(row, "rate_profile"),
        last_verified=_parse_date(row, "last_verified", index),
        recheck_after=_parse_date(row, "recheck_after", index),
        source_of_record=source_of_record,
        workday_tenant=_optional_str(row, "workday_tenant"),
        workday_site=_optional_str(row, "workday_site"),
        workday_dc=_optional_str(row, "workday_dc"),
        workday_facet=_optional_str(row, "workday_facet"),
        name_gate_exempt=name_gate_exempt,
        notes=_optional_str(row, "notes"),
        custom=_custom_board(row, index, platform),
    )


def board_identity(company: Company) -> tuple[Platform, str, str]:
    return (company.platform, company.slug.lower(), (company.workday_site or "").lower())


def board_label(company: Company) -> str:
    if company.workday_site:
        return f"{company.platform.value}/{company.slug}/{company.workday_site}"
    return f"{company.platform.value}/{company.slug}"


def _registry_row(company: Company) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": company.name,
        "platform": company.platform.value,
        "slug": company.slug,
        "priority": company.priority.value,
        "source_of_record": company.source_of_record.value,
    }
    if company.last_verified is not None:
        row["last_verified"] = company.last_verified
    if company.recheck_after is not None:
        row["recheck_after"] = company.recheck_after
    for key in ("workday_tenant", "workday_site", "workday_dc", "workday_facet"):
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
        if company.custom.jobs_path:
            block["jobs_path"] = company.custom.jobs_path
        block["fields"] = dict(company.custom.fields)
        if company.custom.url_template:
            block["url_template"] = company.custom.url_template
        row["custom"] = block
    return row


def registry_entry_yaml(company: Company) -> str:
    dumped = yaml.safe_dump(
        [_registry_row(company)], sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    return dumped.rstrip("\n")


def write_registry(companies: Sequence[Company], path: Path | None = None) -> Path:
    target = path or registry_path()
    order = {Priority.HIGH: 0, Priority.NORMAL: 1, Priority.LOW: 2}
    ordered = sorted(companies, key=lambda item: (order[item.priority], item.name.lower()))
    payload = yaml.safe_dump(
        [_registry_row(item) for item in ordered],
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    staged = target.with_name(f"{target.name}.partial")
    try:
        staged.write_text(payload, encoding="utf-8")
        staged.replace(target)
    except OSError as exc:
        staged.unlink(missing_ok=True)
        raise RegistryError(f"{target} could not be written: {exc.strerror or exc}") from exc
    return target


def load_companies(path: Path | None = None) -> tuple[Company, ...]:
    source = path or registry_path()
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
        companies.append(company)
    return tuple(companies)
