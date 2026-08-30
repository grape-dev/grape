from dataclasses import asdict

from src.analyzers.skill_tool_analyzer import SkillToolAnalyzer
from src.graph.state import AnalysisState
from src.infra.logger import get_step_logger


def tool_analysis_node(state: AnalysisState) -> dict:
    log = get_step_logger("tool_analysis")
    log.info("Starting skill tool analysis")

    updates = {
        "tool_dependencies": [],
        "step_statuses": {**state.get("step_statuses", {}), "tool_analysis": "running"},
    }

    try:
        cli_path = state.get("cli_tool_path", "")
        analyzer = SkillToolAnalyzer(cli_tool_path=cli_path)
        skill_path = state.get("skills_repo_path", "")
        deps = analyzer.analyze(skill_path)

        updates["tool_dependencies"] = [asdict(d) for d in deps]
        updates["step_statuses"]["tool_analysis"] = "completed"
        log.info(f"Tool analysis completed: {len(deps)} dependencies found")
    except Exception as e:
        updates["step_statuses"]["tool_analysis"] = "failed"
        log.info(f"Tool analysis failed: {e}")

    return updates