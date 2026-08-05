from datetime import UTC, datetime
from pathlib import Path

import pytest

from stage.classify import resolve_eligibility, screen_is_cs_role
from stage.domain import (
    DegreeRequirement,
    Job,
    JobFilters,
    QuarantineFilters,
    RejectionReason,
    RoleCategory,
)
from stage.services.sync import normalize_batch
from stage.storage import SourceBatch, open_repository

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def _job(title: str, description: str = "", location: str = "Montreal, QC, Canada") -> Job:
    return Job(
        id=f"greenhouse:acme:{abs(hash((title, description))) % 10**8}",
        source="greenhouse",
        company="Acme",
        title_raw=title,
        title_normalized=title.lower(),
        apply_url_raw="",
        description=description,
        location_raw=location,
        first_seen=NOW,
        last_seen=NOW,
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("PhD required for this position.", DegreeRequirement.PHD),
        ("Doctorate required.", DegreeRequirement.PHD),
        ("Masters required, or equivalent experience.", DegreeRequirement.MASTERS),
        ("Bachelor's degree required.", DegreeRequirement.BACHELORS),
        ("Build backend services in Go.", DegreeRequirement.UNKNOWN),
        ("", DegreeRequirement.UNKNOWN),
    ],
)
def test_degree_requirement_is_read_from_positive_evidence(
    body: str, expected: DegreeRequirement
) -> None:
    verdict = resolve_eligibility(_job("Software Engineer Intern", body))
    assert verdict.degree_requirement is expected


def test_a_phd_requirement_outranks_a_bachelors_mention() -> None:
    verdict = resolve_eligibility(
        _job("Research Intern", "Bachelor's degree required. PhD required for this team.")
    )
    assert verdict.degree_requirement is DegreeRequirement.PHD, (
        "the strictest stated requirement wins, or a bachelors mention masks a doctorate"
    )


def test_an_unstated_degree_is_unknown_and_never_none() -> None:
    verdict = resolve_eligibility(_job("Software Engineer Intern", "Build things."))
    assert verdict.degree_requirement is DegreeRequirement.UNKNOWN, (
        "silence is not a claim that no degree is required"
    )


def test_a_french_degree_requirement_resolves_too() -> None:
    verdict = resolve_eligibility(_job("Stagiaire recherche", "Doctorat requis."))
    assert verdict.degree_requirement is DegreeRequirement.PHD


def test_work_auth_is_set_only_on_positive_exclusion() -> None:
    assert resolve_eligibility(_job("Intern", "Must be a US citizen.")).work_auth_flag
    assert resolve_eligibility(
        _job("Intern", "Active security clearance required.")
    ).work_auth_flag


@pytest.mark.parametrize(
    "body",
    [
        "We will sponsor visas for exceptional candidates.",
        "Sponsorship available.",
        "Open to international students.",
        "",
    ],
)
def test_willingness_to_sponsor_is_not_evidence_of_exclusion(body: str) -> None:
    assert not resolve_eligibility(_job("Intern", body)).work_auth_flag, (
        "will sponsor means welcome, not excluded"
    )


def test_a_clearly_non_cs_role_is_quarantined_with_its_phrase() -> None:
    rejection = screen_is_cs_role(_job("Registered Nurse Intern"))
    assert rejection is not None
    assert rejection.reason is RejectionReason.NOT_A_CS_ROLE
    assert rejection.matched_phrase == "registered nurse"


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineering Intern",
        "Data Science Intern",
        "Stagiaire en genie logiciel",
        "Security Analyst Intern",
        "Summer Intern",
        "Intern",
    ],
)
def test_a_cs_or_unclear_title_is_never_rejected(title: str) -> None:
    assert screen_is_cs_role(_job(title)) is None, (
        "a title with no discipline signal must be kept, never rejected"
    )


def test_a_technical_word_rescues_an_otherwise_non_cs_title() -> None:
    assert screen_is_cs_role(_job("Cashier Systems Software Intern")) is None, (
        "the rescue list runs first, so exclusion fires only on unambiguous evidence"
    )


def test_an_unknown_role_is_never_rejected_for_being_unknown() -> None:
    job = _job("Summer Intern")
    kept, rejected = normalize_batch([job])
    assert kept, "an unresolved role is a gap in understanding, not grounds for rejection"
    assert kept[0].role is RoleCategory.UNKNOWN
    assert not rejected


async def test_eligibility_round_trips_through_storage_and_filters(db_path: Path) -> None:
    doctorate = _job("Research Intern", "PhD required for this role.")
    open_to_all = _job("Software Engineer Intern", "Build things.")
    kept, _ = normalize_batch([doctorate, open_to_all])
    assert len(kept) == 2

    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(source="greenhouse", run_started_at=NOW, jobs=kept)
        )
        stored = {job.id: job for job in await repository.list_jobs(JobFilters())}
        phd_only = await repository.list_jobs(
            JobFilters(degree=DegreeRequirement.PHD)
        )

    assert stored[doctorate.id].degree_requirement is DegreeRequirement.PHD
    assert stored[open_to_all.id].degree_requirement is DegreeRequirement.UNKNOWN
    assert [job.id for job in phd_only] == [doctorate.id]


async def test_a_non_cs_posting_lands_in_quarantine_not_the_jobs_table(
    db_path: Path,
) -> None:
    kept, rejected = normalize_batch(
        [_job("Registered Nurse Intern"), _job("Software Engineer Intern")]
    )
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=NOW,
                jobs=kept,
                quarantined=rejected,
            )
        )
        entries = await repository.list_quarantined(QuarantineFilters())
        counts = await repository.quarantine_reason_counts()

    assert len(kept) == 1
    assert [entry.reason for entry in entries] == [RejectionReason.NOT_A_CS_ROLE]
    assert counts["not-a-cs-role"] == 1
