import os
from dataclasses import asdict

from src.graph.state import AnalysisState
from src.infra.logger import get_step_logger
from src.models.enums import ImplType
from src.analyzers.sdk_interface_analyzer import SdkInterfaceAnalyzer


def sdk_interface_analysis_node(state: AnalysisState) -> dict:
    log = get_step_logger("sdk_interface_analysis")
    log.info("Starting SDK interface analysis")

    updates = {
        "api_interfaces": [],
        "openapi_usages": [],
        "api_business_roles": [],
        "sdk_calls": [],
        "mapping_failures": [],
        "stage_1_result": {},
        "sdk_analysis_skipped": False,
        "sdk_analysis_errors": [],
        "step_statuses": {**state.get("step_statuses", {}), "sdk_interface_analysis": "running"},
    }

    if not state.get("sdk_source_available", False):
        updates["sdk_analysis_skipped"] = True
        updates["sdk_analysis_errors"] = ["SDK source not available, skipping SDK interface analysis"]
        updates["step_statuses"]["sdk_interface_analysis"] = "skipped"
        log.info("Skipped: SDK source not available")
        return updates


    try:
        analyzer = SdkInterfaceAnalyzer()
        skill_path = state.get("skills_repo_path", "")
        result = analyzer.analyze(skill_path, state.get("sdk_source_path", ""))

        updates["api_interfaces"] = [asdict(api) for api in result.api_interfaces]
        updates["openapi_usages"] = [asdict(usage) for usage in result.openapi_usages]
        updates["api_business_roles"] = [asdict(role) for role in result.api_business_roles]
        updates["sdk_calls"] = [asdict(call) for call in result.sdk_calls]
        updates["mapping_failures"] = result.mapping_failures
        updates["stage_1_result"] = asdict(result.stage_1_result) if result.stage_1_result else {}
        updates["step_statuses"]["sdk_interface_analysis"] = "completed"
        log.info(f"SDK analysis completed: {len(result.openapi_usages)} Open APIs found")
    except Exception as e:
        updates["sdk_analysis_errors"] = [f"SDK interface analysis failed: {str(e)}"]
        updates["step_statuses"]["sdk_interface_analysis"] = "failed"
        log.info(f"SDK interface analysis failed: {e}")

    return updates