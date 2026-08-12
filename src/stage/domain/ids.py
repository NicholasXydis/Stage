import re

_SEPARATOR = ":"
_UNSAFE = re.compile(r"[^a-z0-9._-]+")
_CANONICAL = re.compile(r"^[a-z0-9._-]+$")


def _slugify(value: str) -> str:
    return _UNSAFE.sub("-", value.strip().lower()).strip("-")


def _component(value: str) -> str:
    clean = value.strip()
    if not clean:
        return ""
    normalized = _slugify(clean)
    if _CANONICAL.fullmatch(clean.lower()):
        return normalized
    return f"{normalized or 'id'}~{clean.encode().hex()}"


def board_key(source: str, board_slug: str) -> str:
    parts = (_slugify(source), _slugify(board_slug))
    if not all(parts):
        raise ValueError(f"cannot build a stable board key from {source!r}/{board_slug!r}")
    return _SEPARATOR.join(parts)


def board_of(identifier: str, fallback: str) -> str:
    parts = identifier.split(_SEPARATOR)
    if len(parts) < 3:
        return fallback
    return _SEPARATOR.join(parts[:2])


def job_id(source: str, board_slug: str, native_id: str) -> str:
    native = _component(native_id)
    if not native:
        raise ValueError(
            f"cannot build a stable job id from {source!r}/{board_slug!r}/{native_id!r}"
        )
    try:
        prefix = board_key(source, board_slug)
    except ValueError as exc:
        raise ValueError(
            f"cannot build a stable job id from {source!r}/{board_slug!r}/{native_id!r}"
        ) from exc
    return _SEPARATOR.join((prefix, native))
