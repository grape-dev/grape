"""Issue #451 回归测试：移除系统占位回帖「[AGENT Agent] 任务已完成（agent agent）。」

覆盖三条链路：
1. _post_comment_via_provider 不再硬编码 agent_type="agent"
2. _post_result_to_issue conclusion 提取失败时保留有效 result_text
3. 兜底文案不含 "agent agent" / "AGENT Agent"
4. Agent 已发评论时去重跳过
"""
import json
import os
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))


def _make_mock_urlopen(captured, existing_comments=None):
    """构建 mock urlopen：GET 返回 existing_comments，POST 捕获 body。"""
    _existing = json.dumps(existing_comments or []).encode()

    class _MockResp:
        def __init__(self, data=b'{"id":1}'):
            self._data = data

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def _mock(req, timeout=15):
        if req.data:
            captured["body"] = json.loads(req.data.decode())["body"]
            captured["url"] = req.full_url
            return _MockResp()
        return _MockResp(_existing)

    return _mock


# =====================================================================
# 1. _post_comment_via_provider 透传真实 agent_type
# =====================================================================

class TestPostCommentViaProviderAgentType:
    """_post_comment_via_provider 不再硬编码 "agent"，透传真实 agent_type。"""

    def test_passes_real_agent_type_to_post_result(self):
        """传入 agent_type="vod" 时，评论前缀为 [VOD Agent] 而非 [AGENT Agent]。"""
        from src.token_resolver import _post_comment_via_provider
        captured = {}

        class _FakeProvider:
            provider_name = "gitcode"

        with patch("src.repo_key.from_repo_key", return_value="test/repo"), \
             patch("src.api.event_pipeline._is_github_repo", return_value=False), \
             patch("urllib.request.urlopen", side_effect=_make_mock_urlopen(captured)):
            _post_comment_via_provider(
                _FakeProvider(), "gitcode:test/repo", "42",
                "任务已完成。", "fake_token", "vod"
            )
        assert "[VOD Agent]" in captured["body"], \
            f"应显示真实身份 [VOD Agent]，实际: {captured.get('body')}"
        assert "[AGENT Agent]" not in captured["body"], \
            "不应出现 [AGENT Agent] 占位前缀"

    def test_default_agent_type_still_works(self):
        """未传 agent_type 时默认 "agent"，不崩溃。"""
        from src.token_resolver import _post_comment_via_provider
        captured = {}

        class _FakeProvider:
            provider_name = "gitcode"

        with patch("src.repo_key.from_repo_key", return_value="test/repo"), \
             patch("src.api.event_pipeline._is_github_repo", return_value=False), \
             patch("urllib.request.urlopen", side_effect=_make_mock_urlopen(captured)):
            _post_comment_via_provider(
                _FakeProvider(), "gitcode:test/repo", "42",
                "done", "fake_token"
            )
        assert "[AGENT Agent]" in captured["body"]


# =====================================================================
# 2. _post_result_to_issue conclusion 保留 + 兜底简化
# =====================================================================

class TestPostResultToIssueConclusion:
    """_post_result_to_issue conclusion 提取逻辑。"""

    def test_structured_json_conclusion_extracted(self):
        """result_text 含结构化 JSON conclusion 时正常提取。"""
        from src.api.event_pipeline import _post_result_to_issue
        captured = {}
        result_text = json.dumps({
            "actions": ["fixed bug"],
            "conclusion": "已修复占位评论问题",
            "artifacts": ["PR #1"],
        })
        with patch("urllib.request.urlopen", side_effect=_make_mock_urlopen(captured)):
            _post_result_to_issue("test/repo", "5", result_text, "fake_token", "vod")
        assert "已修复占位评论问题" in captured["body"]
        assert "[VOD Agent]" in captured["body"]

    def test_plain_text_result_preserved(self):
        """result_text 为纯文本时保留为 conclusion，不以占位文案覆盖。"""
        from src.api.event_pipeline import _post_result_to_issue
        captured = {}
        result_text = "已修复 bug，测试通过。"
        with patch("urllib.request.urlopen", side_effect=_make_mock_urlopen(captured)):
            _post_result_to_issue("test/repo", "5", result_text, "fake_token", "coding")
        assert "已修复 bug，测试通过。" in captured["body"]
        assert "任务已完成。" not in captured["body"], \
            "有效 result_text 不应被占位文案覆盖"

    def test_empty_result_skips_comment(self):
        """Issue #679：result_text 为空时不发任何评论（无占位文案）。"""
        from src.api.event_pipeline import _post_result_to_issue
        captured = {}
        with patch("urllib.request.urlopen", side_effect=_make_mock_urlopen(captured)):
            _post_result_to_issue("test/repo", "5", "", "fake_token", "vod")
        assert "body" not in captured, \
            "result_text 为空时应跳过回帖，不应发出占位文案"

    def test_json_without_conclusion_skips_comment(self):
        """Issue #679：result_text 为 JSON 但无 conclusion 字段时不发评论（原文也不泄漏）。"""
        from src.api.event_pipeline import _post_result_to_issue
        captured = {}
        result_text = '{"foo": "bar"}'
        with patch("urllib.request.urlopen", side_effect=_make_mock_urlopen(captured)):
            _post_result_to_issue("test/repo", "5", result_text, "fake_token", "sre")
        assert "body" not in captured, \
            "无 conclusion 的 JSON 产出应跳过回帖，不应发出占位文案"

    def test_marker_stripped_from_conclusion(self):
        """result_text 含预附 marker 时，marker 不出现在 conclusion 中（避免双重 marker）。"""
        from src.api.event_pipeline import _post_result_to_issue
        captured = {}
        result_text = "修复完成\n<!-- grape-auto-comment -->"
        with patch("urllib.request.urlopen", side_effect=_make_mock_urlopen(captured)):
            _post_result_to_issue("test/repo", "5", result_text, "fake_token", "vod")
        # conclusion 应保留「修复完成」，body 末尾应有且仅有一个 marker
        assert "修复完成" in captured["body"]
        assert captured["body"].count("<!-- grape-auto-comment -->") == 1, \
            "body 中应仅有一个 grape-auto-comment marker"


# =====================================================================
# 3. 去重：Agent 已发评论时跳过系统回帖
# =====================================================================

class TestPostResultToIssueDedup:
    """_post_result_to_issue 方案 B 去重：Agent 评论已存在时跳过。"""

    def test_skip_when_agent_already_posted(self):
        """Issue 已有含 agent_type tag 的评论时，系统不补发。"""
        from src.api.event_pipeline import _post_result_to_issue
        captured = {}
        existing = [
            {"body": "[VOD Agent] 已完成需求分析，详见 spec。"},
        ]
        with patch("urllib.request.urlopen", side_effect=_make_mock_urlopen(captured, existing)):
            _post_result_to_issue("test/repo", "5", "some result", "fake_token", "vod")
        assert "body" not in captured, \
            "Agent 已发评论时系统不应补发（不应触发 POST）"

    def test_post_when_agent_did_not_post(self):
        """Issue 已有其它 Agent 评论但无目标 Agent 评论时，系统补发一条。"""
        from src.api.event_pipeline import _post_result_to_issue
        captured = {}
        existing = [
            {"body": "[issue agent] 已完成可开发性分析，详见 spec。"},
        ]
        with patch("urllib.request.urlopen", side_effect=_make_mock_urlopen(captured, existing)):
            _post_result_to_issue("test/repo", "5", "done", "fake_token", "vod")
        assert "body" in captured, "Agent 未发评论时系统应补发"
        assert "[VOD Agent]" in captured["body"]

    def test_skip_when_no_existing_comments(self):
        """Issue 无任何评论时，系统补发一条。"""
        from src.api.event_pipeline import _post_result_to_issue
        captured = {}
        with patch("urllib.request.urlopen", side_effect=_make_mock_urlopen(captured, [])):
            _post_result_to_issue("test/repo", "5", "done", "fake_token", "coding")
        assert "body" in captured
        assert "[CODING Agent]" in captured["body"]


# =====================================================================
# 4. complete_task 兜底文案链路（集成）
# =====================================================================
class TestCompleteTaskFallback:
    """complete_task 链路不产出占位文案。"""

    def test_fallback_text_no_agent_agent(self):
        """Issue #679：complete_task 不再拼接任何「任务已完成」兜底文案。"""
        import inspect
        from src.runtime.service import complete_task
        src = inspect.getsource(complete_task)
        # 兜底文案应使用简洁形式，不含「（{agent_type} agent）」
        assert "任务已完成（" not in src, \
            "complete_task 不应再使用「任务已完成（xxx agent）」占位文案"
        assert "任务已完成。" not in src, \
            "complete_task 不应再使用「任务已完成。」占位文案（Issue #679：空则跳过）"
        # 应传 agent_type 参数给 post_issue_comment
        assert "_agent_type" in src, "complete_task 应引用 _agent_type"

    def test_post_comment_via_provider_no_hardcoded_agent(self):
        """_post_comment_via_provider 源码不再含硬编码 "agent" 字面量。"""
        import inspect
        from src.token_resolver import _post_comment_via_provider
        src = inspect.getsource(_post_comment_via_provider)
        # 不应含 _post_result_to_issue(..., "agent", ...) 硬编码调用
        assert '"agent", repo_key' not in src, \
            "_post_comment_via_provider 不应硬编码 agent_type=\"agent\""
        assert "agent_type, repo_key" in src, \
            "_post_comment_via_provider 应透传 agent_type 参数"


# =====================================================================
# 5. Issue #679：三处发帖入口无内容时跳过发帖（不发占位文案）
# =====================================================================

class TestIssue679SkipEmptyPosting:
    """Issue #679：conclusion 与有效正文均缺失时跳过发帖。"""

    def test_pr_result_empty_skips_comment(self):
        """PR 订阅回帖：result_text 为空时不发评论。"""
        from src.api.event_pipeline import _post_result_to_pr
        captured = {}
        with patch("src.api.event_pipeline._is_github_repo", return_value=False), \
             patch("urllib.request.urlopen", side_effect=_make_mock_urlopen(captured)):
            _post_result_to_pr("test/repo", "33", "", "fake_token", "qa")
        assert "body" not in captured, \
            "PR 回帖：result_text 为空时应跳过，不应发出占位文案"

    def test_pr_result_json_without_conclusion_skips_comment(self):
        """PR 订阅回帖：纯 JSON 无 conclusion 时不发评论。"""
        from src.api.event_pipeline import _post_result_to_pr
        captured = {}
        with patch("src.api.event_pipeline._is_github_repo", return_value=False), \
             patch("urllib.request.urlopen", side_effect=_make_mock_urlopen(captured)):
            _post_result_to_pr("test/repo", "33", '{"foo": "bar"}', "fake_token", "qa")
        assert "body" not in captured, \
            "PR 回帖：无 conclusion 的 JSON 产出应跳过"

    def test_pr_result_plain_text_posts(self):
        """PR 订阅回帖：纯文本正文正常发布（行为不变）。"""
        from src.api.event_pipeline import _post_result_to_pr
        captured = {}
        with patch("src.api.event_pipeline._is_github_repo", return_value=False), \
             patch("urllib.request.urlopen", side_effect=_make_mock_urlopen(captured)):
            _post_result_to_pr("test/repo", "33", "conclusion: 审查通过", "fake_token", "qa")
        assert "body" in captured, "PR 回帖：纯文本正文应正常发布"
        assert "审查通过" in captured["body"]
        assert "任务已完成" not in captured["body"]

    def test_issue_result_json_conclusion_posts(self):
        """issue 回帖主路径：结构化 conclusion 正常发布（行为不变）。"""
        from src.api.event_pipeline import _post_result_to_issue
        captured = {}
        result_text = json.dumps({"conclusion": "分析完成，评级 ready"})
        with patch("urllib.request.urlopen", side_effect=_make_mock_urlopen(captured)):
            _post_result_to_issue("test/repo", "5", result_text, "fake_token", "vod")
        assert "分析完成，评级 ready" in captured["body"]
        assert "任务已完成" not in captured["body"]

    def test_service_handler_skips_empty_result_text(self):
        """service handler 发帖链：result_text 为空时不调用 post_issue_comment。"""
        import inspect
        from src.runtime import service as _svc
        src = inspect.getsource(_svc.complete_task)
        assert 'or "任务已完成。"' not in src, \
            "complete_task 不应再对 result_text 做「任务已完成。」兜底"
        assert "if _result_text:" in src, \
            "complete_task 应在 result_text 非空时才调用 post_issue_comment"
