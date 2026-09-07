"""Tests for prompt_builder module."""
import pytest
import os
import sys
import json

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")


@pytest.fixture(scope="module")
def pb():
    """Import once for all tests in this module."""
    from src import prompt_builder
    return prompt_builder


class TestExtractEventNumber:
    def test_issue_number(self, pb):
        evt = {"issue": {"number": 42}}
        assert pb.extract_event_number(evt) == 42

    def test_merge_request_iid(self, pb):
        evt = {"merge_request": {"iid": 7}}
        assert pb.extract_event_number(evt) == 7

    def test_pull_request_number(self, pb):
        evt = {"pull_request": {"number": 13}}
        assert pb.extract_event_number(evt) == 13

    def test_object_attributes_iid(self, pb):
        evt = {"object_attributes": {"iid": 99, "noteable_iid": 88}}
        assert pb.extract_event_number(evt) == 88  # noteable_iid優先

    def test_noteable_iid_priority(self, pb):
        evt = {"object_attributes": {"noteable_iid": 1, "id": 999}}
        assert pb.extract_event_number(evt) == 1

    def test_fallback_number_in_root(self, pb):
        evt = {"number": 100}
        assert pb.extract_event_number(evt) == 100

    def test_no_number(self, pb):
        assert pb.extract_event_number({}) is None


class TestBuildRoleDefinition:
    def test_default_has_never_write_code(self, pb):
        role = pb.build_role_definition()
        assert "Never write code" in role

    def test_implementation_allows_code(self, pb):
        role = pb.build_role_definition(skills=["implementation"])
        assert "MAY write code" in role
        assert "Never write code" not in role

    def test_other_skill_blocks_code(self, pb):
        role = pb.build_role_definition(skills=["issue"])
        assert "Never write code" in role


class TestBuildChatRoleDefinition:
    def test_contains_capabilities(self, pb):
        role = pb.build_chat_role_definition()
        assert "DevOps" in role
        assert "Code review" in role  # domain list, not capabilities list


class TestBuildChatPrompt:
    def test_basic(self, pb):
        prompt = pb.build_chat_prompt("test query", "owner/repo", [])
        assert "test query" in prompt
        assert "owner/repo" in prompt
        # v1.6 移除 session_history 文件注入（引擎 --continue 承载历史）
        assert "session_history.json" not in prompt

    def test_with_history(self, pb):
        history = [
            {"type": "issue_opened", "success": True},
            {"type": "issue_comment", "success": False}
        ]
        prompt = pb.build_chat_prompt("hello", "owner/repo", history)
        # 设计文档 chat-prompt-overview.md：Chat 通用 prompt 不含「近期对话历史」段
        assert "近期对话历史" not in prompt
        assert "hello" in prompt

    def test_with_skills(self, pb):
        prompt = pb.build_chat_prompt("query", "owner/repo", [], ["chat"])
        assert "chat" in prompt

    def test_with_account(self, pb):
        prompt = pb.build_chat_prompt("hello", "owner/repo", [], account="customer-a")
        assert "hello" in prompt
        # v1.6 移除 session_history 文件注入
        assert "session_history.json" not in prompt


class TestHistoryInjection:
    """看护：对话历史按 has_valid_session 条件注入 + Issue Agent 进展始终注入
    （chat-prompt-history-injection-design.md §3 / accc2741）。"""

    HISTORY = [
        {"role": "user", "content": "怎么部署？"},
        {"role": "agent", "content": "[QA Agent] ✅ PR #1 已合并（merged_at 08-18），Issue #1 已自动关闭。"},
        {"role": "agent", "content": "让我看下 Issue 情况。"},
    ]

    def _general(self, has_valid_session):
        from src.prompt_builder import build_general_chat_prompt
        return build_general_chat_prompt("hi", "org/repo", self.HISTORY, skill_name="chat",
                                         has_valid_session=has_valid_session)

    def _task(self, has_valid_session):
        from src.prompt_builder import build_task_chat_prompt
        conv = {"conv_type": "task", "issue_number": 7, "title": "t", "repo_key": "org/repo"}
        return build_task_chat_prompt("hi", "org/repo", self.HISTORY, conv,
                                      has_valid_session=has_valid_session)

    def test_general_no_session_injects_history(self, pb):
        p = self._general(False)
        assert "## 对话历史" in p
        assert "怎么部署？" in p
        assert "## Issue Agent 进展" in p and "[QA Agent] ✅ PR #1" in p

    def test_general_with_session_keeps_progress_and_recent_history(self, pb):
        p = self._general(True)
        # has_session 时仍注入最近 3 条对话历史作为兜底（防止远端 worker session 丢失）
        assert "## 对话历史" in p
        assert "## Issue Agent 进展" in p and "[QA Agent] ✅ PR #1" in p   # 进展始终注入

    def test_task_no_session_injects_history(self, pb):
        p = self._task(False)
        assert "## 对话历史" in p and "怎么部署？" in p
        assert "## Issue Agent 进展" in p

    def test_task_with_session_keeps_progress_and_recent_history(self, pb):
        p = self._task(True)
        # has_session 时仍注入最近 3 条对话历史作为兜底
        assert "## 对话历史" in p
        assert "## Issue Agent 进展" in p and "[QA Agent] ✅ PR #1" in p

    def test_no_history_no_sections(self, pb):
        from src.prompt_builder import build_general_chat_prompt
        p = build_general_chat_prompt("hi", "org/repo", [], has_valid_session=False)
        assert "## 对话历史" not in p and "## Issue Agent 进展" not in p


class TestBuildPrompt:
    @pytest.fixture
    def base_evt(self):
        return {
            "type": "issue_opened",
            "repository": "owner/repo",
            "source": "gitcode",
            "payload": {
                "project": {"path_with_namespace": "owner/repo"},
                "issue": {"number": 1, "title": "Bug: test", "body": "desc"}
            }
        }

    def test_issue_opened_format(self, pb, base_evt):
        prompt = pb.build_prompt(base_evt, [], "", ["issue"])
        assert "Event: issue_opened #1 on owner/repo" in prompt
        assert "Bug: test" in prompt
        assert "Never write code" in prompt

    def test_issue_comment_format(self, pb):
        evt = {
            "type": "issue_comment",
            "repository": "owner/repo",
            "source": "gitcode",
            "payload": {
                "object_attributes": {
                    "noteable_iid": 5, "note": "hello @bot"
                }
            }
        }
        prompt = pb.build_prompt(evt, [], "", ["issue"])
        assert "User commented on issue #5" in prompt
        assert "hello @bot" in prompt

    def test_with_history(self, pb, base_evt):
        history = [{"type": "issue_opened", "success": True}]
        prompt = pb.build_prompt(base_evt, history, "", ["issue"])
        # v1.6 移除 session_history 文件注入（正文由事件 payload + A2A context 承载）
        assert "session_history.json" not in prompt

    def test_implementation_skill_role(self, pb, base_evt):
        prompt = pb.build_prompt(base_evt, [], "", ["implementation"])
        assert "MAY write code" in prompt

    def test_with_feishu_ctx(self, pb, base_evt):
        ctx = {
            "token": "test_token_12345",
            "chat_id": "oc_test_chat",
            "group_members": [
                {"name": "张三", "open_id": "ou_zhang"},
                {"name": "李四", "open_id": "ou_li"}
            ],
            "mappings": {"user1": {"open_id": "ou_1", "name": "张三"}},
            "collaborators": [
                {"username": "user1", "nick_name": "张三", "role_name_cn": "开发者"}
            ],
            "owner_login": "user1",
            "owner_nick": "张三",
            "auto_impl_enabled": True,
            "notify_enabled": True,
        }
        prompt = pb.build_prompt(base_evt, [], "", ["im-notify"], ctx)
        assert "Feishu tenant token: test_token_12345" in prompt
        assert "张三" in prompt
        assert "李四" in prompt
        assert "Auto Implementation" in prompt
        assert "ON" in prompt or "Status" in prompt


class TestTaskEndInstruction:
    """方案 B：prompt 含「任务结束」指令（见 vod-agent-hang-rootcause-fix.md 方案 B）。"""

    @pytest.fixture
    def base_evt(self):
        return {
            "type": "issue_opened",
            "repository": "owner/repo",
            "source": "gitcode",
            "payload": {
                "project": {"path_with_namespace": "owner/repo"},
                "issue": {"number": 1, "title": "Bug: test", "body": "desc"}
            }
        }

    def test_b1_prompt_has_end_instruction(self, pb, base_evt):
        prompt = pb.build_prompt(base_evt, [], "", ["issue"])
        assert "任务结束" in prompt
        assert "完成所有业务动作" in prompt
        assert "结束任务" in prompt

    def test_b2_json_contract_preserved(self, pb, base_evt):
        prompt = pb.build_prompt(base_evt, [], "", ["issue"])
        # 既有「处理记录输出要求」JSON 块仍存在，且在「任务结束」指令之前
        assert "处理记录输出要求" in prompt
        assert '"actions"' in prompt
        assert prompt.index("处理记录输出要求") < prompt.index("任务结束")


class TestThinkingCommentSeparation:
    """Issue #40：DevOps Agent 不得将思考过程/处理记录输出到评论。
    issue-40-agent-comment-leak-fix-design.md §四（Prompt 修复与强化）。"""

    @pytest.fixture
    def base_evt(self):
        return {
            "type": "issue_opened",
            "repository": "owner/repo",
            "source": "gitcode",
            "payload": {
                "project": {"path_with_namespace": "owner/repo"},
                "issue": {"number": 1, "title": "Bug: test", "body": "desc"}
            }
        }

    def test_no_reverse_instruction_left(self, pb, base_evt):
        """负向：反向指令（"说了 Let me…即判未完成"）必须被移除。"""
        prompt = pb.build_prompt(base_evt, [], "", ["issue"])
        assert '禁止"分析完就结束"' not in prompt
        assert "任务将被判定为未完成并重新执行" not in prompt

    def test_thinking_banned_from_output(self, pb, base_evt):
        """正向：思考过程禁止出现在对外输出。"""
        prompt = pb.build_prompt(base_evt, [], "", ["issue"])
        assert "思考过程禁止出现在对外输出" in prompt
        assert "绝不能出现在最终答复、报告或 GitCode 评论 body 中" in prompt

    def test_example_section_present(self, pb, base_evt):
        """正向：内部思考 vs 对外结论 正反例范本存在。"""
        prompt = pb.build_prompt(base_evt, [], "", ["issue"])
        assert "## 内部思考 vs 对外结论（必须分清）" in prompt
        assert '❌ "让我分析一下这个问题……"' in prompt
        assert '❌ "Now I have a thorough understanding……"' in prompt
        assert '✅ "完成：分类=bug，标签=bug/P1，已 A2A 交办 coding。"' in prompt

    def test_four_banned_items_in_comment(self, pb, base_evt):
        """正向：评论评论边界保留四类禁止项 + 发布前自检。"""
        prompt = pb.build_prompt(base_evt, [], "", ["issue"])
        assert "评论内容边界（必须遵守）" in prompt
        assert "处理记录 JSON 块（actions/conclusion/...）" in prompt
        assert "任何 HTML 注释标记" in prompt
        assert "发布前自检" in prompt
        assert "对外发布文案" in prompt

    def test_json_pure_structure_rule(self, pb, base_evt):
        """正向：处理记录 JSON 纯结构约束存在。"""
        prompt = pb.build_prompt(base_evt, [], "", ["issue"])
        assert "JSON 块必须保持纯 JSON 结构" in prompt

    def test_must_execute_action_kept(self, pb, base_evt):
        """正向：必执行工具动作纪律保留（未被误删）。"""
        prompt = pb.build_prompt(base_evt, [], "", ["issue"])
        assert "必须调用工具执行实际动作" in prompt

