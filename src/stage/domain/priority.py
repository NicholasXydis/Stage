SOURCE_PRIORITY: tuple[str, ...] = (
    "greenhouse",
    "lever",
    "smartrecruiters",
    "ashby",
    "workday",
    "workable",
    "bamboohr",
    "recruitee",
    "breezy",
    "collage",
    "oracle_cloud",
    "custom_json",
    "quebec-emploi",
    "jobbank",
    "themuse",
    "simplify",
    "vanshb03",
    "speedyapply",
    "zshah101",
    "hanzili",
    "negar",
    "northwestern-quant",
)


def source_rank(source: str, job_id: str = "") -> tuple[int, str]:
    try:
        index = SOURCE_PRIORITY.index(source)
    except ValueError:
        index = len(SOURCE_PRIORITY)
    return index, job_id
