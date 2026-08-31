from dataclasses import asdict
from typing import List

from langgraph.graph import END, StateGraph


from src.graph.state import AnalysisState
from src.graph.routes import route_by_impl_type
from src.infra.checkpoint_manager import CheckpointManager
from src.models.report import AnalysisConfig, SkillAnalysisResult
from src.nodes.resource_prep_node import resource_preparation_node
from src.nodes.impl_type_detect_node import impl_type_detection_node
from src.nodes.tool_analysis_node import tool_analysis_node
from src.nodes.sdk_interface_node import sdk_interface_analysis_node
from src.nodes.cli_coverage_node import cli_coverage_analysis_node
from src.nodes.report_gen_node import report_generation_node


def build_analysis_graph(
    checkpoint_dir: str = "./cache/checkpoints",
    use_memory_saver: bool = False,
):
    graph = StateGraph(AnalysisState)

    graph.add_node("resource_preparation", resource_preparation_node)
    graph.add_node("impl_type_detection", impl_type_detection_node)
    graph.add_node("tool_analysis", tool_analysis_node)
    graph.add_node("sdk_interface_analysis", sdk_interface_analysis_node)
    graph.add_node("cli_coverage_analysis", cli_coverage_analysis_node)
    graph.add_node("report_generation", report_generation_node)

    graph.set_entry_point("resource_preparation")
    graph.add_edge("resource_preparation", "impl_type_detection")
    graph.add_edge("impl_type_detection", "tool_analysis")
    graph.add_edge("tool_analysis", "sdk_interface_analysis")
    graph.add_edge("sdk_interface_analysis", "cli_coverage_analysis")
    graph.add_edge("cli_coverage_analysis", "report_generation")
    graph.add_edge("report_generation", END)

    cp_manager = CheckpointManager(checkpoint_dir)
    saver = cp_manager.get_saver(use_memory=use_memory_saver)

    return graph.compile(checkpointer=saver)


def create_initial_state(config: AnalysisConfig, skill_name: str) -> AnalysisState:
    config_dict = asdict(config)
    return AnalysisState(
        current_skill_name=skill_name,
        skills_to_analyze=config.skill_names if config.skill_names else [skill_name],
        config=asdict(config),
        sdk_source_available=False,
        sdk_source_path="",
        cli_tool_available=False,
        cli_tool_path="",
        skills_repo_path=config.skills_repo_path,
        resource_errors=[],
        implementation_type="UNKNOWN",
        business_goal="",
        cli_evidence=[],
        sdk_evidence=[],
        impl_detection_errors=[],
        api_interfaces=[],
        openapi_usages=[],
        api_business_roles=[],
        sdk_calls=[],
        mapping_failures=[],
        stage_1_result={},
        sdk_analysis_skipped=False,
        sdk_analysis_errors=[],
        coverage_map={},
        cli_gaps=[],
        alternatives=[],
        koocli_correspondences=[],
        effect_equivalence_results=[],
        incomplete_correspondence_apis=[],
        replacement_feasibility_draft={},
        stage_2_result={},
        stage_3_result={},
        cli_info_source="",
        cli_analysis_errors=[],
        llm_enhancement_status="未启用",
        report_content="",
        report_file_path="",
        report_errors=[],
        analysis_status="pending",
        all_errors=[],
        step_statuses={},
        llm_enabled=config_dict.get("llm_enabled", False),
        llm_config=config_dict.get("llm_config", {}),
        llm_analysis_results=[],
        tool_dependencies=[],
    )


def analyze_all_skills(config: AnalysisConfig) -> List[SkillAnalysisResult]:
    from src.infra.logger import setup_logger, get_step_logger
    setup_logger(level=config.log_level)
    log = get_step_logger("analyze_all_skills")

    graph = build_analysis_graph(
        checkpoint_dir=config.checkpoint_dir,
    )

    from src.resources.skills_repo_manager import SkillsRepoManager
    repo_manager = SkillsRepoManager(config.skills_repo_path)
    all_skills = repo_manager.discover_all_skills()

    skill_names = config.skill_names
    if skill_names:
        filtered = {n: p for n, p in all_skills.items() if n in skill_names}
        for name in skill_names:
            if name not in filtered:
                for n, p in all_skills.items():
                    if name in n or name in str(p):
                        filtered[n] = p
                        break
        all_skills = filtered if filtered else all_skills

    results = []
    for skill_name, skill_path in all_skills.items():
        log.info(f"Analyzing skill: {skill_name}")
        thread_id = f"skill_{skill_name}"
        thread_config = {"configurable": {"thread_id": thread_id}}

        if config.resume:
            initial_state = None
        else:
            initial_state = create_initial_state(config, skill_name)
            initial_state["skills_repo_path"] = str(skill_path)

        final_state = graph.invoke(initial_state, thread_config)
        result = _extract_result(final_state, skill_name)
        results.append(result)

    return results


def _extract_result(final_state: AnalysisState, skill_name: str) -> SkillAnalysisResult:
    from src.models.enums import AnalysisStatus, ImplType
    from src.models.skill import SkillImplResult
    from src.models.api_interface import ApiInterface, SdkCallInfo
    from src.models.cli_command import CliCoverageStatus
    from src.models.cli_gap import CliGap, AlternativeScheme
    from src.models.report import SdkAnalysisResult, CliCoverageResult

    impl_type_str = final_state.get("implementation_type", "UNKNOWN")
    try:
        impl_type = ImplType(impl_type_str)
    except ValueError:
        impl_type = ImplType.UNKNOWN

    skill_info = SkillImplResult(
        skill_name=skill_name,
        implementation_type=impl_type,
        source_path=final_state.get("skills_repo_path", ""),
        business_goal=final_state.get("business_goal", ""),
        cli_evidence=final_state.get("cli_evidence", []),
        sdk_evidence=final_state.get("sdk_evidence", []),
    )

    sdk_result = SdkAnalysisResult(
        api_interfaces=[ApiInterface(**d) for d in final_state.get("api_interfaces", [])],
        sdk_calls=[SdkCallInfo(**d) for d in final_state.get("sdk_calls", [])],
        mapping_failures=final_state.get("mapping_failures", []),
    )

    cli_result = CliCoverageResult(
        coverage_map={k: CliCoverageStatus(**v) for k, v in final_state.get("coverage_map", {}).items()},
        cli_gaps=[CliGap(**d) for d in final_state.get("cli_gaps", [])],
        alternatives=[AlternativeScheme(**d) for d in final_state.get("alternatives", [])],
        info_source=final_state.get("cli_info_source", ""),
    )

    all_errors = final_state.get("all_errors", [])
    status_str = final_state.get("analysis_status", "completed")
    try:
        status = AnalysisStatus(status_str)
    except ValueError:
        status = AnalysisStatus.PARTIAL if all_errors else AnalysisStatus.COMPLETED

    return SkillAnalysisResult(
        skill_info=skill_info,
        sdk_result=sdk_result,
        cli_result=cli_result,
        analysis_status=status,
        errors=all_errors,
    )