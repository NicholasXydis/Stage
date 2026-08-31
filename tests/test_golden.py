import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from stage.domain import Job, SourceSignals
from stage.services.sync import normalize_batch

GOLDEN = Path(__file__).parent / "golden" / "classification.json"
WHEN = datetime(2026, 8, 3, tzinfo=UTC)


def _cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]
    return cases


def _job(case: dict[str, Any]) -> Job:
    spec = case["input"]
    signals = spec.get("signals") or {}
    return Job(
        id=case["id"],
        source=spec["source"],
        company=spec["company"],
        title_raw=spec["title_raw"],
        title_normalized=spec["title_raw"],
        apply_url_raw=f"https://example.test/{case['id']}",
        description="",
        location_raw=spec["location_raw"],
        first_seen=WHEN,
        last_seen=WHEN,
        signals=SourceSignals(
            terms=tuple(signals.get("terms", ())),
            season=signals.get("season", ""),
            category=signals.get("category", ""),
        ),
    )


@pytest.mark.parametrize("case", _cases(), ids=lambda case: str(case["id"]))
def test_golden(case: dict[str, Any]) -> None:
    kept, rejected = normalize_batch([_job(case)])
    expected = case["expected"]

    if not expected["kept"]:
        assert not kept, f"{case['id']} should have been rejected"
        entry = rejected[0]
        assert entry.reason.value == expected["reason"]
        assert entry.matched_phrase == expected["matched_phrase"]
        assert entry.location.value == expected["location"]
        return

    assert not rejected, f"{case['id']} should have been kept"
    job = kept[0]
    actual = {
        "location": job.location.value,
        "remote_scope": job.remote_scope.value if job.remote_scope else None,
        "term": job.term,
        "role": job.role.value,
        "language": job.language.value,
    }
    assert actual == {key: expected[key] for key in actual}


RULE_CATEGORIES = frozenset({"swe", "security", "data", "ml-ai", "quant", "infra", "embedded"})


def test_the_goldens_cover_every_rule_category_in_both_languages() -> None:
    cases = _cases()
    kept = [case["expected"] for case in cases if case["expected"]["kept"]]

    languages = {entry["language"] for entry in kept}
    assert {"en", "fr", "bilingual"} <= languages

    for language in ("en", "fr"):
        covered = {entry["role"] for entry in kept if entry["language"] == language}
        assert covered >= RULE_CATEGORIES, (
            f"{sorted(RULE_CATEGORIES - covered)} have no {language} golden"
        )

    reasons = {case["expected"]["reason"] for case in cases if not case["expected"]["kept"]}
    assert {
        "not-an-internship",
        "out-of-scope-location",
        "unknown-cs-role",
    } == reasons, "only reachable reasons appear in the golden set"

    buckets = {entry["location"] for entry in kept}
    assert {"montreal", "canada", "usa"} <= buckets


def test_french_and_english_reach_the_same_role() -> None:
    by_id = {case["id"]: case["expected"] for case in _cases()}
    for english, french in [
        ("swe-en", "swe-fr"),
        ("data-en", "data-fr"),
        ("security-en", "security-fr"),
        ("mlai-en", "mlai-fr"),
        ("embedded-en", "embedded-fr"),
        ("quant-en", "quant-fr"),
        ("infra-en", "infra-fr"),
        ("firmware-en", "firmware-fr"),
    ]:
        assert by_id[english]["role"] == by_id[french]["role"], (english, french)
