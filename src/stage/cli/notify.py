import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from stage.paths import data_dir, restrict_permissions

WEBHOOK_HOSTS = frozenset(
    {
        "discord.com",
        "discordapp.com",
        "canary.discord.com",
        "ptb.discord.com",
        "media.discordapp.net",
    }
)
DESCRIPTION_LIMIT = 4096
DESCRIPTION_BUDGET = 3900
MAX_LISTED = 40
TITLE_LIMIT = 200
LOCATION_ORDER = ("montreal", "canada")
ROLE_ORDER = ("swe", "quant")
TIMEOUT_SECONDS = 15


class NotifyError(Exception):
    pass


def rank(job: object) -> tuple[int, int, float]:
    location = getattr(getattr(job, "location", None), "value", "")
    role = getattr(getattr(job, "role", None), "value", "")
    place = LOCATION_ORDER.index(location) if location in LOCATION_ORDER else len(LOCATION_ORDER)
    discipline = ROLE_ORDER.index(role) if role in ROLE_ORDER else len(ROLE_ORDER)
    seen = getattr(job, "first_seen", None)
    recency = -seen.timestamp() if seen is not None else 0.0
    return (place, discipline, recency)


@dataclass(frozen=True, slots=True)
class Posting:
    company: str
    title: str
    location: str
    url: str


def webhook_path() -> Path:
    return data_dir() / "notify.json"


def redact(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unreadable url>"
    tail = parts.path.rsplit("/", 1)[-1]
    return f"{parts.scheme}://{parts.netloc}/…/{tail[:4]}…" if tail else url


def validate(url: str) -> str:
    url = url.strip()
    if any(character.isspace() for character in url):
        raise NotifyError("A webhook URL cannot contain spaces or line breaks.")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise NotifyError(f"That is not a usable URL: {exc}") from None
    if parts.scheme != "https" or parts.hostname not in WEBHOOK_HOSTS:
        raise NotifyError(
            "That is not a Discord webhook. Create one in a channel you manage: "
            "Edit Channel, Integrations, New Webhook, then copy the URL."
        )
    if "/api/webhooks/" not in parts.path:
        raise NotifyError("A Discord webhook URL contains /api/webhooks/.")
    return url


def remember(url: str, path: Path | None = None) -> None:
    target = path or webhook_path()
    body = json.dumps({"webhook": validate(url)})
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(body)
    restrict_permissions(target)


def forget(path: Path | None = None) -> None:
    target = path or webhook_path()
    target.unlink(missing_ok=True)


def read(path: Path | None = None) -> str:
    target = path or webhook_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    stored = payload.get("webhook") if isinstance(payload, dict) else None
    return str(stored) if stored else ""


def _entry(posting: Posting) -> str:
    title = posting.title[:TITLE_LIMIT].replace("[", "(").replace("]", ")")
    where = posting.location or "location unknown"
    if not posting.url:
        return f"{title}\n{posting.company} · {where}"
    return f"[{title}]({posting.url})\n{posting.company} · {where}"


def _footer(hidden: int) -> str:
    return f"…and {hidden} more. Run `stage list --new`."


def compose(postings: list[Posting], total: int) -> dict[str, object]:
    lines: list[str] = []
    spent = 0
    for posting in postings[:MAX_LISTED]:
        entry = _entry(posting)
        room = DESCRIPTION_BUDGET - len(_footer(total))
        if lines and spent + len(entry) + 2 > room:
            break
        lines.append(entry)
        spent += len(entry) + 2

    hidden = total - len(lines)
    if hidden > 0:
        lines.append(_footer(hidden))
    description = "\n\n".join(lines)[:DESCRIPTION_LIMIT]
    return {
        "username": "Stage",
        "embeds": [
            {
                "title": f"{total} new posting(s)"[:256],
                "description": description,
                "color": 0x4A9EFF,
            }
        ],
    }


def post(url: str, payload: dict[str, object]) -> None:
    request = Request(
        validate(url),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "stage"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            if response.status >= 400:
                raise NotifyError(f"Discord answered {response.status}.")
    except HTTPError as exc:
        if exc.code in (401, 403, 404):
            raise NotifyError(
                "Discord will not accept that webhook. It was probably deleted; "
                "create a new one and run stage schedule notify again."
            ) from None
        if exc.code == 429:
            raise NotifyError("Discord is rate limiting this webhook. Try again shortly.") from None
        raise NotifyError(f"Discord answered {exc.code}.") from None
    except (URLError, TimeoutError) as exc:
        raise NotifyError(f"Could not reach Discord: {exc}") from None


__all__ = [
    "MAX_LISTED",
    "NotifyError",
    "Posting",
    "compose",
    "forget",
    "post",
    "rank",
    "read",
    "redact",
    "remember",
    "validate",
    "webhook_path",
]
