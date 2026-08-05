import pytest

from stage.lexicon import company_legal_suffixes, generic_company_tokens
from stage.services.discover import name_matches, slug_candidates


def test_the_lexicon_is_folded_at_load() -> None:
    generic = generic_company_tokens()
    assert "societe" in generic
    assert "société" not in generic
    assert "systemes" in generic
    assert "developpement" in generic
    assert "ltee" in company_legal_suffixes()


@pytest.mark.parametrize(
    "token", ["groupe", "societe", "banque", "compagnie", "gestion", "assurances", "reseau"]
)
def test_french_corporate_generics_are_present(token: str) -> None:
    assert token in generic_company_tokens()


def test_groupe_x_and_x_group_behave_the_same() -> None:
    english = slug_candidates("Group Dynamics")
    french = slug_candidates("Groupe Dynamique")
    assert "group" not in english.accepted
    assert "groupe" not in french.accepted
    assert english.accepted[0] == "groupdynamics"
    assert french.accepted[0] == "groupedynamique"


def test_a_french_generic_first_token_is_never_probed() -> None:
    plan = slug_candidates("Banque Nationale du Canada")
    assert "banque" not in plan.accepted
    assert any(slug == "banque" for slug, _ in plan.skipped)


def test_french_legal_suffixes_are_stripped_like_english_ones() -> None:
    assert slug_candidates("Genetec Ltée").accepted[0] == "genetec"
    assert slug_candidates("Coveo Inc").accepted[0] == "coveo"


def test_an_all_generic_french_overlap_is_weak_evidence() -> None:
    assert not name_matches("Groupe Solutions", "Groupe")
    assert name_matches("Groupe Solutions Québec", "Groupe Solutions")


def test_the_eligibility_lexicon_loads_folded_and_covers_both_languages() -> None:
    from stage.lexicon import eligibility_lexicon, fold

    lexicon = eligibility_lexicon()

    assert set(lexicon.degree_required) == {"phd", "masters", "bachelors"}
    for level, phrases in lexicon.degree_required.items():
        assert phrases, f"{level} has no phrases"
        for phrase in phrases:
            assert phrase == fold(phrase), f"{phrase!r} is not folded at load"

    for group in (lexicon.work_auth_excluded, lexicon.non_cs, lexicon.non_cs_rescue):
        assert group
        for phrase in group:
            assert phrase == fold(phrase), f"{phrase!r} is not folded at load"

    assert fold("doctorat requis") in lexicon.degree_required["phd"]
    assert any("infirmiere" in phrase for phrase in lexicon.non_cs)
    assert fold("developpeur") in lexicon.non_cs_rescue


def test_no_phrase_is_both_non_cs_and_a_rescue() -> None:
    from stage.lexicon import eligibility_lexicon

    lexicon = eligibility_lexicon()
    overlap = lexicon.non_cs & lexicon.non_cs_rescue
    assert not overlap, (
        f"{sorted(overlap)} would both reject and rescue the same title"
    )
