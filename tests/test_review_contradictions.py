from datetime import UTC, datetime, timedelta

from stage.domain import Company, CoverageClassification, CoverageDisposition, Platform
from stage.services.coverage import REVIEW_STALE_DAYS, contradicted_reviews

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def _record(
    company: str, disposition: CoverageDisposition, age_days: int = 0
) -> CoverageClassification:
    return CoverageClassification(
        company=company,
        disposition=disposition,
        note="researched",
        checked_on=NOW - timedelta(days=age_days),
    )


def test_a_feed_only_verdict_is_contradicted_once_the_board_is_polled() -> None:
    records = [_record("Acme", CoverageDisposition.FEED_ONLY)]
    rows = [Company(name="Acme", platform=Platform.GREENHOUSE, slug="acme")]
    found = contradicted_reviews(records, rows, now=NOW)
    assert [record.company for record, _ in found] == ["Acme"]
    assert "greenhouse" in found[0][1], "the reason must name what now contradicts the verdict"


def test_an_accented_or_cased_name_still_matches_its_registry_row() -> None:
    records = [_record("hydro-québec", CoverageDisposition.FEED_ONLY)]
    rows = [Company(name="Hydro-Québec", platform=Platform.CUSTOM_JSON, slug="hq")]
    assert contradicted_reviews(records, rows, now=NOW), (
        "the match must fold, or every accented Quebec employer hides its own contradiction"
    )


def test_a_disabled_row_is_reported_differently_from_a_live_one() -> None:
    records = [_record("Acme", CoverageDisposition.UNAVAILABLE)]
    rows = [Company(name="Acme", platform=Platform.WORKDAY, slug="acme", enabled=False)]
    found = contradicted_reviews(records, rows, now=NOW)
    assert found and "disabled" in found[0][1], (
        "a disabled row is weaker evidence than a live one and must read differently"
    )


def test_a_fresh_verdict_with_no_registry_row_is_left_alone() -> None:
    records = [_record("Nowhere", CoverageDisposition.UNAVAILABLE, age_days=1)]
    assert contradicted_reviews(records, (), now=NOW) == (), (
        "a recent verdict about an employer we do not poll is still current"
    )


def test_a_verdict_that_nothing_has_re_derived_ages_out() -> None:
    fresh = _record("Nowhere", CoverageDisposition.FEED_ONLY, age_days=REVIEW_STALE_DAYS - 1)
    stale = _record("Elsewhere", CoverageDisposition.FEED_ONLY, age_days=REVIEW_STALE_DAYS)
    found = contradicted_reviews([fresh, stale], (), now=NOW)
    assert [record.company for record, _ in found] == ["Elsewhere"]
    assert "days old" in found[0][1]


def test_contradictions_read_in_a_stable_order() -> None:
    records = [
        _record("Zeta", CoverageDisposition.FEED_ONLY, age_days=90),
        _record("alpha", CoverageDisposition.FEED_ONLY, age_days=90),
    ]
    found = contradicted_reviews(records, (), now=NOW)
    assert [record.company for record, _ in found] == ["alpha", "Zeta"]
