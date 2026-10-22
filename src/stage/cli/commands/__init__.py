import importlib

_ORDER = (
    "postings",
    "preferences",
    "tracker",
    "pipeline",
    "schedule",
    "insight",
    "discovery",
    "ai",
)

for _name in _ORDER:
    importlib.import_module(f"{__name__}.{_name}")

__all__ = list(_ORDER)
