from datetime import UTC, datetime

import pytest

from stage.classify import screen_degree_scope, screen_is_cs_role
from stage.domain import Job, RejectionReason

NOW = datetime(2026, 8, 15, tzinfo=UTC)


def _job(title: str, *, role: str = "unknown") -> Job:
    from stage.domain import RoleCategory

    return Job(
        id=f"greenhouse:acme:{abs(hash(title))}",
        source="greenhouse",
        company="Acme",
        title_raw=title,
        title_normalized=title.lower(),
        apply_url_raw="https://boards.greenhouse.io/acme/jobs/1",
        description="",
        first_seen=NOW,
        last_seen=NOW,
        role=RoleCategory(role),
    )


@pytest.mark.parametrize(
    "title",
    [
        "Electrical Engineering Intern",
        "Mechanical Engineer Intern",
        "Civil Engineering Internship",
        "R&D Materials Engineer Intern",
        "CNC Machine Park Intern",
    ],
)
def test_a_non_cs_engineering_discipline_is_rejected(title: str) -> None:
    rejection = screen_is_cs_role(_job(title))
    assert rejection is not None, f"{title} is not a CS internship"
    assert rejection.reason is RejectionReason.NOT_A_CS_ROLE


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineering | Mechanical Engineering internship",
    ],
)
def test_a_discipline_title_that_also_names_a_cs_role_survives(title: str) -> None:
    assert screen_is_cs_role(_job(title)) is None, f"{title} is reachable by a CS student"


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineering Masters Intern",
        "Analog IC Design Intern - Master's Degree",
        "Computing Graduate Student Intern",
        "Software Developer Intern, MS, Summer 2027",
        "Embedded Software Intern - MSc Graduation Project",
        "Compiler Engineering Graduate Intern",
        "Data Scientist Graduate Co-op",
        "Embedded Software Engineer Grad Intern",
        "Robotics Automation Intern Graduate",
        "Post Doc - AI/ML and Optimization",
        "High School Internship, Software Engineering (Summer 2027)",
    ],
)
def test_a_graduate_only_internship_is_rejected(title: str) -> None:
    rejection = screen_degree_scope(_job(title))
    assert rejection is not None, f"{title} is closed to an undergraduate"
    assert rejection.reason is RejectionReason.OUT_OF_SCOPE_DEGREE


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer Intern - Security-Data - BS/MS",
        "Campus Quantitative Researcher, UG/MS (Intern)",
        "Quantitative Research Analyst Bachelor's or Master's",
        "2026 BSc/MSc/PhD Quantitative Research Intern",
        "Intern - Quantitative Research (Undergrad or Graduate - Summer 2027)",
        "Undergrad Software Engineering Intern (Grad Year: Spring 2028)",
        "Undergraduate And Graduating High School Senior Internship Computational Sciences",
    ],
)
def test_a_posting_that_also_admits_undergraduates_survives(title: str) -> None:
    assert screen_degree_scope(_job(title)) is None, f"{title} names an undergraduate path"


@pytest.mark.parametrize(
    "title",
    [
        "Business Analyst (BA) Junior",
        "Agile Scrum Master - Krakow",
        "Master Data Intern - Service & Support",
        "Production Master Scheduler",
    ],
)
def test_a_degree_lookalike_is_not_a_degree(title: str) -> None:
    assert screen_degree_scope(_job(title, role="swe")) is None, (
        f"{title} carries no degree requirement; BA is a job title and Master is not a degree here"
    )


def _with(title: str, *, description: str = "", category: str = "") -> Job:
    from dataclasses import replace

    from stage.domain import SourceSignals

    return replace(
        _job(title),
        description=description,
        signals=SourceSignals(category=category),
    )


SOFTWARE_BODY = "You will build backend services in Python and ship software daily."


def test_a_description_alone_does_not_make_a_posting_a_cs_role() -> None:
    rejection = screen_is_cs_role(_with("Summer Intern", description=SOFTWARE_BODY))
    assert rejection is not None, "every employer's body text mentions software"
    assert rejection.reason is RejectionReason.UNKNOWN_CS_ROLE


def test_a_title_still_confers_a_cs_role() -> None:
    assert screen_is_cs_role(_with("Software Engineer Intern")) is None


def test_a_source_category_still_confers_a_cs_role() -> None:
    assert screen_is_cs_role(_with("Backend Intern", category="Software")) is None, (
        "a structured category is the publisher's own statement, unlike prose"
    )


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Robotics Intern", "embedded"),
        ("Stagiaire en robotique", "embedded"),
        ("Web Development Intern", "swe"),
        ("Stagiaire en développement web", "swe"),
        ("AI R&D Engineering Co-op", "ml-ai"),
        ("Stagiaire en algorithme", "swe"),
    ],
)
def test_the_masked_title_gaps_are_closed(title: str, expected: str) -> None:
    from stage.classify import classify_role

    assert classify_role(title, "").role.value == expected, title


@pytest.mark.parametrize(
    "title",
    [
        "Analog/Mixed-Signal IC Design Co-Op/Intern",
        "ASIC Design Engineer Intern",
        "Hardware Engineer Co-op",
        "RFIC Design Intern",
        "Physical Design Engineer Intern",
    ],
)
def test_a_role_a_cs_undergraduate_cannot_hold_is_rejected(title: str) -> None:
    from stage.classify import classify_role

    assert classify_role(title, "").role.value == "unknown", (
        f"{title} is chip or circuit work, not a CS internship"
    )


@pytest.mark.parametrize("title", ["Thermal Controls Engineer Intern", "Sales and Trading Intern"])
def test_a_business_or_thermal_title_is_screened_out(title: str) -> None:
    assert screen_is_cs_role(_job(title)) is not None, f"{title} is not a CS internship"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Embedded Software Engineer Intern", "embedded"),
        ("Firmware Engineering Intern", "embedded"),
        ("Robotics Software Intern", "embedded"),
        ("AI Infra Intern", "infra"),
        ("Stagiaire en infrastructure IA", "infra"),
        ("Research Intern - Model Shaping", "ml-ai"),
        ("Product Engineer Intern, Agent Systems", "ml-ai"),
        ("Robot Manipulation Co-op", "embedded"),
        ("AI Performance Engineering Intern", "swe"),
        ("Intern, Quality - Web Developper (Winter 2027)", "swe"),
        ("Cloud Intern", "infra"),
        ("Research Intern - Multimodal AI for Human Experience", "ml-ai"),
    ],
)
def test_embedded_software_and_the_new_phrases_survive(title: str, expected: str) -> None:
    from stage.classify import classify_role

    assert classify_role(title, "").role.value == expected, (
        f"{title} is software a CS undergraduate can hold"
    )


def test_power_systems_engineering_is_electrical_not_software() -> None:
    rejected = screen_is_cs_role(_job("Internship: Power Systems Engineering - Kitchener"))
    assert rejected is not None, "power systems engineering is electrical work, not a CS role"
    kept = screen_is_cs_role(_job("Intern - Artificial Intelligence for Power System Operations"))
    assert kept is None, "naming a CS role must still rescue a title that also names a discipline"


@pytest.mark.parametrize(
    "title",
    [
        "Hardware Engineering Intern, CPU, GPU, SoC, Digital Design, DV",
        "GPU & AI Accelerator Hardware Design Undergraduate Intern",
        "ASIC Design-for-Test Engineer Intern, DFT",
        "Avionics Hardware & Test Engineer Intern",
        "Quantum Hardware Test Engineering Intern 2027",
        "Embedded Hardware Intern",
    ],
)
def test_hardware_titles_that_name_a_generic_cs_phrase_are_still_rejected(title: str) -> None:
    rejection = screen_is_cs_role(_job(title))
    assert rejection is not None, f"{title} is chip or board work, not a CS internship"
    assert rejection.reason is RejectionReason.NOT_A_CS_ROLE


@pytest.mark.parametrize(
    "title",
    [
        "Student Intern, Software & Hardware Engineer (Austin, TX, US)",
        "Hardware and Software Engineering Intern",
        "Robotics Engineering Intern - Hardware & Software",
        "Hardware Security Engineer Intern",
    ],
)
def test_a_title_that_pairs_hardware_with_software_survives(title: str) -> None:
    assert screen_is_cs_role(_job(title)) is None, f"{title} is reachable by a CS student"


@pytest.mark.parametrize(
    "title",
    [
        "Intern - Process Development Engineer, 3D DRAM CMP",
        "Product Development Engineer Intern - Summer 2027",
        "Intern - Materials Test Engineer - Summer 2027",
        "Materials Application Engineer Intern",
        "Intern - R&D Front-End Innovation Intern - Oral Healthcare",
        "Racing Performance Engineer - Summer 2027 Internship",
        "Intern, Power System Engineer (12 months) - Delta, BC",
        "Electrical System Engineering Intern",
        "Undergraduate or Graduate (Year Round) Intern - Building Systems Engineering",
        "Mechanical Packaging Systems Engineering Intern (Summer 2027)",
        "2027 Co-op Downstream Purification, Analytics, & Bioconjugate Process Development",
    ],
)
def test_an_engineering_discipline_behind_a_generic_phrase_is_rejected(title: str) -> None:
    rejection = screen_is_cs_role(_job(title))
    assert rejection is not None, f"{title} is not a CS internship"
    assert rejection.reason is RejectionReason.NOT_A_CS_ROLE


@pytest.mark.parametrize(
    "title",
    [
        "Research Scientist Intern - Multiple Teams",
        "Research Scientist Intern, Monetization Technology - Business Integrity",
        "AI Research Scientist (Intern)",
        "2027 Machine Learning Center of Excellence (NLP) - Summer Associate",
        "2027 Quantitative Investment Strategies Summer Associate Program (New York)",
        "Summer 2027 Power Technical Internship - Wharton, TX (ASSOCIATE'S DEGREE ONLY)",
        "Summer 2027 Nuclear Technical Internship - Pottstown, PA (ASSOCIATE'S DEGREES ONLY)",
        "Applied Scientist Intern",
        "Applied Science Intern - Computer Vision",
        "2027 Applied Science Internship - Robotics - United States",
    ],
)
def test_a_title_closed_to_bachelors_students_by_convention_is_rejected(title: str) -> None:
    rejection = screen_degree_scope(_job(title))
    assert rejection is not None, f"{title} is not open to an undergraduate"
    assert rejection.reason is RejectionReason.OUT_OF_SCOPE_DEGREE


@pytest.mark.parametrize(
    "title",
    [
        "Research Scientist Intern (BS/MS)",
        "Summer Associate Intern - Undergraduate",
        "Technician Internship (Bachelor's or Associate's Degree)",
        "Applied Science Intern - Undergraduate",
    ],
)
def test_a_conventional_graduate_title_naming_undergraduates_survives(title: str) -> None:
    assert screen_degree_scope(_job(title)) is None, f"{title} names an undergraduate path"


def test_a_summer_associate_body_that_asks_for_undergraduates_survives() -> None:
    from stage.classify import screen_internship

    title = "Summer Associate, Information Security"
    body = "Undergraduate student with an expected graduation date in 2028, pursuing a degree in "
    assert screen_internship(title).is_internship, "a summer associate is still an internship"
    assert screen_degree_scope(_job(title)) is not None, "a bank summer associate is graduate"
    assert screen_degree_scope(_with(title, description=body + "cybersecurity")) is None


@pytest.mark.parametrize(
    "title",
    [
        "Summer 2027 Strategic/Systems Engineering Internship - Oswego, NY",
        "Summer 2027 Strategic/Systems Engineering Intern - Lusby, MD",
    ],
)
def test_nuclear_plant_systems_engineering_is_not_a_cs_role(title: str) -> None:
    rejection = screen_is_cs_role(_job(title))
    assert rejection is not None, f"{title} is plant engineering, not a CS internship"
    assert rejection.reason is RejectionReason.NOT_A_CS_ROLE


@pytest.mark.parametrize(
    "title",
    [
        "Information Technology Intern - Data Engineering",
        "Information Technology Intern - Software",
        "Information Technology Intern - Security",
        "Information Technology Intern - Artificial Intelligence",
        "Artificial Intelligence Information Technology Intern - AI",
        "IT Intern - Artificial Intelligence",
        "Classified Cybersecurity/IT Internship (Summer 2027)",
        "AI and Automation (IT) Intern",
        "IT Intern - AI and Operations",
        "Artificial Intelligence (AI) IT Intern 2027 - REMOTE",
        "Digital & IT Intern - Manufacturing AI/ML",
        "Digital & IT Intern, Analyst, Manufacturing AI/ML - Summer 2027",
        "Digital & IT Intern - Analyst - Data & AI Platform Engineering",
        "IT Intern - Enterprise Data & AI Administration",
        "2027 Summer BI/Data Analytics IT Intern",
        "IT Co-op - Cloud Platforms, Tools & Automation",
        "Digital & IT Intern, Analyst, Enterprise Data Management",
    ],
)
def test_an_it_internship_naming_a_cs_field_is_kept(title: str) -> None:
    assert screen_is_cs_role(_job(title)) is None, f"{title} names a CS field"


@pytest.mark.parametrize(
    "title",
    [
        "IT Intern",
        "Information Technology Intern",
        "Investment Information Technology Intern (Full Year 2027)",
        "IT Co-op",
        "Intern - IT (Summer 2027)",
        "Stagiaire TI",
        "Enterprise Architecture Information Technology Intern (Full Year 2027)",
    ],
)
def test_an_it_internship_naming_no_cs_field_stays_out(title: str) -> None:
    assert screen_is_cs_role(_job(title)) is not None, title


@pytest.mark.parametrize(
    "title",
    [
        "2027 IT Operations Summer Intern",
        "IT Infrastructure Intern (Summer 2027)",
        "Co-Op, IT Systems Spring & Summer 2027",
        "IT DevOps Administrator Intern",
        "IT Infrastructure Observability Intern",
    ],
)
def test_an_it_internship_naming_its_own_area_is_kept(title: str) -> None:
    assert screen_is_cs_role(_job(title)) is None, title


@pytest.mark.parametrize(
    "title",
    [
        "Cybersecurity Presales Intern - GPSU/Heartland (Tulsa)",
        "Cybersecurity & Technology Audit Internship (Summer 2027)",
        "Summer Internship Program 2027 - Internal Audit IT Intern",
        "IT Helpdesk Intern - Summer 2027",
        "IT Technician Intern",
        "IT Internship (Techstop Support)",
        "IT Support Intern",
        "Stagiaire, soutien informatique",
    ],
)
def test_presales_audit_and_help_desk_stay_out(title: str) -> None:
    assert screen_is_cs_role(_job(title)) is not None, f"{title} stays out"


def test_an_applied_scientist_body_that_asks_for_undergraduates_survives() -> None:
    title = "Applied Scientist Intern"
    body = "Currently pursuing a Bachelor's degree in computer science or a related field."
    assert screen_degree_scope(_job(title)) is not None, "an applied scientist intern is graduate"
    assert screen_degree_scope(_with(title, description=body)) is None


@pytest.mark.parametrize(
    "body",
    [
        "Minimum Qualifications: Pursuing a PhD in robotics, machine learning or related fields.",
        "What makes you a strong fit: PhD student in robotics, machine learning, or controls.",
        "What you'll bring: currently remotely pursuing a PhD in Robotics or AI.",
    ],
)
def test_a_body_that_asks_for_a_phd_student_is_rejected(body: str) -> None:
    rejection = screen_degree_scope(_with("Robotics Research Intern", description=body))
    assert rejection is not None, "a PhD student requirement closes the posting to undergraduates"
    assert rejection.reason is RejectionReason.OUT_OF_SCOPE_DEGREE


@pytest.mark.parametrize(
    "body",
    [
        "Currently enrolled in a BS, MS or PhD program with a focus on machine learning.",
        "Open to undergraduates and PhD students in computer science.",
    ],
)
def test_a_body_that_also_admits_undergraduates_next_to_a_phd_survives(body: str) -> None:
    assert screen_degree_scope(_with("Robotics Research Intern", description=body)) is None


@pytest.mark.parametrize(
    "title",
    [
        "Summer Intern - HR Analytics & HRIS",
        "People Analytics Intern - Summer 2027 - Franklin, TN",
        "Talent Analytics Intern",
        "Intern, HR Business Partner Support and Data Analytics (Winter 2027)",
        "MHR Summer 2027 Intern - Data Analytics (HR Systems)",
        "Stagiaire, Partenariat d'affaires RH et analyse de données (Hiver 2027)",
        "Intern, Finance Analytics",
        "Finance & Business Analytics Internship",
        "Finance Reporting & Analytics Intern (Year-Round)",
        "Intern - Pricing Analytics & Product Development",
        "Audit Analytics Intern",
        "Strategic Sourcing Analytics Intern",
        "Supply Chain Analytics Intern",
        "Legal Operations Financial, Data & AI Analytics Intern",
        "Sales Excellence Data Analytics Intern - Summer 2027",
        "Revenue Operations Intern (Data Analytics)",
        "Consumer Analytics & Insights Summer Internship 2027",
        "Macy's 2027 Summer Internship – Consumer Insights: Customer & Digital Intelligence",
        "Spring Co-Op 2027, Commercial Analytics, Market Access",
        "Venue Analytics Intern",
        "Analytics Strategy Intern",
        "2027 Early Talent Internship: Business Management and Project Analytics",
        "Intern Business Analytics & Product Allocation",
        "Third-Party Risk & Analytics Intern (Year-Round)",
        "Intern-Customer Experience Analytics (Health Systems Strategy and Growth)",
        "2027 Undergraduate Summer Internship - Customer Experience Insights & Analytics",
        "Customer Experience & Analytics Intern - Undergraduate",
        "Y Analytics, Health and Social Impact, Spring Research Assistant, Internship",
        "Y Analytics, Responsible Investing, Summer Research Assistant, Internship",
        "Content Research Insights & Analytics Intern, Spring 2027",
        "Pioneering Medicines: Portfolio Strategy & Analytics Co-Op",
    ],
)
def test_analytics_inside_a_business_function_is_not_a_cs_role(title: str) -> None:
    rejection = screen_is_cs_role(_job(title))
    assert rejection is not None, f"{title} is business work, not a CS internship"
    assert rejection.reason is RejectionReason.NOT_A_CS_ROLE


@pytest.mark.parametrize(
    "title",
    [
        "Data Analytics Intern",
        "Customer Experience Test Automation Intern",
        "Quantitative Financial Analytics Intern",
        "Marketing Data Science Intern",
        "Business Intelligence Engineer Intern",
        "Global Supply Chain Data Science Intern",
        "Business Analytics AI Co-op",
        "Transportation Data Analyst Intern - Summer 2027",
    ],
)
def test_a_data_discipline_inside_a_business_function_survives(title: str) -> None:
    assert screen_is_cs_role(_job(title)) is None, f"{title} names a data discipline"


@pytest.mark.parametrize(
    "title",
    [
        "Business Analytics Intern",
        "Business Intelligence (Power BI) Intern - Summer 2027",
        "Supply Chain Data Analyst Internship 2027",
        "Customer Experience Data Analyst Intern",
        "Revenue Operations Data Analyst Intern",
        "Sales Data Analytics Intern",
        "Product Marketing Data Analyst Intern, Summer 2027",
        "Financial Analytics Intern",
        "Junior Insights & Analytics Co-op",
        "Data Analyst Intern, People Products",
        "KIP Spring 2027 - Policy Data Analyst Intern - Empire Center for Public Policy",
        "Data Analytics and Automation Intern - Finance",
    ],
)
def test_data_analytics_named_after_a_business_team_is_out(title: str) -> None:
    rejection = screen_is_cs_role(_job(title))
    assert rejection is not None, f"{title} is analytics for a business team"
    assert rejection.reason is RejectionReason.NOT_A_CS_ROLE


@pytest.mark.parametrize(
    "title",
    [
        "Data Center Infrastructure Engineer Intern, Summer 2027 - Midwest / Southeast",
        "Electronic Reliability Engineer Co-op (Winter 2027)",
        "Customer Reliability Engineer Intern (PT)",
        "Spring 2027 - Top Drain TFET for HV AND GPU enabled atomistic simulations Internship",
        "Summer 2027 RF Systems Engineer Intern - Onsite",
        "Co - op ASIC Systems Engineer",
        "Acoustic Systems Engineering Co-op",
        "Mercury Marine: Drives & Propulsion Systems Engineering Intern",
        "Micro-Grid Systems Engineering Intern",
        "Plant Systems Engineering Intern, Orrville, OH Spring 2027",
        "Manufacturing (Systems) Engineering Intern Summer 2027",
        "2027 Field Test Engineer Intern",
        "Reliability Test Engineer Intern",
        "Summer Intern - Product Assurance Test Engineer",
        "2027 Internship - Radio Frequency (RF) Systems Test Engineer - Electronic Warfare",
        "Manufacturing Test Development Engineer Co-op",
        "New Product Development Test Engineer Intern",
        "Field Application Engineering Intern (Summer 2027)",
        "Design Application Engineering Intern (Summer 2027)",
        "Controls Application Engineering Intern",
        "CST Application Engineer Intern - Silicon",
        "Hardware Characterization Automation Co-op",
        "HVP/Certification Engineer Intern",
        "Imaging Product Engineer Intern",
        "Summer 2027 Internship - RA/QA - Quality Assurance - California",
    ],
)
def test_hardware_work_behind_a_systems_test_or_application_phrase_is_rejected(
    title: str,
) -> None:
    rejection = screen_is_cs_role(_job(title))
    assert rejection is not None, f"{title} is hardware or plant work, not a CS internship"
    assert rejection.reason is RejectionReason.NOT_A_CS_ROLE


@pytest.mark.parametrize(
    "title",
    [
        "Avionics Computer Systems Engineering Co-op (Summer/Fall 2027) - Onsite",
        "Algorithm, Software, or RF Systems Engineering Intern",
        "Infrastructure Engineer Intern",
        "Site Reliability Engineer Intern",
        "Software Test Engineer Intern",
        "Software Development Engineer in Test Intern",
        "Software Quality Assurance Engineering Intern",
    ],
)
def test_the_generic_systems_test_and_application_titles_still_pass(title: str) -> None:
    assert screen_is_cs_role(_job(title)) is None, f"{title} is reachable by a CS student"


CS_MAJORS = "Preferred Degree: Computer Science, Information Systems, or Software Engineering."
OTHER_MAJORS = "Pursuing a bachelor's degree in Mechanical Engineering or Electrical Engineering."
STEM = "Pursuing a degree in Science, Technology, Engineering, or Mathematics (STEM)."


@pytest.mark.parametrize(
    ("title", "source", "description", "category", "kept"),
    [
        ("Systems Engineer Intern - Summer 2027", "workday", CS_MAJORS, "", True),
        ("Systems Engineering Intern - Summer", "workday", OTHER_MAJORS, "", False),
        ("Systems Engineering Intern (Summer 2027)", "workday", STEM, "", False),
        ("Systems Engineer Intern", "simplify", "", "Software", True),
        ("Systems Engineer Intern", "simplify", "", "Hardware", False),
        ("Systems Engineer Intern", "greenhouse", "", "", False),
        ("Network Systems Engineer Co-op", "greenhouse", "", "", False),
        ("GNC Systems Engineering Co-Op (Spring 2027)", "vanshb03", "", "", False),
        ("Secure System Engineering Intern (Summer 2027)", "workday", "", "", True),
        ("IT Systems Engineering Intern (19 hours per week)", "workday", "", "", True),
    ],
)
def test_plain_systems_engineering_stays_only_with_a_sign_of_cs(
    title: str, source: str, description: str, category: str, kept: bool
) -> None:
    from dataclasses import replace

    from stage.domain import SourceSignals

    job = replace(
        _job(title),
        id=f"{source}:acme:1",
        source=source,
        description=description,
        signals=SourceSignals(category=category),
    )
    rejection = screen_is_cs_role(job)
    assert (rejection is None) is kept, title
    if rejection is not None:
        assert rejection.reason is RejectionReason.NOT_A_CS_ROLE


@pytest.mark.parametrize(
    ("company", "title", "graduate"),
    [
        ("Microsoft", "Research Intern - Speech Processing", True),
        ("Microsoft", "Stagiaire en recherche - apprentissage automatique", True),
        ("Microsoft", "Undergraduate Research Intern - Machine Learning", False),
        ("Microsoft", "Software Engineer Intern", False),
        ("Acme", "Research Intern - Machine Learning", False),
    ],
)
def test_an_employer_whose_research_interns_are_doctoral_is_read_as_graduate(
    company: str, title: str, graduate: bool
) -> None:
    from dataclasses import replace

    rejection = screen_degree_scope(replace(_job(title), company=company))
    assert (rejection is not None) is graduate, f"{company} {title}"


@pytest.mark.parametrize(
    ("company", "title", "graduate"),
    [
        ("Navy Federal", "Summer Associate Internship (Data Scientist)", False),
        ("Navy Federal", "Mobile Developer Intern - Summer Associate", False),
        ("Goldman Sachs", "The Core Quantitative Strats | Summer Associate", True),
        ("Amazon", "Robotics Applied Scientist 2 Intern", True),
        ("Amazon", "Applied Scientist II Intern - Robotics", True),
    ],
)
def test_an_employer_can_name_its_undergraduate_program_summer_associate(
    company: str, title: str, graduate: bool
) -> None:
    from dataclasses import replace

    rejection = screen_degree_scope(replace(_job(title), company=company))
    assert (rejection is not None) is graduate, f"{company} {title}"


@pytest.mark.parametrize("source", ["workday", "smartrecruiters"])
def test_plain_systems_engineering_waits_for_a_page_its_board_can_give(source: str) -> None:
    from dataclasses import replace

    from stage.domain.liveness import AWAITING_PAGE

    rejection = screen_is_cs_role(replace(_job("Systems Engineer Intern"), source=source))
    assert rejection is not None and rejection.reason is RejectionReason.UNKNOWN_CS_ROLE
    assert rejection.matched_phrase == f"systems engineer{AWAITING_PAGE}"


def test_every_source_that_reads_a_job_page_is_known_to_the_screen() -> None:
    from stage.domain.liveness import PAGED_SOURCES
    from stage.sources import get_adapters

    paged = {
        name for name, adapter in get_adapters().items() if getattr(adapter, "detail_budget", 0)
    }
    assert paged == PAGED_SOURCES


@pytest.mark.parametrize(
    ("title", "category"),
    [
        ("Sports Trader Intern", "Quant"),
        ("Market Design Intern", "Quant"),
        ("DMFI Intern", "Quant"),
        ("Vector Capitalization Intern - Multiple Teams", "Software"),
        ("Technical Artist Intern", "Software"),
        ("Digital Growth Intern", "Software"),
        ("AI Product Intern", "Software"),
        ("Electric Transmission Asset Analyst Intern - Digital Solutions Team", "Software"),
        ("US Pet Health - Sales Training and AI Enablement Intern", ""),
        ("People Development AI & Digital Learning Intern", ""),
        ("Finance & AI Automation Intern", ""),
        ("SCS Commercial Intern - : AI Data Mining Agent", ""),
        ("Intern, Cybersecurity Policy Analyst", ""),
        ("2027 Product Development, Science, and Technology Intern (Undergraduate)", ""),
        ("Spring/Summer 2027 Manufacturing Science & Technology Co-op", ""),
        ("Research and Technology Co-Op, Jan-Jun 2027", ""),
        ("R&D GSC Product Technology Internship- Brecksville, OH", ""),
        ("ESPN Programming Intern, Bristol, Spring 2027", ""),
        ("Programming Intern", "Software"),
    ],
)
def test_a_business_or_lab_title_is_rejected_even_with_a_cs_category(
    title: str, category: str
) -> None:
    rejection = screen_is_cs_role(_with(title, category=category))
    assert rejection is not None, f"{title} is not a CS internship"
    assert rejection.reason is RejectionReason.NOT_A_CS_ROLE


@pytest.mark.parametrize(
    ("title", "category"),
    [
        ("Quantitative Trader Intern", "Quant"),
        ("Trading Operations Engineer Co-op", "Quant"),
        ("Product Engineer Intern", "Software"),
        ("AI Product Engineer Intern", ""),
        ("Cybersecurity Analyst Intern", ""),
        ("Technology Intern", ""),
        ("Computer Science and Technology Intern", ""),
        ("Gameplay Programming Intern - Undergraduate", ""),
        ("Engine and Tools Programming Intern", ""),
        ("Java Programming Intern (Full Year 2027)", ""),
        ("Statistical Programming Intern", ""),
        ("Stagiaire en programmation UI / UI programming intern", ""),
    ],
)
def test_a_cs_title_next_to_those_words_survives(title: str, category: str) -> None:
    assert screen_is_cs_role(_with(title, category=category)) is None, f"{title} is a CS role"


@pytest.mark.parametrize(
    "title",
    [
        "Technical Intern Summer 2027 - Onsite",
        "College Student - Technical - Intern",
        "Technical Internship (2026)",
    ],
)
def test_a_bare_technical_internship_names_no_cs_field(title: str) -> None:
    rejection = screen_is_cs_role(_job(title))
    assert rejection is not None, f"{title} could be any engineering discipline"
    assert rejection.reason is RejectionReason.UNKNOWN_CS_ROLE


def test_a_technical_internship_with_a_software_category_survives() -> None:
    assert screen_is_cs_role(_with("Technical Intern", category="Software")) is None
    assert screen_is_cs_role(_job("Technical Systems Analyst Co-op")) is None


@pytest.mark.parametrize(
    "title",
    [
        "Student Worker Intern - Firmware Engineer - Firmware Thermal/Body",
        "Media & Web Development Intern - Marketing",
        "Web Development Intern - Marketing & Communications",
        "IS Intern - IT Ecommerce & Marketing Developer",
    ],
)
def test_a_cs_discipline_beats_a_thermal_or_marketing_word(title: str) -> None:
    assert screen_is_cs_role(_job(title)) is None, f"{title} names a CS discipline"


@pytest.mark.parametrize(
    "title",
    ["Thermal Engineering Intern - Summer 2027", "Web Marketing Intern", "Marketing Intern"],
)
def test_thermal_and_marketing_titles_without_a_cs_discipline_stay_out(title: str) -> None:
    assert screen_is_cs_role(_job(title)) is not None, f"{title} stays out"


@pytest.mark.parametrize(
    "title",
    [
        "Systems Engineer Intern - Canton, MS",
        "Software Engineering Intern (Jackson, MS)",
        "Database Administrator Intern - MS Access",
        "IT Intern - MS Office and Excel",
    ],
)
def test_ms_as_a_state_or_a_product_is_not_a_degree(title: str) -> None:
    assert screen_degree_scope(_job(title)) is None


@pytest.mark.parametrize(
    "title", ["MS Software Engineering Intern", "Data Science Intern (MS/PhD)"]
)
def test_ms_as_a_degree_still_closes_the_title(title: str) -> None:
    rejection = screen_degree_scope(_job(title))
    assert rejection is not None and rejection.reason is RejectionReason.OUT_OF_SCOPE_DEGREE


def test_a_college_level_stage_is_not_for_undergraduates() -> None:
    college = "Stage collégial | Technologies numériques - Administration de systèmes - Hiver 2027"
    rejection = screen_degree_scope(_job(college))
    assert rejection is not None and rejection.reason is RejectionReason.OUT_OF_SCOPE_DEGREE
    assert screen_degree_scope(_job("Stage universitaire | Science de la donnée")) is None


def _seen_late(title: str, posted: datetime | None = None) -> Job:
    from dataclasses import replace

    seen = datetime(2026, 11, 2, tzinfo=UTC)
    return replace(_job(title), first_seen=seen, last_seen=seen, source_posted_at=posted)


def test_a_title_naming_two_seasons_is_not_ended_by_the_first() -> None:
    from stage.classify import screen_is_internship

    both = _seen_late("ResponsiveAds Full-Stack Developer (2026 Summer and Fall Internship)")
    assert screen_is_internship(both) is None, "fall 2026 is not over"


def test_a_title_naming_only_a_past_year_has_ended() -> None:
    from stage.classify import screen_is_internship

    rejection = screen_is_internship(_seen_late("Internship/Co-Op, Analytics (2025)"))
    assert rejection is not None and rejection.matched_phrase == "2025 has ended"
    assert screen_is_internship(_seen_late("Software Engineer Intern (2026)")) is None


def test_a_title_naming_a_term_that_ended_is_rejected() -> None:
    from stage.classify import screen_is_internship

    rejection = screen_is_internship(_seen_late("Technology Consulting Intern - Summer 2026"))
    assert rejection is not None and rejection.matched_phrase == "summer 2026 has ended"
    posted = datetime(2026, 10, 21, tzinfo=UTC)
    typo = _seen_late("Software Engineering - 2026 Spring Co-Op", posted)
    assert screen_is_internship(typo) is None, "posted after the term, so the year is a typo"
    for ended in ("Software Intern - Summer 25", "Software Intern Summer2025"):
        rejection = screen_is_internship(_seen_late(ended, datetime(2025, 3, 1, tzinfo=UTC)))
        assert rejection is not None and rejection.matched_phrase == "summer 2025 has ended"
    for kept in (
        "Software Engineer Intern - Summer 2027",
        "Software Intern - Summer 27",
        "CW Security Operation Center Engineer Intern (Fall 2026- Starting in December)",
        "Robotics Intern - Winter 2026",
        "Intern Software Engineer - Summer 2026/2027",
    ):
        assert screen_is_internship(_seen_late(kept)) is None, kept


@pytest.mark.parametrize(
    ("title", "link", "graduate"),
    [
        (
            "Data Science Intern",
            "https://amazon.jobs/en/jobs/10556927/2027-data-science-internship-united-states-phd-or-masters-student",
            True,
        ),
        (
            "Business Intelligence Analyst Intern",
            "https://amat.wd1.myworkdayjobs.com/External/job/Santa-ClaraCA/"
            "Summer-2027-Business-Intelligence-Analyst-Intern--Master-s--Santa-Clara--CA-_R2627411",
            True,
        ),
        (
            "Summer 2027 Intern - Software Engineering",
            "https://ryan.wd1.myworkdayjobs.com/en-US/students-graduates/job/Plano/"
            "Summer-2027-Intern--Software-Engineering_R0020843",
            False,
        ),
        (
            "Firmware Engineer Intern",
            "https://marvell.wd1.myworkdayjobs.com/MarvellCareers/job/Toronto-Canada/"
            "Firmware-Engineer-Intern---BS-MS---2027-Co-Op_2604959",
            False,
        ),
        (
            "Software Intern",
            "https://acme.wd1.myworkdayjobs.com/ext/job/Jackson-MS/Software-Intern--Jackson--MS_R1",
            False,
        ),
    ],
)
def test_a_feed_title_that_drops_the_degree_is_read_from_its_link(
    title: str, link: str, graduate: bool
) -> None:
    from dataclasses import replace

    rejection = screen_degree_scope(replace(_job(title), apply_url_raw=link))
    assert (rejection is not None) is graduate, link


@pytest.mark.parametrize(
    "title",
    [
        "Quality Assurance Intern",
        "Software Quality Assurance Intern",
        "QA Analyst co-op",
        "Spring/Summer 2027 Quality Assurance Intern - Oxnard, CA",
        "Associ\u00e9(e) en assurance qualit\u00e9 des logiciels - Stage",
    ],
)
def test_qa_that_writes_no_code_stays_out(title: str) -> None:
    rejection = screen_is_cs_role(_job(title))
    assert rejection is not None and rejection.reason is RejectionReason.NOT_A_CS_ROLE, title


@pytest.mark.parametrize(
    "title",
    [
        "QA Automation Engineer Intern",
        "IT QA Automation Intern",
        "Quality Assurance Engineer Co-op",
        "Year-Round Intern - Automation Testing Frameworks",
        "Winter 2027 - Software Development, Quality Assurance and AI Intern",
    ],
)
def test_qa_that_writes_code_is_kept(title: str) -> None:
    assert screen_is_cs_role(_job(title)) is None, title
