from enum import StrEnum


class JobStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class Language(StrEnum):
    EN = "en"
    FR = "fr"
    BILINGUAL = "bilingual"

    UNKNOWN = "unknown"


class LocationBucket(StrEnum):
    CANADA = "canada"
    USA = "usa"
    MONTREAL = "montreal"
    INTERNATIONAL = "international"

    UNKNOWN = "unknown"


class RemoteScope(StrEnum):
    CANADA = "remote-canada"
    US = "remote-us"
    UNSPECIFIED = "remote-unspecified"


class RoleCategory(StrEnum):
    SWE = "swe"
    SECURITY = "security"
    DATA = "data"
    ML_AI = "ml-ai"
    QUANT = "quant"
    INFRA = "infra"
    HARDWARE = "hardware"
    EMBEDDED = "embedded"
    GENERAL_CS = "general-cs"
    UNKNOWN = "unknown"


class DegreeRequirement(StrEnum):
    NONE = "none"
    BACHELORS = "bachelors"
    MASTERS = "masters"
    PHD = "phd"
    UNKNOWN = "unknown"


class Platform(StrEnum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    SMARTRECRUITERS = "smartrecruiters"
    RECRUITEE = "recruitee"
    WORKABLE = "workable"
    PERSONIO = "personio"
    TEAMTAILOR = "teamtailor"
    BREEZY = "breezy"
    BAMBOOHR = "bamboohr"
    JOBVITE = "jobvite"
    JOIN = "join"
    WORKDAY = "workday"
    TALEO = "taleo"
    ORACLE_CLOUD = "oracle_cloud"
    SUCCESSFACTORS = "successfactors"
    NJOYN = "njoyn"
    ICIMS = "icims"
    AVATURE = "avature"
    PHENOM = "phenom"
    COLLAGE = "collage"
    CUSTOM_JSON = "custom_json"


class Priority(StrEnum):
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class SourceOfRecord(StrEnum):
    OPENJOBS = "openjobs"
    DISCOVER = "discover"
    MANUAL = "manual"


class ProbeVerdict(StrEnum):
    MATCH = "match"
    UNVERIFIED = "unverified"
    EMPTY = "empty"
    REJECTED = "rejected"
    MISS = "miss"
    ERROR = "error"


class EmployerSize(StrEnum):
    STARTUP = "startup"
    MID = "mid"
    LARGE = "large"


class SyncOutcome(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILURE = "failure"


class ExportFormat(StrEnum):
    CSV = "csv"
    JSON = "json"
    MD = "md"
    PDF = "pdf"


UNKNOWN_TERM = "unknown"
