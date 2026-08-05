from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SourceSignals:

    terms: tuple[str, ...] = field(default_factory=tuple)
    season: str = ""

    sponsorship: str = ""
    degrees: tuple[str, ...] = field(default_factory=tuple)
    category: str = ""
