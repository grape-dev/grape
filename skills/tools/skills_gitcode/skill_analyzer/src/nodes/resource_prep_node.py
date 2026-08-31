from pathlib import Path

from src.graph.state import AnalysisState
from src.infra.logger import get_step_logger
from src.resources.cli_tool_manager import CliToolManager
from src.resources.sdk_source_manager import SdkSourceManager


def _resolve_skill_path(repo_path: str, skill_name: str) -> str:
    repo = Path(repo_path)
    if (repo / "SKILL.md").exists() or (repo / "skill.yaml").exists():
        return str(repo)
    direct = repo / skill_name
    if direct.is_dir():
        return str(direct)
    for match in repo.rglob(skill_name):
        if match.is_dir():
            return str(match)
    return str(direct)


def resource_preparation_node(state: AnalysisState) -> dict:
    log = get_step_logger("resource_preparation")
    log.info("Starting resource preparation")

    updates = {
        "resource_errors": [],
        "step_statuses": {**state.get("step_statuses", {}), "resource_preparation": "running"},
    }

    config = state.get("config", {})
    skip_cli_install = config.get("skip_cli_install", False)

    repo_path = state.get("skills_repo_path", "")
    skill_name = state.get("current_skill_name", "")
    if repo_path and skill_name:
        resolved = _resolve_skill_path(repo_path, skill_name)
        if resolved != repo_path:
            updates["skills_repo_path"] = resolved
            log.info(f"Resolved skill path: {resolved}")

    try:
        sdk_manager = SdkSourceManager(
            cache_dir=config.get("sdk_cache_dir", "./cache/sdk-source"),
            repo_url=config.get("sdk_repo_url", SdkSourceManager.DEFAULT_REPO_URL),
        )
        sdk_path = sdk_manager.ensure_available()
        updates["sdk_source_available"] = True
        updates["sdk_source_path"] = sdk_path
        log.info("SDK source ready")
    except Exception as e:
        updates["sdk_source_available"] = False
        updates["sdk_source_path"] = ""
        updates["resource_errors"] = updates["resource_errors"] + [f"SDK source preparation failed: {str(e)}"]
        log.info(f"SDK source preparation failed: {e}")

    try:
        cli_manager = CliToolManager(
            install_dir=config.get("cli_install_dir", "./cache/koo-cli"),
        )
        cli_path = cli_manager.ensure_available(skip_install=skip_cli_install)
        updates["cli_tool_available"] = True
        updates["cli_tool_path"] = cli_path
        log.info("CLI tool ready")
    except Exception as e:
        updates["cli_tool_available"] = False
        updates["cli_tool_path"] = ""
        updates["resource_errors"] = updates["resource_errors"] + [f"CLI tool preparation failed: {str(e)}"]
        log.info(f"CLI tool preparation failed: {e}")

    status = "completed" if not updates["resource_errors"] else "partial"
    updates["step_statuses"]["resource_preparation"] = status
    log.info(f"Resource preparation finished: {status}")

    return updates