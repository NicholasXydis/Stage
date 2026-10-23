import contextlib
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

TIMEOUT = 300
RETRY_PAUSE = 90
WORKERS = 4
KEPT = (
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "PROGRAMFILES",
    "PROGRAMDATA",
    "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS",
    "PSMODULEPATH",
)
NATIVE_CLAUDE = ("powershell", "-NoProfile", "-Command", "irm https://claude.ai/install.ps1 | iex")
NAMED = re.compile(r"(?<![\w/.\\-])[Ss]tage(?![\w/.\\-])")


@dataclass(frozen=True)
class Canary:
    key: str
    package: str
    listing: tuple[str, ...]
    installer: str = "npm"
    windows: tuple[str, ...] = ()


@dataclass(frozen=True)
class Result:
    canary: Canary
    passed: bool
    shown: str
    report: str


CANARIES = (
    Canary(
        "claude-code",
        "@anthropic-ai/claude-code",
        ("claude", "mcp", "list"),
        windows=NATIVE_CLAUDE,
    ),
    Canary("codex", "@openai/codex", ("codex", "mcp", "list")),
    Canary("gemini", "@google/gemini-cli", ("gemini", "mcp", "list")),
    Canary("copilot", "@github/copilot", ("copilot", "mcp", "list")),
    Canary("opencode", "opencode-ai", ("opencode", "mcp", "list")),
    Canary("qwen", "@qwen-code/qwen-code", ("qwen", "mcp", "list")),
    Canary("droid", "droid", ("droid", "mcp", "list")),
    Canary("kilo", "@kilocode/cli", ("kilo", "mcp", "list")),
    Canary("codebuddy", "@tencent-ai/codebuddy-code", ("codebuddy", "mcp", "list")),
    Canary("cline", "cline", ("cline", "config", "mcp", "--json")),
    Canary("amp", "@sourcegraph/amp", ("amp", "mcp", "list")),
    Canary("hermes", "hermes-agent", ("hermes", "mcp", "list"), "uv"),
    Canary("openhands", "openhands", ("openhands", "mcp", "list"), "uv"),
)


def run(command: list[str], env: dict[str, str], cwd: Path) -> tuple[int, str]:
    program = shutil.which(command[0], path=env["PATH"]) or command[0]
    try:
        process = subprocess.Popen(
            [program, *command[1:]],
            env=env,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        return 127, str(exc)
    with process:
        try:
            out, err = process.communicate(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            stop(process)
            return 124, f"{' '.join(command[:2])} timed out after {TIMEOUT}s"
    return process.returncode, out + err


def stop(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        tree = ["taskkill", "/F", "/T", "/PID", str(process.pid)]
        subprocess.run(tree, capture_output=True, check=False)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.communicate(timeout=10)


def sandbox(canary: Canary, stage: Path, root: Path) -> dict[str, str]:
    base = root / canary.key
    home = base / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {name: os.environ[name] for name in KEPT if name in os.environ}
    if os.name == "nt":
        for name, folder in (("APPDATA", "Roaming"), ("LOCALAPPDATA", "Local")):
            (home / "AppData" / folder).mkdir(parents=True, exist_ok=True)
            env[name] = str(home / "AppData" / folder)
        env["USERPROFILE"] = str(home)
        (home / ".codex").mkdir(exist_ok=True)
        env["CODEX_HOME"] = str(home / ".codex")
    return env | {
        "HOME": str(home),
        "PATH": os.pathsep.join(
            [
                str(home / ".local" / "bin"),
                str(base / "npm"),
                str(base / "npm" / "bin"),
                str(base / "bin"),
                str(stage.parent),
                os.environ["PATH"],
            ]
        ),
        "UV_TOOL_DIR": str(base / "uv-tools"),
        "UV_TOOL_BIN_DIR": str(base / "bin"),
        "STAGE_DB": str(base / "stage.db"),
        "NO_COLOR": "1",
        "TERM": "dumb",
        "LANG": "C.UTF-8",
        "COPILOT_OFFLINE": "true",
        "NPM_CONFIG_UPDATE_NOTIFIER": "false",
        "NPM_CONFIG_FUND": "false",
        "NPM_CONFIG_AUDIT": "false",
    }


def install(canary: Canary, env: dict[str, str], base: Path, again: bool) -> tuple[bool, str]:
    if os.name == "nt" and canary.windows:
        command = list(canary.windows)
    elif canary.installer == "npm":
        command = ["npm", "install", "-g", "--prefix", str(base / "npm"), canary.package]
        command += ["--force"] if again else []
    else:
        command = ["uv", "tool", "install", canary.package]
        command += ["--reinstall"] if again else []
    code, said = run(command, env, base)
    return code == 0, said[-400:]


def attempt(canary: Canary, stage: Path, root: Path, again: bool) -> Result:
    started = time.monotonic()
    env = sandbox(canary, stage, root)
    base = root / canary.key
    home = base / "home"
    installed, said = install(canary, env, base, again)
    connected = listed = removed = False
    if installed:
        code, joined = run([str(stage), "connect", canary.key], env, home)
        connected = code == 0
        code, listing = run(list(canary.listing), env, home)
        listed = NAMED.search(listing) is not None
        run([str(stage), "disconnect", canary.key], env, home)
        code, after = run(list(canary.listing), env, home)
        removed = NAMED.search(after) is None
        said = f"stage connect: {joined[:400]} | {canary.listing[0]}: {listing[-400:]}"
    states = f"{installed!s:10} {connected!s:10} {listed!s:7} {removed!s:8}"
    row = f"{canary.key:12} {states} {round(time.monotonic() - started):>7}"
    if installed and connected and listed and removed:
        return Result(canary, True, row, "")
    detail = " ".join(said.split())[-800:]
    report = f"installed {installed}, connected {connected}, listed {listed}. {detail}"
    return Result(canary, False, f"{row}\n  {detail}", report)


def sweep(canaries: Sequence[Canary], stage: Path, root: Path, again: bool) -> list[Result]:
    results: list[Result] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        pending = [pool.submit(attempt, canary, stage, root, again) for canary in canaries]
        for future in as_completed(pending):
            results.append(future.result())
            print(results[-1].shown, flush=True)
    return sorted(results, key=lambda result: CANARIES.index(result.canary))


def annotate(level: str, results: list[Result]) -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    for result in results:
        print(f"::{level} title={result.canary.key}::{result.report.replace('%', '%25')}")


def main() -> int:
    stage = shutil.which("stage")
    if stage is None:
        print("stage is not on PATH; run this through uv run")
        return 2
    root = Path(tempfile.mkdtemp(prefix="app-canary-", dir=os.environ.get("RUNNER_TEMP")))
    try:
        return check(Path(stage), root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check(stage: Path, root: Path) -> int:
    started = time.monotonic()
    columns = f"{'installed':10} {'connected':10} {'listed':7} {'removed':8}"
    print(f"{'app':12} {columns} seconds", flush=True)
    missed = [result for result in sweep(CANARIES, stage, root, again=False) if not result.passed]
    failed: list[Result] = []
    if missed:
        annotate("warning", missed)
        names = ", ".join(result.canary.key for result in missed)
        print(f"\nTrying {names} once more, in case a release was still publishing.", flush=True)
        time.sleep(RETRY_PAUSE)
        again = sweep([result.canary for result in missed], stage, root, again=True)
        failed = [result for result in again if not result.passed]
    took = f"{time.monotonic() - started:.0f}s"
    if failed:
        annotate("error", failed)
        keys = ", ".join(result.canary.key for result in failed)
        print(f"\nStage no longer connects cleanly to: {keys} ({took})")
        return 1
    print(f"\nEvery app read Stage's entry and let it go again ({took}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
