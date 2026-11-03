from pathlib import Path

import pytest
import yaml

from stage.companies import RegistryError, load_companies, write_registry
from stage.domain import Company, Platform
from stage.paths import registry_path

ROWS = (
    Company(name="Acme", platform=Platform.GREENHOUSE, slug="acme"),
    Company(name="Beta", platform=Platform.LEVER, slug="beta"),
    Company(name="Élan", platform=Platform.ASHBY, slug="elan"),
    Company(name="1Password", platform=Platform.ASHBY, slug="1password"),
)


def test_the_shipped_registry_is_a_directory_of_shards() -> None:
    source = registry_path()
    assert source.is_dir(), "the packaged registry is a directory of per-letter files"
    shards = sorted(path.name for path in source.glob("*.yaml"))
    assert len(shards) > 1, "a single shard defeats the point of splitting the registry"
    assert load_companies(source), "the shipped shards must load"


def test_a_row_lands_in_the_shard_its_folded_first_letter_names(tmp_path: Path) -> None:
    write_registry(ROWS, tmp_path)
    names = {path.stem for path in tmp_path.glob("*.yaml")}
    assert {"a", "b", "e", "other"} <= names, f"unexpected shard layout: {sorted(names)}"
    accented = yaml.safe_load((tmp_path / "e.yaml").read_text(encoding="utf-8"))
    assert accented[0]["name"] == "Élan", "an accented name must fold before it is filed"
    digits = yaml.safe_load((tmp_path / "other.yaml").read_text(encoding="utf-8"))
    assert digits[0]["name"] == "1Password", "a name outside a-z belongs in the other shard"


def test_every_row_survives_a_split_and_reload(tmp_path: Path) -> None:
    write_registry(ROWS, tmp_path)
    reloaded = load_companies(tmp_path)
    assert {company.name for company in reloaded} == {company.name for company in ROWS}
    assert {company.slug for company in reloaded} == {company.slug for company in ROWS}


def test_a_shard_left_empty_by_a_rewrite_is_removed(tmp_path: Path) -> None:
    write_registry(ROWS, tmp_path)
    assert (tmp_path / "b.yaml").exists()
    write_registry([row for row in ROWS if not row.name.startswith("Beta")], tmp_path)
    assert not (tmp_path / "b.yaml").exists(), (
        "a stale shard would resurrect the rows a rewrite removed"
    )
    assert {company.name for company in load_companies(tmp_path)} == {"Acme", "Élan", "1Password"}


def test_a_single_file_registry_still_loads_and_writes(tmp_path: Path) -> None:
    target = tmp_path / "companies.yaml"
    write_registry(ROWS, target)
    assert target.is_file(), "an explicit .yaml path must stay one file"
    assert len(load_companies(target)) == len(ROWS)


def test_a_duplicate_board_across_two_shards_is_still_refused(tmp_path: Path) -> None:
    row = {"name": "Acme", "platform": "greenhouse", "slug": "acme"}
    (tmp_path / "a.yaml").write_text(yaml.safe_dump([row]), encoding="utf-8")
    (tmp_path / "z.yaml").write_text(yaml.safe_dump([{**row, "name": "Zeta"}]), encoding="utf-8")
    with pytest.raises(RegistryError, match="duplicate board"):
        load_companies(tmp_path)


def test_a_broken_shard_names_the_file_that_broke(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text(
        yaml.safe_dump([{"name": "Acme", "platform": "greenhouse", "slug": "acme"}]),
        encoding="utf-8",
    )
    (tmp_path / "q.yaml").write_text(
        "- {name: Q, platform: greenhouse, slug: q, last_verified: nope}\n", encoding="utf-8"
    )
    with pytest.raises(RegistryError, match="q.yaml"):
        load_companies(tmp_path)


def test_a_change_to_one_row_leaves_every_other_shard_untouched(tmp_path: Path) -> None:
    from dataclasses import replace

    write_registry(ROWS, tmp_path)
    other = tmp_path / "b.yaml"
    other.write_text(other.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    before = other.read_bytes()

    write_registry((replace(ROWS[0], enabled=False), *ROWS[1:]), tmp_path)

    assert other.read_bytes() == before
    assert "enabled: false" in (tmp_path / "a.yaml").read_text(encoding="utf-8")


def test_hidden_files_beside_the_shards_are_ignored(tmp_path: Path) -> None:
    folder = tmp_path / "registry"
    folder.mkdir()
    (folder / "a.yaml").write_text("- name: Acme\n  platform: greenhouse\n  slug: acme\n", "utf-8")
    (folder / "._a.yaml").write_bytes(b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        ")

    assert [company.name for company in load_companies(folder)] == ["Acme"]


def test_every_shard_keeps_its_rows_in_name_order() -> None:
    for shard in sorted(registry_path().glob("*.yaml")):
        names = [row["name"] for row in yaml.safe_load(shard.read_text(encoding="utf-8")) or []]
        pairs = zip(names, names[1:], strict=False)
        unordered = [(a, b) for a, b in pairs if a.lower() > b.lower()]
        assert unordered == [], f"{shard.name}: {unordered[:3]}"
