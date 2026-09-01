import pytest

from stage.domain import LocationBucket, RemoteScope
from stage.normalize import resolve_location


@pytest.mark.parametrize(
    ("raw", "bucket"),
    [
        ("Montreal, QC, Canada", LocationBucket.MONTREAL),
        ("Montréal, Quebec, Canada", LocationBucket.MONTREAL),
        ("Montreal, QC", LocationBucket.MONTREAL),
        ("Montreal", LocationBucket.MONTREAL),
        ("Montreal,Quebec,Canada", LocationBucket.MONTREAL),
        ("Canada - Montreal", LocationBucket.MONTREAL),
        ("Canada, Quebec, Montreal", LocationBucket.MONTREAL),
        ("Pointe-Claire, QC, Canada", LocationBucket.MONTREAL),
        ("Toronto, Ontario, Canada", LocationBucket.CANADA),
        ("Toronto, ON, CA", LocationBucket.CANADA),
        ("Vancouver, BC", LocationBucket.CANADA),
        ("Canada", LocationBucket.CANADA),
        ("Alberta; British Columbia; Nova Scotia; Ontario; Quebec", LocationBucket.CANADA),
        ("Hawthorne, CA", LocationBucket.USA),
        ("Costa Mesa, California, United States", LocationBucket.USA),
        ("New York, New York, USA", LocationBucket.USA),
        ("NYC", LocationBucket.USA),
        ("IN - Bangalore, India", LocationBucket.INTERNATIONAL),
        ("Eindhoven, NB, Netherlands", LocationBucket.INTERNATIONAL),
        ("Eindhoven, NL, Netherlands", LocationBucket.INTERNATIONAL),
        ("NL-Amsterdam", LocationBucket.INTERNATIONAL),
        ("Tbilisi, Georgia", LocationBucket.INTERNATIONAL),
        ("New Mexico", LocationBucket.USA),
        ("Fins Only-DE-Munich-MSO", LocationBucket.INTERNATIONAL),
        ("Washington, D.C.", LocationBucket.USA),
        ("United States", LocationBucket.USA),
        ("(Raleigh-Cary, NC, Austin, Dallas, TX, Tampa, FL, Boston, MA )", LocationBucket.USA),
        ("Bengaluru, Karnataka, India", LocationBucket.INTERNATIONAL),
        ("Remote, , India", LocationBucket.INTERNATIONAL),
        ("London, England, United Kingdom", LocationBucket.INTERNATIONAL),
        ("UK - London", LocationBucket.INTERNATIONAL),
        ("Brazil - Rio de Janeiro", LocationBucket.INTERNATIONAL),
        ("Home based - EMEA", LocationBucket.INTERNATIONAL),
    ],
)
def test_corpus_shapes_resolve(raw: str, bucket: LocationBucket) -> None:
    assert resolve_location(raw).bucket is bucket


@pytest.mark.parametrize(
    ("raw", "bucket"),
    [
        ("Hybrid", LocationBucket.UNKNOWN),
        ("In-Office", LocationBucket.UNKNOWN),
        ("BLANK,BLANK,Multiple Locations", LocationBucket.UNKNOWN),
        ("N/A", LocationBucket.UNKNOWN),
        ("Any", LocationBucket.UNKNOWN),
        ("", LocationBucket.UNKNOWN),
        ("   ", LocationBucket.UNKNOWN),
    ],
)
def test_undecidable_strings_stay_unknown(raw: str, bucket: LocationBucket) -> None:
    assert resolve_location(raw).bucket is bucket


def test_international_is_separate_from_unknown() -> None:
    assert resolve_location("Bengaluru, India").bucket is LocationBucket.INTERNATIONAL
    assert resolve_location("Multiple Locations").bucket is LocationBucket.UNKNOWN


@pytest.mark.parametrize(
    ("raw", "bucket"),
    [
        ("London", LocationBucket.INTERNATIONAL),
        ("London, UK", LocationBucket.INTERNATIONAL),
        ("London, ON, Canada", LocationBucket.CANADA),
        ("Waterloo, ON", LocationBucket.CANADA),
        ("Waterloo, IA", LocationBucket.USA),
        ("Victoria, BC, Canada", LocationBucket.CANADA),
        ("Melbourne, Victoria, Australia", LocationBucket.INTERNATIONAL),
        ("Hamilton, Washington, United States", LocationBucket.USA),
        ("Richmond, VA", LocationBucket.USA),
        ("Ontario, California", LocationBucket.USA),
        ("Vancouver, WA", LocationBucket.USA),
    ],
)
def test_ambiguous_place_names_need_corroboration(raw: str, bucket: LocationBucket) -> None:
    assert resolve_location(raw).bucket is bucket


def test_a_french_place_name_does_not_become_montreal() -> None:
    assert (
        resolve_location("Saint-Barthélemy-d'Anjou, Pays de la Loire, France").bucket
        is LocationBucket.INTERNATIONAL
    )
    assert resolve_location("Anjou, QC, Canada").bucket is LocationBucket.MONTREAL


def test_in_office_is_not_indiana() -> None:
    assert resolve_location("In-Office").bucket is LocationBucket.UNKNOWN
    assert resolve_location("Indianapolis, IN").bucket is LocationBucket.USA


def test_remote_on_site_is_not_ontario() -> None:
    assert resolve_location("Remote on-site").bucket is LocationBucket.UNKNOWN


@pytest.mark.parametrize(
    ("raw", "bucket"),
    [
        ("Montreal, QC / Toronto, ON", LocationBucket.MONTREAL),
        ("Montreal; Toronto", LocationBucket.MONTREAL),
        ("Bellevue, Washington; Toronto, Ontario, Canada", LocationBucket.CANADA),
        ("Bangalore, India; Remote, Canada; Remote, United States", LocationBucket.CANADA),
        ("Amsterdam, The Netherlands; Austin, TX", LocationBucket.USA),
        ("Tokyo, Japan; Singapore", LocationBucket.INTERNATIONAL),
    ],
)
def test_multi_location_precedence_reads_outward_from_montreal(
    raw: str, bucket: LocationBucket
) -> None:
    assert resolve_location(raw).bucket is bucket


@pytest.mark.parametrize(
    ("raw", "separator"),
    [
        ("Montreal, QC; Toronto, ON", "semicolon"),
        ("Montreal, QC / Toronto, ON", "slash"),
        ("Montreal, QC • Toronto, ON", "bullet"),
        ("Montreal, QC | Toronto, ON", "pipe"),
    ],
)
def test_multi_location_separators(raw: str, separator: str) -> None:
    assert resolve_location(raw).bucket is LocationBucket.MONTREAL, separator


@pytest.mark.parametrize(
    "raw",
    ["Canada-Remote", "Canada - Remote", "Ahmedabad - India", "UK - London"],
)
def test_the_hyphen_never_splits_a_location(raw: str) -> None:
    assert resolve_location(raw).bucket is not LocationBucket.UNKNOWN


def test_remote_with_a_country_keeps_the_country() -> None:
    resolved = resolve_location("Remote - United States")
    assert resolved.bucket is LocationBucket.USA
    assert resolved.remote_scope is RemoteScope.US


@pytest.mark.parametrize(
    ("raw", "bucket", "scope"),
    [
        ("Remote", LocationBucket.UNKNOWN, RemoteScope.UNSPECIFIED),
        ("Distributed", LocationBucket.UNKNOWN, RemoteScope.UNSPECIFIED),
        ("Home based - Worldwide", LocationBucket.INTERNATIONAL, RemoteScope.UNSPECIFIED),
        ("Remote - Canada", LocationBucket.CANADA, RemoteScope.CANADA),
        ("Remote, , Canada", LocationBucket.CANADA, RemoteScope.CANADA),
        ("United States - Remote", LocationBucket.USA, RemoteScope.US),
        ("Remote - US", LocationBucket.USA, RemoteScope.US),
        ("Montreal, QC", LocationBucket.MONTREAL, None),
        ("Austin, TX", LocationBucket.USA, None),
    ],
)
def test_remote_scope(raw: str, bucket: LocationBucket, scope: RemoteScope | None) -> None:
    resolved = resolve_location(raw)
    assert resolved.bucket is bucket
    assert resolved.remote_scope is scope


def test_scope_pairs_with_its_own_segment() -> None:
    resolved = resolve_location("Montreal, QC; Remote, US")
    assert resolved.bucket is LocationBucket.MONTREAL
    assert resolved.remote_scope is RemoteScope.US


def test_scope_combines_toward_the_more_permissive_reading() -> None:
    assert resolve_location("Remote; Remote - US").remote_scope is RemoteScope.UNSPECIFIED
    assert resolve_location("Remote - Canada; Remote - US").remote_scope is RemoteScope.CANADA


def test_hybrid_is_not_remote() -> None:
    resolved = resolve_location("Hybrid")
    assert resolved.bucket is LocationBucket.UNKNOWN
    assert resolved.remote_scope is None
    assert resolve_location("Hybrid - London, UK").bucket is LocationBucket.INTERNATIONAL


@pytest.mark.parametrize(
    ("raw", "bucket", "scope"),
    [
        ("Montréal, Québec, Canada", LocationBucket.MONTREAL, None),
        ("Ville de Montréal", LocationBucket.MONTREAL, None),
        ("Longueuil, QC", LocationBucket.MONTREAL, None),
        ("Laval, Québec", LocationBucket.MONTREAL, None),
        ("Québec", LocationBucket.CANADA, None),
        ("Ville de Québec", LocationBucket.CANADA, None),
        ("Sherbrooke, QC, Canada", LocationBucket.CANADA, None),
        ("Colombie-Britannique, Canada", LocationBucket.CANADA, None),
        ("Télétravail", LocationBucket.UNKNOWN, RemoteScope.UNSPECIFIED),
        ("Télétravail - Canada", LocationBucket.CANADA, RemoteScope.CANADA),
        ("À distance, Canada", LocationBucket.CANADA, RemoteScope.CANADA),
        ("Travail à distance", LocationBucket.UNKNOWN, RemoteScope.UNSPECIFIED),
        ("Hybride", LocationBucket.UNKNOWN, None),
        ("Montréal, QC / Toronto, ON", LocationBucket.MONTREAL, None),
    ],
)
def test_french_locations(raw: str, bucket: LocationBucket, scope: RemoteScope | None) -> None:
    resolved = resolve_location(raw)
    assert resolved.bucket is bucket
    assert resolved.remote_scope is scope


@pytest.mark.parametrize(
    ("accented", "plain"),
    [
        ("Montréal, Québec", "Montreal, Quebec"),
        ("Québec City, QC", "Quebec City, QC"),
        ("Trois-Rivières, QC, Canada", "Trois-Rivieres, QC, Canada"),
        ("Télétravail", "Teletravail"),
    ],
)
def test_folding_makes_accents_irrelevant_to_matching(accented: str, plain: str) -> None:
    assert resolve_location(accented) == resolve_location(plain)


def test_location_lexicon_stays_out_of_the_company_namespace() -> None:
    from stage.lexicon import generic_company_tokens, location_lexicon

    assert "montreal" in location_lexicon().montreal
    assert "montreal" not in generic_company_tokens()


def test_an_ambiguous_province_name_needs_canadian_corroboration() -> None:
    assert resolve_location("Moncton, New Brunswick, Canada").bucket is LocationBucket.CANADA
    assert resolve_location("Fredericton, New Brunswick").bucket is LocationBucket.CANADA
    assert resolve_location("Nouveau-Brunswick, Canada").bucket is LocationBucket.CANADA
    assert resolve_location("New Brunswick, New Jersey, United States").bucket is (
        LocationBucket.USA
    ), "an explicit country must outrank a province name that is also a US city"


@pytest.mark.parametrize(
    ("raw", "bucket"),
    [
        ("Toronto, Ontario, CA", LocationBucket.CANADA),
        ("Waterloo, Ontario, CA", LocationBucket.CANADA),
        ("Windsor, Ontario, CA", LocationBucket.CANADA),
        ("Montreal, Quebec, CA", LocationBucket.MONTREAL),
        ("Ontario, CA", LocationBucket.USA),
        ("Ontario, California", LocationBucket.USA),
        ("Ontario, CA, USA", LocationBucket.USA),
        ("San Jose, CA", LocationBucket.USA),
    ],
)
def test_a_country_suffix_does_not_turn_a_province_into_california(
    raw: str, bucket: LocationBucket
) -> None:
    assert resolve_location(raw).bucket is bucket, (
        f"{raw!r}: a named city beside Ontario decides the country, not the CA suffix"
    )


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        ("Erlangen, BY, DE, 91058", "DE after a Bavarian code is Germany, not Delaware"),
        ("Bautzen, SN, DE", "Saxony, not Delaware"),
        ("Arequipa, ARE, PE", "PE after a Peruvian code is Peru, not Prince Edward Island"),
        ("Cusco, CUS, PE, 08000", "same, with a postcode"),
        ("Bhopal, MP, IN", "IN after an Indian state code is India, not Indiana"),
        ("CABA, B, AR, 1001", "AR after an Argentine code is Argentina, not Arkansas"),
    ],
)
def test_a_foreign_subdivision_makes_the_next_code_a_country(raw: str, why: str) -> None:
    assert resolve_location(raw).bucket is not LocationBucket.USA, why
    assert resolve_location(raw).bucket is not LocationBucket.CANADA, why


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Wilmington, DE", LocationBucket.USA),
        ("Charlottetown, PE", LocationBucket.CANADA),
        ("Montreal, QC, CA", LocationBucket.MONTREAL),
        ("Guelph, Ontario, CA", LocationBucket.CANADA),
        ("CAN, ON, Milton", LocationBucket.CANADA),
        ("Work From Home, CAN-AB", LocationBucket.CANADA),
        ("US, CA, Santa Clara", LocationBucket.USA),
        ("Winston-Salem, NC", LocationBucket.USA),
        ("New York, NY, United States", LocationBucket.USA),
        ("2 Locations | PT-Northeast VA", LocationBucket.USA),
    ],
)
def test_a_real_state_or_province_code_still_resolves(raw: str, expected: LocationBucket) -> None:
    assert resolve_location(raw).bucket is expected, raw


def test_common_spellings_collapse_onto_one_city() -> None:
    from stage.normalize.location import display_location

    assert display_location("SF") == "San Francisco"
    assert display_location("NYC") == "New York"
    assert display_location("New York City, NY") == "New York, NY"
    assert display_location("bangalore") == "Bengaluru"


def test_a_region_suffix_survives_normalization() -> None:
    from stage.normalize.location import display_location

    assert display_location("San Jose, CA") == "San Jose, CA"
    assert display_location("Toronto, ON, Canada") == "Toronto, ON, Canada"


def test_an_unknown_city_passes_through_untouched() -> None:
    from stage.normalize.location import display_location

    assert display_location("Kitchener, ON") == "Kitchener, ON"
    assert display_location("Nowhere Special") == "Nowhere Special"


def test_normalizing_an_empty_location_stays_empty() -> None:
    from stage.normalize.location import display_location

    assert display_location("") == ""
    assert display_location("   ") == ""


def test_a_multi_site_suffix_is_dropped() -> None:
    from stage.normalize.location import display_location

    assert display_location("Hong Kong +1") == "Hong Kong"
    assert display_location("Arlington, VA +2") == "Arlington, VA"


def test_metro_names_collapse_onto_the_city() -> None:
    from stage.normalize.location import display_location

    assert display_location("GTA") == "Toronto"
    assert display_location("DFW") == "Dallas"
    assert display_location("Bay Area") == "San Francisco"
    assert display_location("Philly") == "Philadelphia"


def test_shouting_boards_are_cased_like_the_rest() -> None:
    from stage.normalize.location import display_location

    assert display_location("POUGHKEEPSIE") == "Poughkeepsie"
    assert display_location("TORONTO") == "Toronto"
    assert display_location("BATON ROUGE") == "Baton Rouge"


def test_a_borough_that_names_another_city_is_left_alone() -> None:
    from stage.normalize.location import display_location

    assert display_location("Brooklyn, OH") == "Brooklyn, OH"
    assert display_location("Manhattan Beach, CA") == "Manhattan Beach, CA"


def test_two_cities_sharing_a_name_stay_apart() -> None:
    from stage.normalize.location import display_location

    assert display_location("Richmond, BC, Canada") == "Richmond, BC, Canada"
    assert display_location("Richmond, VA") == "Richmond, VA"


@pytest.mark.parametrize(
    ("raw", "bucket"),
    [
        ("Dublin, OH", LocationBucket.USA),
        ("Rome, NY", LocationBucket.USA),
        ("Athens, GA", LocationBucket.USA),
        ("Paris, TX", LocationBucket.USA),
        ("Berlin, NH", LocationBucket.USA),
        ("London, ON", LocationBucket.CANADA),
        ("Dublin, Ireland", LocationBucket.INTERNATIONAL),
        ("Rome, Italy", LocationBucket.INTERNATIONAL),
        ("Paris, France", LocationBucket.INTERNATIONAL),
        ("London, UK", LocationBucket.INTERNATIONAL),
        ("Dublin", LocationBucket.INTERNATIONAL),
        ("Berlin, DE", LocationBucket.INTERNATIONAL),
        ("Madrid, MD, ES", LocationBucket.INTERNATIONAL),
        ("Cork, CO, IE", LocationBucket.INTERNATIONAL),
    ],
)
def test_a_state_code_disambiguates_a_city_two_countries_share(
    raw: str, bucket: LocationBucket
) -> None:
    assert resolve_location(raw).bucket is bucket
