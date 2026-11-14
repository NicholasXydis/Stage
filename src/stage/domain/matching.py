from stage.domain.enums import UNKNOWN_TERM, JobLevel, LocationBucket

REPOST = "repost"
_NON_EVIDENCE_BUCKETS = frozenset({LocationBucket.UNKNOWN, LocationBucket.INTERNATIONAL})


def location_agrees(left: LocationBucket, right: LocationBucket) -> bool:
    if left in _NON_EVIDENCE_BUCKETS or right in _NON_EVIDENCE_BUCKETS:
        return False
    return left is right


_TERMLESS_LEVELS = frozenset({JobLevel.NEW_GRAD, JobLevel.ENTRY})


def term_agrees(
    left: str,
    right: str,
    left_level: JobLevel = JobLevel.UNKNOWN,
    right_level: JobLevel = JobLevel.UNKNOWN,
) -> bool:
    if left == UNKNOWN_TERM and right == UNKNOWN_TERM:
        return left_level in _TERMLESS_LEVELS and right_level in _TERMLESS_LEVELS
    if left == UNKNOWN_TERM or right == UNKNOWN_TERM:
        return False
    return left == right


def terms_conflict(left: str, right: str) -> bool:
    return UNKNOWN_TERM not in (left, right) and left != right


def levels_conflict(left: JobLevel, right: JobLevel) -> bool:
    return JobLevel.UNKNOWN not in (left, right) and left != right
