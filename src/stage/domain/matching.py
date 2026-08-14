from stage.domain.enums import UNKNOWN_TERM, LocationBucket

_NON_EVIDENCE_BUCKETS = frozenset({LocationBucket.UNKNOWN, LocationBucket.INTERNATIONAL})


def location_agrees(left: LocationBucket, right: LocationBucket) -> bool:
    if left in _NON_EVIDENCE_BUCKETS or right in _NON_EVIDENCE_BUCKETS:
        return False
    return left is right


def term_agrees(left: str, right: str) -> bool:
    if left == UNKNOWN_TERM or right == UNKNOWN_TERM:
        return False
    return left == right
