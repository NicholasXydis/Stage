import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

TIMEOUT = 300
NAMED = re.compile(r"(?<![\w/.\\-])[Ss]tage(?![\w/.\\-])")


@dataclass(frozen=True)
class Canary:
    key: str
    package: str
    listing: tuple[str, ...]
    installer: str = "npm"


CANARIES = (
    Canary("claude-code", "@anthropic-ai/claude-code", ("claude", "mcp", "list")),
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
    try:
        done = subprocess.run(
            command,
            env=env,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as exc:
        return 127, str(exc)
    return done.returncode, done.stdout + done.stderr


def install(canary: Canary, env: dict[str, str], root: Path) -> tuple[bool, str]:
    if canary.installer == "npm":
        command = ["npm", "install", "-g", "--prefix", str(root / "npm"), canary.package]
    else:
        command = ["uv", "tool", "install", canary.package]
    code, said = run(command, env, root)
    return code == 0, said[-400:]


def main() -> int:
    stage = shutil.which("stage")
    if stage is None:
        print("stage is not on PATH; run this through uv run")
        return 2
    root = Path(tempfile.mkdtemp(prefix="app-canary-"))
    home = root / "home"
    home.mkdir()
    env = {
        "HOME": str(home),
        "PATH": os.pathsep.join(
            [
                str(root / "npm" / "bin"),
                str(root / "bin"),
                str(Path(stage).parent),
                os.environ["PATH"],
            ]
        ),
        "UV_TOOL_DIR": str(root / "uv-tools"),
        "UV_TOOL_BIN_DIR": str(root / "bin"),
        "NO_COLOR": "1",
        "TERM": "dumb",
        "LANG": "C.UTF-8",
        "COPILOT_OFFLINE": "true",
    }
    failed: list[str] = []
    print(f"{'app':12} {'installed':10} {'connected':10} {'listed':7} {'removed':8}")
    for canary in CANARIES:
        installed, said = install(canary, env, root)
        connected = listed = removed = False
        if installed:
            code, said = run([stage, "connect", canary.key], env, home)
            connected = code == 0
            code, listing = run(list(canary.listing), env, home)
            listed = NAMED.search(listing) is not None
            run([stage, "disconnect", canary.key], env, home)
            code, after = run(list(canary.listing), env, home)
            removed = NAMED.search(after) is None
            said = listing
        print(f"{canary.key:12} {installed!s:10} {connected!s:10} {listed!s:7} {removed!s:8}")
        if not (installed and connected and listed and removed):
            failed.append(canary.key)
            print("  " + " ".join(said.split())[-600:])
    if failed:
        print(f"\nStage no longer connects cleanly to: {', '.join(failed)}")
        return 1
    print("\nEvery app read Stage's entry and let it go again.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
