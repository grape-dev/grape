"""Tests for the qa skill (PR 审查 + 工作流质量审查) and its config integration."""
import pytest
import os
import json
import sys

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))

QA_SKILL_DIR = os.path.join(ROOT, "skills", "qa", "qa")
QA_REFS_DIR = os.path.join(QA_SKILL_DIR, "references")
CONFIGS_DIR = os.path.join(ROOT, "configs")

REQUIRED_REFS = [
    "workflow-harness-review.md",
    "findings-format.md",
    "gitcode-api-reference.md",
    "code-review-and-quality.md",
    "skill-review-and-quality.md",
    "pr-config-check.md",
]

PR_TRIGGERS = ["pull_request_opened", "pull_request_updated", "push", "note_hook_pr"]

PR_SUBSCRIBED_EVENTS = ["pull_request_opened", "pull_request_updated", "note_hook_pr"]


def _read_skill_md() -> str:
    with open(os.path.join(QA_SKILL_DIR, "SKILL.md"), encoding="utf-8") as f:
        return f.read()


def _load_json(name: str) -> dict:
    with open(os.path.join(CONFIGS_DIR, name), encoding="utf-8") as f:
        return json.load(f)


class TestQaSkillFile:
    def test_skill_md_exists(self):
        assert os.path.isfile(os.path.join(QA_SKILL_DIR, "SKILL.md"))

    def test_front_matter_name(self):
        assert "name: qa" in _read_skill_md()

    def test_front_matter_description_present(self):
        content = _read_skill_md()
        assert "description:" in content
        assert content.split("---", 2)[1].strip() != ""

    def test_front_matter_no_triggers(self):
        # 路由由 Grape 阶段矩阵/静态映射决定（§3.0.4）；SKILL.md 不再声明 triggers
        content = _read_skill_md()
        assert "triggers:" not in content
        for t in PR_TRIGGERS:
            assert f"- event_type: {t}" not in content, f"triggers 已移除，不应含 {t}"

    def test_required_sections(self):
        content = _read_skill_md()
        for section in ["## 核心原则", "## 分析", "能力四：工作流审查", "## 输出规范", "## 硬规则", "## 参考"]:
            assert section in content, f"missing section {section}"

    def test_required_reference_files_exist(self):
        for ref in REQUIRED_REFS:
            assert os.path.isfile(os.path.join(QA_REFS_DIR, ref)), f"missing reference {ref}"


class TestQaSkillConfig:
    def test_skill_description_has_qa(self):
        cfg = _load_json("skill-description.json")
        assert "qa" in cfg
        assert cfg["qa"]["agent_type"] == "qa"
        assert cfg["qa"]["path"] == "qa/qa"

    def test_agent_skill_mapping_qa_defaults_to_pr(self):
        # 默认场景 qa 映射为 pr；使用专属 skill 的场景（iac→iac-qa, skill-dev→skill-tester）例外
        EXPECTED_QA = {"iac": "iac-qa", "skill-dev": "skill-tester", "api-skill-dev": "rest-api-skill-tester"}
        cfg = _load_json("agent-skill-mapping.json")
        for scene, info in cfg["scenes"].items():
            qa_skill = info["mapping"]["qa"]
            expected = EXPECTED_QA.get(scene, "pr")
            assert qa_skill == expected, f"scene '{scene}' qa should be '{expected}', got '{qa_skill}'"

    def test_agent_description_qa_subscribes_pr_with_stage_gate(self):
        # 生命周期矩阵（vod-skill-upgrade-plan.md §3.0.3）：PR 事件默认归属 qa（阶段矩阵兜底），
        # 不再通过 subscribe_events 订阅（§3.0.4 已清空订阅）
        cfg = _load_json("agent-description.json")
        assert cfg["qa"]["subscribe_events"] == []

    def test_agent_description_enable_defaults(self):
        # enable 默认：全部 true（sre 是否处理由仓库级订阅事件控制，非全局开关）
        cfg = _load_json("agent-description.json")
        for at in ("vod", "issue", "coding", "qa", "sre"):
            assert cfg[at].get("enable", True) is True, f"{at} enable should be true"

    def test_coding_agent_does_not_subscribe_pr(self):
        # coding 不订阅任何事件（通过 A2A 由 Issue/QA Agent 触发，不直接感知 PR 事件）
        cfg = _load_json("agent-description.json")
        assert cfg["coding"]["subscribe_events"] == []

    def test_all_subscribe_events_cleared(self):
        # 阶段矩阵 + 非 Issue 静态映射已接管路由（§3.0.4 彻底删除条件），subscribe_events 全部置空
        cfg = _load_json("agent-description.json")
        for at in ("vod", "issue", "coding", "qa", "sre"):
            assert cfg[at].get("subscribe_events", []) == [], f"{at} subscribe_events should be empty"

    def test_issue_opened_only_vod_subscribes(self):
        # D8：issue_opened 恒给 VOD（静态映射，非订阅）
        cfg = _load_json("agent-description.json")
        assert cfg["vod"]["subscribe_events"] == []
        assert cfg["issue"]["subscribe_events"] == []

    def test_issue_subscribes_issue_events_not_pr(self):
        # Issue 事件由阶段矩阵分发到 issue/vod；不订阅（subscribe_events 已清空）
        cfg = _load_json("agent-description.json")
        assert cfg["issue"]["subscribe_events"] == []

    def test_vod_subscribes_issue_events(self):
        # vod 处理 vod_signal + issue_opened + issue_updated/issue_comment（阶段矩阵/静态映射，非订阅）
        cfg = _load_json("agent-description.json")
        assert cfg["vod"]["subscribe_events"] == []

    def test_pr_events_only_qa_subscribed_stage_gated(self):
        # PR 事件默认归属 qa（阶段矩阵兜底）；subscribe_events 已清空，无 agent 静态订阅
        cfg = _load_json("agent-description.json")
        for evt in PR_SUBSCRIBED_EVENTS:
            subscribers = [a for a, info in cfg.items() if evt in info.get("subscribe_events", [])]
            assert subscribers == [], f"{evt} should NOT be subscribed by any agent (stage matrix only): {subscribers}"


class TestResolveIssueIdPR:
    """resolve_issue_id / resolve_pr_issue_id：PR 事件不得把 PR 号当 issue_id（见 event_pipeline.py）"""

    def _pr_payload(self, pr_number=114, issues=None):
        return {"object_attributes": {"iid": pr_number, "number": pr_number, "title": "feat: x", "source_branch": "feat/x"},
                "issues": issues or []}

    def test_pr_number_not_used_as_issue_id(self):
        from src.api.event_pipeline import resolve_issue_id
        p = self._pr_payload(pr_number=114)
        assert resolve_issue_id(p) == "", "PR 事件不应把 PR 号当 issue_id"

    def test_pr_linked_issues_extracted(self):
        from src.api.event_pipeline import resolve_issue_id, resolve_pr_issue_id
        p = self._pr_payload(pr_number=114, issues=[{"number": 23, "iid": 23}])
        assert resolve_issue_id(p) == "#23", resolve_issue_id(p)
        assert resolve_pr_issue_id(p) == "#23", resolve_pr_issue_id(p)

    def test_issue_event_still_extracts_issue(self):
        from src.api.event_pipeline import resolve_issue_id
        assert resolve_issue_id({"issue": {"number": 21, "iid": 21}}) == "#21"
        # Issue 类 webhook：object_attributes.iid = Issue 号（非 PR payload）
        assert resolve_issue_id({"object_attributes": {"iid": 20, "number": 20}}) == "#20"
        # A2A
        assert resolve_issue_id({"issue_number": 5}) == "#5"


class TestQaSkillPrompt:
    @pytest.fixture(scope="module")
    def pb(self):
        from src import prompt_builder
        return prompt_builder

    def test_build_prompt_loads_qa_skill(self, pb):
        evt = {
            "type": "pull_request_opened",
            "repository": "owner/repo",
            "source": "gitcode",
            "payload": {"pull_request": {"number": 1, "title": "t", "body": "b"}},
            "account": "owner/repo",
        }
        prompt = pb.build_prompt(evt, [], "", ["qa"])
        # 新版：skill 通过 SKILL.md 路径引用注入（agent 运行时读取），prompt 应包含 Skill 指引
        assert "## Skill: qa" in prompt
        assert "SKILL.md" in prompt
        assert "必须先执行" in prompt
