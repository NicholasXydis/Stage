import time

import pytest

from stage.sources._text import LONGEST_TAG, collapse_whitespace, strip_html


def test_script_and_style_content_never_becomes_description_text() -> None:
    body = "<p>Stagiaire</p><script>alert('x')</script><style>a{color:red}</style><p>Montréal</p>"
    stripped = strip_html(body)
    assert "alert" not in stripped and "color:red" not in stripped
    assert stripped.startswith("Stagiaire") and stripped.endswith("Montréal")


def test_block_tags_become_line_breaks_and_entities_are_decoded() -> None:
    assert strip_html("<li>A&amp;B</li><li>C</li>") == "A&B\nC"
    assert strip_html("Caf&eacute; &lt;b&gt;bold&lt;/b&gt;") == "Café bold"


def test_control_characters_never_survive_extraction() -> None:
    assert strip_html("<p>Intern\x1b[31m\x00 role</p>") == "Intern[31m role"
    assert collapse_whitespace("a\x07 b\n c") == "a b c"


MIB = 1024 * 1024

PATHOLOGICAL = {
    "unterminated tags": "<" * MIB,
    "unterminated tags then one close": "<" * MIB + ">",
    "unterminated attributes": "<style x='" * 100_000,
    "opens with no closing tag": "<style>" * 100_000,
    "alternating unclosed raw elements": "<script>a<style>b" * 60_000,
    "trailing spaces with no newline": " " * MIB,
    "tabs before a newline": ("\t" * 4_000 + "\n") * 260,
    "realistic markup": "<p>Stagiaire <b>dev</b> à Montréal.</p>\n" * 26_000,
}


@pytest.mark.parametrize("label", list(PATHOLOGICAL))
@pytest.mark.serial
def test_no_shape_of_hostile_markup_stalls_extraction(label: str) -> None:
    payload = PATHOLOGICAL[label]
    started = time.perf_counter()
    strip_html(payload)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.5, f"{label}: {len(payload)} chars took {elapsed:.1f}s, must stay linear"


def test_a_tag_longer_than_the_bound_survives_as_text_and_that_is_the_trade() -> None:
    oversized = "<a " + "x" * (LONGEST_TAG + 1) + ">Apply"
    stripped = strip_html(oversized)
    assert stripped.endswith(">Apply")
    assert strip_html("<a " + "x" * 400 + ">Apply") == "Apply"


def test_an_unclosed_raw_element_leaves_the_rest_of_the_body_intact() -> None:
    assert strip_html("<script>console.log(1)") == "console.log(1)"
    assert strip_html("<style>a{}<p>Stagiaire</p>") == "a{}Stagiaire"
    assert strip_html("<script>x</script><style>y") == "y"


def test_a_closing_tag_must_match_the_element_it_closes() -> None:
    assert strip_html("<script>keep</style>me</script>") == ""
    assert strip_html("<p>a</p><script>b</script><p>c</p>") == "a\n c"


def test_a_feed_emoji_never_reaches_a_stored_title() -> None:
    assert collapse_whitespace("\U0001f525 Software Developer Intern") == (
        "Software Developer Intern"
    )
    assert collapse_whitespace("Account Executive - Canada \U0001f1e8\U0001f1e6") == (
        "Account Executive - Canada"
    )


def test_stripping_pictographs_leaves_ordinary_text_alone() -> None:
    assert collapse_whitespace("Stagiaire en développement  logiciel (H/F)") == (
        "Stagiaire en développement logiciel (H/F)"
    )
    assert collapse_whitespace("C++ / C# Engineer — Intern") == "C++ / C# Engineer — Intern"


def test_a_title_made_only_of_emoji_keeps_its_original_text() -> None:
    assert collapse_whitespace("\U0001f525") == "\U0001f525", (
        "cleaning must never leave a posting with no title at all"
    )
    assert collapse_whitespace("\U0001f1e7\U0001f1ea") == "\U0001f1e7\U0001f1ea"
