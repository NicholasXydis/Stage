import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
CI_FLOW = ROOT / "assets" / "ci-flow.svg"


def _coverage(report: str) -> str:
    found = re.search(r"^TOTAL\s+.*?(\d+)%\s*$", report, re.MULTILINE)
    if found is None:
        raise SystemExit("no TOTAL coverage line in the pytest output")
    return f"{found.group(1)}%"


def _tests(parallel: str, serial: str) -> str:
    counts = []
    for report in (parallel, serial):
        found = re.search(r"(\d+) passed", report)
        if found is None:
            raise SystemExit("no test count in the pytest output")
        counts.append(int(found.group(1)))
    return f"{sum(counts):,}"


def _swap(body: str, label: str, value: str) -> str:
    pattern = rf"(!\[{label}\]\(https://img\.shields\.io/static/v1\?label={label}&message=)[^&)]*"
    updated, count = re.subn(pattern, lambda m: f"{m.group(1)}{value}", body)
    if not count:
        raise SystemExit(f"no {label} badge in README.md")
    return updated


def _restate_ci_flow(tests: str, coverage: str) -> None:
    if not CI_FLOW.is_file():
        return
    body = CI_FLOW.read_text(encoding="utf-8")
    updated, count = re.subn(
        r"(>)[\d,]+ tests, [\d.]+%",
        lambda m: f"{m.group(1)}{tests} tests, {coverage}",
        body,
    )
    if not count:
        raise SystemExit("no 'N tests, N%' label in assets/ci-flow.svg")
    CI_FLOW.write_text(updated, encoding="utf-8")


def main() -> int:
    parallel = Path(sys.argv[1]).read_text(encoding="utf-8")
    serial = Path(sys.argv[2]).read_text(encoding="utf-8")
    body = README.read_text(encoding="utf-8")
    body = _swap(body, "tests", _tests(parallel, serial).replace(",", "%2C"))
    body = _swap(body, "coverage", _coverage(serial).replace("%", "%25"))
    README.write_text(body, encoding="utf-8")
    _restate_ci_flow(_tests(parallel, serial), _coverage(serial))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
