"""issue-advance 技能统一处理流程校验测试（知识基线 + 深度分析 + 意图处理）"""
import os

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
SKILL = os.path.join(ROOT, "skills", "issue", "issue-advance", "SKILL.md")


def _skill():
    return open(SKILL, encoding="utf-8").read()


class TestIssueAdvanceUnifiedFlow:
    def test_user_request_dev_requires_ready_check(self):
        s = _skill()
        assert "用户要求启动开发/实现功能" in s
        assert "先确认评级" in s or "先确认该 Issue 是否已深度可开发性分析" in s

    def test_not_ready_blocks_a2a(self):
        s = _skill()
        assert "未 ready" in s or "按评级处理" in s or "不交办" in s

    def test_hard_constraint_dev_gate(self):
        s = _skill()
        assert "用户要求开发/实现前，先确认该 Issue 已深度可开发性分析且 ready" in s

    def test_analysis_reference_present(self):
        s = _skill()
        assert "issue-developability-analysis" in s

    def test_unified_flow_not_split(self):
        s = _skill()
        # 合并后：不再有独立"评论处理流程"，统一为 Issue 处理流程
        assert "统一流程" in s or "Issue 处理流程" in s
        assert "评论处理流程" not in s

    def test_comment_also_gets_knowledge_baseline(self):
        s = _skill()
        # 评论处理同样需要知识基线（用户建议）
        assert "任何事件都需" in s or "评论答复也要基于项目理解" in s or "不可跳过" in s

    def test_comment_also_gets_deep_analysis(self):
        s = _skill()
        # 评论事件同样深度可开发性分析
        assert "评论事件同样分析" in s or "每次处理都执行" in s or "每次处理都执行" in s

    def test_a2a_agents_are_real_types(self):
        s = _skill()
        support_section = s.split("### 超出能力范围时主动请求支援")[1].split("### 验证外部 claim")[0]
        assert "| coding |" in support_section
        assert "| qa |" in support_section
        assert "| sre |" in support_section
        for old in ("implementation", "cloud-deploy", "build-debug", "| pr |"):
            assert old not in support_section, f"旧技能名不应出现在支援表: {old}"

    def test_vod_handoff_for_unclear_requirement(self):
        s = _skill()
        assert "**vod**" in s
        assert "需求理解不清晰" in s or "需求不清晰" in s
        assert "vod" in s.split("超出能力范围")[1]

    def test_triggers_removed(self):
        # 路由由 Grape 阶段矩阵/静态映射决定（§3.0.4）；issue-advance SKILL.md 不再声明 triggers
        assert "triggers:" not in _skill()

    def test_experience_verification_routes_to_sre(self):
        """体验验证/运行时验证类任务应路由 sre（coding-worktree-skill-collision.md §4.5.4/4.5.5③）。
        防止体验类需求默认交 coding 导致前台起服务超时（#711）。"""
        s = _skill()
        support_section = s.split("### 超出能力范围时主动请求支援")[1].split("### 验证外部 claim")[0]
        assert "体验验证" in support_section or "运行时验证" in support_section, \
            "A2A 映射表应含「体验验证/运行时验证」行"
        assert "sre" in support_section.split("体验验证")[1] if "体验验证" in support_section else "sre" in support_section, \
            "体验验证应指向 sre"
