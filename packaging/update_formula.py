import json
import sys
import urllib.request
from pathlib import Path

FORMULA = Path(__file__).parent / "stage.rb"
PROJECT = "stage-cli"


def sdist(version: str) -> tuple[str, str]:
    url = f"https://pypi.org/pypi/{PROJECT}/{version}/json"
    with urllib.request.urlopen(url) as response:  # noqa: S310
        payload = json.load(response)
    for entry in payload["urls"]:
        if entry["packagetype"] == "sdist":
            return entry["url"], entry["digests"]["sha256"]
    raise SystemExit(f"{PROJECT} {version} has no sdist on PyPI")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: update_formula.py VERSION")
    version = sys.argv[1].removeprefix("v")
    url, digest = sdist(version)
    body = FORMULA.read_text(encoding="utf-8")
    for placeholder, value in (
        ("PLACEHOLDER_SDIST_URL", url),
        ("PLACEHOLDER_SDIST_SHA256", digest),
    ):
        if placeholder in body:
            body = body.replace(placeholder, value)
            continue
        marker = placeholder.removeprefix("PLACEHOLDER_SDIST_").lower()
        field = "url" if marker == "url" else "sha256"
        lines = []
        for line in body.split("\n"):
            stripped = line.strip()
            if stripped.startswith(f"{field} "):
                indent = line[: len(line) - len(line.lstrip())]
                lines.append(f'{indent}{field} "{value}"')
                continue
            lines.append(line)
        body = "\n".join(lines)
    FORMULA.write_text(body, encoding="utf-8")
    print(f"{FORMULA.name} now points at {PROJECT} {version}")
    print(f"  url    {url}")
    print(f"  sha256 {digest}")


if __name__ == "__main__":
    main()
