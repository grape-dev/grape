import sys
from dataclasses import asdict

from config import ConfigManager
from src.infra.logger import setup_logger, get_step_logger
from src.graph.analysis_graph import analyze_all_skills
from src.models.enums import AnalysisStatus
from src.models.report import AnalysisConfig


def main():
    config_mgr = ConfigManager()
    config, llm_config = config_mgr.parse_args()

    setup_logger(level=config.log_level)
    log = get_step_logger("main")

    log.info(f"Starting analysis with config: skills_repo={config.skills_repo_path}")
    log.info(f"Skill names: {config.skill_names if config.skill_names else 'all'}")
    log.info(f"Resume mode: {config.resume}")
    log.info(f"LLM enhanced: {'enabled' if llm_config.is_enabled else 'disabled'}")
    if llm_config.is_enabled:
        log.info(f"LLM provider: {llm_config.provider.value}, model: {llm_config.model_name}")

    config_dict = asdict(config)
    config_dict["llm_enabled"] = llm_config.is_enabled
    config_dict["llm_config"] = asdict(llm_config)
    config.llm_enabled = llm_config.is_enabled
    config.llm_config_dict = asdict(llm_config)

    try:
        results = analyze_all_skills(config)
    except Exception as e:
        log.info(f"Analysis failed: {e}")
        sys.exit(1)

    log.info(f"\n{'='*60}")
    log.info(f"Analysis Summary")
    log.info(f"{'='*60}")

    for result in results:
        name = result.skill_info.skill_name
        impl_type = result.skill_info.implementation_type.value
        status = result.analysis_status.value
        api_count = len(result.sdk_result.openapi_usages) if result.sdk_result else 0
        corr_count = len(result.correspondence_result.correspondences) if result.correspondence_result else 0
        conclusion = result.replacement_feasibility.conclusion.value if result.replacement_feasibility else "N/A"
        report_path = ""
        if hasattr(result, '_report_path'):
            report_path = result._report_path
        log.info(
            f"  {name}: impl={impl_type}, status={status}, "
            f"apis={api_count}, correspondences={corr_count}, "
            f"conclusion={conclusion}"
        )

    total = len(results)
    completed = sum(1 for r in results if r.analysis_status == AnalysisStatus.COMPLETED)
    partial = sum(1 for r in results if r.analysis_status == AnalysisStatus.PARTIAL)
    failed = sum(1 for r in results if r.analysis_status == AnalysisStatus.FAILED)

    log.info(f"\nTotal: {total} skills analyzed")
    log.info(f"  Completed: {completed}, Partial: {partial}, Failed: {failed}")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
