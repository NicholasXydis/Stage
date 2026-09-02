import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "update_badges.py"

PYTHON_BADGE = "![python](https://img.shields.io/static/v1?label=python&message=3.12&logo=python)"
TESTS_BADGE = "![tests](https://img.shields.io/static/v1?label=tests&message=2%2C294&color=44bb00)"
COVERAGE_BADGE = (
    "![coverage](https://img.shields.io/static/v1?label=coverage&message=89%25&color=44bb00)"
)
SVG_LABEL = '<text x="330" y="86" fill="#F0F6FC" font-size="14">2,294 tests, 89%</text>'


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("update_badges", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["update_badges"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def badges() -> ModuleType:
    return _load()


@pytest.fixture
def readme(tmp_path: Path, badges: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "README.md"
    path.write_text(f"{PYTHON_BADGE} {TESTS_BADGE} {COVERAGE_BADGE}\n", encoding="utf-8")
    monkeypatch.setattr(badges, "README", path)
    return path


@pytest.fixture
def ci_flow(tmp_path: Path, badges: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "ci-flow.svg"
    path.write_text(f"<svg>{SVG_LABEL}</svg>", encoding="utf-8")
    monkeypatch.setattr(badges, "CI_FLOW", path)
    return path


def _reports(tmp_path: Path, parallel: str, serial: str) -> tuple[str, str]:
    first = tmp_path / "pytest.txt"
    second = tmp_path / "pytest-serial.txt"
    first.write_text(parallel, encoding="utf-8")
    second.write_text(serial, encoding="utf-8")
    return str(first), str(second)


def test_the_coverage_total_is_read_from_the_report(badges: ModuleType) -> None:
    assert badges._coverage("TOTAL   12184   1137   3282   470   89%") == "89%"


def test_full_and_empty_coverage_both_parse(badges: ModuleType) -> None:
    assert badges._coverage("TOTAL 10 0 100%") == "100%"
    assert badges._coverage("TOTAL 10 10 0%") == "0%"


def test_a_report_with_no_total_line_is_refused(badges: ModuleType) -> None:
    with pytest.raises(SystemExit):
        badges._coverage("99 passed in 3s\n")


def test_a_percentage_that_is_not_on_a_total_line_is_ignored(badges: ModuleType) -> None:
    with pytest.raises(SystemExit):
        badges._coverage("src/stage/app.py   10   1   91%\n")


def test_the_two_test_counts_are_summed(badges: ModuleType) -> None:
    assert badges._tests("2279 passed, 15 skipped in 14.53s", "15 passed in 5.06s") == "2,294"


def test_deselected_tests_are_not_counted_as_passed(badges: ModuleType) -> None:
    assert badges._tests("2279 passed, 15 skipped", "15 passed, 2279 deselected") == "2,294"


def test_a_thousands_separator_is_added(badges: ModuleType) -> None:
    assert badges._tests("2000 passed", "1500 passed") == "3,500"


def test_a_count_below_a_thousand_carries_no_separator(badges: ModuleType) -> None:
    assert badges._tests("10 passed", "5 passed") == "15"


def test_a_report_with_no_passing_count_is_refused(badges: ModuleType) -> None:
    with pytest.raises(SystemExit):
        badges._tests("collected 0 items", "15 passed")

    with pytest.raises(SystemExit):
        badges._tests("15 passed", "no tests ran")


def test_a_badge_message_is_replaced(badges: ModuleType) -> None:
    swapped = badges._swap(TESTS_BADGE, "tests", "9%2C999")

    assert "message=9%2C999&color=44bb00" in swapped


def test_a_badge_that_ends_in_a_paren_keeps_what_follows_it(badges: ModuleType) -> None:
    row = f"![tests](https://img.shields.io/static/v1?label=tests&message=OLD) {COVERAGE_BADGE}"

    swapped = badges._swap(row, "tests", "7")

    assert "label=tests&message=7)" in swapped
    assert swapped.endswith(COVERAGE_BADGE)


def test_a_swap_never_runs_past_the_end_of_its_own_badge(badges: ModuleType) -> None:
    row = f"![tests](https://img.shields.io/static/v1?label=tests&message=OLD) {COVERAGE_BADGE}"
    stale = r"(!\[tests\]\(https://img\.shields\.io/static/v1\?label=tests&message=)[^&]*(&)"
    ruined, _ = re.subn(stale, lambda m: f"{m.group(1)}7{m.group(2)}", row)

    assert "label=tests&message=7&message=89%25" in ruined
    assert COVERAGE_BADGE not in ruined

    assert badges._swap(row, "tests", "7").endswith(COVERAGE_BADGE)


def test_a_missing_badge_is_refused(badges: ModuleType) -> None:
    with pytest.raises(SystemExit):
        badges._swap("no badges here", "tests", "1")


def test_only_the_named_badge_moves(badges: ModuleType) -> None:
    swapped = badges._swap(f"{TESTS_BADGE} {COVERAGE_BADGE}", "tests", "1")

    assert "label=coverage&message=89%25" in swapped


def test_every_copy_of_a_badge_is_updated(badges: ModuleType) -> None:
    swapped = badges._swap(f"{TESTS_BADGE}\n{TESTS_BADGE}", "tests", "7")

    assert swapped.count("message=7&color=44bb00") == 2


def test_the_diagram_label_is_restated(badges: ModuleType, ci_flow: Path) -> None:
    badges._restate_ci_flow("3,000", "77%")

    assert ">3,000 tests, 77%<" in ci_flow.read_text(encoding="utf-8")


def test_the_diagram_keeps_every_other_number(badges: ModuleType, ci_flow: Path) -> None:
    ci_flow.write_text(f'<rect x="330" y="86"/>{SVG_LABEL}<text>1,450 boards</text>', "utf-8")

    badges._restate_ci_flow("9", "1%")

    body = ci_flow.read_text(encoding="utf-8")
    assert '<rect x="330" y="86"/>' in body
    assert "<text>1,450 boards</text>" in body


def test_an_absent_diagram_is_not_an_error(
    badges: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "absent.svg"
    monkeypatch.setattr(badges, "CI_FLOW", missing)

    badges._restate_ci_flow("1,000", "50%")

    assert not missing.exists()


def test_a_diagram_without_the_label_is_refused(badges: ModuleType, ci_flow: Path) -> None:
    ci_flow.write_text("<svg><text>no counts</text></svg>", encoding="utf-8")

    with pytest.raises(SystemExit):
        badges._restate_ci_flow("1,000", "50%")


def test_a_run_rewrites_both_surfaces(
    badges: ModuleType,
    readme: Path,
    ci_flow: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = _reports(tmp_path, "3000 passed", "111 passed\nTOTAL 100 10 77%")
    monkeypatch.setattr(sys, "argv", ["update_badges.py", *reports])

    assert badges.main() == 0

    body = readme.read_text(encoding="utf-8")
    assert "label=tests&message=3%2C111&color=44bb00" in body
    assert "label=coverage&message=77%25&color=44bb00" in body
    assert ">3,111 tests, 77%<" in ci_flow.read_text(encoding="utf-8")


def test_the_readme_encodes_what_the_diagram_states_plainly(
    badges: ModuleType,
    readme: Path,
    ci_flow: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = _reports(tmp_path, "2279 passed", "15 passed\nTOTAL 100 10 89%")
    monkeypatch.setattr(sys, "argv", ["update_badges.py", *reports])

    badges.main()

    assert "message=2%2C294" in readme.read_text(encoding="utf-8")
    assert ">2,294 tests, 89%<" in ci_flow.read_text(encoding="utf-8")


def test_a_second_run_over_its_own_output_changes_nothing(
    badges: ModuleType,
    readme: Path,
    ci_flow: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = _reports(tmp_path, "3000 passed", "111 passed\nTOTAL 100 10 77%")
    monkeypatch.setattr(sys, "argv", ["update_badges.py", *reports])
    badges.main()
    once = (readme.read_text(encoding="utf-8"), ci_flow.read_text(encoding="utf-8"))

    badges.main()

    assert (readme.read_text(encoding="utf-8"), ci_flow.read_text(encoding="utf-8")) == once


def test_a_run_leaves_the_badges_it_does_not_own_alone(
    badges: ModuleType,
    readme: Path,
    ci_flow: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = _reports(tmp_path, "1 passed", "1 passed\nTOTAL 1 0 100%")
    monkeypatch.setattr(sys, "argv", ["update_badges.py", *reports])

    badges.main()

    assert PYTHON_BADGE in readme.read_text(encoding="utf-8")


def test_a_missing_report_file_is_refused(
    badges: ModuleType, readme: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["update_badges.py", str(tmp_path / "gone.txt"), "x"])

    with pytest.raises(FileNotFoundError):
        badges.main()


def test_the_shipped_readme_and_diagram_agree(badges: ModuleType) -> None:
    body = (ROOT / "README.md").read_text(encoding="utf-8")
    tests = re.search(r"label=tests&message=([^&]+)&", body)
    coverage = re.search(r"label=coverage&message=([^&]+)&", body)
    assert tests is not None
    assert coverage is not None

    counted = tests.group(1).replace("%2C", ",")
    covered = coverage.group(1).replace("%25", "%")
    flow = badges.CI_FLOW.read_text(encoding="utf-8")

    assert f">{counted} tests, {covered}<" in flow
