from datetime import UTC, datetime
from pathlib import Path

from stage.domain import QuarantinedJob, QuarantineFilters, RejectionReason
from stage.services.maintenance import rescreen
from stage.storage import open_repository
from stage.storage.repository import SourceBatch

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _entry(identifier: str, title: str, reason: RejectionReason, phrase: str) -> QuarantinedJob:
    return QuarantinedJob(
        id=identifier,
        source="greenhouse",
        company="Acme",
        title_raw=title,
        reason=reason,
        first_seen=NOW,
        last_seen=NOW,
        apply_url_raw=f"https://jobs.example.test/{identifier}",
        location_raw="Montreal, QC",
        matched_phrase=phrase,
    )


async def _store(repository: object, entries: list[QuarantinedJob]) -> None:
    await repository.apply_source_batch(  # type: ignore[attr-defined]
        SourceBatch(source="greenhouse", run_started_at=NOW, quarantined=tuple(entries))
    )


async def _reasons(repository: object) -> dict[str, tuple[str, str]]:
    rows = await repository.list_quarantined(QuarantineFilters(limit=50))  # type: ignore[attr-defined]
    return {row.id: (row.reason.value, row.matched_phrase) for row in rows}


async def test_a_rejection_gains_the_reason_the_lexicon_now_names(db_path: Path) -> None:
    stale = _entry(
        "a",
        "Marketeer Intern",
        RejectionReason.UNKNOWN_CS_ROLE,
        "no matching CS role or source category",
    )
    async with open_repository(db_path) as repository:
        await _store(repository, [stale])
        result = await rescreen(repository, now=NOW)

        assert result.relabelled == 1, "the sharper reason was not written back"
        reason, phrase = (await _reasons(repository))["a"]
        assert reason == RejectionReason.NOT_A_CS_ROLE.value
        assert phrase == "marketeer", "the evidence must name the rule that fired"


async def test_a_rejection_the_lexicon_still_cannot_explain_is_left_alone(db_path: Path) -> None:
    unchanged = _entry(
        "b",
        "Zzz Widget Wrangler Intern",
        RejectionReason.UNKNOWN_CS_ROLE,
        "no matching CS role or source category",
    )
    async with open_repository(db_path) as repository:
        await _store(repository, [unchanged])
        result = await rescreen(repository, now=NOW)

        assert result.relabelled == 0, "an unchanged verdict must not be rewritten"
        assert (await _reasons(repository))["b"][0] == RejectionReason.UNKNOWN_CS_ROLE.value


async def test_a_posting_that_now_passes_is_restored_rather_than_relabelled(
    db_path: Path,
) -> None:
    passing = _entry(
        "c",
        "Software Engineer Intern",
        RejectionReason.UNKNOWN_CS_ROLE,
        "no matching CS role or source category",
    )
    async with open_repository(db_path) as repository:
        await _store(repository, [passing])
        result = await rescreen(repository, now=NOW)

        assert result.released == 1, "a posting that now passes belongs back in the database"
        assert result.relabelled == 0, "a released posting must not also be relabelled"
        assert "c" not in await _reasons(repository), "quarantine is a move, not a copy"
