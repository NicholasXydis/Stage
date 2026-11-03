import pytest

from stage.domain import LocationBucket, RemoteScope
from stage.normalize import resolve_listing, resolve_location


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
    ("raw", "expected", "why"),
    [
        (
            "GB-NU-CRAMLINGTON-ATLEY WAY NORTH NELSON INDUSTRIAL ESTATE",
            LocationBucket.INTERNATIONAL,
            "England, not Nunavut",
        ),
        ("HU-PE-FOT-EAST GATE BUSINESS PARK", LocationBucket.INTERNATIONAL, "not PEI"),
        ("ESP - MD - LAS ROZAS", LocationBucket.INTERNATIONAL, "Madrid, not Maryland"),
        ("SWE - AB - SOLNA", LocationBucket.INTERNATIONAL, "Stockholm, not Alberta"),
        ("BR-RJ-MACAE-ESTRADA DE FAZENDA 200", LocationBucket.INTERNATIONAL, "DE is a word"),
        ("CRI - SJ - SAN JOSE", LocationBucket.INTERNATIONAL, "San Jose, Costa Rica"),
        ("MEX - BCN - TIJUANA", LocationBucket.INTERNATIONAL, "Baja California"),
        ("GBR-London-5 Canada Square", LocationBucket.INTERNATIONAL, "a street in London"),
        ("CHN - DALIAN", LocationBucket.INTERNATIONAL, "a country code, then a city"),
        ("IND-BLR-Divyasree Technopolis", LocationBucket.INTERNATIONAL, "Bangalore"),
        ("DE-BY-NORDLINGEN-010 ~ BLDG 10", LocationBucket.INTERNATIONAL, "BY is no US state"),
        ("US-IA-CEDAR RAPIDS-124 ~ 400 Collins Rd NE", LocationBucket.USA, "the US prefix"),
        ("USA - AZ - Goodyear - PHX1", LocationBucket.USA, "the USA prefix"),
        ("CA-QC-LONGUEUIL-J01 ~ 1000 Blvd Marie-Victorin", LocationBucket.MONTREAL, "Quebec"),
        ("CAN - ON - TORONTO", LocationBucket.CANADA, "the CAN prefix"),
        ("CO - DEN - Denver", LocationBucket.USA, "Colorado when the city is American"),
        ("AND - Jacksonville, FL", LocationBucket.USA, "a word, not Andorra"),
        ("AUS-TX-Austin", LocationBucket.USA, "an airport code before a state"),
        ("MD - DC - VA", LocationBucket.USA, "a list of states"),
        ("PA - PHL - Remote", LocationBucket.USA, "a state, then an airport code"),
        ("DEU - Berlin, Montreal, QC", LocationBucket.MONTREAL, "a second place"),
        ("IRL - Dublin or Toronto, ON", LocationBucket.CANADA, "a second place"),
        ("GBR - London, USA - New York", LocationBucket.USA, "a second place"),
        (
            "MX-BCN-MEXICALI-238 ~ BLDG 238, Desarrollo Industrial Colorado",
            LocationBucket.INTERNATIONAL,
            "an address, not the state",
        ),
    ],
)
def test_a_workday_country_prefix_decides_the_country(
    raw: str, expected: LocationBucket, why: str
) -> None:
    assert resolve_location(raw).bucket is expected, why


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
        ("Walldorf, DE, 69190", LocationBucket.INTERNATIONAL),
        ("Wilmington, DE", LocationBucket.USA),
        ("Newark, DE", LocationBucket.USA),
    ],
)
def test_a_state_code_disambiguates_a_city_two_countries_share(
    raw: str, bucket: LocationBucket
) -> None:
    assert resolve_location(raw).bucket is bucket


@pytest.mark.parametrize(
    ("raw", "bucket"),
    [
        ("London, New York", LocationBucket.USA),
        ("New York, London", LocationBucket.USA),
        ("New York, London, or Paris", LocationBucket.USA),
        ("London, England, New York, New York", LocationBucket.USA),
        ("New York, San Francisco, Munich or London", LocationBucket.USA),
        ("London, Paris, New York, Singapore, Hong Kong", LocationBucket.USA),
        ("Dubai, London, Paris, Hong Kong, Sydney, Toronto", LocationBucket.CANADA),
        ("San Jose, Costa Rica", LocationBucket.INTERNATIONAL),
        ("Costa Rica, San Jose", LocationBucket.INTERNATIONAL),
        ("Los Angeles, Chile", LocationBucket.INTERNATIONAL),
        ("San Francisco, Heredia, Costa Rica", LocationBucket.INTERNATIONAL),
        ("Saint-Louis, Saint-Pierre, Reunion", LocationBucket.INTERNATIONAL),
        ("Chicago, IL, United States", LocationBucket.USA),
        ("Toronto, ON", LocationBucket.CANADA),
        ("Bengaluru, KA, IN", LocationBucket.INTERNATIONAL),
    ],
)
def test_a_list_of_cities_keeps_the_domestic_one(raw: str, bucket: LocationBucket) -> None:
    assert resolve_location(raw).bucket is bucket


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("Remote (United States | Canada)", "Remote (United States | Canada)"),
        ("New York (In-Office / Hybrid)", "New York (In-Office / Hybrid)"),
        ("IN-Indianapolis;6910 Network Place (Tribune-WXIN/WTTV)", "IN-Indianapolis +1"),
        ("Toronto (Hybrid; Remote) | New York, NY", "Toronto (Hybrid; Remote) +1"),
        ("Montreal, QC | Toronto, ON | Vancouver, BC", "Montreal, QC +2"),
        ("(an unclosed bracket | Boston", "(an unclosed bracket | Boston"),
        ("Austin ) | Boston", "Austin +1"),
    ],
)
def test_a_list_inside_brackets_stays_one_place(raw: str, shown: str) -> None:
    from stage.normalize.location import display_location

    assert display_location(raw) == shown


WORKDAY = "https://acme.wd5.myworkdayjobs.com/en-US/External/job/{}/Software-Intern_R1"


@pytest.mark.parametrize(
    ("raw", "place", "stored", "bucket"),
    [
        ("2 Locations", "Montral", "Montréal / 1 more", LocationBucket.MONTREAL),
        ("3 Locations", "Toronto-Ontario", "Toronto Ontario / 2 more", LocationBucket.CANADA),
        ("2 Locations", "Lvis", "Lévis / 1 more", LocationBucket.CANADA),
        (
            "2 Locations",
            "Irving-Texas---United-States-of-America",
            "Irving Texas - United States of America / 1 more",
            LocationBucket.USA,
        ),
        ("2 Locations", "Grand-Rapids", "Grand Rapids / 1 more", LocationBucket.USA),
        ("2 Locations", "Ratingen-Germany", "2 Locations", LocationBucket.UNKNOWN),
        ("2 Locations", "OCONUS-Diego-Garcia", "2 Locations", LocationBucket.UNKNOWN),
        ("Toronto, ON", "Montral", "Toronto, ON", LocationBucket.CANADA),
        ("2 Locations", "DE-Berlin", "2 Locations", LocationBucket.UNKNOWN),
        ("2 Locations", "Montr%E9al", "2 Locations", LocationBucket.UNKNOWN),
        ("2 Locations", "%E2%80%AEAustin-TX", "Austin TX / 1 more", LocationBucket.USA),
        ("1000 Locations", "Austin-TX", "1000 Locations", LocationBucket.UNKNOWN),
    ],
)
def test_a_multi_site_workday_posting_takes_its_first_place_only_at_home(
    raw: str, place: str, stored: str, bucket: LocationBucket
) -> None:
    from stage.normalize import listed_place

    found = listed_place(raw, WORKDAY.format(place))

    assert found == stored, "a first place abroad stays unknown: the second may be in Canada"
    assert resolve_location(found).bucket is bucket
    assert listed_place(found, WORKDAY.format(place)) == found


def test_a_multi_site_place_needs_a_workday_link_with_a_place() -> None:
    from stage.normalize import listed_place

    assert listed_place("2 Locations", "https://boards.greenhouse.io/acme/jobs/1") == "2 Locations"
    bare = "https://acme.wd5.myworkdayjobs.com/External/job/Software-Intern_R1"
    assert listed_place("2 Locations", bare) == "2 Locations"


def test_the_extra_places_count_toward_the_plus() -> None:
    from stage.normalize.location import display_location

    assert display_location("Montréal / 2 more") == "Montreal +2"
    assert display_location("Montreal, QC / Toronto, ON") == "Montreal, QC +1"


def test_a_dropped_accent_never_maps_a_short_word() -> None:
    from stage.lexicon import accent_dropped_words

    words = accent_dropped_words()

    assert words["montral"] == "Montréal" and words["qubec"] == "Québec"
    assert all(len(key) >= 4 for key in words), "Le-Creusot must not become Île-Creusot"


@pytest.mark.parametrize(
    ("raw", "bucket"),
    [
        ("Burlington, VT", LocationBucket.USA),
        ("Ottawa, IL, United States", LocationBucket.USA),
        ("Burlington MA", LocationBucket.USA),
        ("Burlington, ON", LocationBucket.CANADA),
        ("Toronto, CA", LocationBucket.CANADA),
        ("410 GEORGIA ST W, FLOOR 3:VANCOUVER", LocationBucket.CANADA),
    ],
)
def test_a_us_state_code_names_the_us_town_with_a_canadian_name(
    raw: str, bucket: LocationBucket
) -> None:
    assert resolve_location(raw).bucket is bucket


@pytest.mark.parametrize(
    ("raw", "bucket"),
    [
        ("Xyzville, MH, IND", LocationBucket.INTERNATIONAL),
        ("Xyzville, FR", LocationBucket.INTERNATIONAL),
        ("Xyzville, Fr", LocationBucket.UNKNOWN),
        ("Xyzville, ind", LocationBucket.UNKNOWN),
        ("Xyzville, IN", LocationBucket.USA),
        ("Xyzville, CAN", LocationBucket.CANADA),
        ("Toronto, ON, CAN", LocationBucket.CANADA),
    ],
)
def test_a_trailing_country_code_counts_only_when_nothing_else_placed_it(
    raw: str, bucket: LocationBucket
) -> None:
    assert resolve_location(raw).bucket is bucket


@pytest.mark.parametrize(
    ("place", "stored", "bucket"),
    [
        ("Kentucky", "Kentucky", LocationBucket.USA),
        ("Ohio---Columbus", "Ohio - Columbus", LocationBucket.USA),
        ("Montral", "Montréal", LocationBucket.MONTREAL),
        ("Ratingen-Germany", "Ratingen Germany", LocationBucket.INTERNATIONAL),
        ("Remote", "", LocationBucket.UNKNOWN),
        ("Xyzzy", "", LocationBucket.UNKNOWN),
    ],
)
def test_a_workday_posting_with_no_place_takes_the_one_in_its_link(
    place: str, stored: str, bucket: LocationBucket
) -> None:
    from stage.normalize import listed_place

    found = listed_place("", WORKDAY.format(place))

    assert found == stored
    assert resolve_location(found).bucket is bucket
    assert listed_place(found, WORKDAY.format(place)) == found
    assert listed_place("", "https://boards.greenhouse.io/acme/jobs/1") == ""


@pytest.mark.parametrize(
    ("raw", "bucket"),
    [
        ("Ehningen, DE", LocationBucket.INTERNATIONAL),
        ("Saint Nazaire Area", LocationBucket.INTERNATIONAL),
        ("Aix-en-Provence Les Milles", LocationBucket.INTERNATIONAL),
        ("VALBONNE (Sophia)", LocationBucket.INTERNATIONAL),
        ("Spittal A.D. Drau", LocationBucket.INTERNATIONAL),
        ("Canary Wharf, 1 Churchill Place", LocationBucket.INTERNATIONAL),
        ("Tianjin (Teda)", LocationBucket.INTERNATIONAL),
        ("CRETEIL - CRE1", LocationBucket.INTERNATIONAL),
        ("Queretaro", LocationBucket.INTERNATIONAL),
        ("Wilmington, DE", LocationBucket.USA),
        ("Newark, DE", LocationBucket.USA),
    ],
)
def test_foreign_sites_kept_as_unknown_on_real_data_now_resolve(
    raw: str, bucket: LocationBucket
) -> None:
    assert resolve_location(raw).bucket is bucket, raw


@pytest.mark.parametrize(
    ("raw", "bucket"),
    [
        ("Sophia Antipolis, Alpes-Maritimes, France, FR", LocationBucket.INTERNATIONAL),
        ("Blainville-sur-Orne, Normandie, France", LocationBucket.INTERNATIONAL),
        ("Portage la Prairie, MB", LocationBucket.CANADA),
        ("La Prairie, QC", LocationBucket.MONTREAL),
        ("Casalecchio di Reno", LocationBucket.INTERNATIONAL),
        ("Stockton-on-Tees", LocationBucket.INTERNATIONAL),
        ("Melbourne, Florida", LocationBucket.USA),
    ],
)
def test_the_longest_place_name_decides_the_country(raw: str, bucket: LocationBucket) -> None:
    assert resolve_location(raw).bucket is bucket, raw


@pytest.mark.parametrize(
    ("raw", "bucket"),
    [
        ("Paris, Texas", LocationBucket.USA),
        ("Dublin, Ohio", LocationBucket.USA),
        ("Athens, Georgia", LocationBucket.USA),
        ("London, Kentucky", LocationBucket.USA),
        ("Dublin, Leinster, Ireland", LocationBucket.INTERNATIONAL),
        ("Tbilisi, Georgia", LocationBucket.INTERNATIONAL),
        ("Paris", LocationBucket.INTERNATIONAL),
    ],
)
def test_a_state_name_places_a_city_two_countries_share(raw: str, bucket: LocationBucket) -> None:
    assert resolve_location(raw).bucket is bucket, raw


@pytest.mark.parametrize(
    ("raw", "bucket", "scope"),
    [
        ("Virtual, CAN", LocationBucket.CANADA, RemoteScope.CANADA),
        ("CAN - Remote", LocationBucket.CANADA, RemoteScope.CANADA),
        ("KOHO (CAN)", LocationBucket.CANADA, None),
        ("London, ON, CAN", LocationBucket.CANADA, None),
        ("Can be remote", LocationBucket.UNKNOWN, RemoteScope.UNSPECIFIED),
    ],
)
def test_a_bare_can_field_is_canada(raw: str, bucket: LocationBucket, scope: RemoteScope) -> None:
    resolved = resolve_location(raw)
    assert resolved.bucket is bucket, raw
    assert resolved.remote_scope is scope, raw


@pytest.mark.parametrize(
    ("raw", "bucket"),
    [
        ("Reading, GBR, RG6 1PT", LocationBucket.INTERNATIONAL),
        ("Kingston 5, JM", LocationBucket.INTERNATIONAL),
        ("Xyzville, NI, DE", LocationBucket.INTERNATIONAL),
        ("Leuna, ST, DE, 06237", LocationBucket.INTERNATIONAL),
        ("San Isidro, LIM, PE", LocationBucket.INTERNATIONAL),
        ("Saharanpur, UP, IN", LocationBucket.INTERNATIONAL),
        ("Xyzville-MAX, CO", LocationBucket.UNKNOWN),
    ],
)
def test_a_trailing_country_skips_postcodes_and_follows_a_foreign_region(
    raw: str, bucket: LocationBucket
) -> None:
    assert resolve_location(raw).bucket is bucket, raw


@pytest.mark.parametrize(
    ("raw", "bucket"),
    [
        ("VN-HO CHI MINH-285 CACH MANG THANG TAM STREET", LocationBucket.INTERNATIONAL),
        ("OM-MUSCAT-2ND FL AL RAID HOUSE QURUM BUS CTR", LocationBucket.INTERNATIONAL),
        ("GY-Demerara-Georgetown-Tract X", LocationBucket.INTERNATIONAL),
        ("MX, Baja", LocationBucket.INTERNATIONAL),
        ("SG, 048583", LocationBucket.INTERNATIONAL),
        ("AUS > Whitby > South Western Hwy", LocationBucket.INTERNATIONAL),
        ("DEU > Penig > Am Zeisig 9", LocationBucket.INTERNATIONAL),
        ("OC-JPN-Iwakuni", LocationBucket.INTERNATIONAL),
        ("GBDER01 - Derby - Plot 5", LocationBucket.INTERNATIONAL),
        ("ZAJNB19 - Johannesburg - Randjespark, Denver", LocationBucket.INTERNATIONAL),
        ("PT-Tucson", LocationBucket.UNKNOWN),
        ("TO, ON", LocationBucket.UNKNOWN),
        ("KW, Toronto, Winnipeg, Vancouver, or Remote Canada", LocationBucket.CANADA),
        ("OC - Irvine", LocationBucket.USA),
        ("USHOU19 - The Woodlands - 8770 New Trails Drive", LocationBucket.USA),
        ("INFKN01 - Farukh Nagar - SY Logistic Park", LocationBucket.INTERNATIONAL),
        ("CRSJO01 - San Jose - Epic Corporate Center", LocationBucket.INTERNATIONAL),
        ("CAN > QC > St. Laurent > Poirier", LocationBucket.MONTREAL),
        ("IN-Indianapolis", LocationBucket.USA),
    ],
)
def test_a_leading_country_code_or_site_code_decides_the_country(
    raw: str, bucket: LocationBucket
) -> None:
    assert resolve_location(raw).bucket is bucket, raw


@pytest.mark.parametrize(
    ("raw", "bucket"),
    [
        ("Monterrey, ON, CA", LocationBucket.CANADA),
        ("Vernon, BC, CA, V1T4Y6", LocationBucket.CANADA),
        ("Monterrey, NL, Mexico", LocationBucket.INTERNATIONAL),
    ],
)
def test_a_province_code_then_ca_is_a_canadian_address(raw: str, bucket: LocationBucket) -> None:
    assert resolve_location(raw).bucket is bucket, raw


@pytest.mark.parametrize(
    ("raw", "bucket"),
    [
        ("St-Laurent, Québec, CA, H4R 1K2", LocationBucket.MONTREAL),
        ("Saint-Leonard, QC", LocationBucket.MONTREAL),
        ("Saint-Leonard, NB, Canada", LocationBucket.CANADA),
        ("Kirkland, QC, Canada", LocationBucket.MONTREAL),
        ("Kirkland Lake, ON", LocationBucket.CANADA),
        ("Kirkland, WA", LocationBucket.USA),
        ("Rosemère, Canada", LocationBucket.MONTREAL),
        ("Mascouche", LocationBucket.MONTREAL),
        ("Varennes, QC", LocationBucket.MONTREAL),
        ("Varennes", LocationBucket.UNKNOWN),
        ("Laval", LocationBucket.UNKNOWN),
        ("Saint-Étienne-des-Grès, QC, CA", LocationBucket.CANADA),
        ("St Laurent du Var", LocationBucket.INTERNATIONAL),
    ],
)
def test_montreal_area_towns_need_quebec_when_the_name_travels(
    raw: str, bucket: LocationBucket
) -> None:
    assert resolve_location(raw).bucket is bucket, raw


@pytest.mark.parametrize(
    ("raw", "bucket"),
    [
        ("Jamaica, NY", LocationBucket.USA),
        ("Malta, NY", LocationBucket.USA),
        ("Lebanon, PA", LocationBucket.USA),
        ("South Jordan, UT", LocationBucket.USA),
        ("North Wales, PA", LocationBucket.USA),
        ("New England", LocationBucket.USA),
        ("Jamaica", LocationBucket.INTERNATIONAL),
        ("Glasgow", LocationBucket.INTERNATIONAL),
        ("Glasgow, KY", LocationBucket.USA),
        ("Lund, BC", LocationBucket.CANADA),
        ("Warrington, PA", LocationBucket.USA),
        ("Washington", LocationBucket.USA),
        ("OCONUS-Guam", LocationBucket.USA),
    ],
)
def test_a_town_named_after_a_foreign_place_keeps_its_country(
    raw: str, bucket: LocationBucket
) -> None:
    assert resolve_location(raw).bucket is bucket, raw


@pytest.mark.parametrize(
    ("raw", "place", "stored", "bucket"),
    [
        ("", "Guyancourt", "Guyancourt", LocationBucket.INTERNATIONAL),
        ("2 Locations", "Guyancourt", "2 Locations", LocationBucket.UNKNOWN),
        ("2 Locations", "Needham", "Needham / 1 more", LocationBucket.USA),
        ("", "Remote---CAN", "Remote - CAN", LocationBucket.CANADA),
        ("", "St-Jrme-QC", "St Jérôme QC", LocationBucket.CANADA),
    ],
)
def test_a_workday_link_place_uses_the_wider_lexicon(
    raw: str, place: str, stored: str, bucket: LocationBucket
) -> None:
    from stage.normalize import listed_place

    found = listed_place(raw, WORKDAY.format(place))

    assert found == stored
    assert resolve_location(found).bucket is bucket


@pytest.mark.parametrize(
    ("title", "stored", "bucket"),
    [
        ("2027 | APEJ | Singapore | Engineering | Summer Analyst", "Singapore", "international"),
        ("2027 | Americas | New York City | Risk | Summer Analyst", "New York City", "usa"),
        ("2027 | EMEA | Birmingham | Legal | New Analyst", "Birmingham, EMEA", "international"),
        ("2027 | Americas | Birmingham | Legal | New Analyst", "", "unknown"),
        ("2027| EMEA Frankfurt Investment Banking| New Analyst", "", "unknown"),
        ("Software Engineer Intern | Toronto | 2027", "", "unknown"),
    ],
)
def test_a_blank_place_takes_the_city_from_a_year_region_city_title(
    title: str, stored: str, bucket: str
) -> None:
    from stage.normalize import listed_place

    found = listed_place("", "https://higher.gs.com/roles/150859_GS_CAMPUS", title)

    assert found == stored
    assert resolve_location(found).bucket.value == bucket
    assert listed_place("Paris, France", "", title) == "Paris, France"


def test_screening_reads_the_title_when_the_place_is_blank() -> None:
    from datetime import UTC, datetime

    from stage.domain import Job
    from stage.services.screening import normalize_batch

    when = datetime(2026, 8, 3, tzinfo=UTC)
    posting = Job(
        id="custom_json:goldman:1",
        source="custom_json",
        company="Goldman Sachs",
        title_raw="2027 | APEJ | Singapore | Engineering | Summer Analyst",
        title_normalized="summer analyst",
        apply_url_raw="https://higher.gs.com/roles/150859_GS_CAMPUS",
        description="",
        location_raw="",
        first_seen=when,
        last_seen=when,
    )

    kept, rejected = normalize_batch([posting])

    assert not kept and rejected[0].location is LocationBucket.INTERNATIONAL
    assert rejected[0].location_raw == "Singapore"


@pytest.mark.parametrize(
    ("raw", "bucket"),
    [
        ("Ontario, New York", LocationBucket.USA),
        ("Ontario, NY", LocationBucket.USA),
        ("Ontario, California", LocationBucket.USA),
        ("Toronto, Ontario", LocationBucket.CANADA),
        ("Ontario, Canada", LocationBucket.CANADA),
    ],
)
def test_the_us_towns_named_ontario_stay_in_the_us(raw: str, bucket: LocationBucket) -> None:
    assert resolve_location(raw).bucket is bucket


@pytest.mark.parametrize(
    ("raw", "bucket"),
    [
        ("Concord, Ontario, CA", LocationBucket.CANADA),
        ("Ontario, CA", LocationBucket.USA),
        ("Ontario, , United States", LocationBucket.USA),
        ("2003 West Fourth St, Ontario, OH", LocationBucket.USA),
        ("Burlington Massachusetts", LocationBucket.USA),
        ("Oakville, California", LocationBucket.USA),
        ("Scarborough, Maine, USA", LocationBucket.USA),
        ("410 GEORGIA ST W, FLOOR 3:VANCOUVER", LocationBucket.CANADA),
        ("Boston, MA Headquarters - (NB)", LocationBucket.USA),
        ("Moncton, NB", LocationBucket.CANADA),
        ("CA, N5A6S9", LocationBucket.CANADA),
        ("Toronto, Bay Wellington Tower L47", LocationBucket.CANADA),
        ("NS Sydney", LocationBucket.CANADA),
        ("Lebanon, IN", LocationBucket.USA),
        ("Warsaw, IN", LocationBucket.USA),
        ("WV, Ghent", LocationBucket.USA),
        ("Danbury, CT - Global", LocationBucket.USA),
        ("Austin TX, Chicago IL, London UK", LocationBucket.USA),
        ("Maryland HQ + Boston Office + Ireland - Office - Dublin", LocationBucket.USA),
    ],
)
def test_a_state_beats_a_shared_name(raw: str, bucket: LocationBucket) -> None:
    assert resolve_location(raw).bucket is bucket, raw


@pytest.mark.parametrize(
    ("raw", "bucket"),
    [
        ("Lasalle, ON, CA, N9H1R9", LocationBucket.CANADA),
        ("Lasalle, Ontario, Canada", LocationBucket.CANADA),
        ("Laval, QC, Canada", LocationBucket.MONTREAL),
        ("Vaudreuil, CA-QC, Canada", LocationBucket.MONTREAL),
        ("Canada - Quebec - Kirkland", LocationBucket.MONTREAL),
        ("Lachine", LocationBucket.MONTREAL),
        ("Lachine, CA", LocationBucket.MONTREAL),
    ],
)
def test_a_montreal_suburb_needs_quebec(raw: str, bucket: LocationBucket) -> None:
    assert resolve_location(raw).bucket is bucket, raw


@pytest.mark.parametrize(
    ("raw", "bucket"),
    [
        ("Temuco, AR, CL", LocationBucket.INTERNATIONAL),
        ("Lagos, LA, NG", LocationBucket.INTERNATIONAL),
        ("Mumbai, MH, IN / TN,IN / KA,IN", LocationBucket.INTERNATIONAL),
        ("Rijen, NB, NL / Sliedrecht, ZH, NL", LocationBucket.INTERNATIONAL),
        ("Joure, FR, NL, 8501 XC", LocationBucket.INTERNATIONAL),
        ("D.C., MD, VA", LocationBucket.USA),
        ("Charleston-WV---5701-MacCorkle-Avenue-SE", LocationBucket.USA),
        ("Schwaebisch Gmuend, DE", LocationBucket.INTERNATIONAL),
        ("Wilmington, DE", LocationBucket.USA),
        ("DE - Berlin", LocationBucket.INTERNATIONAL),
        ("PE - Lima", LocationBucket.INTERNATIONAL),
        ("IN, Delhi, New Delhi", LocationBucket.INTERNATIONAL),
        ("AL Paris HO - Digital & IT", LocationBucket.INTERNATIONAL),
        ("AU-Adelaide", LocationBucket.INTERNATIONAL),
        ("POL, Remote", LocationBucket.INTERNATIONAL),
        ("SG", LocationBucket.INTERNATIONAL),
        ("DE", LocationBucket.UNKNOWN),
        ("DE Remote", LocationBucket.UNKNOWN),
        (
            "DE / Courbevoie, Île-de-France, FR / NL / FR / Munich, BY, DE / Berlin, Berlin, DE",
            LocationBucket.INTERNATIONAL,
        ),
        (
            "Tel Aviv-Yafo, Tel Aviv District, IL / Ra'anana, Center District, IL"
            " / Yokne'am Illit, North District, IL",
            LocationBucket.INTERNATIONAL,
        ),
    ],
)
def test_a_code_that_names_a_country_is_not_a_state(raw: str, bucket: LocationBucket) -> None:
    assert resolve_location(raw).bucket is bucket, raw


@pytest.mark.parametrize(
    ("raw", "bucket"),
    [
        ("London OR Dublin", LocationBucket.INTERNATIONAL),
        ("Dublin, London OR Berlin", LocationBucket.INTERNATIONAL),
        ("Wilsonville OR", LocationBucket.USA),
        ("Woodburn, OR Retail", LocationBucket.USA),
        ("San Jose, San Jose, Costa Rica", LocationBucket.INTERNATIONAL),
        ("Baku / Almaty / Georgia / CIS", LocationBucket.INTERNATIONAL),
        ("Georgia", LocationBucket.USA),
        ("Atlanta / Georgia", LocationBucket.USA),
    ],
)
def test_a_list_reads_its_words_in_context(raw: str, bucket: LocationBucket) -> None:
    assert resolve_location(raw).bucket is bucket, raw


@pytest.mark.parametrize(
    ("place", "title", "bucket"),
    [
        ("Laval", "STAGE - Ingénieur Systèmes Embarqués F/H", LocationBucket.INTERNATIONAL),
        ("Laval", "Analyste TI", LocationBucket.UNKNOWN),
        (
            "2 Locations",
            "Praktikant:in Quantitative Investments (m/w/d)",
            LocationBucket.INTERNATIONAL,
        ),
        ("", "ゲームAIリサーチインターン｜Game AI Research Intern", LocationBucket.INTERNATIONAL),
        (
            "3 Locations",
            "Red Team Intern - Summer 2027 (Remote, GBR)",
            LocationBucket.INTERNATIONAL,
        ),
        ("", "CS27 BAC+2 Développeur Web (H/F)", LocationBucket.INTERNATIONAL),
        ("TPIN-GGN", "Customer Data Analyst Intern", LocationBucket.INTERNATIONAL),
        ("Gentilly", "stage 6 mois - data analyst audit interne", LocationBucket.INTERNATIONAL),
        ("2 Locations", "stage 4 à 6 mois documentation", LocationBucket.INTERNATIONAL),
        ("", "Stage de 4 mois en développement logiciel", LocationBucket.UNKNOWN),
        ("Tan Uyen", "Automation Engineer Internship", LocationBucket.INTERNATIONAL),
        ("Vitry-sur-Seine", "Data Intern", LocationBucket.INTERNATIONAL),
        ("19F The Globe Tower", "MLOps Engineer Intern - EDS", LocationBucket.INTERNATIONAL),
        ("Globe Life Field, Arlington, TX", "Data Intern", LocationBucket.USA),
        (
            "Mch CAMP",
            "Working Student - Full-stack development (f/m/div)",
            LocationBucket.INTERNATIONAL,
        ),
        ("2 Locations", "APAC Ecommerce Backend Intern - 2027", LocationBucket.INTERNATIONAL),
        ("", "Quantitative Research Analyst Intern Bs Ms Europe 2", LocationBucket.INTERNATIONAL),
        ("", "Software Engineer Intern Asia", LocationBucket.INTERNATIONAL),
        ("New York, NY", "EMEA Markets Technology Intern", LocationBucket.USA),
        ("Montreal", "Coordinateur de projets (F/H)", LocationBucket.MONTREAL),
        ("Remote", "Industrial Placement (London)", LocationBucket.UNKNOWN),
        ("Remote", "Software Engineer Intern (Remote, US)", LocationBucket.UNKNOWN),
        (
            "Birmingham",
            "2027 | EMEA | Birmingham | Engineering | Summer Analyst",
            LocationBucket.INTERNATIONAL,
        ),
        (
            "Toronto",
            "2027 | Americas | Toronto | Engineering | Summer Analyst",
            LocationBucket.CANADA,
        ),
        (
            "",
            "Stage - Développeur- se Java Fullstack - Services Financiers - Niort",
            LocationBucket.INTERNATIONAL,
        ),
        ("", "Software Developer Intern - London", LocationBucket.UNKNOWN),
        (
            "Flexible / Remote",
            "Summer 2027 Intern - Forward Deployed Engineer - Global Public Sector",
            LocationBucket.UNKNOWN,
        ),
        ("", "Data Engineer Intern - Summer 2027", LocationBucket.UNKNOWN),
    ],
)
def test_a_title_marks_an_unknown_place_abroad(
    place: str, title: str, bucket: LocationBucket
) -> None:
    assert resolve_listing(place, title).bucket is bucket, title
