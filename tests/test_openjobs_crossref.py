import json
from pathlib import Path

from stage.bootstrap.openjobs import (
    SEED_PATH,
    DatasetEntry,
    Seed,
    _same_family,
    crossref,
    is_division_of,
    load_dataset,
    load_seeds,
    match_key,
    mine_country,
    to_registry_rows,
)
from stage.domain import Platform, Priority


def test_the_shipped_seed_list_loads_and_prioritises_montreal() -> None:
    seeds = load_seeds(SEED_PATH)
    assert len(seeds) > 150
    montreal = [seed for seed in seeds if seed.section == "montreal"]
    assert montreal
    assert all(seed.priority is Priority.HIGH for seed in montreal)
    assert any(seed.name == "Eidos-Montréal" for seed in seeds)


def test_match_key_folds_accents_and_drops_legal_suffixes() -> None:
    assert match_key("Hydro-Québec") == "hydroquebec"
    assert match_key("Genetec Inc.") == "genetec"
    assert match_key("Coveo Solutions Inc") == "coveosolutions"


def test_crossref_resolves_through_the_shared_url_table() -> None:
    seeds = (Seed(name="Faire", section="canada", priority=Priority.HIGH),)
    entries = (
        DatasetEntry(
            name="Faire",
            ats_links=("https://boards.greenhouse.io/faire",),
            countries=("Canada",),
        ),
    )
    report = crossref(seeds, entries)
    assert len(report.resolved) == 1
    assert report.resolved[0].candidate.platform is Platform.GREENHOUSE
    assert report.platform_histogram() == [(Platform.GREENHOUSE, 1)]


def test_one_board_claimed_by_several_seeds_is_excluded_as_a_collision() -> None:
    seeds = (Seed(name="Alpha Studios"), Seed(name="Beta Studios"))
    entries = (
        DatasetEntry(name="Alpha Studios", ats_links=("https://jobs.lever.co/bestudios",)),
        DatasetEntry(name="Beta Studios", ats_links=("https://jobs.lever.co/bestudios",)),
    )
    report = crossref(seeds, entries)
    assert report.resolved == []
    assert report.collisions == {"lever/bestudios": ["Alpha Studios", "Beta Studios"]}


def test_a_seed_with_no_ats_link_routes_to_discover_rather_than_being_dropped() -> None:
    seeds = (Seed(name="Mila"),)
    entries = (DatasetEntry(name="Mila", website="https://mila.quebec"),)
    report = crossref(seeds, entries)
    assert [seed.name for seed in report.no_ats_link] == ["Mila"]
    assert report.resolved == []


def test_an_unrecognized_ats_link_is_reported_not_guessed() -> None:
    seeds = (Seed(name="Acme"),)
    entries = (DatasetEntry(name="Acme", ats_links=("https://acme.com/careers",)),)
    report = crossref(seeds, entries)
    assert report.unrecognized == [(seeds[0], "https://acme.com/careers")]


def test_emitted_rows_are_marked_openjobs_and_carry_the_seed_priority() -> None:
    seeds = (Seed(name="Coveo", section="montreal", priority=Priority.HIGH),)
    entries = (DatasetEntry(name="Coveo", ats_links=("https://jobs.ashbyhq.com/coveo",)),)
    rows = to_registry_rows(crossref(seeds, entries))
    assert "source_of_record: openjobs" in rows
    assert "priority: high" in rows
    assert "platform: ashby" in rows


def test_dataset_loading_tolerates_a_wrapped_list_and_junk_rows(tmp_path: Path) -> None:
    path = tmp_path / "companies_v2.json"
    path.write_text(
        json.dumps(
            {
                "companies": [
                    {"name": "Good", "ats_links": ["https://boards.greenhouse.io/good"]},
                    {"name": "  "},
                    "not an object",
                    {"website": "https://no-name.example"},
                ]
            }
        ),
        encoding="utf-8",
    )
    entries = load_dataset(path)
    assert [entry.name for entry in entries] == ["Good"]


def test_every_ats_link_becomes_a_board_not_just_the_first() -> None:
    seeds = (Seed(name="Acme"),)
    entries = (
        DatasetEntry(
            name="Acme",
            ats_links=(
                "https://boards.greenhouse.io/acme",
                "https://jobs.lever.co/acme-labs",
            ),
        ),
    )
    report = crossref(seeds, entries)
    assert sorted(item.candidate.label for item in report.resolved) == [
        "greenhouse/acme",
        "lever/acme-labs",
    ]


def test_a_division_with_its_own_board_is_resolved_under_its_parent_seed() -> None:
    seeds = (Seed(name="Citadel", priority=Priority.NORMAL),)
    entries = (
        DatasetEntry(name="Citadel", ats_links=("https://boards.greenhouse.io/citadel",)),
        DatasetEntry(
            name="Citadel Securities",
            ats_links=("https://boards.greenhouse.io/citadelsecurities",),
        ),
    )
    report = crossref(seeds, entries)
    labels = sorted(item.candidate.label for item in report.resolved)
    assert labels == ["greenhouse/citadel", "greenhouse/citadelsecurities"]
    division = next(item for item in report.resolved if item.related)
    assert division.display_name == "Citadel Securities"


def test_a_name_collision_is_not_a_division() -> None:
    seeds = (Seed(name="Meta"),)
    entries = (
        DatasetEntry(name="Meta Theory", ats_links=("https://jobs.lever.co/metatheory",)),
    )
    report = crossref(seeds, entries)
    assert report.resolved == []
    assert [name for _, name in report.name_collisions] == ["Meta Theory"]


def test_division_detection_needs_qualifier_tokens() -> None:
    assert is_division_of("RBC", "RBC Capital Markets")
    assert is_division_of("Motorola", "Motorola Solutions")
    assert is_division_of("Samsung", "Samsung Semiconductor")
    assert not is_division_of("Meta", "Meta Theory")
    assert not is_division_of("Bell", "Bellwether")
    assert not is_division_of("Apple", "Apple Arts Studios Motion Capture Services")


def test_two_separately_seeded_employers_are_never_one_family() -> None:
    independent = frozenset({match_key("Citadel"), match_key("Citadel Securities")})
    assert not is_division_of("Citadel", "Citadel Securities", independent)
    assert not _same_family(["Citadel", "Citadel Securities"], independent)
    assert is_division_of("Motorola", "Motorola Solutions", independent)


def test_both_citadels_survive_the_join_as_separate_rows() -> None:
    seeds = (Seed(name="Citadel"), Seed(name="Citadel Securities"))
    entries = (
        DatasetEntry(name="Citadel", ats_links=("https://boards.greenhouse.io/citadel",)),
        DatasetEntry(
            name="Citadel Securities",
            ats_links=("https://boards.greenhouse.io/citadelsecurities",),
        ),
    )
    report = crossref(seeds, entries)
    assert sorted(item.display_name for item in report.resolved) == [
        "Citadel",
        "Citadel Securities",
    ]
    assert not any(item.related for item in report.resolved)


def test_a_shared_board_between_two_seeds_is_a_collision_not_a_merge() -> None:
    seeds = (Seed(name="Citadel"), Seed(name="Citadel Securities"))
    entries = (
        DatasetEntry(name="Citadel", ats_links=("https://boards.greenhouse.io/citadel",)),
        DatasetEntry(
            name="Citadel Securities", ats_links=("https://boards.greenhouse.io/citadel",)
        ),
    )
    report = crossref(seeds, entries)
    assert report.resolved == []
    assert "greenhouse/citadel" in report.collisions


def test_a_company_with_several_ats_links_lands_inert_pending_verification() -> None:
    seeds = (Seed(name="Acme"),)
    entries = (
        DatasetEntry(
            name="Acme",
            ats_links=("https://boards.greenhouse.io/acme", "https://jobs.lever.co/acme"),
        ),
    )
    rows = to_registry_rows(crossref(seeds, entries))
    assert rows.count("enabled: false") == 2


def test_country_mining_finds_companies_the_seed_list_never_named() -> None:
    entries = (
        DatasetEntry(
            name="Coveo", ats_links=("https://jobs.ashbyhq.com/coveo",), countries=("Canada",)
        ),
        DatasetEntry(
            name="Unlisted Montreal Studio",
            ats_links=("https://boards.greenhouse.io/unlistedmtl",),
            countries=("Canada",),
        ),
        DatasetEntry(
            name="Elsewhere", ats_links=("https://jobs.lever.co/elsewhere",), countries=("France",)
        ),
    )
    found = mine_country(entries, "Canada", frozenset({"ashby/coveo"}))
    assert [entry.name for entry, _ in found] == ["Unlisted Montreal Studio"]
