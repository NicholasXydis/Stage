import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "stage"


def _python_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_only_the_typer_wrapper_calls_asyncio_run() -> None:
    call_sites = [
        f"{path.relative_to(SRC).as_posix()}:{number}"
        for path in _python_files()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if "asyncio.run(" in line
    ]
    assert len(call_sites) == 1
    assert call_sites[0].startswith("cli/app.py:")


def test_services_never_renders() -> None:
    forbidden = ("rich", "print(", "sys.stdout", "typer", "textual")
    for path in sorted((SRC / "services").rglob("*.py")):
        body = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in body, f"{path.name} contains presentation token {token!r}"


def test_domain_depends_on_nothing_outside_itself() -> None:
    allowed_prefixes = ("stage.domain",)
    for path in sorted((SRC / "domain").rglob("*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            if "stage" not in stripped:
                assert not stripped.startswith("from pydantic"), path.name
                assert not stripped.startswith(("import httpx", "import rich")), path.name
                continue
            assert any(prefix in stripped for prefix in allowed_prefixes), (
                f"{path.name}: {stripped}"
            )


def test_sources_do_not_import_normalize() -> None:
    for path in sorted((SRC / "sources").rglob("*.py")):
        assert "stage.normalize" not in path.read_text(encoding="utf-8"), path.name


def test_the_list_path_never_imports_http_or_validation() -> None:
    probe = (
        "import sys\n"
        "from stage.services.query import list_jobs\n"
        "from stage.storage import open_repository\n"
        "from stage.domain import JobFilters\n"
        "heavy = [name for name in ('httpx', 'pydantic', 'textual') if name in sys.modules]\n"
        "print(','.join(heavy))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == ""


def test_lexicon_depends_on_nothing_in_the_chain() -> None:
    body = (SRC / "lexicon.py").read_text(encoding="utf-8")
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("import stage", "from stage")):
            continue
        assert stripped.startswith("from stage.paths"), stripped


def test_lexicon_files_keep_separate_namespaces() -> None:
    body = (SRC / "lexicon.py").read_text(encoding="utf-8")
    assert "glob" not in body and "iterdir" not in body and "rglob" not in body


def _prose_strings(root: Path) -> list[str]:
    import ast

    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if not isinstance(body, list):
                continue
            for statement in body:
                if (
                    isinstance(statement, ast.Expr)
                    and isinstance(statement.value, ast.Constant)
                    and isinstance(statement.value.value, str)
                ):
                    found.append(f"{path.name}:{statement.lineno}")
    return found


def test_no_docstrings_or_prose_strings_anywhere() -> None:
    root = Path(__file__).resolve().parents[1]
    offenders = _prose_strings(root / "src") + _prose_strings(root / "tests")
    assert not offenders, (
        f"{len(offenders)} prose string(s) reappeared: {offenders[:10]}"
    )


def test_no_hash_comments_outside_tooling_directives() -> None:
    import io
    import tokenize

    root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for base in (root / "src", root / "tests"):
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            source = path.read_text(encoding="utf-8")
            for token in tokenize.generate_tokens(io.StringIO(source).readline):
                if token.type != tokenize.COMMENT:
                    continue
                text = token.string.lstrip("#").strip()
                if text.startswith(("noqa", "type:")):
                    continue
                offenders.append(f"{path.name}:{token.start[0]} {token.string[:40]}")
    assert not offenders, (
        f"{len(offenders)} comment(s) reappeared: {offenders[:10]}"
    )


def test_packaged_data_resolves_inside_the_package_not_the_repo() -> None:
    import stage
    from stage.paths import lexicon_dir, registry_path

    package = Path(stage.__file__).resolve().parent
    for path in (registry_path(), lexicon_dir()):
        assert path.exists(), f"{path} is missing"
        assert package in path.parents or path.parent == package, (
            f"{path} resolves outside {package}; the wheel ships only src/stage"
        )


def test_no_module_reaches_above_the_package_for_data() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "stage"
    offenders = [
        f"{path.relative_to(root).as_posix()}:{number}"
        for path in sorted(root.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if "parents[2]" in line or "parents[3]" in line
    ]
    assert not offenders, (
        f"{offenders} climb above the package; that path is absent in an installed wheel"
    )


def test_captures_do_not_write_into_the_source_tree() -> None:
    from stage.paths import capture_dir

    root = Path(__file__).resolve().parents[1]
    target = capture_dir().resolve()
    assert root not in target.parents and target != root, (
        f"{target} writes captures into the checkout; an installed tool has no writable source tree"
    )
