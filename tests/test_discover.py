from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from stage.domain import (
    Company,
    DiscoveryEvent,
    DiscoveryFinished,
    EmployerSize,
    Platform,
    PlatformCandidate,
    PlatformProbed,
    ProbeVerdict,
    RequestLogged,
    UrlResolved,
    UrlUnrecognized,
)
from stage.http import HttpClient, RatePosture, ValidatorCache
from stage.services.discover import (
    GENERIC_TOKENS,
    MAX_CANDIDATES_PER_COMPANY,
    ClientFactory,
    direct_companies_from_apply_urls,
    name_matches,
    probe_companies,
    resolve_careers_url,
    slug_candidates,
    to_company,
)


@pytest.mark.parametrize(
    ("company", "generic"),
    [
        ("General Motors", "general"),
        ("Royal Bank of Canada", "royal"),
        ("Capital One", "capital"),
        ("Analog Devices", "analog"),
    ],
)
def test_the_four_confirmed_false_positives_are_never_probed(company: str, generic: str) -> None:
    plan = slug_candidates(company)
    assert generic not in plan.accepted
    assert any(slug == generic for slug, _ in plan.skipped)
    assert generic in GENERIC_TOKENS


def test_a_distinctive_first_token_is_still_offered() -> None:
    plan = slug_candidates("Akuna Capital")
    assert "akunacapital" in plan.accepted
    assert "akuna" in plan.accepted


def test_short_first_tokens_are_skipped_even_when_not_in_the_stoplist() -> None:
    plan = slug_candidates("DRW Holdings")
    assert "drw" not in plan.accepted


def test_candidates_are_folded_and_capped() -> None:
    plan = slug_candidates("Eidos-Montréal")
    assert plan.accepted[0] == "eidosmontreal"
    assert "eidos-montreal" in plan.accepted
    assert len(plan.accepted) <= MAX_CANDIDATES_PER_COMPANY


def test_legal_suffixes_do_not_end_up_in_the_token() -> None:
    assert slug_candidates("Genetec Inc").accepted[0] == "genetec"


def test_name_gate_accepts_accents_and_case_but_not_unrelated_boards() -> None:
    assert name_matches("Eidos-Montréal", "Eidos Montreal")
    assert name_matches("Stripe", "Stripe, Inc.")
    assert not name_matches("Analog Devices", "Analog Consulting Group")
    assert not name_matches("Capital One", "Capital Partners LLC")
    assert not name_matches("Acme", "")


@pytest.mark.parametrize(
    ("company", "board"),
    [
        ("Vidéotron", "Videotron"),
        ("Videotron", "Vidéotron"),
        ("Hydro-Québec", "Hydro-Quebec"),
        ("Société Générale", "Societe Generale"),
        ("Eidos-Montréal", "EIDOS MONTREAL"),
    ],
)
def test_quebec_names_match_boards_that_spell_them_unaccented(company: str, board: str) -> None:
    assert name_matches(company, board)


def test_the_gate_is_token_boundary_not_substring() -> None:
    assert not name_matches("Faire", "Groupe Affaires Québec")
    assert not name_matches("Ubi", "Ubisoft Montréal")
    assert not name_matches("Stage", "Programme Stagiaire")
    assert name_matches("Faire", "Faire Wholesale")
    assert name_matches("Ubisoft", "Ubisoft Montréal")


def test_an_all_generic_overlap_needs_more_than_one_token() -> None:
    assert name_matches("National Bank of Canada", "National Bank")
    assert not name_matches("Capital One", "Capital")


def test_direct_apply_urls_keep_the_exact_detected_board_token() -> None:
    rows = direct_companies_from_apply_urls(
        {
            "Acme": (
                "https://boards.greenhouse.io/acme/jobs/123",
                "https://jobs.lever.co/acme/456",
            ),
            "Needs Manual Review": ("https://cae.wd3.myworkdayjobs.com/en-US/careers",),
        }
    )

    assert [(row.name, row.platform, row.slug) for row in rows] == [
        ("Acme", Platform.GREENHOUSE, "acme"),
    ]


def test_resolve_careers_url_produces_a_pasteable_registry_row() -> None:
    from stage.companies import registry_entry_yaml

    event = resolve_careers_url("https://boards.greenhouse.io/faire")
    assert isinstance(event, UrlResolved)
    rendered = registry_entry_yaml(to_company("Faire", event.candidate))
    assert "platform: greenhouse" in rendered
    assert "slug: faire" in rendered
    assert "source_of_record: discover" in rendered


def test_resolve_careers_url_carries_all_four_workday_fields() -> None:
    from stage.companies import registry_entry_yaml

    event = resolve_careers_url("https://cae.wd3.myworkdayjobs.com/en-US/cae_careers")
    assert isinstance(event, UrlResolved)
    rendered = registry_entry_yaml(to_company("CAE", event.candidate))
    assert "workday_tenant: cae" in rendered
    assert "workday_site: cae_careers" in rendered
    assert "workday_dc: wd3" in rendered


def test_an_unrecognized_careers_page_points_at_custom_json_rather_than_guessing() -> None:
    event = resolve_careers_url("https://www.hydroquebec.com/carrieres/")
    assert isinstance(event, UrlUnrecognized)
    assert "custom_json" in event.detail


Handler = Callable[[httpx.Request], httpx.Response]


def _client_factory(handler: Handler) -> ClientFactory:
    def factory(hosts: frozenset[str], posture: RatePosture) -> HttpClient:
        return HttpClient(
            allowed_hosts=hosts,
            posture=posture,
            cache=ValidatorCache(),
            transport=httpx.MockTransport(handler),
            jitter=False,
        )

    return factory


async def _collect(events: AsyncIterator[DiscoveryEvent]) -> list[DiscoveryEvent]:
    return [event async for event in events]


async def test_a_board_whose_name_does_not_match_is_rejected_not_reported() -> None:

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": [{"id": 1}, {"id": 2}]})
        return httpx.Response(200, json={"name": "Analog Consulting Group"})

    events = await _collect(
        probe_companies(
            ["Analog Devices"],
            platforms=[Platform.GREENHOUSE],
            client_factory=_client_factory(handler),
        )
    )
    probed = [event for event in events if isinstance(event, PlatformProbed)]
    assert [event.result.verdict for event in probed] == [ProbeVerdict.REJECTED] * len(probed)
    finished = events[-1]
    assert isinstance(finished, DiscoveryFinished)
    assert finished.matched == ()
    assert finished.rejected


async def test_a_matching_board_name_is_the_only_thing_that_produces_a_match() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": [{"id": 1}]})
        return httpx.Response(200, json={"name": "Faire"})

    events = await _collect(
        probe_companies(
            ["Faire"], platforms=[Platform.GREENHOUSE], client_factory=_client_factory(handler)
        )
    )
    finished = events[-1]
    assert isinstance(finished, DiscoveryFinished)
    assert [result.candidate.slug for result in finished.matched] == ["faire"]


async def test_a_platform_without_board_metadata_never_reaches_match() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "a"}, {"id": "b"}])

    events = await _collect(
        probe_companies(
            ["Shopify"], platforms=[Platform.LEVER], client_factory=_client_factory(handler)
        )
    )
    finished = events[-1]
    assert isinstance(finished, DiscoveryFinished)
    assert finished.matched == ()
    assert finished.unverified
    assert "confirm by hand" in finished.unverified[0].detail


async def test_an_implausible_job_count_is_rejected_for_the_stated_size() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": [{"id": n} for n in range(900)]})
        return httpx.Response(200, json={"name": "Mila"})

    events = await _collect(
        probe_companies(
            ["Mila"],
            platforms=[Platform.GREENHOUSE],
            size=EmployerSize.STARTUP,
            client_factory=_client_factory(handler),
        )
    )
    finished = events[-1]
    assert isinstance(finished, DiscoveryFinished)
    assert finished.matched == ()
    assert "plausible ceiling" in finished.rejected[0].detail


async def test_a_platform_serving_html_for_an_unknown_token_is_a_miss_not_an_error() -> None:

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<!doctype html><title>Not found</title>")

    events = await _collect(
        probe_companies(
            ["Faire"], platforms=[Platform.BAMBOOHR], client_factory=_client_factory(handler)
        )
    )
    finished = events[-1]
    assert isinstance(finished, DiscoveryFinished)
    assert finished.errors == 0
    assert finished.missed > 0


async def test_a_200_with_zero_jobs_is_never_a_match() -> None:

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"totalFound": 0, "content": []})

    events = await _collect(
        probe_companies(
            ["Faire"],
            platforms=[Platform.SMARTRECRUITERS],
            client_factory=_client_factory(handler),
        )
    )
    probed = [event for event in events if isinstance(event, PlatformProbed)]
    assert all(event.result.verdict is ProbeVerdict.EMPTY for event in probed)
    finished = events[-1]
    assert isinstance(finished, DiscoveryFinished)
    assert finished.matched == ()


async def test_a_missing_board_is_a_miss_not_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    events = await _collect(
        probe_companies(
            ["Nonexistent Co"],
            platforms=[Platform.GREENHOUSE],
            client_factory=_client_factory(handler),
        )
    )
    finished = events[-1]
    assert isinstance(finished, DiscoveryFinished)
    assert finished.missed > 0
    assert finished.errors == 0


async def test_the_per_host_ceiling_stops_a_batch_probe_and_says_so() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"jobs": []})

    def factory(hosts: frozenset[str], posture: RatePosture) -> HttpClient:
        return HttpClient(
            allowed_hosts=hosts,
            posture=RatePosture(concurrency=1, min_interval_s=0.0, max_requests_per_run=4),
            cache=ValidatorCache(),
            transport=httpx.MockTransport(handler),
            jitter=False,
        )

    names = [f"Company{n}" for n in range(20)]
    events = await _collect(
        probe_companies(names, platforms=[Platform.GREENHOUSE], client_factory=factory)
    )
    finished = events[-1]
    assert isinstance(finished, DiscoveryFinished)
    assert calls == 4
    assert finished.ceiling_hit
    assert "ceiling" in finished.ceiling_hit[0]


async def test_probing_never_contacts_a_host_outside_the_platform_table() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(404)

    await _collect(probe_companies(["Faire", "Coveo"], client_factory=_client_factory(handler)))
    from stage.sources.platforms import PROBES

    templates = {probe.host for probe in PROBES}
    for host in seen:
        assert any(
            host == template or host.endswith(template.replace("{slug}.", "."))
            for template in templates
        ), host


def test_a_candidate_label_survives_into_the_registry_row() -> None:
    candidate = PlatformCandidate(Platform.ASHBY, "coveo")
    assert candidate.label == "ashby/coveo"
    assert to_company("Coveo", candidate).slug == "coveo"


def test_a_platform_with_no_adapter_is_emitted_disabled() -> None:
    from stage.companies import registry_entry_yaml

    company = to_company("Acme", PlatformCandidate(Platform.TEAMTAILOR, "acme"))
    assert company.enabled is False
    rendered = registry_entry_yaml(company)
    assert "enabled: false" in rendered
    assert "notes" not in rendered


def test_a_row_is_emitted_enabled_only_once_something_verified_it() -> None:
    from datetime import date

    unverified = to_company("Faire", PlatformCandidate(Platform.GREENHOUSE, "faire"))
    assert unverified.enabled is False
    assert unverified.last_verified is None

    verified = to_company(
        "Faire", PlatformCandidate(Platform.GREENHOUSE, "faire"), verified_on=date(2026, 8, 1)
    )
    assert verified.enabled is True


def test_every_emitted_row_carries_provenance_and_verification_date() -> None:
    from datetime import date

    from stage.companies import registry_entry_yaml

    rendered = registry_entry_yaml(
        to_company(
            "Faire", PlatformCandidate(Platform.GREENHOUSE, "faire"), verified_on=date(2026, 8, 1)
        )
    )
    assert "source_of_record: discover" in rendered
    assert "last_verified: 2026-08-01" in rendered


async def test_a_platform_failing_every_probe_with_non_json_is_surfaced() -> None:

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<!doctype html>")

    events = await _collect(
        probe_companies(
            ["Eidos Montreal"],
            platforms=[Platform.BAMBOOHR],
            client_factory=_client_factory(handler),
        )
    )
    finished = events[-1]
    assert isinstance(finished, DiscoveryFinished)
    assert finished.non_json == (("bamboohr", 3),)
    assert finished.errors == 0


async def test_a_single_non_json_probe_is_not_reported_as_drift() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<!doctype html>")

    events = await _collect(
        probe_companies(
            ["Faire"], platforms=[Platform.BAMBOOHR], client_factory=_client_factory(handler)
        )
    )
    finished = events[-1]
    assert isinstance(finished, DiscoveryFinished)
    assert finished.non_json == ()


async def test_every_discovery_request_is_auditable() -> None:

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": [{"id": 1}]})
        return httpx.Response(200, json={"name": "Faire"})

    events = await _collect(
        probe_companies(
            ["Faire"], platforms=[Platform.GREENHOUSE], client_factory=_client_factory(handler)
        )
    )
    logged = [event for event in events if isinstance(event, RequestLogged)]
    assert [event.source for event in logged] == ["discover", "discover"]
    assert any(record.url.endswith("/jobs?content=true") is False for record in logged)
    finished = events[-1]
    assert isinstance(finished, DiscoveryFinished)
    assert len(logged) == finished.requests


@pytest.mark.parametrize(
    ("company", "board"),
    [
        ("Alpha Sense", "AlphaSense"),
        ("Car Gurus", "CarGurus"),
        ("Triple dot studios", "Tripledot Studios"),
        ("Eidos Montreal", "Eidos-Montréal"),
    ],
)
def test_word_splitting_differences_are_the_same_employer(company: str, board: str) -> None:
    assert name_matches(company, board)


def test_concatenation_does_not_reopen_the_substring_hole() -> None:
    assert not name_matches("Faire", "Groupe Affaires Québec")
    assert not name_matches("Ubi", "Ubisoft")


def test_an_acquisition_named_row_is_not_disabled_by_the_name_gate() -> None:
    from stage.domain import Company, Platform
    from stage.services.discover import _acquisition_named

    exempt = Company(
        name="TELUS Health",
        platform=Platform.WORKDAY,
        slug="lifeworks",
        name_gate_exempt=True,
    )
    ordinary = Company(name="Acme", platform=Platform.GREENHOUSE, slug="acme")

    assert _acquisition_named(exempt)
    assert not _acquisition_named(ordinary)
    assert not _acquisition_named(ordinary), "the default must never be an exemption"


def test_every_acquisition_named_row_in_the_shipped_registry_carries_the_marker() -> None:
    from stage.companies import load_companies
    from stage.services.discover import _acquisition_named

    rows = {row.slug: row for row in load_companies(None)}
    for slug in ("lifeworks", "telusdigitalbr", "globalhr"):
        assert slug in rows, slug
        assert _acquisition_named(rows[slug]), slug

    exempt = [row for row in load_companies(None) if _acquisition_named(row)]
    assert len(exempt) >= 3
    assert all(row.name_gate_exempt for row in exempt), "exemption is a field, never prose"
    assert any(row.enabled for row in exempt), "a live board is what the exemption protects"


def test_apply_can_actually_disable_a_row_that_fails(tmp_path: "object") -> None:
    from datetime import date
    from pathlib import Path

    from stage.companies import load_companies, write_registry
    from stage.domain import (
        Company,
        DiscoveryFinished,
        Platform,
        PlatformCandidate,
        ProbeResult,
        ProbeVerdict,
    )
    from stage.services.discover import apply_verification

    live = Company(name="Live", platform=Platform.GREENHOUSE, slug="live")
    dead = Company(name="Dead", platform=Platform.GREENHOUSE, slug="dead")

    def result(company: Company, verdict: ProbeVerdict) -> ProbeResult:
        return ProbeResult(
            company=company.name,
            candidate=PlatformCandidate(Platform.GREENHOUSE, company.slug),
            url=f"https://boards-api.greenhouse.io/v1/boards/{company.slug}/jobs",
            verdict=verdict,
        )

    outcome = DiscoveryFinished(
        matched=(result(live, ProbeVerdict.MATCH),),
        unverified=(),
        rejected=(result(dead, ProbeVerdict.REJECTED),),
        missed=0,
        errors=0,
        requests=2,
        elapsed_ms=0.0,
    )
    updated, ok, off = apply_verification([live, dead], outcome, date(2026, 8, 4))

    assert ok == 1 and off == 1, "the disable path must be reachable, not merely untriggered"
    by_slug = {row.slug: row for row in updated}
    assert by_slug["live"].last_verified == date(2026, 8, 4)
    assert by_slug["dead"].enabled is False
    assert len(updated) == 2, "a failing row is switched off, never deleted"

    target = Path(str(tmp_path)) / "companies.yaml"
    write_registry(updated, target)
    assert {row.slug for row in load_companies(target)} == {"live", "dead"}


def test_an_omitted_enabled_key_defaults_to_polled(tmp_path: "object") -> None:
    from pathlib import Path

    from stage.companies import load_companies

    target = Path(str(tmp_path)) / "companies.yaml"
    target.write_text(
        "- name: Implicit\n  platform: greenhouse\n  slug: implicit\n"
        "- name: Explicit Off\n  platform: greenhouse\n  slug: switched-off\n  enabled: false\n",
        encoding="utf-8",
    )
    rows = {row.slug: row for row in load_companies(target)}
    assert rows["implicit"].enabled is True
    assert rows["switched-off"].enabled is False


def test_applying_a_rejection_records_why_the_row_was_disabled() -> None:
    from datetime import date

    from stage.domain import ProbeResult
    from stage.services.discover import apply_verification

    rejected = ProbeResult(
        company="Coveo (FR)",
        candidate=PlatformCandidate(platform=Platform.GREENHOUSE, slug="coveofr"),
        verdict=ProbeVerdict.REJECTED,
        url="https://boards.example.test/coveofr",
        board_name="Coveo Solutions",
        job_count=12,
        detail="board is named 'Coveo Solutions', which does not contain 'Coveo (FR)'",
    )
    outcome = DiscoveryFinished(
        matched=(),
        unverified=(),
        rejected=(rejected,),
        missed=0,
        errors=0,
        requests=1,
        elapsed_ms=1.0,
    )
    row = Company(name="Coveo (FR)", platform=Platform.GREENHOUSE, slug="coveofr")

    updated, verified, disabled = apply_verification((row,), outcome, date(2026, 8, 8))
    assert (verified, disabled) == (0, 1)
    assert updated[0].enabled is False
    assert updated[0].notes is not None
    assert updated[0].notes.startswith("2026-08-08: ")
    assert "Coveo Solutions" in updated[0].notes


def test_re_enabling_a_row_clears_the_reason_it_was_disabled_for() -> None:
    from datetime import date

    from stage.domain import ProbeResult
    from stage.services.discover import apply_verification

    matched = ProbeResult(
        company="Coveo",
        candidate=PlatformCandidate(platform=Platform.GREENHOUSE, slug="coveo"),
        verdict=ProbeVerdict.MATCH,
        url="https://boards.example.test/coveo",
        board_name="Coveo Solutions",
        job_count=12,
    )
    outcome = DiscoveryFinished(
        matched=(matched,),
        unverified=(),
        rejected=(),
        missed=0,
        errors=0,
        requests=1,
        elapsed_ms=1.0,
    )
    stale = Company(
        name="Coveo",
        platform=Platform.GREENHOUSE,
        slug="coveo",
        enabled=False,
        notes="2026-01-01: name-gate rejection",
    )
    updated, _, _ = apply_verification((stale,), outcome, date(2026, 8, 8))
    assert updated[0].enabled is True
    assert updated[0].notes is None, "a disable reason is a claim about a moment that has passed"


def test_apply_never_enables_a_board_that_exposes_no_name() -> None:
    from datetime import date

    from stage.domain import ProbeResult
    from stage.services.discover import apply_verification

    unverified = ProbeResult(
        company="Acme",
        candidate=PlatformCandidate(platform=Platform.LEVER, slug="acme"),
        verdict=ProbeVerdict.UNVERIFIED,
        url="https://api.lever.co/v0/postings/acme",
        board_name="",
        job_count=12,
        detail="lever exposes no board name",
    )
    outcome = DiscoveryFinished(
        matched=(),
        unverified=(unverified,),
        rejected=(),
        missed=0,
        errors=0,
        requests=1,
        elapsed_ms=1.0,
    )
    off = Company(name="Acme", platform=Platform.LEVER, slug="acme", enabled=False)
    updated, verified, disabled = apply_verification((off,), outcome, date(2026, 8, 8))
    assert (verified, disabled) == (0, 0)
    assert updated[0].enabled is False, "a 200 with jobs is not evidence of the right company"

    on = Company(name="Acme", platform=Platform.LEVER, slug="acme", enabled=True)
    kept, _, _ = apply_verification((on,), outcome, date(2026, 8, 8))
    assert kept[0].enabled is True, "an already-enabled row must not be switched off either"
