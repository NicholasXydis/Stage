WIDE = r"""
  ____  _
 / ___|| |_   __ _   __ _   ___
 \___ \| __| / _` | / _` | / _ \
  ___) | |_ | (_| || (_| ||  __/
 |____/ \__| \__,_| \__, | \___|
                    |___/"""

COMPACT = r"""
 ___ _
/ __| |_ __ _ __ _ ___
\__ \  _/ _` / _` / -_)
|___/\__\__,_\__, \___|
             |___/"""

MIN_WIDE = 34


def block(art: str) -> str:
    lines = art.strip("\n").split("\n")
    width = max(len(line) for line in lines)
    return "\n".join(line.ljust(width) for line in lines)


def banner(width: int) -> str:
    return block(WIDE if width >= MIN_WIDE else COMPACT)


ACCENT = "default"


__all__ = ["ACCENT", "COMPACT", "MIN_WIDE", "WIDE", "banner", "block"]
