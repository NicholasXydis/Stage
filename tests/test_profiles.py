import pytest

from stage.http import PROFILES, RatePosture, UnknownProfileError, profile, resolve
from stage.sources import get_adapters


def test_the_documented_tiers_exist() -> None:
    assert set(PROFILES) == {
        "standard",
        "broad",
        "moderate",
        "paginated",
        "conservative",
        "workday",
        "feeds",
        "discovery",
    }


def test_the_paginated_tier_raises_only_the_ceiling_never_the_rate() -> None:
    moderate, paginated = profile("moderate"), profile("paginated")
    assert (paginated.concurrency, paginated.min_interval_s) == (
        moderate.concurrency,
        moderate.min_interval_s,
    ), "the ceiling bounds a bug's blast radius; the rate is what a host actually feels"
    assert paginated.max_requests_per_run > moderate.max_requests_per_run


def test_discovery_never_loosens_a_platforms_own_tier() -> None:
    for name in ("standard", "moderate", "conservative"):
        combined = resolve(name, ["discovery"])
        own = profile(name)
        assert combined.concurrency <= own.concurrency
        assert combined.min_interval_s >= own.min_interval_s
        assert combined.max_requests_per_run <= own.max_requests_per_run

    assert resolve("conservative", ["discovery"]).min_interval_s == 1.0


def test_conservative_is_stricter_than_standard() -> None:
    standard = profile("standard")
    conservative = profile("conservative")

    assert conservative.concurrency < standard.concurrency
    assert conservative.min_interval_s > standard.min_interval_s
    assert conservative.max_requests_per_run < standard.max_requests_per_run


def test_workday_pacing_is_the_control_that_must_not_drift() -> None:
    workday = profile("workday")
    assert (workday.concurrency, workday.min_interval_s) == (2, 1.5), (
        "all tenants share one Akamai-fronted bucket; the stride is the ban-risk control"
    )


def test_the_workday_ceiling_can_cover_a_whole_rotation_slice() -> None:
    from stage.sources.workday import WorkdayAdapter

    ceiling = profile("workday").max_requests_per_run
    needed = WorkdayAdapter.rotation_slice + WorkdayAdapter.retry_reserve
    assert ceiling >= needed, (
        f"ceiling {ceiling} starves a slice of {WorkdayAdapter.rotation_slice}: its tail is skipped"
    )


def test_an_unknown_profile_names_the_known_ones() -> None:
    with pytest.raises(UnknownProfileError, match="standard"):
        profile("turbo")


def test_an_override_can_only_tighten_never_loosen() -> None:
    tightened = resolve("standard", ["conservative"])
    assert tightened == profile("conservative")

    loosened = resolve("conservative", ["standard"])
    assert loosened.concurrency == 1
    assert loosened.min_interval_s == 1.0
    assert loosened.max_requests_per_run == 80


def test_the_strictest_field_wins_across_several_overrides() -> None:
    resolved = resolve("standard", [None, "moderate", "feeds"])

    assert resolved == RatePosture(concurrency=2, min_interval_s=0.5, max_requests_per_run=20)


def test_every_adapter_declares_a_known_profile() -> None:
    for adapter in get_adapters().values():
        assert adapter.rate_profile in PROFILES


def test_strictest_is_per_dimension_and_never_adopts_a_whole_profile() -> None:
    loose_rate_tight_ceiling = RatePosture(
        concurrency=8, min_interval_s=0.1, max_requests_per_run=10
    )
    tight_rate_loose_ceiling = RatePosture(
        concurrency=1, min_interval_s=5.0, max_requests_per_run=9000
    )

    merged = loose_rate_tight_ceiling.strictest(tight_rate_loose_ceiling)
    assert merged.concurrency == 1
    assert merged.min_interval_s == 5.0
    assert merged.max_requests_per_run == 10

    assert merged not in (loose_rate_tight_ceiling, tight_rate_loose_ceiling), (
        "the result is a new posture, not whichever input won a single comparison"
    )
    assert merged == tight_rate_loose_ceiling.strictest(loose_rate_tight_ceiling)


def test_the_broad_tier_raises_only_the_ceiling_never_the_rate() -> None:
    standard, broad = profile("standard"), profile("broad")
    assert (broad.concurrency, broad.min_interval_s) == (
        standard.concurrency,
        standard.min_interval_s,
    ), "a bigger board list needs more requests, not a faster one"
    assert broad.max_requests_per_run > standard.max_requests_per_run


def test_every_shipped_platform_ceiling_covers_its_enabled_board_count() -> None:
    import collections

    from stage.companies import load_companies
    from stage.sources import get_adapters, load_builtins

    load_builtins()
    enabled: collections.Counter[str] = collections.Counter(
        company.platform.value for company in load_companies() if company.enabled
    )
    for adapter in get_adapters().values():
        boards = enabled.get(adapter.platform.value, 0)
        if not boards or adapter.rotation_slice:
            continue
        ceiling = profile(adapter.rate_profile).max_requests_per_run
        assert ceiling >= boards, (
            f"{adapter.name}: {boards} boards against a ceiling of {ceiling} truncates every run"
        )
