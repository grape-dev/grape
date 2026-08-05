"""Tests for the qa skill (PR 审查 + 工作流质量审查) and its config integration."""
import pytest
import os
import json
import sys

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))

QA_SKILL_DIR = os.path.join(ROOT, "skills", "qa")
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

    def test_front_matter_triggers_match_pr(self):
        content = _read_skill_md()
        for t in PR_TRIGGERS:
            assert f"- event_type: {t}" in content, f"missing trigger {t}"

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
        assert cfg["qa"]["path"] == "qa"

    def test_agent_skill_mapping_qa_defaults_to_pr(self):
        # pr 保持默认：qa agent 默认技能为 pr（qa skill 为按仓库启用的增强备选）
        cfg = _load_json("agent-skill-mapping.json")
        for scene, info in cfg["scenes"].items():
            assert info["mapping"]["qa"] == "pr", f"scene '{scene}' qa mapping mismatch"

    def test_agent_description_qa_subscribes_pr_events(self):
        cfg = _load_json("agent-description.json")
        qa = cfg["qa"]
        for evt in PR_SUBSCRIBED_EVENTS:
            assert evt in qa["subscribe_events"], f"qa missing subscribe event {evt}"

    def test_coding_agent_not_subscribed_to_pr_events(self):
        cfg = _load_json("agent-description.json")
        for evt in PR_SUBSCRIBED_EVENTS:
            assert evt not in cfg["coding"]["subscribe_events"], f"coding still on {evt}"

    def test_no_routing_conflict_for_pr_events(self):
        # 每个 PR 事件只被一个 agent 订阅，避免 get_domain_agent 首匹配产生非确定性
        cfg = _load_json("agent-description.json")
        subscribed = {evt: [] for evt in PR_SUBSCRIBED_EVENTS}
        for agent, info in cfg.items():
            for evt in info.get("subscribe_events", []):
                if evt in subscribed:
                    subscribed[evt].append(agent)
        for evt, agents in subscribed.items():
            assert len(agents) == 1, f"{evt} subscribed by {agents}"


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
