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
