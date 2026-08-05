import importlib
import pkgutil
from types import MappingProxyType

from stage.domain import Platform
from stage.sources.base import (
    Adapter,
    AdapterError,
    FetchResult,
    PayloadValidationError,
    capture_payload,
)
from stage.sources.feed import (
    FeedAdapter,
    get_feeds,
    register_feed,
    upcoming_season_year,
)

_ADAPTERS: dict[str, Adapter] = {}
_LOADED = False


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
        raise AdapterError(f"no adapter named {name!r} (known: {known})") from exc


def adapter_for_platform(platform: Platform) -> Adapter | None:
    for adapter in get_adapters().values():
        if adapter.platform is platform:
            return adapter
    return None


__all__ = [
    "Adapter",
    "AdapterError",
    "FeedAdapter",
    "FetchResult",
    "PayloadValidationError",
    "adapter_for_platform",
    "capture_payload",
    "get_feeds",
    "get_adapter",
    "get_adapters",
    "load_builtins",
    "register",
    "register_feed",
    "upcoming_season_year",
]
