from dataclasses import asdict

from src.graph.state import AnalysisState
from src.infra.logger import get_step_logger
from src.models.enums import (
    Acceptability,
    AnalysisStatus,
    CliCoveredStatus,
    CliInfoSource,
    CorrespondenceStatus,
    DimensionStatus,
    HttpMethod,
    ImplType,
    IsMandatory,
    LlmEnhancementStatus,
    ReplacementConclusion,
)
from src.models.skill import SkillImplResult
from src.models.api_interface import (
    ApiBusinessRole,
    ApiInterface,
    ApiParameter,
    OpenAPIUsage,
    SdkCallInfo,
    SourceSdkCall,
)
from src.models.cli_command import (
    CliCommand,
    CliCoverageStatus,
    EffectEquivalenceResult,
    EquivalenceDimensionResult,
    KooCliCommand,
    KooCliCorrespondence,
    KooCliParameter,
)
from src.models.cli_gap import CliGap, AlternativeScheme
from src.models.replacement import BlockingPoint, KooCliReplacementFeasibility
from src.models.report import (
    CliCorrespondenceResult,
    CliCoverageResult,
    ReportOutput,
    ReportStageResult,
    SdkAnalysisResult,
    SkillAnalysisResult,
)
from src.analyzers.report_generator import ReportGenerator


def _safe_enum(enum_cls, value, default=None):
    if value is None:
        return default
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except (ValueError, TypeError):
        return default


def _dict_to_api_parameter(d: dict) -> ApiParameter:
    return ApiParameter(
        name=d.get("name", ""),
        param_type=d.get("param_type", ""),
        is_required=d.get("is_required", False),
        description=d.get("description", ""),
    )


def _dict_to_source_sdk_call(d: dict) -> SourceSdkCall:
    if d is None:
        return SourceSdkCall("", "", "")
    return SourceSdkCall(
        service_name=d.get("service_name", ""),
        client_class=d.get("client_class", ""),
        method_name=d.get("method_name", ""),
        source_file=d.get("source_file", ""),
        line_number=d.get("line_number", 0),
        call_arguments=d.get("call_arguments", {}),
    )


def _dict_to_api_business_role(d: dict) -> ApiBusinessRole:
    if d is None:
        return None
    return ApiBusinessRole(
        usage_id=d.get("usage_id", ""),
        business_role=d.get("business_role", ""),
        business_stage=d.get("business_stage", ""),
        is_mandatory=_safe_enum(IsMandatory, d.get("is_mandatory"), IsMandatory.MANDATORY),
        upstream_dependencies=d.get("upstream_dependencies", []),
        downstream_dependencies=d.get("downstream_dependencies", []),
        state_impact=d.get("state_impact", ""),
        rule_basis=d.get("rule_basis", ""),
        llm_suggestion=d.get("llm_suggestion", ""),
    )


def _dict_to_openapi_usage(d: dict) -> OpenAPIUsage:
    source_sdk = d.get("source_sdk_call")
    business_role = d.get("business_role")
    return OpenAPIUsage(
        usage_id=d.get("usage_id", ""),
        service_name=d.get("service_name", ""),
        api_name=d.get("api_name", ""),
        api_path=d.get("api_path", ""),
        http_method=_safe_enum(HttpMethod, d.get("http_method"), HttpMethod.GET),
        required_params=[_dict_to_api_parameter(p) for p in d.get("required_params", [])],
        optional_params=[_dict_to_api_parameter(p) for p in d.get("optional_params", [])],
        actual_params=d.get("actual_params", {}),
        source_sdk_call=_dict_to_source_sdk_call(source_sdk if isinstance(source_sdk, dict) else None),
        response_fields_used=d.get("response_fields_used", []),
        error_branches=d.get("error_branches", []),
        business_role=_dict_to_api_business_role(business_role if isinstance(business_role, dict) else None),
    )


def _dict_to_koocli_parameter(d: dict) -> KooCliParameter:
    return KooCliParameter(
        name=d.get("name", ""),
        description=d.get("description", ""),
        required=d.get("required", False),
        value_type=d.get("value_type", "str"),
    )


def _dict_to_koocli_command(d: dict) -> KooCliCommand:
    if d is None:
        return None
    return KooCliCommand(
        command=d.get("command", ""),
        service_name=d.get("service_name", ""),
        operation_name=d.get("operation_name", ""),
        resource_object=d.get("resource_object", ""),
        parameters=[_dict_to_koocli_parameter(p) for p in d.get("parameters", [])],
        output_fields=d.get("output_fields", []),
        info_source=_safe_enum(CliInfoSource, d.get("info_source"), CliInfoSource.NONE),
    )


def _dict_to_equivalence_dimension_result(d: dict) -> EquivalenceDimensionResult:
    return EquivalenceDimensionResult(
        dimension_name=d.get("dimension_name", ""),
        status=_safe_enum(DimensionStatus, d.get("status"), DimensionStatus.UNKNOWN),
        basis=d.get("basis", ""),
        difference=d.get("difference", ""),
        impact=d.get("impact", ""),
    )


def _dict_to_effect_equivalence_result(d: dict) -> EffectEquivalenceResult:
    candidate = d.get("candidate_command")
    return EffectEquivalenceResult(
        usage_id=d.get("usage_id", ""),
        api_name=d.get("api_name", ""),
        candidate_command=_dict_to_koocli_command(candidate if isinstance(candidate, dict) else None),
        dimensions=[_dict_to_equivalence_dimension_result(dim) for dim in d.get("dimensions", [])],
        overall_status=_safe_enum(CorrespondenceStatus, d.get("overall_status"), CorrespondenceStatus.UNKNOWN),
        confidence=d.get("confidence", 0.0),
        consistency_basis=d.get("consistency_basis", ""),
        difference_description=d.get("difference_description", ""),
        impact_description=d.get("impact_description", ""),
        acceptable=_safe_enum(Acceptability, d.get("acceptable"), Acceptability.UNKNOWN),
        manual_verification_items=d.get("manual_verification_items", []),
        llm_assisted=d.get("llm_assisted", False),
    )


def _dict_to_koocli_correspondence(d: dict) -> KooCliCorrespondence:
    selected = d.get("selected_command")
    equiv = d.get("equivalence_result")
    return KooCliCorrespondence(
        usage_id=d.get("usage_id", ""),
        api_name=d.get("api_name", ""),
        service_name=d.get("service_name", ""),
        candidate_commands=[_dict_to_koocli_command(c) for c in d.get("candidate_commands", []) if isinstance(c, dict)],
        selected_command=_dict_to_koocli_command(selected if isinstance(selected, dict) else None),
        status=_safe_enum(CorrespondenceStatus, d.get("status"), CorrespondenceStatus.UNKNOWN),
        equivalence_result=_dict_to_effect_equivalence_result(equiv) if isinstance(equiv, dict) else None,
        difference_description=d.get("difference_description", ""),
        impact_description=d.get("impact_description", ""),
        acceptable=_safe_enum(Acceptability, d.get("acceptable"), Acceptability.UNKNOWN),
        info_source=_safe_enum(CliInfoSource, d.get("info_source"), CliInfoSource.NONE),
        manual_verification_items=d.get("manual_verification_items", []),
        api_explorer_url=d.get("api_explorer_url", ""),
    )


def _dict_to_blocking_point(d: dict) -> BlockingPoint:
    return BlockingPoint(
        api_name=d.get("api_name", ""),
        reason=d.get("reason", ""),
        business_impact=d.get("business_impact", ""),
        recommendation=d.get("recommendation", ""),
    )


def _dict_to_replacement_feasibility(d: dict) -> KooCliReplacementFeasibility:
    if d is None:
        return None
    return KooCliReplacementFeasibility(
        conclusion=_safe_enum(ReplacementConclusion, d.get("conclusion"), ReplacementConclusion.NEEDS_CONFIRMATION),
        reason=d.get("reason", ""),
        blocking_points=[_dict_to_blocking_point(p) for p in d.get("blocking_points", [])],
        replaceable_scope=d.get("replaceable_scope", []),
        non_replaceable_scope=d.get("non_replaceable_scope", []),
        statistics=d.get("statistics", {}),
        llm_status=_safe_enum(LlmEnhancementStatus, d.get("llm_status"), LlmEnhancementStatus.DISABLED),
    )


def _dict_to_report_stage_result(d: dict) -> ReportStageResult:
    if d is None:
        return None
    return ReportStageResult(
        stage_name=d.get("stage_name", ""),
        summary=d.get("summary", ""),
        details=d.get("details", {}),
        warnings=d.get("warnings", []),
    )


def report_generation_node(state: AnalysisState) -> dict:
    log = get_step_logger("report_generation")
    log.info("Starting report generation")

    updates = {
        "report_errors": [],
        "step_statuses": {**state.get("step_statuses", {}), "report_generation": "running"},
    }

    try:
        skill_result = _reconstruct_skill_result(state)
        config = state.get("config", {})
        output_dir = config.get("output_dir", "./reports")

        llm_results = state.get("llm_analysis_results", [])
        if state.get("llm_enabled", False) and not llm_results:
            try:
                summary = _llm_summarize(state, skill_result)
                if summary:
                    llm_results = llm_results + [summary]
                    existing = state.get("llm_analysis_results", [])
                    updates["llm_analysis_results"] = existing + [summary]
            except Exception as e:
                log.info(f"LLM summary generation failed (degraded): {e}")

        generator = ReportGenerator(output_dir=output_dir)
        skills_to_analyze = state.get("skills_to_analyze", [])
        if len(skills_to_analyze) > 5:
            generator.set_skip_llm(True)
        tool_deps = state.get("tool_dependencies", [])
        output = generator.generate(skill_result, llm_analysis_results=llm_results, tool_dependencies=tool_deps)

        updates["report_content"] = output.content_md
        updates["report_file_path"] = output.file_path
        updates["analysis_status"] = _determine_overall_status(state)
        updates["step_statuses"]["report_generation"] = "completed"
        log.info(f"Report generated: {output.file_path}")
    except Exception as e:
        updates["report_errors"] = [f"Report generation failed: {str(e)}"]
        updates["analysis_status"] = AnalysisStatus.FAILED.value
        updates["step_statuses"]["report_generation"] = "failed"
        log.info(f"Report generation failed: {e}")

    return updates


def _reconstruct_skill_result(state: AnalysisState) -> SkillAnalysisResult:
    skill_name = state.get("current_skill_name", "unknown")

    impl_type_str = state.get("implementation_type", "UNKNOWN")
    impl_type = _safe_enum(ImplType, impl_type_str, ImplType.UNKNOWN)

    skill_info = SkillImplResult(
        skill_name=skill_name,
        implementation_type=impl_type,
        source_path=state.get("skills_repo_path", ""),
        business_goal=state.get("business_goal", ""),
        cli_evidence=state.get("cli_evidence", []),
        sdk_evidence=state.get("sdk_evidence", []),
    )

    openapi_usages = [_dict_to_openapi_usage(d) for d in state.get("openapi_usages", [])]
    api_business_roles = [_dict_to_api_business_role(d) for d in state.get("api_business_roles", []) if isinstance(d, dict)]

    sdk_result = SdkAnalysisResult(
        api_interfaces=[ApiInterface(**d) for d in state.get("api_interfaces", [])],
        openapi_usages=openapi_usages,
        api_business_roles=api_business_roles,
        sdk_calls=[SdkCallInfo(**d) for d in state.get("sdk_calls", [])],
        mapping_failures=state.get("mapping_failures", []),
        stage_1_result=_dict_to_report_stage_result(state.get("stage_1_result")),
    )

    coverage_map = {}
    for k, v in state.get("coverage_map", {}).items():
        try:
            matched_cli = v.get("matched_cli")
            coverage_map[k] = CliCoverageStatus(
                api_name=v.get("api_name", k),
                is_covered=v.get("is_covered", "not_covered"),
                matched_cli=matched_cli,
                uncertain_reason=v.get("uncertain_reason", ""),
            )
        except Exception:
            pass

    cli_result = CliCoverageResult(
        coverage_map=coverage_map,
        cli_gaps=[CliGap(**d) for d in state.get("cli_gaps", [])],
        alternatives=[AlternativeScheme(**d) for d in state.get("alternatives", [])],
        info_source=state.get("cli_info_source", ""),
    )

    correspondences = [_dict_to_koocli_correspondence(d) for d in state.get("koocli_correspondences", []) if isinstance(d, dict)]
    equivalence_results = [_dict_to_effect_equivalence_result(d) for d in state.get("effect_equivalence_results", []) if isinstance(d, dict)]

    correspondence_result = CliCorrespondenceResult(
        correspondences=correspondences,
        equivalence_results=equivalence_results,
        incomplete_apis=state.get("incomplete_correspondence_apis", []),
        info_source=state.get("cli_info_source", "none"),
        analysis_warnings=state.get("cli_analysis_errors", []),
        stage_2_result=_dict_to_report_stage_result(state.get("stage_2_result")),
    )

    replacement_feasibility = _dict_to_replacement_feasibility(state.get("replacement_feasibility_draft"))

    all_errors = state.get("all_errors", [])

    return SkillAnalysisResult(
        skill_info=skill_info,
        sdk_result=sdk_result,
        cli_result=cli_result,
        correspondence_result=correspondence_result,
        replacement_feasibility=replacement_feasibility,
        stage_1_result=_dict_to_report_stage_result(state.get("stage_1_result")),
        stage_2_result=_dict_to_report_stage_result(state.get("stage_2_result")),
        stage_3_result=_dict_to_report_stage_result(state.get("stage_3_result")),
        analysis_status=AnalysisStatus.PARTIAL if all_errors else AnalysisStatus.COMPLETED,
        errors=all_errors,
    )


def _determine_overall_status(state: AnalysisState) -> str:
    step_statuses = state.get("step_statuses", {})
    all_errors = state.get("all_errors", [])

    if any(s == "failed" for s in step_statuses.values()):
        return AnalysisStatus.PARTIAL.value
    if all_errors:
        return AnalysisStatus.PARTIAL.value
    return AnalysisStatus.COMPLETED.value


def _llm_summarize(state: AnalysisState, skill_result: SkillAnalysisResult) -> dict:
    from src.llm.llm_client import LlmClient
    from src.analyzers.llm_enhanced_analyzer import LlmEnhancedAnalyzer
    from src.models.llm_config import LlmConfig

    llm_config_dict = state.get("llm_config", {})
    llm_config = LlmConfig(**llm_config_dict) if llm_config_dict else LlmConfig()
    llm_client = LlmClient(llm_config)
    analyzer = LlmEnhancedAnalyzer(llm_client)

    result = analyzer.summarize_analysis(skill_result)
    if result:
        return asdict(result)
    return {}
