import argparse
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from pathlib import Path

from stage.dedup.identity import employer_name
from stage.domain.visits import refused
from stage.lexicon import fold
from stage.normalize.location import display_location

OPEN_MAINS = "FROM jobs WHERE status = 'open' AND duplicate_of IS NULL"
IN_SCOPE = "location IN ('usa', 'canada', 'montreal', 'unknown')"
OUTSIDE_CS = re.compile(
    r"\b(accounting|actuarial|audit|chemical|civil|clinical|communications|construction|"
    r"environmental|facilities|finance|financial|hr|legal|manufacturing|marketing|mechanical|"
    r"nurse|optical|photonic|procurement|recruiting|regulatory|renewable|sales|supply chain|"
    r"tax|traffic|transportation)\b"
)
TECHNICAL = re.compile(
    r"\b(ai|algorithm|algorithms|cloud|computer|computing|cyber|cybersecurity|data|database|"
    r"developer|devops|embedded|firmware|machine learning|ml|mobile|network|platform|"
    r"programming|quantum|robotics|simulation|software|web)\b"
)
RAW_PLACE = re.compile(r"[0-9~]|\bLocations?\b|\(")
MORE_PLACES = re.compile(r"\s\+[0-9]+$")
FAMILY_EDGE = re.compile(r"^(intern|internship|co|op|summer|winter|fall|spring|20\d\d|student)\b")


def _connect(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve().as_posix()}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def _section(title: str, lines: Iterable[str], limit: int) -> Iterator[str]:
    rows = list(lines)
    yield f"\n## {title} ({len(rows)})"
    yield from (f"  {row}" for row in rows[:limit])
    if len(rows) > limit:
        yield f"  ... {len(rows) - limit} more"


def _kept_outside_cs(db: sqlite3.Connection) -> Iterator[str]:
    for company, title, role in db.execute(f"SELECT company, title_raw, role {OPEN_MAINS}"):
        if found := OUTSIDE_CS.search(fold(title)):
            yield f"{found.group(1):14} {role:10} {company[:24]:24} | {title[:80]}"


def _rejected_technical(db: sqlite3.Connection, since: str) -> Iterator[str]:
    families: Counter[str] = Counter()
    examples: dict[str, str] = {}
    rows = db.execute(
        "SELECT DISTINCT company, title_raw FROM quarantine WHERE reason = 'unknown-cs-role' "
        f"AND last_seen >= ? AND {IN_SCOPE}",
        (since,),
    )
    for company, title in rows:
        words = fold(title).split()
        if OUTSIDE_CS.search(" ".join(words)):
            continue
        for start in range(len(words) - 1):
            pair = " ".join(words[start : start + 2])
            if TECHNICAL.search(pair) and not FAMILY_EDGE.match(pair):
                families[pair] += 1
                examples.setdefault(pair, f"{company[:20]}: {title[:60]}")
    for pair, count in families.most_common():
        yield f"{count:4} {pair:28} e.g. {examples[pair]}"


def _raw_places(db: sqlite3.Connection) -> Iterator[str]:
    for company, place in db.execute(f"SELECT company, location_raw {OPEN_MAINS}"):
        shown = display_location(place or "")
        if not shown or RAW_PLACE.search(MORE_PLACES.sub("", shown)):
            yield f"{shown[:44]:44} <- {(place or '')[:44]:44} | {company[:24]}"


def _missing_terms(db: sqlite3.Connection) -> Iterator[str]:
    counts = Counter(term for (term,) in db.execute(f"SELECT term {OPEN_MAINS}"))
    total = sum(counts.values())
    for term, count in counts.most_common():
        yield f"{term:14} {count:5}  {count / total:.0%}"


def _words(title: str) -> frozenset[str]:
    return frozenset(word for word in fold(title).split() if not FAMILY_EDGE.match(word))


def _close_repeats(db: sqlite3.Connection) -> Iterator[str]:
    groups: dict[tuple[str, str], list[tuple[str, str, frozenset[str]]]] = defaultdict(list)
    for source, company, title, place in db.execute(
        f"SELECT source, company, title_raw, location_raw {OPEN_MAINS}"
    ):
        key = (employer_name(company), fold(display_location(place or "")))
        groups[key].append((source, title, _words(title)))
    for (employer, place), rows in sorted(groups.items()):
        for index, (source, title, words) in enumerate(rows):
            for other_source, other_title, other_words in rows[index + 1 :]:
                shared = len(words & other_words) / max(len(words | other_words), 1)
                if source != other_source and words != other_words and shared >= 0.75:
                    yield f"{employer[:20]:20} {place[:16]:16} | {title[:44]} ~ {other_title[:44]}"


def _failing_boards(db: sqlite3.Connection) -> Iterator[str]:
    rows = db.execute(
        "SELECT source, board, label, consecutive_failures, last_error FROM source_visits "
        "WHERE consecutive_failures >= 3 AND last_attempt_at >= date('now', '-7 days') "
        "ORDER BY consecutive_failures DESC"
    )
    for source, board, label, failures, error in rows:
        kind = "refuses" if refused(error) else "fails"
        yield f"{failures:3} {kind:7} {source:12} {(label or board)[:30]:30} | {error[:60]}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Report what a copied Stage database has wrong.")
    parser.add_argument("database", type=Path)
    parser.add_argument("--since", default="", help="rejections seen on or after this date")
    parser.add_argument("--limit", type=int, default=40)
    options = parser.parse_args()
    db = _connect(options.database)
    sections = (
        ("Kept postings with a non-CS word", _kept_outside_cs(db)),
        ("Rejected technical titles, by phrase", _rejected_technical(db, options.since)),
        ("Places shown raw or blank", _raw_places(db)),
        ("Terms", _missing_terms(db)),
        ("Close titles not joined", _close_repeats(db)),
        ("Boards failing 3 times or more", _failing_boards(db)),
    )
    for title, lines in sections:
        for line in _section(title, lines, options.limit):
            print(line)


if __name__ == "__main__":
    main()
