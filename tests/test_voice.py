import ast
import re
from collections.abc import Iterator
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "stage"
PATTERN_CALLS = frozenset(
    {"compile", "search", "match", "fullmatch", "sub", "subn", "split", "findall", "finditer"}
)
SQL = re.compile(
    r"\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|PRAGMA|BEGIN|COMMIT|WITH|WHERE)\b"
)
ENTITY = re.compile(r"&#?\w+;")
CLASS_NAME = re.compile(r"\b(?:[A-Z][a-z0-9]+)+(?:Error|Exception|Timeout|Interrupt)\b")
SEMICOLONS_KEPT = {
    ("domain/preferences.py", "; "),
    ("domain/preferences.py", ";"),
    ("cli/render/__init__.py", "; "),
    ("services/discover.py", "findReqs;siteNumber={},limit=1"),
    ("sources/oracle_cloud.py", "findReqs;siteNumber={}{},offset={},limit={}"),
    (
        "http/client.py",
        "stage-cli/{} (+https://github.com/NicholasXydis/Stage; internship aggregator)",
    ),
    ("http/client.py", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
    ("ai/server.py", "data:{};base64,{}"),
    ("services/connect.py", "{}\\System32;{}"),
}
CLASS_NAMES_KEPT = {
    ("http/client.py", "BreakerOpenError"),
}
PLAIN_ERROR_TABLE = "domain/text.py"


def _files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _not_text(tree: ast.AST) -> set[int]:
    skipped: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            skipped.update(id(part) for part in node.values if isinstance(part, ast.Constant))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "re"
            and node.func.attr in PATTERN_CALLS
            and node.args
        ):
            skipped.add(id(node.args[0]))
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            skipped.update(id(name) for name in ast.walk(node.value))
    return skipped


def _constant(part: ast.expr) -> str:
    return part.value if isinstance(part, ast.Constant) and isinstance(part.value, str) else "{}"


def _strings(path: Path) -> Iterator[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skipped = _not_text(tree)
    for node in ast.walk(tree):
        if id(node) in skipped:
            continue
        if isinstance(node, ast.JoinedStr):
            yield node.lineno, "".join(_constant(part) for part in node.values)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


def test_no_text_joins_sentences_with_a_semicolon() -> None:
    found = []
    for path in _files():
        name = path.relative_to(SRC).as_posix()
        for line, text in _strings(path):
            if ";" not in ENTITY.sub("", text) or SQL.match(text):
                continue
            if (name, text) not in SEMICOLONS_KEPT:
                found.append(f"{name}:{line}: {text[:80]!r}")
    assert not found, "\n".join(found)


def test_no_text_names_a_python_exception_class() -> None:
    found = []
    for path in _files():
        name = path.relative_to(SRC).as_posix()
        if name == PLAIN_ERROR_TABLE:
            continue
        for line, text in _strings(path):
            for match in CLASS_NAME.finditer(text):
                if (name, match.group(0)) not in CLASS_NAMES_KEPT:
                    found.append(f"{name}:{line}: {match.group(0)}")
    assert not found, "\n".join(found)


def _class_names_shown(tree: ast.AST) -> Iterator[ast.JoinedStr]:
    cleaned = {
        id(argument)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "plain_error"
        for argument in node.args
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr) or id(node) in cleaned:
            continue
        if any(
            isinstance(part, ast.FormattedValue)
            and isinstance(part.value, ast.Attribute)
            and part.value.attr == "__name__"
            for part in node.values
        ):
            yield node


def test_no_text_interpolates_a_class_name_unless_plain_error_strips_it() -> None:
    found = [
        f"{name}:{node.lineno}"
        for path in _files()
        for node in _class_names_shown(ast.parse(path.read_text(encoding="utf-8")))
        if (name := path.relative_to(SRC).as_posix()) != PLAIN_ERROR_TABLE
    ]
    assert not found, "\n".join(found)


def test_did_you_mean_comes_from_the_one_helper() -> None:
    found = [
        f"{path.relative_to(SRC).as_posix()}:{line}"
        for path in _files()
        if path.relative_to(SRC).as_posix() != PLAIN_ERROR_TABLE
        for line, text in _strings(path)
        if "did you mean" in text.lower()
    ]
    assert not found, "\n".join(found)


def test_the_kept_semicolons_are_still_there() -> None:
    held = {
        (path.relative_to(SRC).as_posix(), text) for path in _files() for _, text in _strings(path)
    }
    named = {(name, match.group(0)) for name, text in held for match in CLASS_NAME.finditer(text)}
    assert not SEMICOLONS_KEPT - held
    assert not CLASS_NAMES_KEPT - named
