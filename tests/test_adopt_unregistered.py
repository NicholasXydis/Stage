from datetime import date

import pytest

from stage.domain import Company, Platform, PlatformCandidate, ProbeResult, ProbeVerdict
from stage.services.discover import (
    adopt_unregistered,
    adoption_refusal,
    slug_is_distinctive,
)

TODAY = date(2026, 8, 11)


def _result(
    platform: Platform, slug: str, jobs: int, verdict: ProbeVerdict = ProbeVerdict.MATCH
) -> ProbeResult:
    return ProbeResult(
        company="ignored",
        candidate=PlatformCandidate(platform=platform, slug=slug),
        verdict=verdict,
        url=f"https://example.test/{slug}",
        job_count=jobs,
    )


DISTINCTIVE = [
    ("Jump Trading", "jumptrading"),
    ("Western Digital", "westerndigital"),
    ("Astera Labs", "asteralabs"),
    ("Tenstorrent", "tenstorrent"),
]

GENERIC = [
    ("Apple", "apple"),
    ("Base Power", "base"),
    ("Jump Trading", "jump"),
    ("Magna", "magna"),
]


@pytest.mark.parametrize(("caption", "slug"), DISTINCTIVE)
def test_a_distinctive_slug_is_evidence(caption: str, slug: str) -> None:
    assert slug_is_distinctive(caption, slug)


@pytest.mark.parametrize(("caption", "slug"), GENERIC)
def test_a_short_or_generic_slug_is_never_evidence(caption: str, slug: str) -> None:
    assert not slug_is_distinctive(caption, slug), (
        f"{slug!r} is a common word, so a 200 with jobs may be somebody else's board"
    )


def test_only_a_self_naming_board_is_adopted() -> None:
    for verdict in (ProbeVerdict.UNVERIFIED, ProbeVerdict.EMPTY, ProbeVerdict.MISS):
        refusal = adoption_refusal(_result(Platform.GREENHOUSE, "tenstorrent", 9, verdict))
        assert "only a self-naming board" in refusal


def test_a_board_with_no_postings_is_refused() -> None:
    assert "no postings" in adoption_refusal(_result(Platform.GREENHOUSE, "tenstorrent", 0))


def test_the_apple_case_is_refused_because_bamboohr_names_no_board() -> None:
    refusal = adoption_refusal(_result(Platform.BAMBOOHR, "apple", 4, ProbeVerdict.UNVERIFIED))
    assert "only a self-naming board" in refusal, "a 200 with jobs is not evidence of the company"


def test_a_short_slug_is_adopted_when_the_board_names_itself() -> None:
    for slug in ("imc", "virtu", "appian", "atoms", "cresta"):
        assert not adoption_refusal(_result(Platform.GREENHOUSE, slug, 40)), (
            f"{slug!r} is short but the MATCH verdict already means the board named itself"
        )


def test_adoption_skips_rows_already_present_by_key_or_caption() -> None:
    existing = [
        Company(name="Tenstorrent", platform=Platform.GREENHOUSE, slug="tenstorrent"),
        Company(name="Other", platform=Platform.LEVER, slug="appian"),
    ]
    results = [
        ("Tenstorrent", _result(Platform.GREENHOUSE, "tenstorrent", 129)),
        ("Appian", _result(Platform.LEVER, "appian", 190)),
        ("Astera Labs", _result(Platform.GREENHOUSE, "asteralabs", 164)),
    ]
    report = adopt_unregistered(existing, results, today=TODAY)
    assert [row.company.name for row in report.adopted] == ["Astera Labs"]
    assert report.already_known == 2
    assert report.postings == 164


def test_an_adopted_row_is_enabled_verified_and_carries_its_provenance() -> None:
    results = [("Astera Labs", _result(Platform.GREENHOUSE, "asteralabs", 164))]
    row = adopt_unregistered([], results, today=TODAY).adopted[0].company
    assert row.enabled and row.last_verified == TODAY
    assert row.source_of_record.value == "discover"
    assert "164 job(s)" in (row.notes or ""), "the evidence must survive into the registry"


def test_two_captions_for_one_board_adopt_once() -> None:
    results = [
        ("Astera Labs", _result(Platform.GREENHOUSE, "asteralabs", 164)),
        ("Astera Labs Inc", _result(Platform.GREENHOUSE, "asteralabs", 164)),
    ]
    report = adopt_unregistered([], results, today=TODAY)
    assert len(report.adopted) == 1, "one board must never become two registry rows"


def test_a_refusal_records_why_so_the_next_pass_does_not_repeat_it() -> None:
    results = [("Ghost", _result(Platform.GREENHOUSE, "ghostboard", 0))]
    report = adopt_unregistered([], results, today=TODAY)
    assert not report.adopted
    assert report.refused and "no postings" in report.refused[0][2]
