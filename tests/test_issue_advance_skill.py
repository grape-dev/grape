"""issue-advance 技能评论处理校验测试（用户要求开发前置 ready 校验）"""
import os

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
SKILL = os.path.join(ROOT, "skills", "issue-advance", "SKILL.md")


def _skill():
    return open(SKILL, encoding="utf-8").read()


class TestIssueAdvanceDevGate:
    def test_user_request_dev_requires_ready_check(self):
        s = _skill()
        assert "用户要求启动开发/实现功能" in s
        assert "先确认该 Issue 是否已深度可开发性分析" in s or "前置校验" in s

    def test_not_ready_blocks_a2a(self):
        s = _skill()
        # 未 ready 不直接 A2A 交办 coding
        assert "未 ready" in s or "按评级处理后再交办" in s or "不交办" in s

    def test_hard_constraint_dev_gate(self):
        s = _skill()
        assert "用户要求开发/实现前，先确认该 Issue 已深度可开发性分析且 ready" in s

    def test_analysis_reference_present(self):
        s = _skill()
        assert "issue-developability-analysis" in s

    def test_a2a_agents_are_real_types(self):
        s = _skill()
        # 支援表用真实 agent_type（coding/qa/sre），不用旧技能名（implementation/pr/cloud-deploy/build-debug）
        support_section = s.split("### 超出能力范围时主动请求支援")[1].split("### 验证外部 claim")[0]
        assert "| coding |" in support_section
        assert "| qa |" in support_section
        assert "| sre |" in support_section
        for old in ("implementation", "cloud-deploy", "build-debug", "| pr |"):
            assert old not in support_section, f"旧技能名不应出现在支援表: {old}"

    def test_vod_handoff_for_unclear_requirement(self):
        s = _skill()
        # 需求不清晰时 A2A 求助 vod
        assert "**vod**" in s
        assert "需求理解不清晰" in s or "需求不清晰" in s
        assert "vod" in s.split("超出能力范围")[1]
