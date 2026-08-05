from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from stage.domain import Company, Platform, Priority, SourceOfRecord
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


def _parse_date(row: dict[str, Any], key: str) -> date | None:
    value = row.get(key)
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise RegistryError(f"companies.yaml: field {key!r} must be a date")


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

    enabled = row.get("enabled", True)
    if not isinstance(enabled, bool):
        raise RegistryError(f"companies.yaml entry {index}: field 'enabled' must be a boolean")

    return Company(
        name=_require_str(row, "name", index),
        platform=platform,
        slug=_require_str(row, "slug", index),
        priority=priority,
        enabled=enabled,
        rate_profile=_optional_str(row, "rate_profile"),
        last_verified=_parse_date(row, "last_verified"),
        source_of_record=source_of_record,
        workday_tenant=_optional_str(row, "workday_tenant"),
        workday_site=_optional_str(row, "workday_site"),
        workday_dc=_optional_str(row, "workday_dc"),
        workday_facet=_optional_str(row, "workday_facet"),
        name_gate_exempt=bool(row.get("name_gate_exempt", False)),
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
    target.write_text(
        yaml.safe_dump(
            [_registry_row(item) for item in ordered],
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        ),
        encoding="utf-8",
    )
    return target


def load_companies(path: Path | None = None) -> tuple[Company, ...]:
    source = path or registry_path()
    if not source.exists():
        raise RegistryError(f"registry not found at {source}")

    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
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
