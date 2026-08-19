import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from stage.domain import Platform, PlatformCandidate
from stage.lexicon import company_legal_suffixes, division_qualifiers, fold
from stage.sources.platforms import identify_url

SEED_PATH = Path(__file__).resolve().parents[1] / "data" / "seed_companies.yaml"


@dataclass(frozen=True, slots=True)
class Seed:
    name: str
    section: str = "other"
    note: str | None = None


@dataclass(frozen=True, slots=True)
class DatasetEntry:
    name: str
    website: str = ""
    ats_links: tuple[str, ...] = ()
    countries: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Resolution:
    seed: Seed
    entry: DatasetEntry
    candidate: PlatformCandidate
    display_name: str
    related: bool = False


@dataclass(slots=True)
class CrossReference:
    resolved: list[Resolution] = field(default_factory=list)
    no_ats_link: list[Seed] = field(default_factory=list)
    unmatched: list[Seed] = field(default_factory=list)
    unrecognized: list[tuple[Seed, str]] = field(default_factory=list)
    collisions: dict[str, list[str]] = field(default_factory=dict)
    name_collisions: list[tuple[Seed, str]] = field(default_factory=list)

    def platform_histogram(self) -> list[tuple[Platform, int]]:
        counted = Counter(item.candidate.platform for item in self.resolved)
        return sorted(counted.items(), key=lambda pair: (-pair[1], pair[0].value))


def name_tokens(name: str) -> tuple[str, ...]:
    suffixes = company_legal_suffixes()
    tokens = [token for token in fold(name).split() if token]
    while len(tokens) > 1 and tokens[-1] in suffixes:
        tokens = tokens[:-1]
    return tuple(tokens)


def match_key(name: str) -> str:
    return "".join(name_tokens(name))


def recognized_boards(entry: DatasetEntry) -> tuple[PlatformCandidate, ...]:
    found: list[PlatformCandidate] = []
    seen: set[str] = set()
    for link in entry.ats_links:
        candidate = identify_url(link)
        if candidate is None or candidate.label in seen:
            continue
        seen.add(candidate.label)
        found.append(candidate)
    return tuple(found)


def _prefix_index(entries: Sequence[DatasetEntry]) -> dict[tuple[str, ...], list[DatasetEntry]]:
    index: defaultdict[tuple[str, ...], list[DatasetEntry]] = defaultdict(list)
    for entry in entries:
        tokens = name_tokens(entry.name)
        for size in range(1, len(tokens)):
            index[tokens[:size]].append(entry)
    return index


def is_division_of(
    parent: str, candidate_name: str, independent: frozenset[str] = frozenset()
) -> bool:
    if match_key(candidate_name) in independent:
        return False
    root = name_tokens(parent)
    tokens = name_tokens(candidate_name)
    if len(tokens) <= len(root) or tokens[: len(root)] != root:
        return False
    qualifiers = division_qualifiers()
    return all(token in qualifiers for token in tokens[len(root) :])


def _same_family(names: Sequence[str], independent: frozenset[str] = frozenset()) -> bool:
    if sum(1 for name in names if match_key(name) in independent) > 1:
        return False
    shortest = min(names, key=len)
    root = name_tokens(shortest)
    return all(name_tokens(name)[: len(root)] == root for name in names)


def load_seeds(path: Path = SEED_PATH) -> tuple[Seed, ...]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path} must contain a list of seed entries")
    seeds: list[Seed] = []
    for row in raw:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise ValueError(f"{path}: every entry needs a string 'name'")
        note = row.get("note")
        seeds.append(
            Seed(
                name=row["name"],
                section=str(row.get("section", "other")),
                note=str(note) if note is not None else None,
            )
        )
    return tuple(seeds)


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str))
    if isinstance(value, dict):
        return tuple(item for item in value.values() if isinstance(item, str))
    return ()


def load_dataset(path: Path) -> tuple[DatasetEntry, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows: Iterable[Any]
    if isinstance(payload, dict):
        rows = next((value for value in payload.values() if isinstance(value, list)), [])
    elif isinstance(payload, list):
        rows = payload
    else:
        raise ValueError(f"{path}: expected a JSON list or an object wrapping one")

    entries: list[DatasetEntry] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        website = row.get("website")
        entries.append(
            DatasetEntry(
                name=name.strip(),
                website=website if isinstance(website, str) else "",
                ats_links=_strings(row.get("ats_links")),
                countries=_strings(row.get("countries")),
            )
        )
    return tuple(entries)


def crossref(seeds: Sequence[Seed], entries: Sequence[DatasetEntry]) -> CrossReference:
    exact: dict[str, DatasetEntry] = {}
    for entry in entries:
        exact.setdefault(match_key(entry.name), entry)
    related_index = _prefix_index(entries)
    independent = frozenset(match_key(seed.name) for seed in seeds)

    report = CrossReference()
    claims: defaultdict[str, list[str]] = defaultdict(list)
    pending: list[Resolution] = []

    for seed in seeds:
        primary = exact.get(match_key(seed.name))
        family: list[tuple[DatasetEntry, bool]] = []
        if primary is not None:
            family.append((primary, False))
        for entry in related_index.get(name_tokens(seed.name), ()):
            if primary is not None and match_key(entry.name) == match_key(primary.name):
                continue
            if not is_division_of(seed.name, entry.name, independent):
                report.name_collisions.append((seed, entry.name))
                continue
            family.append((entry, True))

        if not family:
            report.unmatched.append(seed)
            continue

        for entry, related in family:
            if not entry.ats_links:
                if not related:
                    report.no_ats_link.append(seed)
                continue
            candidates = recognized_boards(entry)
            if not candidates:
                if not related:
                    report.unrecognized.append((seed, entry.ats_links[0]))
                continue
            for candidate in candidates:
                claims[candidate.label].append(entry.name)
                pending.append(
                    Resolution(
                        seed=seed,
                        entry=entry,
                        candidate=candidate,
                        display_name=entry.name,
                        related=related,
                    )
                )

    contested: set[str] = set()
    for label, names in claims.items():
        unique = sorted(set(names))
        if len(unique) <= 1 or _same_family(unique, independent):
            continue
        report.collisions[label] = unique
        contested.add(label)

    kept: dict[str, Resolution] = {}
    for item in pending:
        if item.candidate.label in contested:
            continue
        current = kept.get(item.candidate.label)
        if current is None or (current.related and not item.related):
            kept[item.candidate.label] = item
    report.resolved = list(kept.values())
    return report


def to_registry_rows(report: CrossReference) -> str:
    from stage.companies import registry_entry_yaml
    from stage.domain import Company, SourceOfRecord
    from stage.services.discover import is_routable

    boards_per_entry = Counter(item.entry.name for item in report.resolved)
    blocks = []
    for item in sorted(report.resolved, key=lambda entry: entry.display_name.lower()):
        routable = is_routable(item.candidate.platform)
        siblings = boards_per_entry[item.entry.name]
        contested = siblings > 1
        routable = routable and not contested
        blocks.append(
            registry_entry_yaml(
                Company(
                    name=item.display_name,
                    platform=item.candidate.platform,
                    slug=item.candidate.slug,
                    enabled=routable,
                    source_of_record=SourceOfRecord.OPENJOBS,
                    workday_tenant=item.candidate.workday_tenant,
                    workday_site=item.candidate.workday_site,
                    workday_dc=item.candidate.workday_dc,
                    oracle_host=item.candidate.oracle_host,
                    oracle_site=item.candidate.oracle_site,
                )
            )
        )
    return "\n".join(blocks) + ("\n" if blocks else "")


def mine_country(
    entries: Sequence[DatasetEntry], country: str, known: frozenset[str] = frozenset()
) -> list[tuple[DatasetEntry, PlatformCandidate]]:
    wanted = fold(country)
    found: list[tuple[DatasetEntry, PlatformCandidate]] = []
    seen: set[str] = set()
    for entry in entries:
        if not any(fold(value) == wanted for value in entry.countries):
            continue
        boards = recognized_boards(entry)
        if len(boards) != 1:
            continue
        candidate = boards[0]
        if candidate.label in known or candidate.label in seen:
            continue
        seen.add(candidate.label)
        found.append((entry, candidate))
    return sorted(found, key=lambda pair: pair[0].name.lower())


def mined_registry_rows(found: Sequence[tuple[DatasetEntry, PlatformCandidate]]) -> str:
    from stage.companies import registry_entry_yaml
    from stage.domain import Company, SourceOfRecord
    from stage.services.discover import is_routable

    blocks = []
    for entry, candidate in found:
        routable = is_routable(candidate.platform)
        blocks.append(
            registry_entry_yaml(
                Company(
                    name=entry.name,
                    platform=candidate.platform,
                    slug=candidate.slug,
                    enabled=routable,
                    source_of_record=SourceOfRecord.OPENJOBS,
                    workday_tenant=candidate.workday_tenant,
                    workday_site=candidate.workday_site,
                    workday_dc=candidate.workday_dc,
                    oracle_host=candidate.oracle_host,
                    oracle_site=candidate.oracle_site,
                )
            )
        )
    return "\n".join(blocks) + ("\n" if blocks else "")


def format_report(report: CrossReference, total_seeds: int) -> str:
    divisions = [item for item in report.resolved if item.related]
    lines = [
        f"seeds: {total_seeds}",
        f"resolved to a platform: {len(report.resolved)} board(s)",
        f"  of which division boards: {len(divisions)}",
        f"matched but no ats_links: {len(report.no_ats_link)}  -> stage discover",
        f"ats_links present but unrecognized: {len(report.unrecognized)}  -> custom_json / feeds",
        f"absent from the dataset: {len(report.unmatched)}  -> stage discover",
        "",
        "platform          hits",
    ]
    lines.extend(f"{platform.value:<17} {count}" for platform, count in report.platform_histogram())
    if report.collisions:
        lines.append("")
        lines.append("collisions (one board claimed by several seeds — excluded, review by hand):")
        lines.extend(
            f"  {label}: {', '.join(names)}" for label, names in sorted(report.collisions.items())
        )
    if divisions:
        lines.append("")
        lines.append("division boards found:")
        lines.extend(
            f"  {item.display_name} ({item.candidate.label}) under {item.seed.name}"
            for item in sorted(divisions, key=lambda entry: entry.display_name.lower())
        )
    if report.name_collisions:
        lines.append("")
        lines.append("rejected as name collisions, not divisions:")
        lines.extend(
            f"  {name} (looks like {seed.name} but the extra tokens are not qualifiers)"
            for seed, name in sorted(report.name_collisions, key=lambda pair: pair[1].lower())
        )
    if report.unmatched:
        lines.append("")
        lines.append("route to `stage discover --url`:")
        lines.extend(f"  {seed.name}" for seed in report.unmatched)
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cross-reference seeds against the OpenJobs dataset",
    )
    parser.add_argument("--dataset", type=Path, required=True, help="Local companies_v2.json")
    parser.add_argument("--seeds", type=Path, default=SEED_PATH)
    parser.add_argument("--emit-yaml", type=Path, help="Emit candidate rows here")
    parser.add_argument(
        "--mine-country",
        help="Also mine this country",
    )
    args = parser.parse_args(argv)

    seeds = load_seeds(args.seeds)
    entries = load_dataset(args.dataset)
    report = crossref(seeds, entries)

    mined: list[tuple[DatasetEntry, PlatformCandidate]] = []
    if args.mine_country:
        from stage.companies import RegistryError, board_label, load_companies

        known = {item.candidate.label for item in report.resolved}
        try:
            known |= {board_label(company) for company in load_companies()}
        except RegistryError as exc:
            sys.stdout.write(f"\nregistry unreadable, mining without it: {exc}\n")
        mined = mine_country(entries, args.mine_country, frozenset(known))
        sys.stdout.write(
            f"\nmined {len(mined)} new {args.mine_country} board(s) not already known\n"
        )

    sys.stdout.write(format_report(report, len(seeds)) + "\n")
    if args.emit_yaml is not None:
        args.emit_yaml.write_text(
            to_registry_rows(report) + mined_registry_rows(mined), encoding="utf-8"
        )
        sys.stdout.write(
            f"\nwrote {len(report.resolved) + len(mined)} candidate row(s) to {args.emit_yaml}\n"
            "Review before merging into data/companies.yaml — ats_links go stale.\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
