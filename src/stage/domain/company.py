from dataclasses import dataclass
from datetime import date

from stage.domain.custom_board import CustomBoard
from stage.domain.enums import Platform, Priority, SourceOfRecord


@dataclass(frozen=True, slots=True)
class Company:
    name: str
    platform: Platform
    slug: str
    priority: Priority = Priority.NORMAL
    enabled: bool = True
    rate_profile: str | None = None
    last_verified: date | None = None
    source_of_record: SourceOfRecord = SourceOfRecord.MANUAL
    workday_tenant: str | None = None
    workday_site: str | None = None
    workday_dc: str | None = None
    workday_facet: str | None = None
    name_gate_exempt: bool = False
    notes: str | None = None
    recheck_after: date | None = None
    custom: CustomBoard | None = None

    def due_for_recheck(self, today: date) -> bool:
        return self.recheck_after is not None and self.recheck_after <= today

    @property
    def registry_key(self) -> str:
        parts = [self.platform.value, self.slug]
        parts.extend(part for part in (self.workday_site, self.workday_dc) if part)
        return ":".join(parts)
