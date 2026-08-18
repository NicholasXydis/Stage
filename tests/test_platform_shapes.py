import pytest

from stage.domain import Company, Platform
from stage.sources.ashby import AshbyAdapter
from stage.sources.platforms import (
    PROBES_BY_PLATFORM,
    SlugRejectedError,
    identify_url,
    job_count,
    oracle_target,
    safe_path_slug,
    safe_slug,
)


@pytest.mark.parametrize(
    ("url", "platform", "slug"),
    [
        ("https://boards.greenhouse.io/stripe", Platform.GREENHOUSE, "stripe"),
        ("https://job-boards.greenhouse.io/datadog/jobs/123", Platform.GREENHOUSE, "datadog"),
        ("https://jobs.lever.co/shopify/abc-def", Platform.LEVER, "shopify"),
        ("https://jobs.ashbyhq.com/coveo", Platform.ASHBY, "coveo"),
        ("https://jobs.ashbyhq.com/mistral.ai", Platform.ASHBY, "mistral.ai"),
        ("https://careers.smartrecruiters.com/Ubisoft", Platform.SMARTRECRUITERS, "ubisoft"),
        ("https://apply.workable.com/genetec/", Platform.WORKABLE, "genetec"),
        ("https://acme.recruitee.com/o/intern", Platform.RECRUITEE, "acme"),
        ("https://acme.bamboohr.com/careers/list", Platform.BAMBOOHR, "acme"),
        ("https://acme.breezy.hr/p/xyz", Platform.BREEZY, "acme"),
        ("https://acme.teamtailor.com/jobs", Platform.TEAMTAILOR, "acme"),
        ("https://acme.jobs.personio.de/", Platform.PERSONIO, "acme"),
        ("https://jobs.jobvite.com/acme/jobs", Platform.JOBVITE, "acme"),
        ("https://join.com/companies/acme", Platform.JOIN, "acme"),
    ],
)
def test_careers_urls_resolve_to_a_platform_and_token(
    url: str, platform: Platform, slug: str
) -> None:
    candidate = identify_url(url)
    assert candidate is not None
    assert candidate.platform is platform
    assert candidate.slug == slug


def test_workday_needs_four_fields_and_only_a_url_can_supply_them() -> None:
    candidate = identify_url("https://cae.wd3.myworkdayjobs.com/en-US/cae_careers")
    assert candidate is not None
    assert candidate.platform is Platform.WORKDAY
    assert candidate.workday_tenant == "cae"
    assert candidate.workday_dc == "wd3"
    assert candidate.workday_site == "cae_careers"


def test_workday_locale_segment_does_not_become_the_site() -> None:
    english = identify_url("https://bombardier.wd3.myworkdayjobs.com/en-CA/Bombardier_Careers")
    french = identify_url("https://bombardier.wd3.myworkdayjobs.com/fr-CA/Bombardier_Careers")
    assert english == french
    assert english is not None
    assert english.workday_site == "Bombardier_Careers"


def test_workday_cxs_endpoint_form_resolves_identically() -> None:
    candidate = identify_url("https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs")
    assert candidate is not None
    assert (candidate.workday_tenant, candidate.workday_dc, candidate.workday_site) == (
        "acme",
        "wd5",
        "External",
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://www.acme.com/careers",
        "https://careers.google.com/jobs/results/",
        "https://boards.greenhouse.io/",
        "not a url at all",
    ],
)
def test_unrecognized_urls_return_nothing_rather_than_guessing(url: str) -> None:
    assert identify_url(url) is None


def test_path_slug_permits_a_dotted_ashby_identifier() -> None:
    probe = PROBES_BY_PLATFORM[Platform.ASHBY]
    assert safe_path_slug("mistral.ai") == "mistral.ai"
    assert probe.url_for("mistral.ai").endswith("/mistral.ai")
    company = Company(name="Mistral AI", platform=Platform.ASHBY, slug="mistral.ai")
    assert AshbyAdapter().url_for(company).endswith("/mistral.ai")


def test_slug_gate_refuses_anything_that_could_rewrite_a_host() -> None:
    for hostile in ("evil.com", "acme/../x", "acme?x=1", "ACME_CORP", "a" * 80, ""):
        with pytest.raises(SlugRejectedError):
            safe_slug(hostile)


def test_probe_urls_are_built_only_from_gated_slugs() -> None:
    probe = PROBES_BY_PLATFORM[Platform.BAMBOOHR]
    assert probe.host_for("acme") == "acme.bamboohr.com"
    with pytest.raises(SlugRejectedError):
        probe.url_for("evil.com")


def test_job_count_reads_an_explicit_total_before_falling_back_to_length() -> None:
    probe = PROBES_BY_PLATFORM[Platform.SMARTRECRUITERS]
    assert job_count({"totalFound": 42, "content": [{}, {}]}, probe) == 42
    assert job_count({"content": [{}, {}]}, probe) == 2
    assert job_count({"nothing": True}, probe) is None


def test_oracle_candidate_urls_retain_the_exact_host_and_site() -> None:
    candidate = identify_url(
        "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/jobsearch/job/334366"
    )
    assert candidate is not None
    assert candidate.platform is Platform.ORACLE_CLOUD
    assert candidate.oracle_host == "eeho.fa.us2.oraclecloud.com"
    assert candidate.oracle_site == "jobsearch"


def test_oracle_target_refuses_hosts_or_sites_that_could_rewrite_requests() -> None:
    assert oracle_target("EEHO.FA.US2.ORACLECLOUD.COM", "jobsearch") == (
        "eeho.fa.us2.oraclecloud.com",
        "jobsearch",
    )
    for host, site in (
        ("evil.example", "jobsearch"),
        ("eeho.fa.us2.oraclecloud.com", "job/search"),
    ):
        with pytest.raises(SlugRejectedError):
            oracle_target(host, site)


def test_the_greenhouse_api_host_resolves_like_the_board_host() -> None:
    from stage.sources.platforms import identify_url

    api = identify_url("https://api.greenhouse.io/v1/boards/robinhood/jobs")
    assert api is not None, "an api.greenhouse.io URL is a greenhouse board, not an unknown site"
    assert (api.platform.value, api.slug) == ("greenhouse", "robinhood")
    board = identify_url("https://boards.greenhouse.io/robinhood")
    assert board is not None
    assert board.slug == api.slug, "both greenhouse hosts must name the same board"
