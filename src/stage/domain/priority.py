SOURCE_PRIORITY: tuple[str, ...] = (
    "greenhouse",
    "lever",
    "smartrecruiters",
    "ashby",
    "workday",
    "simplify",
    "vanshb03",
    "speedyapply",
)


def source_rank(source: str, job_id: str = "") -> tuple[int, str]:
    try:
        index = SOURCE_PRIORITY.index(source)
    except ValueError:
        index = len(SOURCE_PRIORITY)
    return index, job_id
