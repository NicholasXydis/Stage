import importlib
import pkgutil
import re
from types import MappingProxyType

from stage.domain import Platform
from stage.sources.base import (
    Adapter,
    AdapterError,
    FetchResult,
    PayloadValidationError,
    capture_payload,
    convert_rows,
    payload_error,
    sentences,
)
from stage.sources.feed import (
    FeedAdapter,
    get_feeds,
    register_feed,
    upcoming_season_year,
)

_ADAPTERS: dict[str, Adapter] = {}
_LOADED = False
PLATFORM_DOMAINS: tuple[tuple[str, Platform], ...] = (
    ("myworkdayjobs.com", Platform.WORKDAY),
    ("myworkdaysite.com", Platform.WORKDAY),
    ("jobvite.com", Platform.JOBVITE),
    ("greenhouse.io", Platform.GREENHOUSE),
    ("lever.co", Platform.LEVER),
    ("ashbyhq.com", Platform.ASHBY),
    ("smartrecruiters.com", Platform.SMARTRECRUITERS),
    ("workable.com", Platform.WORKABLE),
    ("bamboohr.com", Platform.BAMBOOHR),
    ("breezy.hr", Platform.BREEZY),
    ("recruitee.com", Platform.RECRUITEE),
    ("oraclecloud.com", Platform.ORACLE_CLOUD),
)
PLATFORM_FRONTS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[a-z0-9-]+\.(wd\d+\.myworkdayjobs\.com)"),
    re.compile(r"[a-z0-9-]+\.(fa\.[a-z0-9-]+\.oraclecloud\.com)"),
    re.compile(r"[a-z0-9-]+\.(bamboohr\.com)"),
    re.compile(r"[a-z0-9-]+\.(breezy\.hr)"),
    re.compile(r"[a-z0-9-]+\.(recruitee\.com)"),
)


def register[A: Adapter](cls: type[A]) -> type[A]:
    adapter = cls()
    existing = _ADAPTERS.get(adapter.name)
    if existing is not None and type(existing) is not cls:
        raise AdapterError(f"two adapters claim the name {adapter.name!r}")
    _ADAPTERS[adapter.name] = adapter
    return cls


def load_builtins() -> None:
    global _LOADED
    if _LOADED:
        return
    for module in pkgutil.iter_modules(__path__):
        if module.name.startswith("_") or module.name in ("base", "feed", "platforms"):
            continue
        importlib.import_module(f"{__name__}.{module.name}")
    _LOADED = True


def get_adapters() -> MappingProxyType[str, Adapter]:
    load_builtins()
    return MappingProxyType(_ADAPTERS)


def get_adapter(name: str) -> Adapter:
    adapters = get_adapters()
    try:
        return adapters[name]
    except KeyError as exc:
        known = ", ".join(sorted(adapters)) or "none"
        raise AdapterError(f'Stage has no adapter named "{name}". Choose from: {known}.') from exc


def adapter_for_platform(platform: Platform) -> Adapter | None:
    for adapter in get_adapters().values():
        if adapter.platform is platform:
            return adapter
    return None


def platform_bucket(host: str) -> str:
    for domain, platform in PLATFORM_DOMAINS:
        if host != domain and not host.endswith(f".{domain}"):
            continue
        adapter = adapter_for_platform(platform)
        if adapter is not None:
            return adapter.bucket_key or min(adapter.hosts, default=host)
    return host


def platform_front(host: str) -> str:
    for pattern in PLATFORM_FRONTS:
        found = pattern.fullmatch(host)
        if found is not None:
            return found.group(1)
    return host


__all__ = [
    "PLATFORM_DOMAINS",
    "PLATFORM_FRONTS",
    "Adapter",
    "AdapterError",
    "FeedAdapter",
    "FetchResult",
    "PayloadValidationError",
    "adapter_for_platform",
    "capture_payload",
    "convert_rows",
    "payload_error",
    "sentences",
    "get_feeds",
    "get_adapter",
    "get_adapters",
    "load_builtins",
    "platform_bucket",
    "platform_front",
    "register",
    "register_feed",
    "upcoming_season_year",
]
