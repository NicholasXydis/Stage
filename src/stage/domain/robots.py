import math
import re
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class RobotsRules:
    rules: tuple[tuple[str, bool], ...] = ()
    crawl_delay: float | None = None

    def allows(self, path: str, *, caseless: bool = False) -> bool:
        matched = [
            (len(pattern), allow)
            for pattern, allow in self.rules
            if _matches(pattern, path, caseless=caseless)
        ]
        return not matched or max(matched)[1]


@dataclass(slots=True)
class _Group:
    agents: set[str] = field(default_factory=set)
    rules: list[tuple[str, bool]] = field(default_factory=list)
    delays: list[float] = field(default_factory=list)
    closed: bool = False


def robots_rules(text: str, agent: str) -> RobotsRules:
    groups: list[_Group] = []
    for raw in text.lstrip("\ufeff").splitlines():
        key, colon, value = raw.split("#", 1)[0].partition(":")
        key, value = key.strip().lower(), value.strip()
        if not colon:
            continue
        if key == "user-agent":
            if not groups or groups[-1].closed:
                groups.append(_Group())
            groups[-1].agents.add(value.split("/", 1)[0].strip().lower())
        elif groups and key in ("allow", "disallow", "crawl-delay"):
            group = groups[-1]
            group.closed = True
            if key == "crawl-delay":
                group.delays.extend(_seconds(value))
            elif value:
                group.rules.append((value, key == "allow"))
    wanted = agent.lower()
    chosen = [group for group in groups if wanted in group.agents] or [
        group for group in groups if "*" in group.agents
    ]
    delays = [delay for group in chosen for delay in group.delays]
    return RobotsRules(
        rules=tuple(rule for group in chosen for rule in group.rules),
        crawl_delay=max(delays) if delays else None,
    )


def _seconds(value: str) -> list[float]:
    try:
        seconds = float(value)
    except ValueError:
        return []
    return [seconds] if math.isfinite(seconds) and seconds >= 0 else []


def _matches(pattern: str, path: str, *, caseless: bool = False) -> bool:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    expression = ".*".join(re.escape(part) for part in body.split("*"))
    flags = re.IGNORECASE if caseless else 0
    return re.match(expression + (r"\Z" if anchored else ""), path, flags) is not None


__all__ = ["RobotsRules", "robots_rules"]
