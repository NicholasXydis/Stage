from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from stage.companies import RegistryError, load_companies
from stage.domain import Job, LocationBucket, web_url
from stage.normalize.urls import canonical_apply_url, is_tracker_url
from stage.services.sync import normalize_batch
from stage.sources import get_feeds, load_builtins
from stage.sources.base import convert_rows
from stage.sources.platforms import identify_url
from stage.sources.simplify import SimplifyFeed

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)

UNPARSEABLE = ("http://[", "http://[oops]", "https://[::1", "http://[]:80x")


def _job(url: str, identifier: str = "greenhouse:acme:1") -> Job:
    return Job(
        id=identifier,
        source="greenhouse",
        company="Acme",
        title_raw="Software Engineer Intern",
        title_normalized="software engineer intern",
        apply_url_raw=url,
        description="",
        first_seen=NOW,
        last_seen=NOW,
        location_raw="Montréal, QC",
        location=LocationBucket.MONTREAL,
    )


@pytest.mark.parametrize("url", UNPARSEABLE)
def test_no_url_helper_raises_on_an_unparseable_address(url: str) -> None:
    assert web_url(url) is None
    assert canonical_apply_url(url) == ""
    assert is_tracker_url(url) is False
    assert identify_url(url) is None


def test_one_unparseable_apply_url_does_not_cost_the_whole_source_batch() -> None:
    kept, rejected = normalize_batch(
        [_job("http://["), _job("https://ok.example", "greenhouse:acme:2")]
    )
    assert len(kept) + len(rejected) == 2
    assert {job.id for job in kept} >= {"greenhouse:acme:2"}


def test_an_unparseable_url_is_never_authorised_as_a_registry_host() -> None:
    from stage.http import HostNotAllowedError, HttpClient

    client = HttpClient(allowed_hosts=frozenset({"ok.example"}))
    with pytest.raises(HostNotAllowedError):
        client._authorize("http://[")


FEED_BASE = {
    "id": "1",
    "company_name": "Acme",
    "title": "SWE Intern",
    "locations": ["Montreal"],
    "url": "https://boards.example.test/1",
    "active": True,
    "is_visible": True,
    "date_posted": 1700000000,
}

UNCONVERTIBLE = (
    ("a timestamp beyond the platform range", {"date_posted": 10**100}),
    ("a timestamp far in the past", {"date_posted": -(10**18)}),
)

UNUSABLE = (
    ("an empty company name", {"company_name": ""}),
    ("an empty title", {"title": ""}),
    ("an empty id", {"id": ""}),
    ("a whitespace-only title", {"title": "   "}),
)


def _simplify() -> SimplifyFeed:
    load_builtins()
    feed = get_feeds()["simplify"]
    assert isinstance(feed, SimplifyFeed)
    return feed


@pytest.mark.parametrize("label,override", UNCONVERTIBLE)
def test_a_schema_valid_row_that_cannot_convert_costs_only_itself(
    label: str, override: dict[str, object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STAGE_CAPTURE_DIR", str(tmp_path))
    feed = _simplify()

    listings, dropped = feed._validate([dict(FEED_BASE, **override), dict(FEED_BASE, id="2")], NOW)
    assert dropped == 0, f"{label} is schema-valid, so validation must not be what catches it"

    jobs, unconvertible = convert_rows(
        lambda listing: feed._to_job(listing, NOW),
        listings,
        source="simplify",
        slug="2027",
    )
    assert (len(jobs), unconvertible) == (1, 1), label
    assert list(tmp_path.glob("simplify-posting-*.json")), "the dropped row must be captured"


@pytest.mark.parametrize("label,override", UNUSABLE)
def test_a_row_with_no_usable_identity_is_dropped_at_validation(
    label: str, override: dict[str, object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STAGE_CAPTURE_DIR", str(tmp_path))
    feed = _simplify()

    listings, dropped = feed._validate([dict(FEED_BASE, **override), dict(FEED_BASE, id="2")], NOW)
    assert (len(listings), dropped) == (1, 1), label
    assert list(tmp_path.glob("simplify-posting-*.json")), "the dropped row must be captured"


def _registry(tmp_path: Path, row: Mapping[str, object]) -> Path:
    target = tmp_path / "companies.yaml"
    target.write_text(yaml.safe_dump([row]), encoding="utf-8")
    return target


@pytest.mark.parametrize("field", ["enabled", "name_gate_exempt"])
@pytest.mark.parametrize("written", ["false", "no", "off", 0, 1, "true"])
def test_only_a_real_yaml_boolean_sets_a_registry_flag(
    tmp_path: Path, field: str, written: object
) -> None:
    row = {"name": "A", "platform": "greenhouse", "slug": "a", field: written}
    with pytest.raises(RegistryError, match="must be an unquoted true or false"):
        load_companies(_registry(tmp_path, row))


@pytest.mark.parametrize("field", ["enabled", "name_gate_exempt"])
def test_a_real_boolean_is_accepted_and_omission_keeps_the_default(
    tmp_path: Path, field: str
) -> None:
    base: dict[str, object] = {"name": "A", "platform": "greenhouse", "slug": "a"}
    for value in (True, False):
        company = load_companies(_registry(tmp_path, {**base, field: value}))[0]
        assert getattr(company, field) is value
    omitted = load_companies(_registry(tmp_path, base))[0]
    assert (omitted.enabled, omitted.name_gate_exempt) == (True, False)


BAD_REGISTRIES = {
    "an unparseable date": "- {name: A, platform: greenhouse, slug: a, last_verified: nope}\n",
    "a date that is a boolean": "- {name: A, platform: greenhouse, slug: a, last_verified: true}\n",
    "a flow mapping left open": "- {name: A, platform: greenhouse\n  slug: [unclosed\n",
    "a tab where yaml forbids one": "- name: A\n\tplatform: greenhouse\n",
    "a scalar instead of a list": "just a string\n",
    "an entry that is not a mapping": "- [a, b]\n",
}


@pytest.mark.parametrize("label", list(BAD_REGISTRIES))
def test_a_broken_registry_is_a_registry_error_not_a_traceback(tmp_path: Path, label: str) -> None:
    target = tmp_path / "companies.yaml"
    target.write_text(BAD_REGISTRIES[label], encoding="utf-8")
    with pytest.raises(RegistryError):
        load_companies(target)


@pytest.mark.parametrize(
    "command",
    [
        ["sync"],
        ["canary"],
        ["coverage"],
        ["discover", "--verify"],
    ],
)
def test_every_registry_reading_command_reports_a_broken_registry(
    tmp_path: Path, command: list[str]
) -> None:
    from typer.testing import CliRunner

    from stage.cli.app import app

    target = tmp_path / "companies.yaml"
    target.write_text(
        "- {name: A, platform: greenhouse, slug: a, last_verified: nope}\n", encoding="utf-8"
    )
    arguments = [*command, "--registry", str(target)]
    if command[0] != "discover":
        arguments += ["--db", str(tmp_path / "s.db")]
    result = CliRunner().invoke(app, arguments)
    assert result.exit_code == 2, result.stdout
    assert "last_verified" in result.stdout, result.stdout
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_a_missing_registry_is_reported_rather_than_raised(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from stage.cli.app import app

    result = CliRunner().invoke(
        app,
        ["discover", "--verify", "--registry", str(tmp_path / "absent.yaml")],
    )
    assert result.exit_code == 2, result.stdout
    assert "registry not found" in result.stdout


def test_a_disable_reason_survives_a_registry_rewrite(tmp_path: Path) -> None:
    from stage.companies import write_registry

    note = "404 on every run since 2026-08-07 — dead board, re-check with discover --verify"
    target = _registry(
        tmp_path,
        {
            "name": "AeroSpike",
            "platform": "greenhouse",
            "slug": "aerospike",
            "enabled": False,
            "notes": note,
        },
    )
    loaded = load_companies(target)
    assert loaded[0].notes == note

    write_registry(loaded, target)
    assert load_companies(target)[0].notes == note, (
        "discover --verify --apply rewrites the whole registry; a field it drops is curation lost"
    )


def test_the_real_registry_survives_a_rewrite_without_losing_a_field() -> None:
    import yaml as yaml_module

    from stage.companies import write_registry
    from stage.paths import registry_path

    before = yaml_module.safe_load(registry_path().read_text(encoding="utf-8"))
    rows = load_companies()
    assert len(rows) == len(before)

    import tempfile

    copy = Path(tempfile.mkdtemp()) / "companies.yaml"
    write_registry(rows, copy)
    after = yaml_module.safe_load(copy.read_text(encoding="utf-8"))

    index = {(row["platform"], row["slug"], row.get("workday_site", "")): row for row in after}
    for row in before:
        twin = index.get((row["platform"], row["slug"], row.get("workday_site", "")))
        assert twin is not None, row["name"]
        assert twin == row or all(twin.get(k) == v for k, v in row.items()), row["name"]
