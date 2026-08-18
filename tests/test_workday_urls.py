import pytest

from stage.domain import Platform as _Platform
from stage.sources.platforms import SlugRejectedError, workday_target
from stage.sources.platforms import identify_url as _identify_url


def test_a_well_formed_row_builds_the_cxs_target() -> None:
    host, path = workday_target("cae", "career", "wd3")
    assert host == "cae.wd3.myworkdayjobs.com"
    assert path == "/wday/cxs/cae/career/jobs"


def test_the_site_keeps_its_case_because_workday_is_case_sensitive() -> None:
    _, path = workday_target("autodesk", "Ext", "wd1")
    assert path == "/wday/cxs/autodesk/Ext/jobs"


@pytest.mark.parametrize(
    "tenant",
    ["evil.com", "acme/../other", "ACME_CORP", "acme:8080", "-acme", ""],
)
def test_a_tenant_that_would_rewrite_the_host_is_refused(tenant: str) -> None:
    with pytest.raises(SlugRejectedError):
        workday_target(tenant, "career", "wd3")


@pytest.mark.parametrize("dc", ["wd3.evil.com", "", "xx1", "wd", "wd3/x", "wd99999"])
def test_a_datacenter_that_would_rewrite_the_host_is_refused(dc: str) -> None:
    with pytest.raises(SlugRejectedError):
        workday_target("acme", "career", dc)


@pytest.mark.parametrize(
    "site",
    ["../../wday/admin", "career/jobs", "career?x=1", "car%2Feer", "", "a" * 65],
)
def test_a_site_that_would_climb_out_of_the_cxs_path_is_refused(site: str) -> None:
    with pytest.raises(SlugRejectedError):
        workday_target("acme", site, "wd3")


def test_every_workday_registry_row_passes_validation_today() -> None:
    from stage.companies import load_companies
    from stage.domain import Platform

    rows = [row for row in load_companies(None) if row.platform is Platform.WORKDAY]
    assert rows

    missing = [row.name for row in rows if not (row.workday_tenant and row.workday_dc)]
    assert not missing, f"rows with no tenant or datacenter: {missing}"

    unaddressable = [row.name for row in rows if not row.workday_site]
    assert not unaddressable, f"an unaddressable row must be dropped, not kept: {unaddressable}"

    for row in rows:
        workday_target(row.workday_tenant or "", row.workday_site or "", row.workday_dc or "")


@pytest.mark.parametrize(
    ("url", "platform", "slug"),
    [
        ("https://bombardier.taleo.net/careersection/4/", _Platform.TALEO, "bombardier"),
        ("https://cgi.njoyn.com/corp/xweb/xweb.asp", _Platform.NJOYN, "cgi"),
        ("https://careers-acme.icims.com/jobs", _Platform.ICIMS, "acme"),
        ("https://emploi.hydroquebec.com/go/Emplois/", _Platform.SUCCESSFACTORS, "hydroquebec"),
        (
            "https://acme.oraclecloud.com/hcmUI/CandidateExperience/en/sites/x",
            _Platform.ORACLE_CLOUD,
            "acme",
        ),
    ],
)
def test_the_unadaptered_vendors_are_recognised_so_a_count_can_be_trusted(
    url: str, platform: "_Platform", slug: str
) -> None:
    candidate = _identify_url(url)
    assert candidate is not None, url
    assert candidate.platform is platform
    assert candidate.slug == slug


def test_a_vanity_careers_host_yields_the_company_not_the_careers_word() -> None:
    for host in ("emploi.hydroquebec.com", "careers.hydroquebec.com", "hydroquebec.com"):
        candidate = _identify_url(f"https://{host}/go/x")
        assert candidate is not None and candidate.slug == "hydroquebec", host


def test_a_recognised_vendor_host_is_not_reinterpreted_as_successfactors() -> None:
    candidate = _identify_url("https://boards.greenhouse.io/go/x")
    assert candidate is not None and candidate.platform is _Platform.GREENHOUSE
    assert _identify_url("https://acme.com/go-kart-racing") is None


@pytest.mark.parametrize("dc", ["wd1", "wd3", "wd5", "wd10", "wd99", "wd102", "wd501"])
def test_a_two_digit_datacenter_is_accepted_and_not_read_as_a_prefix(dc: str) -> None:
    host, _ = workday_target("desjardins", "Desjardins", dc)
    assert host == f"desjardins.{dc}.myworkdayjobs.com"


def test_path_shape_matchers_run_after_every_vendor_hostname_matcher() -> None:
    vendor_hosts = [
        "boards.greenhouse.io/go/x",
        "jobs.lever.co/go/x",
        "api.ashbyhq.com/go/x",
        "acme.taleo.net/go/x",
        "acme.njoyn.com/go/x",
        "careers-acme.icims.com/go/x",
        "acme.wd3.myworkdayjobs.com/go/x",
    ]
    for host in vendor_hosts:
        candidate = _identify_url(f"https://{host}")
        assert candidate is not None, host
        assert candidate.platform is not _Platform.SUCCESSFACTORS, host


def test_an_experience_layer_is_a_weaker_verdict_than_an_ats_match() -> None:
    front_end = _identify_url("https://emplois.bnc.ca/en_CA/careers/searchjobs")
    assert front_end is not None
    assert front_end.platform is _Platform.AVATURE
    assert not front_end.resolves_board
    assert "board unresolved" in front_end.label

    board = _identify_url("https://boards.greenhouse.io/acme")
    assert board is not None and board.resolves_board
    assert "board unresolved" not in board.label


def test_the_avature_grammar_comes_from_avature_dogfooding_its_own_shape() -> None:
    for url in (
        "https://careers.avature.net/en_US/main/SearchJobs",
        "https://emplois.bnc.ca/en_CA/careers/searchjobs",
        "https://emplois.bnc.ca/fr_CA/careers/talentcommunity",
    ):
        candidate = _identify_url(url)
        assert candidate is not None and candidate.platform is _Platform.AVATURE, url
    assert _identify_url("https://acme.com/en/careers/searchjobs") is None, (
        "a hyphen locale is not the grammar — the underscore is what makes it distinctive"
    )
