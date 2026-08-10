from stage.classify.eligibility import (
    EligibilityVerdict,
    resolve_eligibility,
    screen_degree_scope,
    screen_is_cs_role,
)
from stage.classify.internship import InternshipVerdict, screen_internship
from stage.classify.role import RoleVerdict, classify_role
from stage.classify.scope import (
    OUT_OF_SCOPE_BUCKETS,
    Rejection,
    screen_is_internship,
    screen_location,
    to_quarantined,
)

__all__ = [
    "EligibilityVerdict",
    "resolve_eligibility",
    "screen_degree_scope",
    "screen_is_cs_role",
    "OUT_OF_SCOPE_BUCKETS",
    "InternshipVerdict",
    "Rejection",
    "RoleVerdict",
    "classify_role",
    "screen_internship",
    "screen_is_internship",
    "screen_location",
    "to_quarantined",
]
