from dataclasses import asdict

from src.analyzers.cli_correspondence_analyzer import CliCorrespondenceAnalyzer
from src.analyzers.replacement_feasibility_evaluator import ReplacementFeasibilityEvaluator
from src.graph.state import AnalysisState
from src.infra.logger import get_step_logger
from src.nodes.report_gen_node import _dict_to_openapi_usage, _dict_to_api_business_role


def cli_coverage_analysis_node(state: AnalysisState) -> dict:
    log = get_step_logger("cli_coverage_analysis")
    log.info("Starting KooCLI correspondence analysis")

    updates = {
        "coverage_map": {},
        "cli_gaps": [],
        "alternatives": [],
        "koocli_correspondences": [],
        "effect_equivalence_results": [],
        "incomplete_correspondence_apis": [],
        "replacement_feasibility_draft": {},
        "stage_2_result": {},
        "stage_3_result": {},
        "cli_info_source": "",
        "cli_analysis_errors": [],
        "step_statuses": {**state.get("step_statuses", {}), "cli_coverage_analysis": "running"},
    }

    usage_data = state.get("openapi_usages", [])
    if not usage_data:
        updates["cli_info_source"] = "skipped"
        updates["step_statuses"]["cli_coverage_analysis"] = "skipped"
        log.info("Skipped: no Open API usages to analyze")
        return updates

    try:
        openapi_usages = [_dict_to_openapi_usage(d) for d in usage_data]
        role_data = state.get("api_business_roles", [])
        api_business_roles = [_dict_to_api_business_role(d) for d in role_data if isinstance(d, dict)]

        analyzer = CliCorrespondenceAnalyzer(
            cli_tool_available=state.get("cli_tool_available", False),
            cli_tool_path=state.get("cli_tool_path", ""),
        )
        result = analyzer.analyze(openapi_usages, api_business_roles)

        feasibility_evaluator = ReplacementFeasibilityEvaluator()
        feasibility, stage_3 = feasibility_evaluator.evaluate(
            openapi_usages,
            api_business_roles,
            result.correspondences,
            result.equivalence_results,
        )

        updates["koocli_correspondences"] = [asdict(c) for c in result.correspondences]
        updates["effect_equivalence_results"] = [asdict(e) for e in result.equivalence_results]
        updates["incomplete_correspondence_apis"] = result.incomplete_apis
        updates["replacement_feasibility_draft"] = asdict(feasibility)
        updates["stage_2_result"] = asdict(result.stage_2_result) if result.stage_2_result else {}
        updates["stage_3_result"] = asdict(stage_3)
        updates["cli_info_source"] = result.info_source
        updates["step_statuses"]["cli_coverage_analysis"] = "completed"
        log.info(f"KooCLI correspondence analysis completed: {len(result.correspondences)} APIs analyzed")
    except Exception as e:
        updates["cli_analysis_errors"] = [f"KooCLI correspondence analysis failed: {str(e)}"]
        updates["step_statuses"]["cli_coverage_analysis"] = "failed"
        log.info(f"KooCLI correspondence analysis failed: {e}")

    return updates
