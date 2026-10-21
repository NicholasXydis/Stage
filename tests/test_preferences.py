import json
import os
import re
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stage.cli.app import app
from stage.domain import Job, JobFilters, LocationBucket, RoleCategory
from stage.domain.preferences import (
    MAX_ABOUT,
    MAX_COMPANIES,
    ChosenCompany,
    PreferenceError,
    Preferences,
    check_overlap,
    parse_about,
    parse_companies,
    parse_locations,
    parse_roles,
    parse_terms,
    split_values,
)
from stage.services.preferences import (
    DAMAGED,
    clear_preferences,
    load_preferences,
    preferences_path,
    save_preferences,
)

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
NOW = datetime.now(UTC)
EXPECTED = ["can-quant", "can-swe", "mtl-general", "followed-usa", "someday"]
HEADER = "Mine: canada | swe, quant | summer-2027 | following Cisco | leaving out Acme Corp"


def _fold(value: str) -> str:
    from stage.lexicon import fold

    return fold(value)


def _plain(text: str) -> str:
    return " ".join(ANSI.sub("", text).split())


def _job(
    name: str,
    *,
    company: str = "Acme",
    location: LocationBucket = LocationBucket.USA,
    role: RoleCategory = RoleCategory.SWE,
    term: str = "summer-2027",
    hours: int = 1,
) -> Job:
    seen = NOW - timedelta(hours=hours)
    return Job(
        id=f"greenhouse:{_fold(company).replace(' ', '-')}:{name}",
        source="greenhouse",
        company=company,
        title_raw=f"Intern {name}",
        title_normalized=f"intern {name}",
        apply_url_raw=f"https://example.com/{name}",
        description="",
        first_seen=seen,
        last_seen=seen,
        location=location,
        role=role,
        term=term,
    )


def _store(path: Path, jobs: list[Job], *, legacy_remote: str = "") -> str:
    import sqlite3
    from contextlib import closing

    from stage.storage.repository import SourceBatch
    from stage.storage.sqlite_repo import SqliteRepository

    repository = SqliteRepository.connect(path)
    repository.apply_source_batch(
        SourceBatch(source="greenhouse", run_started_at=NOW, jobs=tuple(jobs))
    )
    repository.close()
    if legacy_remote:
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("UPDATE jobs SET location = 'remote' WHERE id = ?", (legacy_remote,))
            conn.commit()
    return str(path)


def _catalogue() -> list[Job]:
    canada, montreal = LocationBucket.CANADA, LocationBucket.MONTREAL
    unknown, usa = LocationBucket.UNKNOWN, LocationBucket.USA
    general, quant = RoleCategory.GENERAL_CS, RoleCategory.QUANT
    return [
        _job("usa-new", company="Jane Street", location=usa, hours=0),
        _job("nowhere", company="Mystery", location=unknown, hours=1),
        _job("can-quant", company="Coveo", location=canada, role=quant, hours=2),
        _job("can-swe", company="Nuvei", location=canada, hours=3),
        _job("mtl-general", company="Mila", location=montreal, role=general, hours=4),
        _job("mtl-data", company="Ubisoft", location=montreal, role=RoleCategory.DATA, hours=5),
        _job("followed-usa", company="Cisco", location=usa, hours=6),
        _job("followed-nowhere", company="Cisco", location=unknown, hours=7),
        _job("avoided", company="Acme Corp", location=canada, hours=8),
        _job("fall", company="Lightspeed", location=canada, term="fall-2026", hours=9),
        _job("someday", company="Shopify", location=canada, term="unknown", hours=10),
    ]


MINE = Preferences(
    locations=(LocationBucket.CANADA,),
    roles=(RoleCategory.SWE, RoleCategory.QUANT),
    terms=("summer-2027",),
    follow=(ChosenCompany("Cisco", "cisco"),),
    avoid=(ChosenCompany("Acme Corp", "acme corp"),),
)


def _names(jobs: tuple[Job, ...] | list[Job]) -> list[str]:
    return [job.id.rsplit(":", 1)[1] for job in jobs]


def _seeded(db_path: Path) -> str:
    db = _store(db_path, _catalogue(), legacy_remote="greenhouse:mystery:nowhere")
    save_preferences(MINE)
    return db


def test_empty_preferences_change_nothing() -> None:
    filters = JobFilters(limit=10)

    assert Preferences().apply(filters) == filters
    assert Preferences().is_empty
    assert not Preferences().narrows


def test_canada_covers_montreal_and_no_location_is_not_a_place() -> None:
    applied = Preferences(locations=(LocationBucket.CANADA,)).apply(JobFilters())

    assert applied.locations == {LocationBucket.CANADA, LocationBucket.MONTREAL}


def test_montreal_alone_does_not_widen_to_canada() -> None:
    applied = Preferences(locations=(LocationBucket.MONTREAL,)).apply(JobFilters())

    assert applied.locations == {LocationBucket.MONTREAL}


def test_roles_always_keep_general_cs() -> None:
    applied = Preferences(roles=(RoleCategory.QUANT,)).apply(JobFilters())

    assert applied.roles == {RoleCategory.QUANT, RoleCategory.GENERAL_CS, RoleCategory.UNKNOWN}


def test_terms_always_keep_unknown() -> None:
    applied = Preferences(terms=("summer-2027",)).apply(JobFilters())

    assert applied.terms == {"summer-2027", "unknown"}


def test_an_explicit_flag_wins_over_its_preference() -> None:
    explicit = JobFilters(
        location=LocationBucket.USA, role=RoleCategory.DATA, term="fall-2026", company="Acme"
    )

    applied = MINE.apply(explicit)

    assert applied.locations == frozenset()
    assert applied.roles == frozenset()
    assert applied.terms == frozenset()
    assert applied.avoid_companies == frozenset()
    assert applied.follow_companies == frozenset()


def test_following_only_matters_alongside_saved_places() -> None:
    followed = (ChosenCompany("Cisco", "cisco"),)
    placed = Preferences(locations=(LocationBucket.USA,), follow=followed)

    assert Preferences(follow=followed).apply(JobFilters()).follow_companies == frozenset()
    assert placed.apply(JobFilters()).follow_companies == {"cisco"}
    typed = placed.apply(JobFilters(location=LocationBucket.CANADA))
    assert typed.follow_companies == frozenset()


def test_the_header_is_one_short_line() -> None:
    everything = JobFilters(
        location=LocationBucket.USA, role=RoleCategory.DATA, term="fall-2026", company="X"
    )

    assert MINE.describe(JobFilters()) == HEADER
    assert MINE.describe(JobFilters(location=LocationBucket.USA)) == (
        "Mine: swe, quant | summer-2027 | leaving out Acme Corp"
    )
    assert MINE.describe(everything) == (
        "Mine: the filters you typed replace your saved preferences"
    )


def test_values_split_on_commas_and_repeats_without_duplicates() -> None:
    assert split_values(["montreal, canada", "canada", " ", "usa;"]) == [
        "montreal",
        "canada",
        "usa",
    ]


@pytest.mark.parametrize(
    ("value", "said"),
    [
        ("montrael", "Unknown location 'montrael'. Did you mean 'montreal'?"),
        ("mars", "Unknown location 'mars'. Choose from: montreal, canada, usa"),
        ("unknown", "stage list --mine --location unknown shows them"),
        ("international", "Choose from: montreal, canada, usa"),
    ],
)
def test_a_place_outside_the_choices_is_explained(value: str, said: str) -> None:
    with pytest.raises(PreferenceError) as caught:
        parse_locations([value])

    assert said in str(caught.value)


def test_a_blank_place_clears_the_field() -> None:
    assert parse_locations([""]) == ()


@pytest.mark.parametrize(
    ("value", "said"),
    [("sew", "Did you mean 'swe'?"), ("hardware", "Choose from: swe"), ("designer", "general-cs")],
)
def test_a_role_outside_the_choices_is_explained(value: str, said: str) -> None:
    with pytest.raises(PreferenceError, match=re.escape(said)):
        parse_roles([value])


def test_roles_and_locations_ignore_case() -> None:
    assert parse_roles(["SWE", "Ml-Ai"]) == (RoleCategory.SWE, RoleCategory.ML_AI)
    assert parse_locations(["Montreal"]) == (LocationBucket.MONTREAL,)


def test_terms_are_lowercased_unknown_is_implied_and_garbage_is_refused() -> None:
    assert parse_terms(["Summer-2027", "unknown"]) == ("summer-2027",)
    with pytest.raises(PreferenceError, match="Terms look like summer-2027"):
        parse_terms(["summer"])
    with pytest.raises(PreferenceError, match="At most 12 terms"):
        parse_terms([f"summer-{2000 + year}" for year in range(13)])


def test_employers_are_cleaned_deduplicated_and_bounded() -> None:
    found = parse_companies(["Shopify​", "shopify", "Acme\tCorp"], _fold)

    assert [company.name for company in found] == ["Shopify", "Acme Corp"]
    with pytest.raises(PreferenceError, match="no letters or digits"):
        parse_companies(["!!!"], _fold)
    with pytest.raises(PreferenceError, match="at most 120 characters"):
        parse_companies(["x" * 121], _fold)
    with pytest.raises(PreferenceError, match=f"At most {MAX_COMPANIES} employers"):
        parse_companies([f"Employer {index}" for index in range(MAX_COMPANIES + 1)], _fold)


def test_a_comma_inside_a_stored_employer_name_keeps_it_whole() -> None:
    known = {"src inc": "SRC, Inc", "shopify": "Shopify"}

    found = parse_companies(["SRC, Inc, shopify"], _fold, known)
    split = parse_companies(["SRC, Inc"], _fold)

    assert [e.name for e in found] == ["SRC, Inc", "Shopify"]
    assert [e.name for e in split] == ["SRC", "Inc"]


def test_a_huge_paste_stops_at_the_cap_without_folding_everything() -> None:
    calls: list[str] = []

    def counting(value: str) -> str:
        calls.append(value)
        return _fold(value)

    with pytest.raises(PreferenceError, match=str(MAX_COMPANIES)):
        parse_companies([",".join(f"e{index}" for index in range(10_000))], counting)

    assert len(calls) < 1_000


def test_an_employer_cannot_be_both_followed_and_avoided() -> None:
    cisco = (ChosenCompany("Cisco", "cisco"),)

    with pytest.raises(PreferenceError, match="Cisco cannot be both followed and avoided."):
        check_overlap(cisco, cisco)


def test_the_note_about_you_drops_hidden_characters_and_is_bounded() -> None:
    assert parse_about("line one\nline‮two\x00") == "line one line two"
    with pytest.raises(PreferenceError, match="at most 2,000 characters"):
        parse_about("x" * (MAX_ABOUT + 1))


def test_preferences_survive_a_save_and_a_load(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"
    original = replace(MINE, about="third-year CS student")

    save_preferences(original, path)
    loaded = load_preferences(path)

    assert loaded.preferences == original
    assert loaded.problems == ()
    assert not loaded.damaged


def test_a_missing_file_means_no_preferences(tmp_path: Path) -> None:
    loaded = load_preferences(tmp_path / "absent.json")

    assert loaded.preferences.is_empty
    assert loaded.problems == ()
    assert not loaded.damaged


@pytest.mark.parametrize("body", [b"{not json", b"[1, 2]", b"\xff\xfe\xfa"])
def test_a_damaged_file_says_so_in_plain_words(tmp_path: Path, body: bytes) -> None:
    path = tmp_path / "preferences.json"
    path.write_bytes(body)

    loaded = load_preferences(path)

    assert loaded.damaged
    assert loaded.preferences.is_empty
    assert loaded.problems == (DAMAGED,)
    assert "Error" not in DAMAGED


def test_one_bad_field_does_not_lose_the_others(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"
    path.write_text(
        json.dumps(
            {
                "locations": ["mars"],
                "roles": "swe",
                "terms": ["summer-2027"],
                "follow": [{"name": "Shopify"}, "Ubisoft", 7, {"nope": 1}],
                "about": 42,
            }
        ),
        encoding="utf-8",
    )

    loaded = load_preferences(path)

    assert loaded.preferences.terms == ("summer-2027",)
    assert [e.folded for e in loaded.preferences.follow] == ["shopify", "ubisoft"]
    assert loaded.preferences.locations == ()
    assert (
        "preferences.json: locations was ignored. Unknown location 'mars'. "
        "Choose from: montreal, canada, usa"
    ) in loaded.problems
    assert len(loaded.problems) == 5
    assert not loaded.damaged


def test_a_hand_edited_file_keeps_only_the_first_employers(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"
    names = [f"Employer {index}" for index in range(MAX_COMPANIES + 5)]
    path.write_text(json.dumps({"avoid": names}), encoding="utf-8")

    loaded = load_preferences(path)

    assert len(loaded.preferences.avoid) == MAX_COMPANIES
    assert loaded.problems


def test_a_file_with_a_long_note_and_a_scalar_list_keeps_the_rest(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"
    path.write_text(
        json.dumps({"roles": ["swe"], "about": "x" * (MAX_ABOUT + 1), "avoid": "Acme"}),
        encoding="utf-8",
    )

    loaded = load_preferences(path)

    assert loaded.preferences == Preferences(roles=(RoleCategory.SWE,))
    assert len(loaded.problems) == 2


def test_a_hand_edited_overlap_is_avoided(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({"follow": ["Shopify", "Ubisoft"], "avoid": ["shopify"]}))

    loaded = load_preferences(path)

    assert [e.folded for e in loaded.preferences.follow] == ["ubisoft"]
    assert [e.folded for e in loaded.preferences.avoid] == ["shopify"]
    assert loaded.problems


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_the_file_is_readable_only_by_its_owner(tmp_path: Path) -> None:
    path = save_preferences(Preferences(about="private"), tmp_path / "preferences.json")

    assert path.stat().st_mode & 0o777 == 0o600


def test_a_save_replaces_the_file_and_leaves_no_temporary_behind(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"
    save_preferences(Preferences(roles=(RoleCategory.SWE,)), path)
    save_preferences(Preferences(roles=(RoleCategory.QUANT,)), path)

    assert load_preferences(path).preferences.roles == (RoleCategory.QUANT,)
    assert [entry.name for entry in tmp_path.iterdir()] == ["preferences.json"]


def test_clearing_reports_whether_anything_was_saved(tmp_path: Path) -> None:
    path = save_preferences(Preferences(about="x"), tmp_path / "preferences.json")

    assert clear_preferences(path)
    assert not clear_preferences(path)


async def test_mine_filters_newest_first_and_counts_what_has_no_location(db_path: Path) -> None:
    from stage.services.query import count_without_location, list_jobs, search_jobs
    from stage.storage import open_repository

    _store(db_path, _catalogue(), legacy_remote="greenhouse:mystery:nowhere")
    mine = MINE.apply(JobFilters(limit=None))
    unknown = MINE.apply(JobFilters(limit=None, location=LocationBucket.UNKNOWN))

    async with open_repository(db_path) as repository:
        listed = await list_jobs(repository, mine, window_days=None)
        plain = await list_jobs(repository, JobFilters(limit=None), window_days=None)
        searched = await search_jobs(repository, "intern", mine, window_days=None)
        hidden = await list_jobs(repository, unknown, window_days=None)
        counted = await count_without_location(repository, mine, window_days=None)

    assert _names(listed.jobs) == EXPECTED
    assert listed.total_matching == len(EXPECTED)
    assert listed.no_location == 2
    assert sorted(_names(hidden.jobs)) == ["followed-nowhere", "nowhere"]
    assert counted == 2
    assert plain.no_location is None
    assert _names(plain.jobs)[:3] == ["usa-new", "nowhere", "can-quant"]
    assert sorted(_names(searched.jobs)) == sorted(EXPECTED)
    assert searched.no_location == 2


async def test_a_limit_takes_the_newest_matches(db_path: Path) -> None:
    from stage.services.query import list_jobs
    from stage.storage import open_repository

    _store(db_path, _catalogue())

    async with open_repository(db_path) as repository:
        top = await list_jobs(repository, MINE.apply(JobFilters(limit=2)), window_days=None)

    assert _names(top.jobs) == EXPECTED[:2]
    assert top.total_matching == len(EXPECTED)


async def test_without_saved_places_nothing_is_left_out(db_path: Path) -> None:
    from stage.services.query import list_jobs
    from stage.storage import open_repository

    _store(db_path, _catalogue())
    roles_only = Preferences(roles=(RoleCategory.SWE,), follow=(ChosenCompany("Cisco", "cisco"),))

    async with open_repository(db_path) as repository:
        listed = await list_jobs(repository, roles_only.apply(JobFilters()), window_days=None)

    assert listed.no_location is None
    assert "nowhere" in _names(listed.jobs)


def test_prefs_saves_from_flags_and_shows_them_back() -> None:
    result = CliRunner().invoke(
        app,
        [
            "prefs",
            "--location",
            "montreal,canada",
            "--role",
            "swe",
            "--role",
            "quant",
            "--term",
            "Summer-2027",
            "--follow",
            "Shopify",
            "--avoid",
            "Acme Corp",
            "--about",
            "third-year student",
        ],
    )
    said = _plain(result.stdout)

    assert result.exit_code == 0
    assert "Saved your preferences" in said
    assert "Following Shopify" in said
    assert "Leaving out Acme Corp" in said
    assert "stage list --mine shows postings that match, newest first." in said
    saved = load_preferences().preferences
    assert saved.locations == (LocationBucket.MONTREAL, LocationBucket.CANADA)
    assert saved.roles == (RoleCategory.SWE, RoleCategory.QUANT)
    assert saved.terms == ("summer-2027",)
    assert saved.about == "third-year student"


def test_prefs_changes_only_the_fields_it_is_given() -> None:
    runner = CliRunner()
    runner.invoke(app, ["prefs", "--location", "montreal", "--role", "swe"])
    runner.invoke(app, ["prefs", "--role", "data"])

    saved = load_preferences().preferences
    assert saved.locations == (LocationBucket.MONTREAL,)
    assert saved.roles == (RoleCategory.DATA,)


def test_an_empty_value_clears_just_that_field() -> None:
    runner = CliRunner()
    runner.invoke(app, ["prefs", "--follow", "Shopify", "--role", "swe"])
    runner.invoke(app, ["prefs", "--follow", ""])

    saved = load_preferences().preferences
    assert saved.follow == ()
    assert saved.roles == (RoleCategory.SWE,)


@pytest.mark.parametrize(
    ("flag", "value", "said"),
    [
        ("--location", "montrael", "Did you mean 'montreal'?"),
        ("--role", "hardware", "Unknown role 'hardware'. Choose from: swe"),
        ("--term", "summer", "--term must look like"),
        ("--term", "monsoon-2027", "--term must look like"),
        ("--follow", "!!!", "no letters or digits"),
    ],
)
def test_bad_input_is_explained_and_nothing_is_saved(flag: str, value: str, said: str) -> None:
    result = CliRunner().invoke(app, ["prefs", flag, value])

    assert result.exit_code == 2
    assert said in _plain(result.stdout)
    assert not preferences_path().exists()


def test_prefs_without_a_terminal_shows_rather_than_waits() -> None:
    result = CliRunner().invoke(app, ["prefs"], input="")

    assert result.exit_code == 0
    assert "Nothing chosen yet" in _plain(result.stdout)


def test_prefs_json_and_clear() -> None:
    runner = CliRunner()
    runner.invoke(app, ["prefs", "--location", "usa"])

    shown = json.loads(runner.invoke(app, ["prefs", "--json"]).stdout)
    cleared = json.loads(runner.invoke(app, ["prefs", "--clear", "--json"]).stdout)
    again = runner.invoke(app, ["prefs", "--clear"])

    assert shown["locations"] == ["usa"]
    assert cleared == {"cleared": True}
    assert "No preferences were saved" in _plain(again.stdout)


def test_prefs_json_after_a_change_prints_what_was_saved() -> None:
    result = CliRunner().invoke(app, ["prefs", "--role", "swe", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["roles"] == ["swe"]


@pytest.mark.parametrize(
    ("args", "said"),
    [
        (["--clear", "--role", "swe"], "--clear cannot be combined with other options."),
        (["--clear", "--show"], "--clear cannot be combined with other options."),
        (["--show", "--role", "swe"], "--show cannot be combined with options that change"),
    ],
)
def test_conflicting_options_are_refused_like_elsewhere(args: list[str], said: str) -> None:
    result = CliRunner().invoke(app, ["prefs", *args])

    assert result.exit_code == 2
    assert said in _plain(result.stdout)
    assert not preferences_path().exists()


def test_the_guided_setup_keeps_changes_clears_and_retries() -> None:
    from stage.cli.commands.preferences import _guided

    current = Preferences(locations=(LocationBucket.USA,), roles=(RoleCategory.SWE,), about="hello")
    answers = iter(["", "designer", "quant, data", "-", "Shopify", "", "-"])
    warnings: list[str] = []

    def ask(question: str, shown: str) -> str:
        answer = next(answers)
        return shown if answer == "" else answer

    result = _guided(current, {}, ask, warnings.append)

    assert result.locations == (LocationBucket.USA,)
    assert result.roles == (RoleCategory.QUANT, RoleCategory.DATA)
    assert result.terms == ()
    assert [e.name for e in result.follow] == ["Shopify"]
    assert result.about == ""
    assert len(warnings) == 1
    assert warnings[0].startswith("Unknown role 'designer'.")


def test_pressing_enter_keeps_a_saved_employer_even_when_the_database_forgot_it() -> None:
    from stage.cli.commands.preferences import _guided

    current = Preferences(follow=(ChosenCompany("SRC, Inc", "src inc"),))

    result = _guided(current, {}, lambda _, shown: shown, lambda _: None)

    assert result == current


def test_the_guided_setup_refuses_to_avoid_what_it_just_followed() -> None:
    from stage.cli.commands.preferences import _guided

    current = Preferences(
        avoid=(ChosenCompany("Shopify", "shopify"), ChosenCompany("Acme", "acme"))
    )
    answers = iter(["", "", "", "Shopify", "shopify", "Acme, Ubisoft", ""])
    warnings: list[str] = []

    result = _guided(current, {}, lambda _, shown: next(answers) or shown, warnings.append)

    assert [e.folded for e in result.follow] == ["shopify"]
    assert [e.folded for e in result.avoid] == ["acme", "ubisoft"]
    assert warnings == ["Shopify cannot be both followed and avoided."]


def test_the_setup_asks_plain_questions() -> None:
    from stage.cli.commands.preferences import _guided

    questions: list[str] = []

    def ask(question: str, shown: str) -> str:
        questions.append(question)
        return shown

    _guided(Preferences(), {}, ask, lambda _: None)

    assert questions == [
        "Locations (montreal, canada, usa)",
        "Roles (swe, security, data, ml-ai, quant, infra, embedded, general-cs)",
        "Terms (such as summer-2027)",
        "Employers to follow, included from any place",
        "Employers to leave out",
        "A short note about you (optional)",
    ]


def test_a_long_saved_note_is_not_echoed_into_the_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    import typer

    from stage.cli.commands.preferences import _ask

    seen: dict[str, object] = {}

    def prompt(text: str, default: str, show_default: bool) -> str:
        seen.update(text=text, show_default=show_default)
        return default

    monkeypatch.setattr(typer, "prompt", prompt)

    assert _ask("A short note about you", "x" * 500) == "x" * 500
    assert seen == {
        "text": "A short note about you (Enter keeps the saved one)",
        "show_default": False,
    }
    _ask("Roles", "swe")
    assert seen["show_default"] is True


def test_prefs_in_a_terminal_walks_through_each_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from stage.cli.commands import preferences as command

    monkeypatch.setattr(command, "_interactive", lambda: True)

    result = CliRunner().invoke(
        app, ["prefs"], input="montreal\nswe, robotics\nswe\nsummer-2027\n\n\nstudent\n"
    )
    said = _plain(result.stdout)
    saved = load_preferences().preferences

    assert result.exit_code == 0
    assert "Press Enter to keep a value, or type - to clear it." in said
    assert "Unknown role 'robotics'." in said
    assert saved == Preferences(
        locations=(LocationBucket.MONTREAL,),
        roles=(RoleCategory.SWE,),
        terms=("summer-2027",),
        about="student",
    )


def test_following_an_unknown_employer_suggests_a_close_one(db_path: Path) -> None:
    _store(db_path, [_job("a", company="Shopify Inc")])
    db = str(db_path)

    result = CliRunner().invoke(app, ["prefs", "--follow", "Shopfy Inc", "--db", db])
    data = CliRunner().invoke(app, ["prefs", "--follow", "Shopfy Inc", "--json", "--db", db])

    assert result.exit_code == 0
    assert (
        "Saved 'Shopfy Inc', but Stage has no postings from it yet. Did you mean 'Shopify Inc'?"
        in _plain(result.stdout)
    )
    assert json.loads(data.stdout)["follow"] == ["Shopfy Inc"]
    assert "Did you mean 'Shopify Inc'?" in data.stderr


def test_follow_takes_the_stored_spelling_and_accents_do_not_matter(db_path: Path) -> None:
    db = _store(
        db_path,
        [_job("a", company="Société Générale"), _job("b", company="SRC, Inc")],
    )

    result = CliRunner().invoke(
        app, ["prefs", "--follow", "societe generale", "--follow", "src, inc", "--db", db]
    )

    assert result.exit_code == 0
    assert "but Stage has no postings" not in _plain(result.stdout)
    names = [e.name for e in load_preferences().preferences.follow]
    assert names == ["Société Générale", "SRC, Inc"]


def test_an_employer_cannot_be_followed_and_avoided_from_the_command_line() -> None:
    runner = CliRunner()
    both = runner.invoke(app, ["prefs", "--follow", "Shopify", "--avoid", "shopify"])
    runner.invoke(app, ["prefs", "--follow", "Shopify"])
    later = runner.invoke(app, ["prefs", "--avoid", "SHOPIFY"])

    assert both.exit_code == 2
    assert later.exit_code == 2
    assert "Shopify cannot be both followed and avoided." in _plain(later.stdout)
    assert load_preferences().preferences.avoid == ()


def test_a_save_that_cannot_happen_is_explained_not_raised() -> None:
    preferences_path().mkdir(parents=True)

    saved = CliRunner().invoke(app, ["prefs", "--role", "swe"])
    cleared = CliRunner().invoke(app, ["prefs", "--clear"])

    assert saved.exit_code == 1
    assert "Could not save" in _plain(saved.stdout)
    assert cleared.exit_code == 1
    assert "Could not delete" in _plain(cleared.stdout)
    assert not [e for e in preferences_path().parent.iterdir() if e.name.startswith(".pref")]


def test_prefs_never_creates_a_database(tmp_path: Path) -> None:
    missing = tmp_path / "absent.db"

    result = CliRunner().invoke(app, ["prefs", "--follow", "Shopify", "--db", str(missing)])

    assert result.exit_code == 0
    assert not missing.exists()


def test_prefs_show_counts_open_postings_per_employer(db_path: Path) -> None:
    db = _store(db_path, _catalogue())
    save_preferences(
        Preferences(follow=(ChosenCompany("Cisco", "cisco"), ChosenCompany("Nope", "nope")))
    )
    elsewhere = str(db_path.with_name("x.db"))

    shown = _plain(CliRunner().invoke(app, ["prefs", "--show", "--db", db]).stdout)
    missing = _plain(CliRunner().invoke(app, ["prefs", "--show", "--db", elsewhere]).stdout)

    assert "Following Cisco (2 open); Nope (0 open)" in shown
    assert "Following Cisco; Nope" in missing
    assert not db_path.with_name("x.db").exists()


def test_employers_show_with_semicolons_and_accept_them_back(db_path: Path) -> None:
    db = _store(db_path, [_job("a", company="SRC, Inc"), _job("b", company="Cisco")])

    result = CliRunner().invoke(app, ["prefs", "--follow", "SRC, Inc; Cisco", "--db", db])

    assert "SRC, Inc (1 open); Cisco (1 open)" in _plain(result.stdout)
    assert [e.name for e in load_preferences().preferences.follow] == ["SRC, Inc", "Cisco"]


def test_markup_in_an_employer_name_is_printed_not_parsed() -> None:
    result = CliRunner().invoke(app, ["prefs", "--avoid", "[/bold]evil", "--follow", "[link=x]y"])

    assert result.exit_code == 0
    assert "[/bold]evil" in result.stdout
    assert "[link=x]y" in result.stdout


def test_the_prefs_help_shows_no_type_noise() -> None:
    output = ANSI.sub("", CliRunner().invoke(app, ["prefs", "--help"]).stdout)

    assert "--location" in output
    assert not re.search(r"<(str|int|text|path)>|\[[0-9]+<=", output)


def test_list_mine_needs_saved_preferences(db_path: Path) -> None:
    db = _store(db_path, _catalogue())

    result = CliRunner().invoke(app, ["list", "--mine", "--db", db])

    assert result.exit_code == 2
    assert _plain(result.stdout) == (
        "No preferences saved yet. Run stage prefs to choose places, roles and terms."
    )


def test_a_note_alone_gives_mine_nothing_to_apply(db_path: Path) -> None:
    db = _store(db_path, _catalogue())
    save_preferences(Preferences(about="hello"))

    result = CliRunner().invoke(app, ["list", "--mine", "--db", db])

    assert result.exit_code == 2
    assert "only hold a note about you" in _plain(result.stdout)


def test_a_damaged_preferences_file_gives_one_clear_message(db_path: Path) -> None:
    db = _store(db_path, _catalogue())
    preferences_path().parent.mkdir(parents=True, exist_ok=True)
    preferences_path().write_text("{broken", encoding="utf-8")

    result = CliRunner().invoke(app, ["list", "--mine", "--db", db])

    assert result.exit_code == 2
    assert _plain(result.stdout) == (
        "Your saved preferences could not be read. Run stage prefs to save them again, "
        "or stage prefs --clear."
    )


def test_a_partly_bad_file_warns_and_keeps_json_clean(db_path: Path) -> None:
    db = _store(db_path, _catalogue())
    preferences_path().parent.mkdir(parents=True, exist_ok=True)
    preferences_path().write_text(
        json.dumps({"locations": ["canada"], "roles": ["sorcery"]}), encoding="utf-8"
    )
    runner = CliRunner()

    shown = runner.invoke(app, ["list", "--mine", "--db", db])
    data = runner.invoke(app, ["list", "--mine", "--json", "--db", db])

    said = _plain(shown.stdout)
    assert "preferences.json: roles was ignored. Unknown role 'sorcery'." in said
    assert "roles was ignored" in data.stderr
    assert {row["location"] for row in json.loads(data.stdout)} == {"canada", "montreal"}


def test_list_mine_prints_one_line_then_the_list_then_what_was_left_out(db_path: Path) -> None:
    db = _seeded(db_path)
    runner = CliRunner()

    shown = _plain(runner.invoke(app, ["list", "--mine", "--db", db]).stdout)
    last = _plain(runner.invoke(app, ["list", "--mine", "--last", "0", "--db", db]).stdout)
    data = json.loads(runner.invoke(app, ["list", "--mine", "--json", "--db", db]).stdout)

    assert shown.startswith(f"{HEADER} # Age")
    assert shown.endswith(
        "acts on numbered rows. 2 more have no location given: stage list --mine --location unknown"
    )
    assert "stage list --last 0 --mine --location unknown" in last
    assert [row["company"] for row in data] == ["Coveo", "Nuvei", "Mila", "Cisco", "Shopify"]


def test_the_suggested_command_shows_exactly_what_was_left_out(db_path: Path) -> None:
    db = _seeded(db_path)

    data = (
        CliRunner()
        .invoke(app, ["list", "--mine", "--location", "unknown", "--json", "--db", db])
        .stdout
    )

    assert sorted(row["company"] for row in json.loads(data)) == ["Cisco", "Mystery"]


def test_one_left_out_posting_reads_as_one(db_path: Path) -> None:
    db = _store(
        db_path,
        [
            _job("here", company="Coveo", location=LocationBucket.CANADA),
            _job("nowhere", company="Mystery", location=LocationBucket.UNKNOWN),
        ],
    )
    save_preferences(Preferences(locations=(LocationBucket.CANADA,)))

    shown = _plain(CliRunner().invoke(app, ["list", "--mine", "--db", db]).stdout)

    assert "1 more has no location given" in shown


def test_search_mine_names_its_query_in_the_hint(db_path: Path) -> None:
    db = _seeded(db_path)

    shown = _plain(CliRunner().invoke(app, ["search", "intern", "--mine", "--db", db]).stdout)

    assert shown.startswith(HEADER)
    assert '2 more have no location given: stage search "intern" --mine --location unknown' in shown


def test_export_mine_writes_newest_first_and_says_what_was_left_out(
    db_path: Path, tmp_path: Path
) -> None:
    import csv

    db = _seeded(db_path)
    out = tmp_path / "mine.csv"

    result = CliRunner().invoke(
        app, ["export", "--format", "csv", "--mine", "--out", str(out), "--db", db]
    )

    assert result.exit_code == 0
    assert _plain(result.stdout).startswith(HEADER)
    assert "stage export --format csv --mine --location unknown" in _plain(result.stdout)
    with out.open(encoding="utf-8-sig") as handle:
        companies = [row["company"] for row in csv.DictReader(handle)]
    assert companies == ["Coveo", "Nuvei", "Mila", "Cisco", "Shopify"]


def test_naming_a_company_drops_follow_and_avoid_from_the_header(db_path: Path) -> None:
    db = _seeded(db_path)

    shown = _plain(
        CliRunner().invoke(app, ["list", "--mine", "--company", "Acme Corp", "--db", db]).stdout
    )

    assert shown.startswith("Mine: canada | swe, quant | summer-2027 ")
    assert "following" not in shown
    assert "leaving out" not in shown
    assert "Acme Corp" in shown


def test_list_mine_stays_off_the_lexicon_and_the_renderer_in_json() -> None:
    probe = (
        "import sys\n"
        "from stage.domain import LocationBucket\n"
        "from stage.domain.preferences import ChosenCompany, Preferences\n"
        "from stage.services.preferences import save_preferences\n"
        "save_preferences(Preferences(locations=(LocationBucket.CANADA,),"
        " follow=(ChosenCompany('Shopify', 'shopify'),)))\n"
        "from stage.cli.app import app\n"
        "try:\n"
        "    app(['list', '--mine', '--json'])\n"
        "except SystemExit:\n"
        "    pass\n"
        "loaded = [n for n in ('yaml', 'stage.lexicon', 'rich.console') if n in sys.modules]\n"
        "print('LOADED:' + ','.join(loaded), file=sys.stderr)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )

    assert result.stderr.strip().splitlines()[-1] == "LOADED:"


def test_the_tui_puts_mine_first_and_says_when_nothing_is_saved() -> None:
    from stage.tui.state import FilterState, filter_rows

    state = FilterState()

    assert filter_rows(state)[0][:3] == (
        "mine",
        "on",
        "Only my preferences (none saved; run stage prefs)",
    )
    assert filter_rows(state, saved=True)[0][:3] == ("mine", "on", "Only my preferences")


def test_the_tui_state_applies_preferences_only_when_mine_is_on() -> None:
    from stage.tui.state import FilterState

    state = FilterState()

    assert state.as_filters(MINE).locations == frozenset()
    state.choose("mine", "on")
    assert LocationBucket.MONTREAL in state.as_filters(MINE).locations
    assert state.as_filters(None).locations == frozenset()
    state.clear()
    assert not state.mine


async def test_the_tui_mine_row_filters_and_explains(db_path: Path) -> None:
    from textual.widgets import OptionList

    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    db = _seeded(db_path)
    app_under_test = StageApp(Path(db), "")

    async with app_under_test.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        screen = app_under_test.screen
        assert isinstance(screen, PostingsScreen)
        await pilot.press("f")
        await pilot.pause()
        assert screen.query_one("#filters", OptionList).highlighted == 0
        await pilot.press("enter")
        await pilot.pause()
        refresh = type(screen).refresh_results.__wrapped__  # type: ignore[attr-defined]
        await refresh(screen)
        await pilot.pause()
        messages = [str(note.message) for note in app_under_test._notifications]

        assert screen.state.mine
        assert "mine" in str(screen.query_one("#chips").render())
        assert _names(screen._jobs) == EXPECTED
        assert messages == [
            f"{HEADER}. 2 with no location given are left out; turn Mine off to see them."
        ]


async def test_the_tui_mine_row_refuses_without_preferences(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    app_under_test = StageApp(tmp_path / "empty.db", "")

    async with app_under_test.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        screen = app_under_test.screen
        assert isinstance(screen, PostingsScreen)
        await pilot.press("f")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        messages = [str(note.message) for note in app_under_test._notifications]

        assert not screen.state.mine
        assert messages == ["No preferences saved yet. Run stage prefs in a terminal."]


async def test_the_tui_warns_about_a_damaged_preferences_file(tmp_path: Path) -> None:
    from stage.tui.app import StageApp

    preferences_path().parent.mkdir(parents=True, exist_ok=True)
    preferences_path().write_text("{broken", encoding="utf-8")
    app_under_test = StageApp(tmp_path / "empty.db", "")

    async with app_under_test.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        messages = [str(note.message) for note in app_under_test._notifications]

    assert DAMAGED in messages


async def test_a_refresh_started_before_mine_never_speaks_for_it(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from textual.widgets import OptionList

    from stage.services import query
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    db = _seeded(db_path)
    gate = asyncio.Event()
    real = query.list_jobs
    calls: list[JobFilters] = []

    async def first_one_waits(*args: object, **kwargs: object) -> object:
        calls.append(args[1])  # type: ignore[arg-type]
        if len(calls) == 1:
            await gate.wait()
        return await real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(query, "list_jobs", first_one_waits)
    app_under_test = StageApp(Path(db), "")

    async with app_under_test.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        screen = app_under_test.screen
        assert isinstance(screen, PostingsScreen)
        await pilot.press("f")
        await pilot.pause()
        screen.query_one("#filters", OptionList).highlighted = 0
        await pilot.press("enter")
        gate.set()
        await pilot.pause()
        await pilot.pause()
        await type(screen).refresh_results.__wrapped__(screen)  # type: ignore[attr-defined]
        await pilot.pause()
        messages = [str(note.message) for note in app_under_test._notifications]

    assert messages == [
        f"{HEADER}. 2 with no location given are left out; turn Mine off to see them."
    ]


async def test_a_cancelled_refresh_hands_the_mine_notice_to_the_next_one(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from stage.services import query
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    db = _seeded(db_path)
    app_under_test = StageApp(Path(db), "")

    async with app_under_test.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        screen = app_under_test.screen
        assert isinstance(screen, PostingsScreen)
        gate = asyncio.Event()

        async def waits(*args: object, **kwargs: object) -> object:
            await gate.wait()
            raise AssertionError

        monkeypatch.setattr(query, "list_jobs", waits)
        screen.state.choose("mine", "on")
        screen._announce_mine = True
        refresh = type(screen).refresh_results.__wrapped__  # type: ignore[attr-defined]
        pending = asyncio.ensure_future(refresh(screen))
        await asyncio.sleep(0)
        assert not screen._announce_mine
        pending.cancel()
        await asyncio.wait([pending])

        assert pending.cancelled()
        assert screen._announce_mine


def test_a_file_saved_in_the_first_format_still_loads(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"
    old = {"follow": [{"name": "Société Générale", "key": "stale"}], "locations": ["canada"]}
    path.write_text(json.dumps(old), encoding="utf-8")

    loaded = load_preferences(path)

    assert [company.folded for company in loaded.preferences.follow] == ["societe generale"]
    assert loaded.preferences.locations == (LocationBucket.CANADA,)
    assert loaded.problems == ()
