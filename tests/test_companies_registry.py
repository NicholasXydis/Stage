from pathlib import Path

import pytest

from stage.companies import RegistryError, board_identity, load_companies
from stage.domain import Company, Platform


def test_sibling_workday_sites_on_one_tenant_are_distinct_boards() -> None:
    external = Company(
        name="RBC", platform=Platform.WORKDAY, slug="rbc",
        workday_tenant="rbc", workday_site="External", workday_dc="wd3",
    )
    capital_markets = Company(
        name="RBC Capital Markets", platform=Platform.WORKDAY, slug="rbc",
        workday_tenant="rbc", workday_site="CapitalMarkets", workday_dc="wd3",
    )
    assert board_identity(external) != board_identity(capital_markets)


def test_the_registry_accepts_sibling_boards_for_one_employer(tmp_path: Path) -> None:
    path = tmp_path / "companies.yaml"
    path.write_text(
        "- name: RBC\n"
        "  platform: workday\n"
        "  slug: rbc\n"
        "  workday_tenant: rbc\n"
        "  workday_site: External\n"
        "  workday_dc: wd3\n"
        "- name: RBC Capital Markets\n"
        "  platform: workday\n"
        "  slug: rbc\n"
        "  workday_tenant: rbc\n"
        "  workday_site: CapitalMarkets\n"
        "  workday_dc: wd3\n",
        encoding="utf-8",
    )
    assert len(load_companies(path)) == 2


def test_the_same_board_twice_is_still_refused(tmp_path: Path) -> None:
    path = tmp_path / "companies.yaml"
    path.write_text(
        "- name: RBC\n  platform: workday\n  slug: rbc\n  workday_site: External\n"
        "- name: RBC Again\n  platform: workday\n  slug: rbc\n  workday_site: External\n",
        encoding="utf-8",
    )
    with pytest.raises(RegistryError, match="duplicate board"):
        load_companies(path)


def test_the_shipped_registry_loads_and_every_enabled_row_is_verified() -> None:
    companies = load_companies()
    assert len(companies) > 70
    for company in companies:
        if company.enabled:
            assert company.last_verified is not None, company.name
