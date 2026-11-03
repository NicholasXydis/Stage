from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import date
from pathlib import Path

import httpx
import pytest

from stage.domain import (
    Company,
    DiscoveryEvent,
    DiscoveryFinished,
    DiscoveryStarted,
    EmployerSize,
    Platform,
    PlatformCandidate,
    PlatformProbed,
    ProbeResult,
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
            "CAE": ("https://cae.wd3.myworkdayjobs.com/en-US/careers",),
            "No Site": ("https://nosite.wd3.myworkdayjobs.com/en-US/job/Montreal/Intern_R1",),
        }
    )

    assert [(row.name, row.platform, row.slug, row.workday_site) for row in rows] == [
        ("Acme", Platform.GREENHOUSE, "acme", None),
        ("CAE", Platform.WORKDAY, "cae", "careers"),
    ]


def test_direct_apply_urls_recognise_oracle_and_jobvite_boards() -> None:
    rows = direct_companies_from_apply_urls(
        {
            "Qux": (
                "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/7",
            ),
            "Anthology": ("https://jobs.jobvite.com/anthology/job/o1",),
            "Bad Site": ("https://bad.wd3.myworkdayjobs.com/Bad.Site/job/x",),
        }
    )
    assert [(row.name, row.platform, row.oracle_site) for row in rows] == [
        ("Qux", Platform.ORACLE_CLOUD, "CX_1"),
        ("Anthology", Platform.JOBVITE, None),
    ]


def test_direct_apply_urls_honour_platform_choices_for_board_checks() -> None:
    urls = {
        "CAE": ("https://cae.wd3.myworkdayjobs.com/en-US/careers",),
        "Acme": ("https://boards.greenhouse.io/acme/jobs/123",),
    }
    only = direct_companies_from_apply_urls(urls, platforms=[Platform.WORKDAY])
    assert [row.name for row in only] == ["CAE"]
    skipped = direct_companies_from_apply_urls(urls, excluded=[Platform.WORKDAY])
    assert [row.name for row in skipped] == ["Acme"]


def test_a_greenhouse_link_outranks_a_workday_link_for_one_employer() -> None:
    rows = direct_companies_from_apply_urls(
        {
            "Acme": (
                "https://acme.wd3.myworkdayjobs.com/en-US/External",
                "https://boards.greenhouse.io/acme/jobs/123",
            ),
        }
    )
    assert [row.platform for row in rows] == [Platform.GREENHOUSE]


def test_direct_apply_urls_exclude_a_named_platform() -> None:
    rows = direct_companies_from_apply_urls(
        {
            "Acme": (
                "https://boards.greenhouse.io/acme/jobs/123",
                "https://jobs.lever.co/acme/456",
            ),
        },
        excluded=[Platform.GREENHOUSE],
    )

    assert [(row.name, row.platform, row.slug) for row in rows] == [
        ("Acme", Platform.LEVER, "acme"),
    ]


async def test_excluded_platforms_are_not_probed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    events = await _collect(
        probe_companies(
            ["Faire"],
            excluded=[Platform.GREENHOUSE, Platform.WORKABLE],
            client_factory=_client_factory(handler),
        )
    )
    started = events[0]
    assert isinstance(started, DiscoveryStarted)
    assert "greenhouse" not in started.platforms
    assert "workable" not in started.platforms
    assert "lever" in started.platforms


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
            posture=replace(posture, min_interval_s=0.0),
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
    assert "too many for a startup" in finished.rejected[0].detail


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


async def test_an_acquisition_named_row_is_not_disabled_by_the_name_gate() -> None:
    from stage.domain import Company, Platform
    from stage.services.discover import verify_registry

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": [{"id": 1}], "meta": {"total": 1}})
        return httpx.Response(200, json={"name": "LifeWorks"})

    exempt = Company(
        name="TELUS Health", platform=Platform.GREENHOUSE, slug="lifeworks", name_gate_exempt=True
    )
    ordinary = Company(name="Acme", platform=Platform.GREENHOUSE, slug="lifeworks2")
    events = await _collect(
        verify_registry((exempt, ordinary), client_factory=_client_factory(handler))
    )
    verdicts = {
        event.result.company: event.result.verdict
        for event in events
        if isinstance(event, PlatformProbed)
    }
    assert verdicts == {"TELUS Health": ProbeVerdict.UNVERIFIED, "Acme": ProbeVerdict.REJECTED}


def test_every_acquisition_named_row_in_the_shipped_registry_carries_the_marker() -> None:
    from stage.companies import load_companies

    rows = {row.slug: row for row in load_companies(None)}
    for slug in ("lifeworks", "globalhr"):
        assert slug in rows, slug
        assert rows[slug].name_gate_exempt, slug

    exempt = [row for row in load_companies(None) if row.name_gate_exempt]
    assert len(exempt) >= 3
    assert any(row.enabled for row in exempt), "a live board is what the exemption protects"


def _probed(
    company: Company,
    verdict: ProbeVerdict,
    *,
    status: int | None = None,
    detail: str = "",
) -> ProbeResult:
    return ProbeResult(
        company=company.name,
        candidate=PlatformCandidate(company.platform, company.slug),
        verdict=verdict,
        url=f"https://boards.example.test/{company.slug}",
        detail=detail,
        status=status,
    )


def test_apply_can_actually_disable_a_row_that_fails(tmp_path: Path) -> None:
    from stage.companies import load_companies, write_registry
    from stage.services.discover import apply_verification

    live = Company(name="Live", platform=Platform.GREENHOUSE, slug="live")
    dead = Company(name="Dead", platform=Platform.GREENHOUSE, slug="dead")
    results = [_probed(live, ProbeVerdict.MATCH), _probed(dead, ProbeVerdict.REJECTED)]
    updated, report = apply_verification([live, dead], results, date(2026, 8, 4))

    assert (report.verified, report.disabled) == (1, 1)
    by_slug = {row.slug: row for row in updated}
    assert by_slug["live"].last_verified == date(2026, 8, 4)
    assert by_slug["dead"].enabled is False
    assert len(updated) == 2, "a failing row is switched off, never deleted"

    target = tmp_path / "companies.yaml"
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
    from stage.services.discover import apply_verification

    row = Company(name="Coveo (FR)", platform=Platform.GREENHOUSE, slug="coveofr", notes="kept")
    detail = "board is named 'Coveo Solutions', which does not contain 'Coveo (FR)'"
    results = [_probed(row, ProbeVerdict.REJECTED, detail=detail)]

    updated, report = apply_verification((row,), results, date(2026, 8, 8))
    assert (report.verified, report.disabled) == (0, 1)
    assert updated[0].enabled is False
    assert updated[0].notes == (
        "kept\n2026-08-08: switched off by stage discover --verify, " + detail
    )


def test_a_404_is_gone_in_the_registry_as_on_your_own_list() -> None:
    from stage.services.discover import apply_verification

    rows = [
        Company(name=name, platform=Platform.GREENHOUSE, slug=name.lower())
        for name in ("Missing", "Ended", "Flaky", "Odd")
    ]
    results = [
        _probed(rows[0], ProbeVerdict.MISS, status=404),
        _probed(rows[1], ProbeVerdict.MISS, status=410),
        _probed(rows[2], ProbeVerdict.ERROR, status=503),
        _probed(rows[3], ProbeVerdict.MISS, detail="ValueError: not JSON"),
    ]
    updated, report = apply_verification(rows, results, date(2026, 8, 8))

    assert report.disabled == 2
    assert [row.enabled for row in updated] == [False, False, True, True]
    assert (
        updated[0].notes
        == "2026-08-08: switched off by stage discover --verify, the board answered 404"
    )


def test_only_a_row_verify_switched_off_comes_back_when_it_answers() -> None:
    from stage.services.discover import apply_verification

    struck = Company(
        name="Coveo",
        platform=Platform.GREENHOUSE,
        slug="coveo",
        enabled=False,
        notes="2026-01-01: seen in feeds\n2026-08-01: switched off by stage discover --verify, x",
    )
    legacy = Company(
        name="Old",
        platform=Platform.GREENHOUSE,
        slug="old",
        enabled=False,
        notes="2026-02-01: board is named 'Else', which does not contain 'Old'",
    )
    policy = [
        Company(
            name=name, platform=Platform.GREENHOUSE, slug=name.lower(), enabled=False, notes=note
        )
        for name, note in (
            ("Google", "2026-08-21: google.com/robots.txt disallows /jobs"),
            ("Tesla", "2026-08-20: closed for good, Akamai answers 403"),
            ("Quiet", None),
            ("Back", "2026-08-01: switched off by stage discover --verify, x\n2026-08-02: parked"),
        )
    ]
    rows = [struck, legacy, *policy]
    updated, report = apply_verification(
        rows, [_probed(row, ProbeVerdict.MATCH) for row in rows], date(2026, 8, 8)
    )

    assert [row.enabled for row in updated] == [True, True, False, False, False, False]
    assert updated[0].notes == "2026-01-01: seen in feeds"
    assert updated[1].notes is None and updated[1].last_verified == date(2026, 8, 8)
    assert [row.notes for row in updated[2:]] == [row.notes for row in policy]
    assert (report.verified, report.held) == (2, ("Google", "Tesla", "Quiet", "Back"))


def test_a_gone_row_already_off_keeps_its_note() -> None:
    from stage.services.discover import apply_verification

    off = Company(
        name="Meta", platform=Platform.GREENHOUSE, slug="meta", enabled=False, notes="robots.txt"
    )
    updated, report = apply_verification(
        (off,), [_probed(off, ProbeVerdict.MISS, status=404)], date(2026, 8, 8)
    )
    assert updated == (off,) and report.disabled == 0


def test_a_matching_live_row_keeps_its_notes() -> None:
    from stage.services.discover import apply_verification

    row = Company(name="Acme", platform=Platform.GREENHOUSE, slug="acme", notes="quirk")
    updated, _ = apply_verification((row,), [_probed(row, ProbeVerdict.MATCH)], date(2026, 8, 8))
    assert updated[0].notes == "quirk" and updated[0].last_verified == date(2026, 8, 8)


def test_apply_never_enables_a_board_that_exposes_no_name() -> None:
    from stage.services.discover import apply_verification

    off = Company(name="Acme", platform=Platform.LEVER, slug="acme", enabled=False)
    unverified = [_probed(off, ProbeVerdict.UNVERIFIED, detail="lever exposes no board name")]
    updated, report = apply_verification((off,), unverified, date(2026, 8, 8))
    assert (report.verified, report.disabled) == (0, 0)
    assert updated[0].enabled is False, "a 200 with jobs is not evidence of the right company"

    on = Company(name="Acme", platform=Platform.LEVER, slug="acme", enabled=True)
    kept, _ = apply_verification((on,), unverified, date(2026, 8, 8))
    assert kept[0].enabled is True, "an already-enabled row must not be switched off either"


def test_a_mixed_case_lever_board_is_probed_as_sync_reads_it() -> None:
    from stage.domain import Platform
    from stage.sources.platforms import PROBES_BY_PLATFORM

    probe = PROBES_BY_PLATFORM[Platform.LEVER]
    assert probe.url_for("CesiumAstro") == "https://api.lever.co/v0/postings/CesiumAstro"


async def test_unregistered_discovery_writes_its_request_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    import json
    from datetime import date

    from rich.console import Console

    from stage.cli.commands import discovery
    from stage.domain import RequestLogged
    from stage.services import discover as discover_module

    async def probes(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
        yield RequestLogged(
            source="discover",
            method="GET",
            url="https://api.lever.co/v0/postings/acme",
            status=404,
            elapsed_ms=1.0,
            attempt=1,
        )

    monkeypatch.setattr(discover_module, "select_unregistered", lambda *_a, **_k: ([], ["Acme"]))
    monkeypatch.setattr(discover_module, "probe_companies", probes)
    log = io.StringIO()
    await discovery._adopt_unregistered(
        Console(file=io.StringIO()),
        registry=None,
        db=tmp_path / "stage.db",
        platforms=None,
        excluded=None,
        size=None,
        limit=1,
        direct_only=False,
        adopt_unnamed=False,
        apply_rows=False,
        today=date(2026, 11, 1),
        stream=log,
        progress=None,
    )

    assert json.loads(log.getvalue())["url"] == "https://api.lever.co/v0/postings/acme"


async def test_a_platform_named_twice_is_probed_once() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    events = await _collect(
        probe_companies(
            ["Zyxwv"],
            platforms=[Platform.LEVER, Platform.LEVER],
            client_factory=_client_factory(handler),
        )
    )
    started = events[0]
    assert isinstance(started, DiscoveryStarted)
    assert started.platforms == ("lever",)
    assert sum(isinstance(event, PlatformProbed) for event in events) == started.probes_planned
