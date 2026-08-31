import os
from dataclasses import asdict

from src.graph.state import AnalysisState
from src.infra.logger import get_step_logger
from src.models.enums import ImplType
from src.models.llm_config import LlmConfig
from src.analyzers.impl_type_detector import ImplTypeDetector


def impl_type_detection_node(state: AnalysisState) -> dict:
    log = get_step_logger("impl_type_detection")
    log.info("Starting implementation type detection")

    updates = {
        "impl_detection_errors": [],
        "step_statuses": {**state.get("step_statuses", {}), "impl_type_detection": "running"},
    }

    try:
        detector = ImplTypeDetector()
        skill_path = state.get("skills_repo_path", "")
        result = detector.detect(skill_path)

        updates["implementation_type"] = result.implementation_type.value
        updates["business_goal"] = result.business_goal
        updates["cli_evidence"] = result.cli_evidence
        updates["sdk_evidence"] = result.sdk_evidence
        updates["step_statuses"]["impl_type_detection"] = "completed"
        log.info(f"Detected implementation type: {result.implementation_type.value}")
    except Exception as e:
        updates["implementation_type"] = ImplType.UNKNOWN.value
        updates["business_goal"] = ""
        updates["cli_evidence"] = []
        updates["sdk_evidence"] = []
        updates["impl_detection_errors"] = [f"Implementation type detection failed: {str(e)}"]
        updates["step_statuses"]["impl_type_detection"] = "failed"
        log.info(f"Implementation type detection failed: {e}")

    if state.get("llm_enabled", False) and updates.get("implementation_type") == ImplType.UNKNOWN.value:
        try:
            llm_result = _llm_enhance_business_logic(state, updates)
            if llm_result:
                existing = state.get("llm_analysis_results", [])
                updates["llm_analysis_results"] = existing + [asdict(llm_result)]
                log.info("LLM enhanced business logic analysis completed")
        except Exception as e:
            log.info(f"LLM enhanced analysis failed (degraded): {e}")

    return updates


def _llm_enhance_business_logic(state: AnalysisState, updates: dict):
    from src.llm.llm_client import LlmClient
    from src.analyzers.llm_enhanced_analyzer import LlmEnhancedAnalyzer
    from src.models.skill import SkillImplResult

    llm_config_dict = state.get("llm_config", {})
    llm_config = LlmConfig(**llm_config_dict) if llm_config_dict else LlmConfig()
    llm_client = LlmClient(llm_config)

    skill_info = SkillImplResult(
        skill_name=state.get("current_skill_name", ""),
        implementation_type=ImplType(updates.get("implementation_type", "UNKNOWN")),
        source_path=state.get("skills_repo_path", ""),
        business_goal=updates.get("business_goal", ""),
    )

    analyzer = LlmEnhancedAnalyzer(llm_client)
    return analyzer.analyze_business_logic(skill_info, [])
