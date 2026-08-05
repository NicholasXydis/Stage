import pytest

from stage.http import PROFILES, RatePosture, UnknownProfileError, profile, resolve
from stage.sources import get_adapters


def test_the_documented_tiers_exist() -> None:
    assert set(PROFILES) == {
        "standard",
        "moderate",
        "conservative",
        "workday",
        "feeds",
        "discovery",
    }


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


def test_workday_matches_the_shared_bucket_posture() -> None:
    workday = profile("workday")
    assert (workday.concurrency, workday.min_interval_s, workday.max_requests_per_run) == (
        2,
        1.5,
        120,
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
