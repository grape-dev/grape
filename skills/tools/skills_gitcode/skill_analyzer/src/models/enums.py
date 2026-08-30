from enum import Enum


class ImplType(str, Enum):
    KOOCLI = "KooCLI"
    SDK = "SDK"
    HYBRID = "HYBRID"
    UNKNOWN = "UNKNOWN"


class AnalysisStatus(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class HttpMethod(str, Enum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    PATCH = "PATCH"


class CliCoveredStatus(str, Enum):
    COVERED = "covered"
    NOT_COVERED = "not_covered"
    UNCERTAIN = "uncertain"


class ImpactLevel(str, Enum):
    CRITICAL = "critical"
    IMPORTANT = "important"
    NORMAL = "normal"


class IsMandatory(str, Enum):
    MANDATORY = "mandatory"
    OPTIONAL = "optional"


class AlternativeFeasibility(str, Enum):
    FULL = "full"
    PARTIAL = "partial"
    NOT_FEASIBLE = "not_feasible"
    NONE = "none"


class StepStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    RETRYING = "retrying"


class CorrespondenceStatus(str, Enum):
    EXACT = "效果完全一致"
    PARTIAL = "不完全一致"
    NONE = "无对应命令"
    UNKNOWN = "待确认"


class DimensionStatus(str, Enum):
    SATISFIED = "满足"
    NOT_SATISFIED = "不满足"
    UNKNOWN = "待确认"
    NOT_APPLICABLE = "不适用"


class Acceptability(str, Enum):
    ACCEPTABLE = "可接受"
    NOT_ACCEPTABLE = "不可接受"
    UNKNOWN = "待确认"


class ReplacementConclusion(str, Enum):
    FULLY_REPLACEABLE = "可以完全替换"
    PARTIALLY_REPLACEABLE = "部分可替换"
    NOT_REPLACEABLE = "不能完全替换"
    NEEDS_CONFIRMATION = "待确认"


class LlmEnhancementStatus(str, Enum):
    DISABLED = "未启用"
    ENABLED = "已启用"
    DEGRADED = "已降级"
    FAILED = "失败"


class CliInfoSource(str, Enum):
    GUIDANCE = "guidance"
    LOCAL_CLI = "local_cli"
    MIXED = "mixed"
    NONE = "none"


class ToolCategory(str, Enum):
    SDK = "SDK"
    KOOCLI = "KooCLI"
    LANGUAGE = "Language"
    TOOL = "Tool"
    LIBRARY = "Library"
