import importlib

_ORDER = ("postings", "pipeline", "schedule", "insight", "discovery")

for _name in _ORDER:
    importlib.import_module(f"{__name__}.{_name}")

__all__ = list(_ORDER)
