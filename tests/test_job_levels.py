import json
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stage.cli.app import app
from stage.domain import (
    Job,
    JobFilters,
    JobLevel,
    LocationBucket,
    QuarantineFilters,
    RejectionReason,
    RoleCategory,
)
from stage.domain.filters import DEFAULT_LEVELS, EVERY_LEVEL, with_default_levels
from stage.domain.preferences import PreferenceError, Preferences, parse_levels
from stage.services.preferences import load_preferences, preferences_path, save_preferences

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
NOW = datetime.now(UTC)


def _plain(text: str) -> str:
    return " ".join(ANSI.sub("", text).split())


def _job(name: str, level: JobLevel, hours: int = 1) -> Job:
    seen = NOW - timedelta(hours=hours)
    return Job(
        id=f"greenhouse:acme:{name}",
        source="greenhouse",
        company="Acme",
        title_raw=f"Engineer {name}",
        title_normalized=f"engineer {name}",
        apply_url_raw=f"https://example.com/{name}",
        description="",
        first_seen=seen,
        last_seen=seen,
        level=level,
    )


def _store(path: Path) -> Path:
    from stage.storage.repository import SourceBatch
    from stage.storage.sqlite_repo import SqliteRepository

    jobs = (
        _job("intern", JobLevel.INTERNSHIP, hours=1),
        _job("grad", JobLevel.NEW_GRAD, hours=2),
        _job("entry", JobLevel.ENTRY, hours=3),
    )
    repository = SqliteRepository.connect(path)
    try:
        repository.apply_source_batch(
            SourceBatch(source="greenhouse", run_started_at=NOW, jobs=jobs)
        )
    finally:
        repository.close()
    return path


def _listed(path: Path, *flags: str) -> list[str]:
    result = CliRunner().invoke(app, ["list", "--json", "--db", str(path), *flags])
    assert result.exit_code == 0, result.output
    return [job["id"].rsplit(":", 1)[1] for job in json.loads(result.stdout)]


def test_levels_are_spelled_like_the_other_values_and_start_unknown() -> None:
    assert [level.value for level in JobLevel] == ["internship", "new-grad", "entry", "unknown"]
    unscreened = Job(
        id="greenhouse:acme:1",
        source="greenhouse",
        company="Acme",
        title_raw="Intern",
        title_normalized="intern",
        apply_url_raw="https://example.com/1",
        description="",
        first_seen=NOW,
        last_seen=NOW,
    )
    assert unscreened.level is JobLevel.UNKNOWN
    assert RejectionReason.NOT_EARLY_CAREER.value == "not-early-career"
    assert RejectionReason.NOT_AN_INTERNSHIP.value == "not-an-internship"


def test_a_listing_shows_internships_unless_a_level_is_typed(db_path: Path) -> None:
    _store(db_path)

    assert _listed(db_path) == ["intern"]
    assert _listed(db_path, "--level", "new-grad") == ["grad"]
    assert _listed(db_path, "--level", "ENTRY") == ["entry"]
    assert _listed(db_path, "--level", "all") == ["intern", "grad", "entry"]


@pytest.mark.parametrize(
    ("flags", "said"),
    [
        (["--level", "senior"], 'Unknown level "senior"'),
        (["--level", "unknown"], 'Unknown level "unknown"'),
        (["--level", ""], "--level needs a value"),
        (["--level", "entry", "--level", "all"], "--level takes one value"),
    ],
)
def test_a_level_stage_cannot_filter_on_is_refused(
    db_path: Path, flags: list[str], said: str
) -> None:
    _store(db_path)

    result = CliRunner().invoke(app, ["list", "--db", str(db_path), *flags])

    assert result.exit_code == 2
    assert said in _plain(result.stderr)
    if said.startswith("Unknown"):
        assert "internship, new-grad, entry, all" in _plain(result.stderr)


def test_saved_levels_apply_under_mine_and_a_typed_level_replaces_them(db_path: Path) -> None:
    _store(db_path)
    save_preferences(Preferences(levels=(JobLevel.NEW_GRAD, JobLevel.ENTRY)))

    assert _listed(db_path, "--mine") == ["grad", "entry"]
    assert _listed(db_path, "--mine", "--level", "internship") == ["intern"]
    assert _listed(db_path, "--mine", "--level", "all") == ["intern", "grad", "entry"]
    assert _listed(db_path) == ["intern"]


def test_mine_keeps_internships_only_when_no_level_is_saved(db_path: Path) -> None:
    _store(db_path)
    save_preferences(Preferences(roles=(RoleCategory.SWE,)))

    assert _listed(db_path, "--mine") == ["intern"]


def test_preferences_choose_levels_the_way_they_choose_roles() -> None:
    chosen = Preferences(levels=(JobLevel.NEW_GRAD,))

    assert Preferences().apply(JobFilters()).levels == DEFAULT_LEVELS
    assert chosen.apply(JobFilters()).levels == {JobLevel.NEW_GRAD}
    typed = chosen.apply(JobFilters(level=JobLevel.ENTRY))
    assert (typed.level, typed.levels) == (JobLevel.ENTRY, frozenset())
    assert chosen.apply(JobFilters(levels=EVERY_LEVEL)).levels == EVERY_LEVEL
    assert chosen.narrows and not chosen.is_empty
    assert chosen.describe(chosen.apply(JobFilters())) == "Mine: new-grad"
    assert with_default_levels(JobFilters()).levels == DEFAULT_LEVELS
    assert with_default_levels(JobFilters(level=JobLevel.ENTRY)).levels == frozenset()


def test_levels_parse_like_roles_and_refuse_what_cannot_be_chosen() -> None:
    assert parse_levels(["new-grad, ENTRY", "entry"]) == (JobLevel.NEW_GRAD, JobLevel.ENTRY)
    assert parse_levels([""]) == ()
    for value in ("unknown", "senior"):
        with pytest.raises(PreferenceError, match=f'Unknown level "{value}"'):
            parse_levels([value])


def test_preferences_saved_before_levels_existed_still_load() -> None:
    path = preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"format": 1, "locations": ["usa"], "roles": ["swe"], "terms": []}),
        encoding="utf-8",
    )

    loaded = load_preferences()

    assert loaded.problems == () and not loaded.damaged
    assert loaded.preferences.levels == ()
    assert loaded.preferences.locations == (LocationBucket.USA,)


def test_saved_levels_round_trip_and_a_bad_one_is_ignored_with_a_warning() -> None:
    saved = Preferences(levels=(JobLevel.ENTRY, JobLevel.NEW_GRAD))
    path = save_preferences(saved)

    assert json.loads(path.read_text(encoding="utf-8"))["levels"] == ["entry", "new-grad"]
    assert load_preferences().preferences == saved

    path.write_text(json.dumps({"format": 1, "levels": ["senior"]}), encoding="utf-8")
    loaded = load_preferences()
    assert loaded.preferences.levels == ()
    assert any("levels was ignored" in problem for problem in loaded.problems)


def test_prefs_saves_and_clears_levels(db_path: Path) -> None:
    runner = CliRunner()

    saved = runner.invoke(app, ["prefs", "--level", "new-grad,entry", "--json"])
    shown = runner.invoke(app, ["prefs", "--show"])
    cleared = runner.invoke(app, ["prefs", "--level", "", "--json"])
    refused = runner.invoke(app, ["prefs", "--level", "senior"])

    assert saved.exit_code == shown.exit_code == cleared.exit_code == 0, saved.output
    assert json.loads(saved.stdout)["levels"] == ["new-grad", "entry"]
    assert "Levels new-grad, entry" in _plain(shown.stdout)
    assert json.loads(cleared.stdout)["levels"] == []
    assert refused.exit_code == 2 and 'Unknown level "senior"' in _plain(refused.stderr)
    assert load_preferences().preferences.levels == ()


def test_a_database_from_before_levels_upgrades_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.storage import migrations
    from stage.storage.sql import to_text
    from stage.storage.sqlite_repo import SqliteRepository

    target = tmp_path / "stage.db"
    shipped = migrations.discover()
    assert [migration.name for migration in shipped][:2] == ["0001_initial", "0002_job_level"]
    monkeypatch.setattr(migrations, "discover", lambda: shipped[:1])
    SqliteRepository.connect(target).close()
    with closing(sqlite3.connect(target)) as conn, conn:
        assert "level" not in {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        assert "employment_type" not in {
            row[1] for row in conn.execute("PRAGMA table_info(quarantine)")
        }
        conn.execute(
            "INSERT INTO quarantine (id, source, company, title_raw, reason, first_seen, "
            "last_seen) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "simplify:feed:9",
                "simplify",
                "Acme",
                "Software Engineer",
                "out-of-scope-location",
                to_text(NOW),
                to_text(NOW),
            ),
        )
        conn.execute(
            "INSERT INTO jobs (id, source, company, title_raw, title_normalized, apply_url_raw, "
            "first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "greenhouse:acme:1",
                "greenhouse",
                "Acme",
                "Intern",
                "intern",
                "https://example.com/1",
                to_text(NOW),
                to_text(NOW),
            ),
        )
    monkeypatch.setattr(migrations, "discover", lambda: shipped)

    repository = SqliteRepository.connect(target)
    try:
        applied = migrations.applied_versions(repository._conn)
        stored = repository.get_job("greenhouse:acme:1")
        found = repository.list_jobs(JobFilters(levels=DEFAULT_LEVELS))
        jobs_columns = {row[1] for row in repository._conn.execute("PRAGMA table_info(jobs)")}
        held_columns = {row[1] for row in repository._conn.execute("PRAGMA table_info(quarantine)")}
        held = repository.list_quarantined(QuarantineFilters(limit=None))
    finally:
        repository.close()

    assert applied == tuple(migration.version for migration in shipped)
    assert "level" in jobs_columns and "employment_type" in held_columns
    assert [(entry.id, entry.employment_type) for entry in held] == [("simplify:feed:9", "")]
    assert stored is not None and stored.level is JobLevel.INTERNSHIP
    assert [job.id for job in found] == ["greenhouse:acme:1"]
    assert len(list(tmp_path.glob("stage.db.bak-*"))) == 1


def test_a_new_database_stores_levels_and_indexes_them(db_path: Path) -> None:
    from stage.storage.sqlite_repo import SqliteRepository

    _store(db_path)
    repository = SqliteRepository.connect(db_path)
    try:
        indexes = {row[1] for row in repository._conn.execute("PRAGMA index_list(jobs)")}
        stored = repository.get_job("greenhouse:acme:grad")
        counted = repository.composition("level")
    finally:
        repository.close()

    assert "idx_jobs_level" in indexes
    assert stored is not None and stored.level is JobLevel.NEW_GRAD
    assert counted == {"internship": 1, "new-grad": 1, "entry": 1}


def test_a_level_reaches_json_exports_and_ai_rows() -> None:
    from stage.ai.shape import posting_row
    from stage.cli.serialize import jobs_to_json, preferences_to_json
    from stage.services.export import COLUMNS

    job = _job("grad", JobLevel.NEW_GRAD)
    names = [name for name, _ in COLUMNS]

    assert json.loads(jobs_to_json([job]))[0]["level"] == "new-grad"
    assert names[names.index("role") + 1] == "level"
    assert dict(COLUMNS)["level"](job) == "new-grad"
    assert posting_row(job, None)["level"] == "new-grad"
    assert json.loads(preferences_to_json(Preferences(levels=(JobLevel.ENTRY,))))["levels"] == [
        "entry"
    ]


def test_show_prints_the_level(db_path: Path) -> None:
    _store(db_path)

    result = CliRunner().invoke(app, ["show", "greenhouse:acme:grad", "--db", str(db_path)])

    assert result.exit_code == 0, result.output
    assert "level new-grad" in _plain(result.stdout)


def test_screening_labels_a_kept_posting_and_rejects_the_rest_as_not_early_career() -> None:
    from stage.services.screening import normalize_batch

    job = Job(
        id="greenhouse:acme:1",
        source="greenhouse",
        company="Acme",
        title_raw="Software Engineering Intern",
        title_normalized="software engineering intern",
        apply_url_raw="https://example.com/1",
        description="",
        first_seen=NOW,
        last_seen=NOW,
        location_raw="Montreal, QC",
    )

    kept, rejected = normalize_batch([job, _job("staff", JobLevel.UNKNOWN)])

    assert [entry.level for entry in kept] == [JobLevel.INTERNSHIP]
    assert [entry.reason for entry in rejected] == [RejectionReason.NOT_EARLY_CAREER]


def test_the_tui_filters_by_level_and_keeps_internships_by_default() -> None:
    from stage.tui.state import FilterState, filter_rows

    state = FilterState()
    assert state.as_filters().levels == DEFAULT_LEVELS
    state.choose("level", "new-grad")
    assert (state.as_filters().level, state.as_filters().levels) == (JobLevel.NEW_GRAD, frozenset())
    state.choose("level", "all")
    assert state.as_filters().levels == EVERY_LEVEL
    state.choose("level", "all")
    assert "level" not in state.values
    state.mine = True
    assert state.as_filters(Preferences(levels=(JobLevel.ENTRY,))).levels == {JobLevel.ENTRY}
    offered = [value for name, value, _, _ in filter_rows(state) if name == "level"]
    assert offered == ["internship", "new-grad", "entry", "all"]


def test_ai_tools_filter_by_level_and_name_it_in_the_command() -> None:
    from stage.ai import tools as toolbox

    typed, _, _ = toolbox._chosen(None, None, "new-grad", None, None, None, False)
    every, _, _ = toolbox._chosen(None, None, "all", None, None, None, False)
    plain, _, _ = toolbox._chosen(None, None, None, None, None, None, False)
    arguments: dict[str, object] = {
        "words": "",
        "location": None,
        "role": None,
        "level": "all",
        "term": None,
        "language": None,
        "company": None,
        "mine": False,
        "untracked": False,
        "new": True,
        "days": None,
    }

    assert (typed.level, typed.levels) == (JobLevel.NEW_GRAD, frozenset())
    assert every.levels == EVERY_LEVEL and plain.levels == DEFAULT_LEVELS
    assert toolbox._command(arguments) == "stage list --level all --new --all"


def test_the_left_out_hint_repeats_a_typed_level() -> None:
    from stage.cli.commands.postings import _no_location_hint

    every = _no_location_hint(2, "stage list", JobFilters(levels=EVERY_LEVEL), False)
    grad = _no_location_hint(2, "stage list", JobFilters(level=JobLevel.NEW_GRAD), False)

    assert every.endswith("stage list --mine --level all --location unknown")
    assert grad.endswith("stage list --mine --level new-grad --location unknown")
