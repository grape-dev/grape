from typing import Annotated, Any, Dict, List, Optional

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from src.models.enums import ImplType


class AnalysisState(TypedDict, total=False):
    current_skill_name: str
    skills_to_analyze: List[str]
    config: Dict[str, Any]

    sdk_source_available: bool
    sdk_source_path: str
    cli_tool_available: bool
    cli_tool_path: str
    skills_repo_path: str
    resource_errors: List[str]

    implementation_type: str
    business_goal: str
    cli_evidence: List[str]
    sdk_evidence: List[str]
    impl_detection_errors: List[str]

    api_interfaces: List[Dict[str, Any]]
    openapi_usages: List[Dict[str, Any]]
    api_business_roles: List[Dict[str, Any]]
    sdk_calls: List[Dict[str, Any]]
    mapping_failures: List[str]
    stage_1_result: Dict[str, Any]
    sdk_analysis_skipped: bool
    sdk_analysis_errors: List[str]

    coverage_map: Dict[str, Dict[str, Any]]
    cli_gaps: List[Dict[str, Any]]
    alternatives: List[Dict[str, Any]]
    koocli_correspondences: List[Dict[str, Any]]
    effect_equivalence_results: List[Dict[str, Any]]
    incomplete_correspondence_apis: List[str]
    replacement_feasibility_draft: Dict[str, Any]
    stage_2_result: Dict[str, Any]
    stage_3_result: Dict[str, Any]
    cli_info_source: str
    cli_analysis_errors: List[str]
    llm_enhancement_status: str

    report_content: str
    report_file_path: str
    report_errors: List[str]

    analysis_status: str
    all_errors: Annotated[List[str], add_messages]
    step_statuses: Dict[str, str]

    llm_enabled: bool
    llm_config: Dict[str, Any]
    llm_analysis_results: List[Dict[str, Any]]

    tool_dependencies: List[Dict[str, Any]]