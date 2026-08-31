from dataclasses import dataclass, field
from typing import Dict, List

from src.models.api_interface import ApiBusinessRole, ApiInterface, OpenAPIUsage, SdkCallInfo
from src.models.cli_command import CliCommand, CliCoverageStatus, EffectEquivalenceResult, KooCliCorrespondence
from src.models.cli_gap import AlternativeScheme, CliGap
from src.models.enums import AnalysisStatus
from src.models.replacement import KooCliReplacementFeasibility
from src.models.skill import SkillImplResult


@dataclass
class ReportStageResult:
    stage_name: str
    summary: str
    details: Dict = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


@dataclass
class SdkAnalysisResult:
    api_interfaces: List[ApiInterface] = field(default_factory=list)
    openapi_usages: List[OpenAPIUsage] = field(default_factory=list)
    api_business_roles: List[ApiBusinessRole] = field(default_factory=list)
    sdk_calls: List[SdkCallInfo] = field(default_factory=list)
    mapping_failures: List[str] = field(default_factory=list)
    stage_1_result: ReportStageResult = None


@dataclass
class CliCoverageResult:
    coverage_map: Dict[str, CliCoverageStatus] = field(default_factory=dict)
    cli_gaps: List[CliGap] = field(default_factory=list)
    alternatives: List[AlternativeScheme] = field(default_factory=list)
    info_source: str = "guidance"


@dataclass
class CliCorrespondenceResult:
    correspondences: List[KooCliCorrespondence] = field(default_factory=list)
    equivalence_results: List[EffectEquivalenceResult] = field(default_factory=list)
    incomplete_apis: List[str] = field(default_factory=list)
    info_source: str = "none"
    analysis_warnings: List[str] = field(default_factory=list)
    stage_2_result: ReportStageResult = None


@dataclass
class SkillAnalysisResult:
    skill_info: SkillImplResult
    sdk_result: SdkAnalysisResult = field(default_factory=SdkAnalysisResult)
    cli_result: CliCoverageResult = field(default_factory=CliCoverageResult)
    correspondence_result: CliCorrespondenceResult = field(default_factory=CliCorrespondenceResult)
    replacement_feasibility: KooCliReplacementFeasibility = None
    stage_1_result: ReportStageResult = None
    stage_2_result: ReportStageResult = None
    stage_3_result: ReportStageResult = None
    analysis_status: AnalysisStatus = AnalysisStatus.COMPLETED
    errors: List[str] = field(default_factory=list)


@dataclass
class ReportOutput:
    report_title: str
    generation_time: str
    content_md: str
    file_path: str


@dataclass
class AnalysisConfig:
    skills_repo_path: str
    skill_names: List[str] = field(default_factory=list)
    sdk_cache_dir: str = "./cache/sdk-source"
    cli_install_dir: str = "./cache/koo-cli"
    output_dir: str = "./reports"
    checkpoint_dir: str = "./cache/checkpoints"
    skip_cli_install: bool = False
    resume: bool = False
    log_level: str = "INFO"
    sdk_repo_url: str = "https://github.com/huaweicloud/huaweicloud-sdk-python-v3"
    max_retries: int = 3
    llm_enabled: bool = False
    llm_config_dict: Dict = field(default_factory=dict)
