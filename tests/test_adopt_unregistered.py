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


def test_a_nameless_board_with_postings_becomes_a_review_candidate() -> None:
    results = [
        ("Perplexity AI", _result(Platform.ASHBY, "perplexity", 99, ProbeVerdict.UNVERIFIED))
    ]
    report = adopt_unregistered([], results, today=TODAY)
    assert not report.adopted, "a board that cannot name itself is never adopted automatically"
    assert [entry.company for entry in report.review] == ["Perplexity AI"], (
        "an Ashby or Lever board answering with postings must reach a human, not vanish"
    )
    assert report.review[0].job_count == 99


def test_a_nameless_board_with_no_postings_is_not_a_review_candidate() -> None:
    results = [("Ghost", _result(Platform.LEVER, "ghostboard", 0, ProbeVerdict.UNVERIFIED))]
    assert not adopt_unregistered([], results, today=TODAY).review, (
        "an empty board is §5.3's token that does not exist, not a decision for a human"
    )


def test_review_candidates_carry_whether_the_slug_is_distinctive() -> None:
    results = [
        ("Tenstorrent", _result(Platform.ASHBY, "tenstorrent", 12, ProbeVerdict.UNVERIFIED)),
        ("Apple", _result(Platform.BAMBOOHR, "apple", 4, ProbeVerdict.UNVERIFIED)),
    ]
    marks = {
        entry.company: entry.distinctive
        for entry in adopt_unregistered([], results, today=TODAY).review
    }
    assert marks == {"Tenstorrent": True, "Apple": False}, (
        "the generic-slug collision is the risk a human is being asked to rule on"
    )


def test_a_review_candidate_already_in_the_registry_is_not_re_offered() -> None:
    existing = [Company(name="Perplexity AI", platform=Platform.ASHBY, slug="perplexity")]
    results = [
        ("Perplexity AI", _result(Platform.ASHBY, "perplexity", 99, ProbeVerdict.UNVERIFIED))
    ]
    assert not adopt_unregistered(existing, results, today=TODAY).review


def test_one_nameless_board_under_two_captions_is_reviewed_once() -> None:
    results = [
        ("Astera Labs", _result(Platform.LEVER, "asteralabs", 164, ProbeVerdict.UNVERIFIED)),
        ("Astera Labs Inc", _result(Platform.LEVER, "asteralabs", 164, ProbeVerdict.UNVERIFIED)),
    ]
    assert len(adopt_unregistered([], results, today=TODAY).review) == 1


def test_a_nameless_board_is_adopted_only_when_provenance_is_asserted() -> None:
    results = [
        ("Perplexity AI", _result(Platform.ASHBY, "perplexity", 99, ProbeVerdict.UNVERIFIED))
    ]
    report = adopt_unregistered([], results, today=TODAY, adopt_unnamed=True)
    assert [row.company.name for row in report.adopted] == ["Perplexity AI"]
    assert not report.review, "an adopted board is no longer a pending decision"


def test_an_adopted_nameless_row_records_that_the_name_gate_could_not_confirm_it() -> None:
    results = [("Alan", _result(Platform.ASHBY, "alan", 90, ProbeVerdict.UNVERIFIED))]
    row = adopt_unregistered([], results, today=TODAY, adopt_unnamed=True).adopted[0].company
    assert row.name_gate_exempt, "a later --verify sweep must not disable it for lacking a name"
    assert "provenance is the evidence" in (row.notes or "")
    assert "apply URL" in (row.notes or ""), "the reader needs the evidence, not a verdict"


def test_provenance_adoption_still_refuses_a_board_with_no_postings() -> None:
    results = [("Ghost", _result(Platform.LEVER, "ghostboard", 0, ProbeVerdict.UNVERIFIED))]
    report = adopt_unregistered([], results, today=TODAY, adopt_unnamed=True)
    assert not report.adopted, "an empty board is not evidence of anything, whatever the provenance"


def test_provenance_adoption_never_rescues_a_name_gate_rejection() -> None:
    results = [("Coveo", _result(Platform.GREENHOUSE, "someoneelse", 40, ProbeVerdict.REJECTED))]
    report = adopt_unregistered([], results, today=TODAY, adopt_unnamed=True)
    assert not report.adopted, "a board that named a different company is still a rejection"
