import json
import os
import re
import socket
import tempfile
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from stage.domain.text import counted, plain_error
from stage.paths import data_dir, exclusive, restrict_permissions, tighten_permissions

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
LOCATION_ORDER = ("montreal", "canada", "usa")
ROLE_ORDER = ("swe", "quant", "ml-ai", "general-cs", "data", "security", "infra", "embedded")
MAX_MESSAGES = 4
TIMEOUT_SECONDS = 15
SENT_NAME = "notify-sent.json"
_MARKDOWN = re.compile(r"([\\*_~`|>])")
_LINE_START = re.compile(r"^(\d*)([#+.)-])")


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


def sent_path() -> Path:
    return data_dir() / SENT_NAME


def _sent_state(path: Path | None = None) -> dict[str, object]:
    try:
        payload = json.loads((path or sent_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_sent_state(payload: dict[str, object], path: Path | None = None) -> None:
    target = path or sent_path()
    descriptor, staged = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload))
        Path(staged).replace(target)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise


def _sent_lock(path: Path | None = None) -> AbstractContextManager[None]:
    target = path or sent_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    return exclusive(target.with_name(f".{target.name}.lock"))


def _moment(value: object) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def announced_until(path: Path | None = None) -> datetime | None:
    payload = _sent_state(path)
    return _moment(payload["until"]) if "until" in payload else None


def mark_announced(moment: datetime, path: Path | None = None) -> None:
    with _sent_lock(path):
        payload = _sent_state(path)
        payload["until"] = moment.isoformat()
        _write_sent_state(payload, path)


def is_backlog(job: object, first_reads: Mapping[str, datetime]) -> bool:
    seen = getattr(job, "first_seen", None)
    if not isinstance(seen, datetime):
        return False
    for key in (getattr(job, "board_key", ""), getattr(job, "source", "")):
        moment = first_reads.get(str(key))
        if moment is not None and seen <= moment:
            return True
    return False


def redact(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unreadable url>"
    tail = parts.path.rsplit("/", 1)[-1]
    host = parts.hostname or ""
    return f"{parts.scheme}://{host}/.../{tail[:4]}..." if tail else f"{parts.scheme}://{host}/..."


def validate(url: str) -> str:
    url = url.strip()
    if any(character.isspace() for character in url):
        raise NotifyError("A webhook URL cannot contain spaces or line breaks.")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise NotifyError(f"That is not a usable URL ({exc}).") from None
    if parts.scheme != "https" or parts.hostname not in WEBHOOK_HOSTS:
        raise NotifyError(
            "That is not a Discord webhook. Create one in a channel you manage: "
            "Edit Channel, Integrations, New Webhook, then copy the URL."
        )
    if parts.username or parts.password or parts.port or parts.query or parts.fragment:
        raise NotifyError(
            "A Discord webhook URL has no name, password, port, query or fragment. "
            "Copy the URL exactly as Discord shows it."
        )
    if not re.fullmatch(r"(/api(/v\d+)?)/webhooks/\d+/[\w-]+/?", parts.path):
        raise NotifyError(
            "That webhook URL is cut short. Copy the whole URL, which ends with "
            "/api/webhooks/ followed by a number and a token."
        )
    return url


def remember(url: str, path: Path | None = None) -> None:
    target = path or webhook_path()
    body = json.dumps({"webhook": validate(url)})
    target.parent.mkdir(parents=True, exist_ok=True)
    tighten_permissions(target)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(body)
    try:
        restrict_permissions(target)
    except OSError:
        target.unlink(missing_ok=True)
        raise


def forget(path: Path | None = None) -> None:
    target = path or webhook_path()
    target.unlink(missing_ok=True)


def read(path: Path | None = None) -> str:
    target = path or webhook_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    stored = payload.get("webhook") if isinstance(payload, dict) else None
    return str(stored) if stored else ""


def _plain(text: str) -> str:
    return _MARKDOWN.sub(r"\\\1", text)


def _unlinked(text: str) -> str:
    flat = " ".join(text.split()).replace("[", "(").replace("]", ")")
    return _plain(flat).replace("://", r"\://")


def _opening(text: str) -> str:
    return _LINE_START.sub(r"\1\\\2", text)


def _link(url: str) -> str:
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def _entry(posting: Posting) -> str:
    where = _unlinked(posting.location or "location unknown")
    company = _opening(_unlinked(posting.company))
    if not posting.url:
        return f"{_opening(_unlinked(posting.title[:TITLE_LIMIT]))}\n{company} · {where}"
    title = _plain(posting.title[:TITLE_LIMIT].replace("[", "(").replace("]", ")"))
    return f"[{title}]({_link(posting.url)})\n{company} · {where}"


def _footer(hidden: int) -> str:
    return f"...and {hidden} more. Run `stage list` to see them all."


def _fitted(postings: list[Posting], room: int) -> list[str]:
    lines: list[str] = []
    spent = 0
    for posting in postings[:MAX_LISTED]:
        entry = _entry(posting)
        if lines and spent + len(entry) + 2 > room:
            break
        lines.append(entry)
        spent += len(entry) + 2
    return lines


def _embed(title: str, lines: list[str]) -> dict[str, object]:
    return {
        "username": "Stage",
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": title[:256],
                "description": "\n\n".join(lines)[:DESCRIPTION_LIMIT],
                "color": 0x4A9EFF,
            }
        ],
    }


def messages(
    postings: list[Posting], total: int, limit: int = MAX_MESSAGES
) -> list[dict[str, object]]:
    room = DESCRIPTION_BUDGET - len(_footer(total))
    pages: list[list[str]] = []
    left = postings
    while left and len(pages) < limit:
        lines = _fitted(left, room)
        pages.append(lines)
        left = left[len(lines) :]
    shown = sum(len(page) for page in pages)
    if total > shown:
        if pages:
            pages[-1].append(_footer(total - shown))
        else:
            pages.append([_footer(total)])
    title = counted(total, "new posting")
    if len(pages) == 1:
        return [_embed(title, pages[0])]
    return [
        _embed(f"{title} ({number} of {len(pages)})", page) for number, page in enumerate(pages, 1)
    ]


def compose(postings: list[Posting], total: int) -> dict[str, object]:
    return messages(postings, total, limit=1)[0]


def post(url: str, payload: dict[str, object]) -> None:
    request = Request(
        validate(url),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "stage"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS):
            pass
    except HTTPError as exc:
        if exc.code in (401, 403, 404):
            raise NotifyError(
                "Discord will not accept that webhook, so it was probably deleted. "
                "Create a new one and run stage schedule notify URL again."
            ) from None
        if exc.code == 429:
            raise NotifyError("Discord is rate limiting this webhook for now.") from None
        raise NotifyError(f"Discord answered with an error ({exc.code}).") from None
    except (OSError, HTTPException) as exc:
        reason = (exc.reason if isinstance(exc, URLError) else None) or exc
        if isinstance(reason, socket.gaierror):
            raise NotifyError("No internet connection, so Stage could not reach Discord.") from None
        if isinstance(reason, HTTPException):
            raise NotifyError(
                "Discord broke off its answer, so Stage cannot tell whether the message arrived."
            ) from None
        raise NotifyError(f"Stage could not reach Discord ({plain_error(str(reason))}).") from None


__all__ = [
    "MAX_LISTED",
    "NotifyError",
    "Posting",
    "announced_until",
    "compose",
    "forget",
    "is_backlog",
    "mark_announced",
    "post",
    "rank",
    "read",
    "redact",
    "remember",
    "sent_path",
    "validate",
    "webhook_path",
]
