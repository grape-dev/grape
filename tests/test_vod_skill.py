"""VOD 技能 v2.0 测试（见 vod-skill-upgrade-plan.md VD-1~14）"""
import os, json
import pytest

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
VOD_SKILL = os.path.join(ROOT, "skills", "vod", "vod", "SKILL.md")
ELICIT_GUIDE = os.path.join(ROOT, "skills", "vod", "vod", "references", "requirement-elicit-guide.md")


def _skill():
    return open(VOD_SKILL, encoding="utf-8").read()


class TestVodSkill:
    def test_knowledge_baseline_present(self):
        assert "项目知识基线" in _skill()
        assert "必须最先" in _skill() or "最先" in _skill()

    def test_reuses_project_info(self):
        assert "project-info" in _skill() or "codegraph-project-analysis" in _skill()

    def test_dual_scenarios(self):
        s = _skill()
        assert "场景 A" in s and "场景 B" in s

    def test_history_overview(self):
        assert "历史全貌" in _skill() or "近期 Issue" in _skill()

    def test_guide_funnel(self):
        s = _skill()
        assert "需求引导漏斗" in s or "引导漏斗" in s
        assert "3 轮" in s

    def test_issue_review(self):
        s = _skill()
        assert "审视" in s
        assert "自建排除" in s or "auto-submitted" in s

    def test_a2a_handoff(self):
        s = _skill()
        assert "A2A" in s and "issue" in s
        assert "issue_number" in s

    def test_hard_constraints(self):
        s = _skill()
        assert "不建 PR" in s or "禁止" in s

    def test_elicit_guide(self):
        assert os.path.exists(ELICIT_GUIDE)
        g = open(ELICIT_GUIDE, encoding="utf-8").read()
        assert "清晰度四要素" in g or "四要素" in g
        assert "结构化 Issue 模板" in g or "## 目标" in g
        assert "A2A 交办" in g

    def test_skill_description_synced(self):
        cfg = json.load(open(os.path.join(ROOT, "configs", "skill-description.json"), encoding="utf-8"))
        assert "知识基线" in cfg["vod"]["description"] or "引导" in cfg["vod"]["description"]

    def test_agent_description_vod_subscribes_issue_opened(self):
        # issue_opened 由阶段矩阵恒给 VOD（§3.0.3 D8）；subscribe_events 已清空（§3.0.4）
        cfg = json.load(open(os.path.join(ROOT, "configs", "agent-description.json"), encoding="utf-8"))
        assert cfg["vod"].get("subscribe_events", []) == []
