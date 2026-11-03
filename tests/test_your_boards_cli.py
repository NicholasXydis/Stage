import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner, Result

from stage.cli.app import app
from stage.cli.schedule import _ACTIONS
from stage.companies import (
    PERSONAL_LIMIT,
    load_companies,
    load_personal,
    update_personal,
    write_registry,
)
from stage.domain import Company, Job, LocationBucket, Platform, SourceOfRecord
from stage.http import RatePosture
from stage.paths import boards_path
from stage.storage.repository import SourceBatch
from stage.storage.sqlite_repo import SqliteRepository

GREENHOUSE = "https://boards-api.greenhouse.io/v1/boards"
WEEKLY = next(action.cli_arguments for action in _ACTIONS if action.key == "discover")
WEDNESDAY = next(action.cli_arguments for action in _ACTIONS if action.key == "verify")
NAMED_ONLY = tuple(argument for argument in WEEKLY if argument != "--adopt-unnamed")
PACKAGED = (
    Company(name="Coveo", platform=Platform.GREENHOUSE, slug="coveo"),
    Company(
        name="RBC",
        platform=Platform.WORKDAY,
        slug="rbc",
        workday_tenant="rbc",
        workday_site="External",
        workday_dc="wd3",
    ),
)


@pytest.fixture
def packaged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "packaged.yaml"
    write_registry(PACKAGED, target)
    monkeypatch.delenv("STAGE_REGISTRY", raising=False)
    monkeypatch.setattr("stage.companies.registry_path", lambda: target)
    monkeypatch.setattr(
        "stage.services.discover.resolve",
        lambda *_: RatePosture(concurrency=8, min_interval_s=0.0, max_requests_per_run=500),
    )
    return target


def _today() -> date:
    return datetime.now(UTC).date()


def _seed(db: Path, employers: Sequence[tuple[str, str]], jobs: Sequence[Job] = ()) -> Path:
    now = datetime.now(UTC)
    feed = [
        Job(
            id=f"simplify:feed:{index}",
            source="simplify",
            company=name,
            title_raw="Software Engineer Intern",
            title_normalized="software engineer intern",
            apply_url_raw=url,
            description="",
            first_seen=now,
            last_seen=now,
            location_raw="Montreal, QC",
            location=LocationBucket.MONTREAL,
        )
        for index, (name, url) in enumerate(employers)
    ]
    repository = SqliteRepository.connect(db)
    repository.apply_source_batch(
        SourceBatch(source="simplify", run_started_at=now, jobs=tuple(feed))
    )
    if jobs:
        repository.apply_source_batch(
            SourceBatch(source=jobs[0].source, run_started_at=now, jobs=tuple(jobs))
        )
    repository.close()
    return db


def _named_board(slug: str, name: str, jobs: int = 3) -> None:
    respx.get(f"{GREENHOUSE}/{slug}/jobs").mock(
        return_value=httpx.Response(
            200, json={"jobs": [{"id": n} for n in range(jobs)], "meta": {"total": jobs}}
        )
    )
    respx.get(f"{GREENHOUSE}/{slug}").mock(return_value=httpx.Response(200, json={"name": name}))


def _run(*args: str) -> Result:
    return CliRunner().invoke(app, list(args))


def _row(name: str, slug: str, **fields: object) -> Company:
    return Company(
        name=name,
        platform=Platform.GREENHOUSE,
        slug=slug,
        source_of_record=SourceOfRecord.DISCOVER,
        **fields,  # type: ignore[arg-type]
    )


def _mine(*rows: Company) -> None:
    update_personal(lambda _: (list(rows), None))


@respx.mock
def test_the_weekly_discovery_adopts_into_your_board_list(packaged: Path, db_path: Path) -> None:
    _seed(db_path, [("Zyxwv Robotics", "https://job-boards.greenhouse.io/zyxwvrobotics/jobs/1")])
    _named_board("zyxwvrobotics", "Zyxwv Robotics")
    before = packaged.read_bytes()

    result = _run(*WEEKLY, "--db", str(db_path))

    assert result.exit_code == 0, result.output
    assert "added to your board list" in result.output
    rows = load_personal().rows
    assert [(row.name, row.slug, row.enabled) for row in rows] == [
        ("Zyxwv Robotics", "zyxwvrobotics", True)
    ]
    assert (rows[0].notes or "").startswith(f"{_today()}: seen in feed postings")
    assert packaged.read_bytes() == before

    again = _run(*WEEKLY, "--db", str(db_path))
    assert again.exit_code == 0, again.output
    assert len(load_personal().rows) == 1


@respx.mock
@pytest.mark.parametrize("by", ["flag", "env"])
def test_an_explicit_registry_still_takes_the_adopted_rows(
    packaged: Path, db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, by: str
) -> None:
    _seed(db_path, [("Zyxwv Robotics", "https://job-boards.greenhouse.io/zyxwvrobotics/jobs/1")])
    _named_board("zyxwvrobotics", "Zyxwv Robotics")
    target = tmp_path / "maintainer"
    write_registry(PACKAGED, target)
    flags = ["--registry", str(target)] if by == "flag" else []
    if by == "env":
        monkeypatch.setenv("STAGE_REGISTRY", str(target))

    result = _run(
        "discover", "--unregistered", "--direct-only", "--apply", "--db", str(db_path), *flags
    )

    assert result.exit_code == 0, result.output
    assert [row.slug for row in load_companies(target)] == ["coveo", "rbc", "zyxwvrobotics"]
    assert not boards_path().exists()


@respx.mock
def test_a_full_board_list_adopts_nothing_and_asks_nothing(packaged: Path, db_path: Path) -> None:
    _seed(db_path, [("Zyxwv Robotics", "https://job-boards.greenhouse.io/zyxwvrobotics/jobs/1")])
    _mine(*(_row(f"Board {index}", f"board-{index}") for index in range(PERSONAL_LIMIT)))

    result = _run("discover", "--unregistered", "--direct-only", "--apply", "--db", str(db_path))

    assert result.exit_code == 0, result.output
    assert "full at 500 boards" in result.output
    assert not respx.calls
    assert len(load_personal().rows) == PERSONAL_LIMIT


@respx.mock
def test_the_last_free_place_goes_to_the_first_board(packaged: Path, db_path: Path) -> None:
    _seed(
        db_path,
        [
            ("Zyxwv Robotics", "https://job-boards.greenhouse.io/zyxwvrobotics/jobs/1"),
            ("Qwvut Labs", "https://job-boards.greenhouse.io/qwvutlabs/jobs/2"),
        ],
    )
    _named_board("zyxwvrobotics", "Zyxwv Robotics")
    _named_board("qwvutlabs", "Qwvut Labs")
    _mine(*(_row(f"Board {index}", f"board-{index}") for index in range(PERSONAL_LIMIT - 1)))

    result = _run("discover", "--unregistered", "--direct-only", "--apply", "--db", str(db_path))

    assert result.exit_code == 0, result.output
    assert "1 more were left out" in result.output
    assert len(load_personal().rows) == PERSONAL_LIMIT


@respx.mock
def test_an_unreadable_list_is_never_overwritten(packaged: Path, db_path: Path) -> None:
    _seed(db_path, [("Zyxwv Robotics", "https://job-boards.greenhouse.io/zyxwvrobotics/jobs/1")])
    target = boards_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("- name: [broken\n", encoding="utf-8")

    for flag in ("--unregistered", "--verify"):
        result = _run("discover", flag, "--apply", "--db", str(db_path))
        assert result.exit_code == 2, result.output
        assert "could not be read" in result.stderr

    assert target.read_text(encoding="utf-8") == "- name: [broken\n"
    assert not respx.calls


@respx.mock
def test_a_workday_apply_link_is_checked_and_kept_whole(packaged: Path, db_path: Path) -> None:
    link = "https://zyx.wd3.myworkdayjobs.com/en-US/Careers/job/Toronto/Intern_R1"
    _seed(db_path, [("Zyx Systems", link)])
    route = respx.post("https://zyx.wd3.myworkdayjobs.com/wday/cxs/zyx/Careers/jobs").mock(
        return_value=httpx.Response(200, json={"total": 12, "jobPostings": [{"title": "Intern"}]})
    )

    review = _run(*NAMED_ONLY, "--db", str(db_path))
    assert review.exit_code == 0, review.output
    assert "Zyx Systems (workday/zyx)" in review.output
    assert not boards_path().exists()

    adopted = _run(*WEEKLY, "--db", str(db_path))
    assert adopted.exit_code == 0, adopted.output
    row = load_personal().rows[0]
    assert (row.platform, row.workday_tenant, row.workday_site, row.workday_dc) == (
        Platform.WORKDAY,
        "zyx",
        "Careers",
        "wd3",
    )
    assert route.call_count == 2


@respx.mock
def test_the_wednesday_check_prunes_only_your_own_boards(packaged: Path, db_path: Path) -> None:
    today = _today()
    note = f"{today - timedelta(days=30)}: seen in feed postings"
    struck = f"{note}\n{today - timedelta(days=7)}: switched off, the board answered 404"
    _mine(
        _row("Alive", "alive", notes=note),
        _row("Gone", "gone", notes=note),
        _row("Twice", "twice", enabled=False, notes=struck),
        _row("Back", "back", enabled=False, notes=struck),
        _row("Barren", "barren", notes=f"{today - timedelta(days=400)}: seen in feed postings"),
        Company(
            name="Zyx",
            platform=Platform.WORKDAY,
            slug="zyx",
            workday_tenant="zyx",
            workday_site="Careers",
            workday_dc="wd3",
            notes=note,
        ),
    )
    _seed(db_path, [])
    for slug, name in (("alive", "Alive"), ("back", "Back"), ("coveo", "Coveo")):
        _named_board(slug, name)
    for slug in ("gone", "twice", "coveo-gone"):
        respx.get(f"{GREENHOUSE}/{slug}/jobs").mock(return_value=httpx.Response(404))
    respx.get(f"{GREENHOUSE}/barren/jobs").mock(return_value=httpx.Response(500))
    workday = respx.post("https://zyx.wd3.myworkdayjobs.com/wday/cxs/zyx/Careers/jobs").mock(
        return_value=httpx.Response(200, json={"total": 2, "jobPostings": [{"title": "Intern"}]})
    )
    shipped_workday = respx.post(url__startswith="https://rbc.wd3.myworkdayjobs.com/")
    gone = Company(name="Coveo Gone", platform=Platform.GREENHOUSE, slug="coveo-gone")
    write_registry((*PACKAGED, gone), packaged)
    before = packaged.read_bytes()

    result = _run(*WEDNESDAY, "--db", str(db_path))

    assert result.exit_code == 0, result.output
    rows = {row.name: row for row in load_personal().rows}
    assert sorted(rows) == ["Alive", "Back", "Gone", "Zyx"]
    assert rows["Alive"].enabled and rows["Alive"].last_verified == today
    assert not rows["Gone"].enabled
    assert rows["Back"].enabled and rows["Back"].notes == note
    assert rows["Zyx"].last_verified == today
    assert workday.call_count == 1
    assert not shipped_workday.called
    assert packaged.read_bytes() == before
    assert "1 shipped board answered as gone" in result.output
    assert "Twice: gone two weeks running" in result.output
    assert "Barren: no internship in 365 days" in result.output


@respx.mock
def test_doctor_counts_your_boards(packaged: Path, db_path: Path) -> None:
    today = _today()
    _mine(
        _row("New", "new", notes=f"{today}: seen in feed postings"),
        _row("Week", "week", notes=f"{today - timedelta(days=6)}: seen in feed postings"),
        _row("Old", "old", notes=f"{today - timedelta(days=40)}: seen in feed postings"),
        _row("Off", "off", enabled=False),
        _row("Coveo Too", "coveo"),
    )
    _seed(db_path, [])

    shown = _run("doctor", "--db", str(db_path))
    assert "Your boards 3 boards polled, 2 added in the last 7 days" in shown.output
    assert "1 switched off, 1 now shipped with Stage" in shown.output

    payload = json.loads(_run("doctor", "--db", str(db_path), "--json").stdout)
    assert payload["your_boards"]["boards"] == 5
    assert payload["your_boards"]["added_last_7_days"] == 2
    assert payload["your_boards"]["problem"] is None


@respx.mock
def test_doctor_says_when_your_list_cannot_be_read(packaged: Path, db_path: Path) -> None:
    target = boards_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("- name: [broken\n", encoding="utf-8")
    _seed(db_path, [])

    shown = _run("doctor", "--db", str(db_path))
    assert "Your boards could not be read, so Stage ignores them" in shown.output
    payload = json.loads(_run("doctor", "--db", str(db_path), "--json").stdout)
    assert "not valid YAML" in payload["your_boards"]["problem"]

    synced = _run("sync", "--dry-run", "--source", "greenhouse", "--db", str(db_path))
    assert "Your board list is ignored until it can be read" in synced.stderr
    assert target.read_text(encoding="utf-8") == "- name: [broken\n"


def test_coverage_treats_your_boards_as_registered(packaged: Path, db_path: Path) -> None:
    _seed(db_path, [("Zyxwv Robotics", "https://job-boards.greenhouse.io/zyxwvrobotics/jobs/1")])
    before = _run("coverage", "--unregistered", "--db", str(db_path))
    assert "Zyxwv Robotics" in before.output

    _mine(_row("Zyxwv Robotics", "zyxwvrobotics"))
    after = _run("coverage", "--unregistered", "--db", str(db_path))
    assert after.exit_code == 0, after.output
    assert "3 enabled rows" in after.output
    assert "absent from the registry" not in after.output


@respx.mock
def test_an_explicit_registry_takes_the_verify_results_instead(
    packaged: Path, db_path: Path, tmp_path: Path
) -> None:
    _mine(_row("Mine", "mine", notes=f"{_today()}: seen in feed postings"))
    before = boards_path().read_bytes()
    target = tmp_path / "maintainer.yaml"
    write_registry(
        (PACKAGED[0], Company(name="Renamed", platform=Platform.GREENHOUSE, slug="renamed")), target
    )
    _named_board("coveo", "Coveo")
    _named_board("renamed", "Somebody Else")
    _seed(db_path, [])

    result = _run(*WEDNESDAY, "--registry", str(target), "--db", str(db_path))

    assert result.exit_code == 0, result.output
    assert "1 disabled" in result.output
    assert {row.name: row.enabled for row in load_companies(target)} == {
        "Coveo": True,
        "Renamed": False,
    }
    assert boards_path().read_bytes() == before


@respx.mock
def test_the_wednesday_check_with_no_boards_of_your_own_writes_nothing(
    packaged: Path, db_path: Path
) -> None:
    _named_board("coveo", "Coveo")
    before = packaged.read_bytes()

    result = _run(*WEDNESDAY, "--db", str(db_path))

    assert result.exit_code == 0, result.output
    assert "No boards of your own to update" in result.output
    assert packaged.read_bytes() == before
    assert not boards_path().exists()


@respx.mock
def test_verify_into_a_registry_keeps_deliberate_switch_offs(
    packaged: Path, db_path: Path, tmp_path: Path
) -> None:
    struck = f"{_today()}: switched off by stage discover --verify, the board answered 404"
    rows = (
        Company(
            name="Coveo", platform=Platform.GREENHOUSE, slug="coveo", enabled=False, notes=struck
        ),
        Company(
            name="Google",
            platform=Platform.GREENHOUSE,
            slug="google",
            enabled=False,
            notes="robots",
        ),
        Company(name="Gone", platform=Platform.GREENHOUSE, slug="gone"),
    )
    target = tmp_path / "maintainer.yaml"
    write_registry(rows, target)
    _named_board("coveo", "Coveo")
    _named_board("google", "Google")
    respx.get(f"{GREENHOUSE}/gone/jobs").mock(return_value=httpx.Response(404))
    _seed(db_path, [])

    result = _run(*WEDNESDAY, "--registry", str(target), "--db", str(db_path))

    assert result.exit_code == 0, result.output
    after = {row.name: row for row in load_companies(target)}
    assert after["Coveo"].enabled and after["Coveo"].notes is None
    assert not after["Google"].enabled and after["Google"].notes == "robots"
    assert not after["Gone"].enabled and "answered 404" in (after["Gone"].notes or "")
    assert "left switched off, since their notes give another reason: Google" in result.output
    assert "gone   Gone" in result.output and "Slug guessing" not in result.output


@respx.mock
def test_verify_without_a_name_gate_succeeds_and_gives_no_probe_hints(
    packaged: Path, db_path: Path, tmp_path: Path
) -> None:
    target = tmp_path / "maintainer.yaml"
    write_registry((Company(name="Acme", platform=Platform.LEVER, slug="acme"),), target)
    respx.get(url__startswith="https://api.lever.co/v0/postings/acme").mock(
        return_value=httpx.Response(200, json=[{"id": "1"}])
    )

    result = _run("discover", "--verify", "--registry", str(target), "--db", str(db_path))

    assert result.exit_code == 0, result.output
    assert "publishes no board name" in result.output
    for hint in ("Slug guessing", "Verify before adding", "Nothing resolved"):
        assert hint not in result.output


@respx.mock
def test_discover_says_when_a_board_or_employer_is_already_registered(
    packaged: Path, db_path: Path
) -> None:
    by_url = _run("discover", "--url", "https://boards.greenhouse.io/coveo", "--name", "Coveo")
    by_name = _run("discover", "coveo", "--db", str(db_path))

    assert by_url.exit_code == 0 and by_name.exit_code == 0, by_name.output
    assert "Already registered as Coveo (polled)" in by_url.output
    assert "coveo is already registered as greenhouse/coveo (polled)" in by_name.output
    assert "--apply adds" not in by_url.output + by_name.output
    assert not respx.calls


@respx.mock
def test_discover_offers_your_board_list_and_never_the_source_tree(
    packaged: Path, db_path: Path
) -> None:
    robots = respx.get("https://api.ashbyhq.com/robots.txt").mock(return_value=httpx.Response(404))
    link = ["discover", "--url", "https://jobs.ashbyhq.com/zyxwv", "--name", "Zyxwv"]

    offered = _run(*link)
    applied = _run(*link, "--apply")
    again = _run(*link, "--apply")

    assert "--apply adds this to your board list at" in offered.output
    assert "src/stage" not in offered.output + applied.output
    assert applied.exit_code == 0 and "Added Zyxwv to your board list" in applied.output
    assert [(row.name, row.slug, row.enabled) for row in load_personal().rows] == [
        ("Zyxwv", "zyxwv", True)
    ]
    assert "Already registered as Zyxwv" in again.output
    assert len(load_personal().rows) == 1
    assert robots.call_count == 1, "only the --apply that adds a board reads robots.txt"


@respx.mock
def test_discover_by_name_applies_a_board_that_names_itself(packaged: Path, db_path: Path) -> None:
    _named_board("zyxwvrobotics", "Zyxwv Robotics")
    respx.get(url__startswith=GREENHOUSE).mock(return_value=httpx.Response(404))

    result = _run(
        "discover", "Zyxwv Robotics", "--platform", "greenhouse", "--apply", "--db", str(db_path)
    )

    assert result.exit_code == 0, result.output
    rows = load_personal().rows
    assert [(row.name, row.slug, row.last_verified) for row in rows] == [
        ("Zyxwv Robotics", "zyxwvrobotics", _today())
    ]
    assert "src/stage" not in result.output


def test_discover_refuses_a_url_it_cannot_add_and_apply_without_a_name(
    packaged: Path,
) -> None:
    nameless = _run("discover", "--url", "https://jobs.ashbyhq.com/zyxwv", "--apply")
    siteless = _run(
        "discover", "--url", "https://rbc.wd3.myworkdayjobs.com/", "--name", "RBC2", "--apply"
    )

    assert nameless.exit_code == 2 and "--apply with --url needs --name" in nameless.stderr
    assert siteless.exit_code == 1 and "Not addable" in siteless.output
    assert not boards_path().exists()


@pytest.mark.parametrize(
    "args",
    [
        ("--url", "https://jobs.ashbyhq.com/zyxwv", "--name", "", "--apply"),
        ("--url", "https://jobs.ashbyhq.com/zyxwv", "--name", "   ", "--apply"),
        (
            "--url",
            "  ",
        ),
        ("", "--apply"),
        ("Acme", " ", "--apply"),
    ],
)
def test_discover_refuses_a_blank_name_or_link_before_adding_anything(
    packaged: Path, args: tuple[str, ...]
) -> None:
    result = _run("discover", *args)

    assert result.exit_code == 2, result.output
    assert "needs" in result.stderr
    assert not boards_path().exists()


def test_company_without_verify_explains_itself() -> None:
    result = _run("discover", "--company", "Coveo")
    empty = _run("discover", "--verify", "--company", "")

    assert result.exit_code == 2
    assert "--company picks the registry rows that --verify rechecks" in result.stderr
    assert empty.exit_code == 2 and "--company needs a value" in empty.stderr
