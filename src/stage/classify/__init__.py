from stage.classify.eligibility import (
    EligibilityVerdict,
    resolve_eligibility,
    screen_degree_scope,
    screen_is_cs_role,
)
from stage.classify.internship import InternshipVerdict, screen_internship
from stage.classify.role import RoleVerdict, classify_role
from stage.classify.scope import (
    Rejection,
    screen_is_internship,
    to_quarantined,
)

__all__ = [
    "EligibilityVerdict",
    "resolve_eligibility",
    "screen_degree_scope",
    "screen_is_cs_role",
    "InternshipVerdict",
    "Rejection",
    "RoleVerdict",
    "classify_role",
    "screen_internship",
    "screen_is_internship",
    "to_quarantined",
]
