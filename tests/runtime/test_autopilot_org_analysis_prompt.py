"""组织分析任务的 prompt 识别改为 skill_name（不再依赖 GRAPE_MANAGED_BY）。"""

from src.domains.runtime.engine import scheduler


def _task(**overrides):
    task = {
        "name": "组织仓库 AI 分析 - demo",
        "instruction": "分析组织仓库",
        "skill_name": "",
        "custom_fields": "",
        "work_branch": "",
    }
    task.update(overrides)
    return task


def test_org_analysis_output_requirements_keyed_by_skill_name():
    prompt = scheduler._build_autopilot_prompt(
        _task(skill_name="org-repo-analyzer"), "gitcode:demo/org", "demo/org")
    assert "## 输出要求" in prompt
    assert "不要创建 Issue/PR" in prompt


def test_normal_autopilot_task_has_no_org_analysis_output_requirements():
    prompt = scheduler._build_autopilot_prompt(
        _task(skill_name="code-quality"), "gitcode:demo/repo", "demo/repo")
    assert "## 输出要求" not in prompt