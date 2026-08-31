import re

from typer.testing import CliRunner

from stage.cli.app import app

ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

COMMANDS = (
    "sync",
    "list",
    "search",
    "show",
    "open",
    "export",
    "coverage",
    "purge",
    "rescreen",
    "doctor",
    "stats",
    "canary",
    "sources",
    "quarantine",
    "discover",
    "help",
)


def test_root_help_lists_every_command() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in COMMANDS:
        assert command in result.stdout


def test_every_command_has_help() -> None:
    runner = CliRunner()

    for command in COMMANDS:
        result = runner.invoke(app, [command, "--help"])

        assert result.exit_code == 0
        assert "Usage:" in result.stdout


def test_help_shows_common_workflows() -> None:
    result = CliRunner().invoke(app, ["help"])

    assert result.exit_code == 0
    assert "Start here:" in result.stdout
    assert "Filters" in result.stdout
    assert "Health and maintenance:" in result.stdout
    assert "stage search" in result.stdout
    assert "stage help COMMAND" in result.stdout


def test_the_guide_points_at_every_way_in() -> None:
    result = CliRunner().invoke(app, ["help"])
    output = ANSI_ESCAPE.sub("", result.stdout)

    assert "stage tui" in output
    assert "stage discover --url" in output
    assert "--install-completion" in output
    for flag in ("--role", "--location", "--last", "--lang", "--term", "--source", "--company"):
        assert flag in output


def test_help_shows_one_command() -> None:
    result = CliRunner().invoke(app, ["help", "sync"])
    output = ANSI_ESCAPE.sub("", result.stdout)

    assert result.exit_code == 0
    assert "Usage:" in output
    assert "--dry-run" in output


def test_help_rejects_unknown_command() -> None:
    result = CliRunner().invoke(app, ["help", "missing"])

    assert result.exit_code == 2
    assert "Unknown command" in result.stderr


def test_version_flag_reports_the_installed_version() -> None:
    from stage import __version__

    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_version_short_flag_matches_the_long_one() -> None:
    runner = CliRunner()

    assert runner.invoke(app, ["-V"]).stdout == runner.invoke(app, ["--version"]).stdout


def test_completion_can_be_installed_from_the_root_command() -> None:
    result = CliRunner().invoke(app, ["--help"])
    output = ANSI_ESCAPE.sub("", result.stdout)

    assert result.exit_code == 0
    assert "--install-completion" in output


def test_the_readme_gets_a_new_user_running() -> None:
    from pathlib import Path

    readme = Path(__file__).resolve().parent.parent / "README.md"
    if not readme.is_file():
        return
    body = readme.read_text(encoding="utf-8")

    for essential in (
        "uv tool install stage-cli",
        "stage sync",
        "stage tui",
        "stage list",
        "stage search",
        "stage --install-completion",
        "stage help",
    ):
        assert essential in body, essential


def test_the_readme_only_promises_filters_that_exist() -> None:
    import re
    from pathlib import Path

    readme = Path(__file__).resolve().parent.parent / "README.md"
    if not readme.is_file():
        return
    body = readme.read_text(encoding="utf-8")
    listed = set(re.findall(r"`(--[a-z][a-z-]+)`", body))
    output = ANSI_ESCAPE.sub("", CliRunner().invoke(app, ["list", "--help"]).stdout)
    real = set(re.findall(r"--[a-z][a-z-]+", output))
    filters = {flag for flag in listed if flag in {*real, "--format", "--last", "--all"}}

    assert filters, "the README should show at least one real filter"
    assert not (filters - real - {"--format"})


def test_commands_are_grouped_by_how_often_they_are_used() -> None:
    result = CliRunner().invoke(app, ["--help"])
    output = ANSI_ESCAPE.sub("", result.stdout)
    panels = [
        name
        for name in ("Everyday", "Keeping current", "Registry and maintenance", "Health")
        if name in output
    ]

    assert panels == ["Everyday", "Keeping current", "Registry and maintenance", "Health"]


def test_browsing_commands_sit_in_the_everyday_panel() -> None:
    result = CliRunner().invoke(app, ["--help"])
    output = ANSI_ESCAPE.sub("", result.stdout)
    everyday = output.split("Everyday")[1].split("╰")[0]

    for command in ("tui", "list", "search", "show", "open", "export"):
        assert command in everyday


def test_maintenance_commands_are_not_in_the_everyday_panel() -> None:
    result = CliRunner().invoke(app, ["--help"])
    output = ANSI_ESCAPE.sub("", result.stdout)
    everyday = output.split("Everyday")[1].split("╰")[0]

    for command in ("discover", "classify", "canary", "purge", "rescreen"):
        assert command not in everyday


def test_every_command_belongs_to_a_panel() -> None:
    from stage.cli.options import app as typer_app

    unpanelled = [
        info.name or (info.callback.__name__ if info.callback else "?")
        for info in typer_app.registered_commands
        if not info.rich_help_panel
    ]

    assert not unpanelled


def test_the_guide_stays_a_starting_point_not_a_manual() -> None:
    result = CliRunner().invoke(app, ["help"])
    output = ANSI_ESCAPE.sub("", result.stdout)
    guide = output.split("Start here:")[-1]

    assert "stage canary" not in guide
    assert "--verbose on doctor" not in guide
    assert "--all on coverage" not in guide


def test_the_completion_hint_stays_on_one_line() -> None:
    result = CliRunner().invoke(app, ["--help"])
    output = ANSI_ESCAPE.sub("", result.stdout)
    line = next(row for row in output.split("\n") if "--show-completion" in row)

    assert "copy it" not in line
    assert "customize" not in output


def test_no_help_screen_shows_a_bare_type_name() -> None:
    import re

    runner = CliRunner()
    generic = []
    for command in (*COMMANDS, "tui", "canary", "classify", "discover", "quarantine"):
        output = ANSI_ESCAPE.sub("", runner.invoke(app, [command, "--help"]).stdout)
        for match in re.finditer(r"│\s+(--[a-z][\w-]*)\s+(<[a-z ]+>|\[[0-9]+<=)", output):
            generic.append(f"{command} {match.group(1)}")

    assert not generic


def test_no_help_screen_shows_a_type_name_or_range_notation() -> None:
    import re

    runner = CliRunner()
    noise = []
    for command in (*COMMANDS, "tui", "canary", "classify", "discover", "quarantine", "open"):
        output = ANSI_ESCAPE.sub("", runner.invoke(app, [command, "--help"]).stdout)
        for match in re.finditer(r"<(str|int|path|text|int range)>|<>|\d+<=[a-z]<=", output):
            noise.append(f"{command}: {match.group(0)}")

    assert not noise


def test_a_number_out_of_range_is_explained_in_words() -> None:
    result = CliRunner().invoke(app, ["list", "--limit", "0"])
    output = ANSI_ESCAPE.sub("", result.stdout + result.stderr)

    assert result.exit_code == 2
    assert "out of range" in output
    assert "<=x<=" not in output


def test_the_word_type_parses_exactly_like_a_plain_string() -> None:
    from stage.cli.options import WORD

    for probe in ("python", "c++", "", "  spaced  ", "--dashy", "ashby:acme:1"):
        assert WORD.convert(probe, None, None) == probe


def test_the_count_type_still_enforces_its_bounds() -> None:
    import pytest as _pytest
    import typer

    from stage.cli.options import _count

    counter = _count(1, 10)

    assert counter.convert("5", None, None) == 5
    with _pytest.raises((typer.BadParameter, Exception)):
        counter.convert("0", None, None)


def test_every_guide_entry_fits_one_aligned_row() -> None:
    result = CliRunner().invoke(app, ["help"])
    output = ANSI_ESCAPE.sub("", result.stdout)
    guide = output.split("Start here:")[-1]
    rows = [line for line in guide.split("\n") if line.startswith("  ") and line.strip()]

    for row in rows:
        assert not row.startswith("      "), row
        assert len(row) <= 80, row


def test_the_guide_names_a_command_the_same_way_its_help_does() -> None:
    import re

    runner = CliRunner()
    guide = ANSI_ESCAPE.sub("", runner.invoke(app, ["help"]).stdout)
    mismatched = []
    for command in ("discover", "classify", "help"):
        page = ANSI_ESCAPE.sub("", runner.invoke(app, [command, "--help"]).stdout)
        found = re.search(r"│\s+\*?\s*([A-Z][A-Z.]*)\s", page)
        if not found:
            continue
        placeholder = found.group(1).rstrip(".")
        used = re.search(rf"stage {command} ([A-Z]+)", guide)
        if used and used.group(1) != placeholder:
            mismatched.append(f"{command}: guide says {used.group(1)}, help says {placeholder}")

    assert not mismatched


def test_the_guide_opens_with_the_same_banner_the_tui_shows() -> None:
    from stage.banner import WIDE
    from stage.tui.screens.splash import BANNER

    result = CliRunner().invoke(app, ["help"])
    output = ANSI_ESCAPE.sub("", result.stdout)

    assert BANNER is WIDE
    for line in WIDE.strip("\n").split("\n"):
        assert line.rstrip() in output


def test_the_banner_is_safe_on_every_terminal() -> None:
    import unicodedata

    from stage.banner import COMPACT, WIDE

    for art in (WIDE, COMPACT):
        for char in art:
            assert ord(char) < 128, repr(char)
            assert unicodedata.east_asian_width(char) not in {"A", "W", "F"}


def test_a_narrow_terminal_gets_the_compact_banner() -> None:
    from stage.banner import COMPACT, MIN_WIDE, banner

    narrow = banner(MIN_WIDE - 1)

    assert narrow.split("\n")[0].rstrip() == COMPACT.strip("\n").split("\n")[0].rstrip()
    assert max(len(line) for line in narrow.split("\n")) < MIN_WIDE


def test_running_stage_with_no_arguments_shows_the_command_list() -> None:
    from stage.banner import WIDE

    result = CliRunner().invoke(app, [])
    output = ANSI_ESCAPE.sub("", result.stdout)

    assert result.exit_code == 0
    assert WIDE.strip("\n").split("\n")[-1].rstrip() in output
    assert "Usage: " in output


def test_the_banner_greets_every_way_in() -> None:
    from stage.banner import WIDE

    signature = WIDE.strip("\n").split("\n")[-1].rstrip()
    runner = CliRunner()
    for argv in ([], ["--help"], ["help"]):
        output = ANSI_ESCAPE.sub("", runner.invoke(app, argv).stdout)
        assert signature in output, argv


def test_one_command_help_stays_free_of_the_banner() -> None:
    from stage.banner import WIDE

    signature = WIDE.strip("\n").split("\n")[-1].rstrip()
    runner = CliRunner()
    for argv in (["list", "--help"], ["help", "list"], ["schedule", "--help"]):
        output = ANSI_ESCAPE.sub("", runner.invoke(app, argv).stdout)
        assert signature not in output, argv


def test_the_readme_shows_the_same_banner_as_the_cli() -> None:
    from pathlib import Path

    from stage.banner import WIDE

    readme = Path(__file__).resolve().parent.parent / "README.md"
    if not readme.is_file():
        return
    body = readme.read_text(encoding="utf-8")

    for line in WIDE.strip("\n").split("\n"):
        assert line.rstrip() in body, line


def test_no_option_summary_ends_in_a_full_stop() -> None:

    runner = CliRunner()
    for argv in (["--help"], ["list", "--help"], ["sync", "--help"]):
        output = ANSI_ESCAPE.sub("", runner.invoke(app, argv).stdout)
        for line in output.split("\n"):
            if not line.startswith("│ --"):
                continue
            assert not line.replace("│", "").rstrip().endswith("."), line


def test_a_browsing_command_is_not_described_as_searching() -> None:
    runner = CliRunner()
    for command in ("list", "export", "quarantine"):
        output = ANSI_ESCAPE.sub("", runner.invoke(app, [command, "--help"]).stdout)
        collapsed = " ".join(output.replace("│", " ").split())
        assert "searches every row" not in collapsed, command
        assert "search titles" not in collapsed, command


def test_the_window_escape_hatch_is_documented_everywhere() -> None:
    from pathlib import Path

    output = ANSI_ESCAPE.sub("", CliRunner().invoke(app, ["list", "--help"]).stdout)
    assert "0 for no limit" in " ".join(output.replace("│", " ").split())

    guide = ANSI_ESCAPE.sub("", CliRunner().invoke(app, ["help"]).stdout)
    assert "--last 0" in guide

    readme = Path(__file__).resolve().parent.parent / "README.md"
    if readme.is_file():
        assert "--last 0" in readme.read_text(encoding="utf-8")
