import json
from pathlib import Path

import pytest

from stage.lexicon import fold

FIXTURE = Path(__file__).parent / "fixtures" / "bilingual_titles.json"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Développeur·euse", "developpeur"),
        ("Programmeur·euse", "programmeur"),
        ("Directeur·trice", "directeur"),
        ("Conseiller·ère", "conseiller"),
        ("Concepteur·rice", "concepteur"),
        ("Technicien·ne", "technicien"),
        ("sénior·e", "senior"),
        ("Développeur.euse", "developpeur"),
        ("Programmeur.se", "programmeur"),
        ("Chef.fe", "chef"),
        ("Assistant.e", "assistant"),
        ("Créateur.rice", "createur"),
        ("Chef(fe)", "chef"),
        ("Ingénieur(e)", "ingenieur"),
        ("Chargé(e)", "charge"),
        ("Développeur(se)", "developpeur"),
        ("Programmeur(euse)", "programmeur"),
        ("Journalier(ère)", "journalier"),
        ("Desenvolvedor(a)", "desenvolvedor"),
        ("Franqueado(a)", "franqueado"),
    ],
)
def test_inclusive_forms_collapse_to_the_base(raw: str, expected: str) -> None:
    assert fold(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Vue.js", "vue js"),
        ("Node.js", "node js"),
        ("React.Js", "react js"),
        ("Securiti.ai", "securiti ai"),
        ("Software Engineer(s)", "software engineer s"),
        ("Washington, D.C.", "washington d c"),
        ("St. John's, NL", "st john s nl"),
        ("Abasolo, N.L., Mexico", "abasolo n l mexico"),
    ],
)
def test_lookalikes_survive_untouched(raw: str, expected: str) -> None:
    assert fold(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "Co-op Software Engineer",
        "Co-Op Developer Intern",
        "Full-Stack Engineer",
        "Front-End Developer",
        "Mid-Market Account Executive",
        "Pre-Sales Engineer",
    ],
)
def test_the_hyphen_is_not_an_inclusive_marker(raw: str) -> None:
    folded = fold(raw)
    assert "co op" in folded or len(folded.split()) >= 3
    assert fold("Co-op") == "co op"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Kitchener-Waterloo, ON", "kitchener waterloo on"),
        ("Trois-Rivières, QC", "trois rivieres qc"),
        ("Saint-Laurent, QC, Canada", "saint laurent qc canada"),
        ("Dollard-des-Ormeaux", "dollard des ormeaux"),
        ("Vaudreuil-Dorion", "vaudreuil dorion"),
        ("Eidos-Montréal", "eidos montreal"),
    ],
)
def test_hyphenated_place_names_are_unharmed(raw: str, expected: str) -> None:
    assert fold(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("AI/ML Intern", "ai ml intern"),
        ("Montreal/Toronto", "montreal toronto"),
        ("She/He", "she he"),
        ("Ruby/RoR Developer", "ruby ror developer"),
        ("Senior/Staff Engineer", "senior staff engineer"),
        ("Vitória/ES", "vitoria es"),
    ],
)
def test_the_slash_is_not_an_inclusive_marker(raw: str, expected: str) -> None:
    assert fold(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("les candidat(e)s", "les candidats"),
        ("étudiant(e)s", "etudiants"),
        ("développeur(euse)s", "developpeurs"),
        ("Chef(fe)s", "chefs"),
    ],
)
def test_the_inclusive_plural_collapses_to_the_masculine_plural(raw: str, expected: str) -> None:
    assert fold(raw) == expected


def test_a_space_before_the_median_dot_is_tolerated() -> None:
    assert fold("Concepteur ·trice de jeu sénior") == "concepteur de jeu senior"
    assert fold("technique. Le poste est ouvert") == "technique le poste est ouvert"


def test_the_suffix_vocabulary_is_data_not_code() -> None:
    from stage.lexicon import feminine_suffixes

    suffixes = feminine_suffixes()
    assert {"e", "euse", "rice", "trice", "ere"} <= suffixes
    assert "s" not in suffixes, "'s' would strip the plural from 'Engineer(s)'"


def test_the_loader_refuses_a_bare_s() -> None:
    import textwrap

    from stage.lexicon import LexiconError, feminine_suffixes

    feminine_suffixes.cache_clear()
    try:
        broken = Path(__import__("tempfile").mkdtemp())
        (broken / "inclusive_suffixes.yaml").write_text(
            textwrap.dedent("""
                feminine_suffixes:
                  - e
                  - s
                """),
            encoding="utf-8",
        )
        import os

        os.environ["STAGE_LEXICON"] = str(broken)
        with pytest.raises(LexiconError, match="Engineer"):
            feminine_suffixes()
    finally:
        os.environ.pop("STAGE_LEXICON", None)
        feminine_suffixes.cache_clear()


def test_a_collapsed_form_matches_the_plain_form() -> None:
    assert fold("Développeur·euse logiciel") == fold("Développeur logiciel")
    assert fold("Chef.fe d'équipe") == fold("Chef d'équipe")
    assert fold("Analyste financier·e") == fold("Analyste financier")


_ORPHANS = {"euse", "euses", "trice", "trices", "rice", "rices", "ere", "eres", "fe"}


def test_no_inclusive_suffix_survives_except_the_hyphen_form() -> None:
    pairs = json.loads(FIXTURE.read_text(encoding="utf-8"))["pairs"]
    assert pairs, "fixture is empty"
    leftover = {pair["fr"] for pair in pairs if set(fold(pair["fr"]).split()) & _ORPHANS}
    assert all("-" in text for text in leftover), leftover
    assert leftover == {"Modeleur-euse 3D Sénior(e)", "Programmeur-euse Outils"}, leftover


def test_the_hyphen_surface_forms_are_the_ones_the_lexicon_must_carry() -> None:
    assert fold("Modeleur-euse 3D Sénior(e)") == "modeleur euse 3d senior"
    assert fold("Programmeur-euse Outils") == "programmeur euse outils"


def test_the_fixture_is_real_employer_text() -> None:
    pairs = json.loads(FIXTURE.read_text(encoding="utf-8"))["pairs"]
    assert len(pairs) >= 30
    assert len({pair["company"] for pair in pairs}) >= 5
    assert sum(pair["internship"] for pair in pairs) >= 4
    for pair in pairs:
        assert pair["en"] and pair["fr"]
        assert pair["en"] in pair["title_raw"]


@pytest.mark.serial
def test_a_long_unbroken_run_of_letters_does_not_stall_the_fold() -> None:
    import time

    payload = "a" * 65536
    started = time.perf_counter()
    folded = fold.__wrapped__(payload)
    elapsed = time.perf_counter() - started

    assert folded == payload
    assert elapsed < 1.0, f"folding {len(payload)} chars took {elapsed:.1f}s, needs the stem bound"


def test_the_stem_bound_is_wide_enough_for_the_longest_real_word() -> None:
    from stage.lexicon import LONGEST_WORD_STEM

    stem = "a" * LONGEST_WORD_STEM
    assert fold.__wrapped__(f"{stem}·euse") == stem
    assert fold.__wrapped__("anticonstitutionnellement·euse") == "anticonstitutionnellement"
    assert fold.__wrapped__("Programmeur·euse") == "programmeur"
