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
        role = pb.build_role_definition(skills=["implementation"], agent_type="coding")
        assert "MAY write code" in role
        assert "Never write code" not in role

    def test_other_skill_blocks_code(self, pb):
        role = pb.build_role_definition(skills=["issue"])
        assert "Never write code" in role

    def test_security_rules_merged_continuous_numbering(self, pb):
        """issue-219 方向 C：SECURITY 与规则合并为单一连续编号 1-7（无粘连/无重复编号）。"""
        role = pb.build_role_definition(agent_type="issue")
        assert "## SECURITY (MUST FOLLOW):" in role
        assert "data.3." not in role  # 缺逗号隐式拼接 bug 已修复
        for n in range(1, 8):
            assert f"{n}." in role
        assert role.count("3.") == 1
        assert role.count("4.") == 1


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
        from src.prompt_builder import build_issue_chat_prompt
        conv = {"conv_type": "task", "issue_number": 7, "title": "t", "repo_key": "org/repo"}
        return build_issue_chat_prompt("hi", "org/repo", self.HISTORY, conv,
                                       has_valid_session=has_valid_session)

    def test_general_no_session_injects_history(self, pb):
        p = self._general(False)
        assert "## Chat History" in p
        assert "怎么部署？" in p
        assert "## Issue Agent Progress" in p and "[QA Agent] ✅ PR #1" in p

    def test_general_with_session_keeps_progress_and_recent_history(self, pb):
        p = self._general(True)
        # has_session 时仍注入最近 3 条对话历史作为兜底（防止远端 worker session 丢失）
        assert "## Chat History" in p
        assert "## Issue Agent Progress" in p and "[QA Agent] ✅ PR #1" in p   # 进展始终注入

    def test_task_no_session_injects_history(self, pb):
        p = self._task(False)
        assert "## Chat History" in p and "怎么部署？" in p
        assert "## Issue Agent Progress" in p

    def test_task_with_session_keeps_progress_and_recent_history(self, pb):
        p = self._task(True)
        # has_session 时仍注入最近 3 条对话历史作为兜底
        assert "## Chat History" in p
        assert "## Issue Agent Progress" in p and "[QA Agent] ✅ PR #1" in p

    def test_no_history_no_sections(self, pb):
        from src.prompt_builder import build_general_chat_prompt
        p = build_general_chat_prompt("hi", "org/repo", [], has_valid_session=False)
        assert "## Chat History" not in p and "## Issue Agent Progress" not in p


class TestBuildPrompt:
    @pytest.fixture
    def base_evt(self):
        return {
            "type": "issue_opened",
            "repository": "owner/repo",
            "source": "gitcode",
            "repo_key": "gitcode:owner/repo",
            "issue_id": "1",
            "payload": {
                "project": {"path_with_namespace": "owner/repo"},
                "issue": {"number": 1, "title": "Bug: test", "body": "desc"}
            }
        }

    def test_issue_opened_format(self, pb, base_evt):
        prompt = pb.build_devops_agent_prompt(base_evt, [], "", ["issue"])
        assert "Event: issue_opened #1 on owner/repo" in prompt
        assert "Bug: test" in prompt
        assert "Never write code" in prompt

    def test_issue_comment_format(self, pb):
        evt = {
            "type": "issue_comment",
            "repository": "owner/repo",
            "source": "gitcode",
            "repo_key": "gitcode:owner/repo",
            "issue_id": "5",
            "payload": {
                "object_attributes": {
                    "noteable_iid": 5, "note": "hello @bot"
                }
            }
        }
        prompt = pb.build_devops_agent_prompt(evt, [], "", ["issue"])
        assert "User commented on issue #5" in prompt
        assert "hello @bot" in prompt
        assert "Issue comment format (MUST FOLLOW)" in prompt

    def test_with_history(self, pb, base_evt):
        history = [{"type": "issue_opened", "success": True}]
        prompt = pb.build_devops_agent_prompt(base_evt, history, "", ["issue"])
        # v1.6 移除 session_history 文件注入（正文由事件 payload + A2A context 承载）
        assert "session_history.json" not in prompt

    def test_implementation_skill_role(self, pb, base_evt):
        prompt = pb.build_devops_agent_prompt(base_evt, [], "", ["implementation"], agent_type="coding")
        assert "MAY write code" in prompt

    def test_role_definition_heading_and_blank_line(self, pb, base_evt):
        """issue-219 方向 B：角色段加 `## Role Definition` 标题。"""
        prompt = pb.build_devops_agent_prompt(base_evt, [], "", ["issue"])
        assert "\n\n## Role Definition\n" in prompt
        assert prompt.index("## Role Definition") < prompt.index("You are a DevOps")

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
        prompt = pb.build_devops_agent_prompt(base_evt, [], "", ["im-notify"], ctx)
        assert "Feishu tenant token: test_token_12345" in prompt
        assert "张三" in prompt
        assert "李四" in prompt
        assert "Auto Implementation" in prompt
        assert "ON" in prompt or "Status" in prompt


class TestTaskEndInstruction:
    """方案 B：prompt 含「任务完成」指令（issue-219 方向 D：Task Completion 段，英文）。"""

    @pytest.fixture
    def base_evt(self):
        return {
            "type": "issue_opened",
            "repository": "owner/repo",
            "source": "gitcode",
            "repo_key": "gitcode:owner/repo",
            "issue_id": "1",
            "payload": {
                "project": {"path_with_namespace": "owner/repo"},
                "issue": {"number": 1, "title": "Bug: test", "body": "desc"}
            }
        }

    def test_b1_prompt_has_end_instruction(self, pb, base_evt):
        prompt = pb.build_devops_agent_prompt(base_evt, [], "", ["issue"])
        assert "## Task Completion" in prompt
        assert "finishes the task" in prompt
        assert "grape tasks complete" in prompt

    def test_b2_json_contract_preserved(self, pb, base_evt):
        prompt = pb.build_devops_agent_prompt(base_evt, [], "", ["issue"])
        # JSON 合约仍存在，且在「Task Completion」段内（早于 Comment Reply）
        assert '"actions"' in prompt
        assert '"issue_summary"' in prompt
        assert prompt.index("## Task Completion") < prompt.index("## Comment Reply")


class TestThinkingCommentSeparation:
    """Issue #40：DevOps Agent 不得将思考过程/处理记录输出到评论。
    issue-40-agent-comment-leak-fix-design.md §四（Prompt 修复与强化）。"""

    @pytest.fixture
    def base_evt(self):
        return {
            "type": "issue_opened",
            "repository": "owner/repo",
            "source": "gitcode",
            "repo_key": "gitcode:owner/repo",
            "issue_id": "1",
            "payload": {
                "project": {"path_with_namespace": "owner/repo"},
                "issue": {"number": 1, "title": "Bug: test", "body": "desc"}
            }
        }

    def test_no_reverse_instruction_left(self, pb, base_evt):
        """负向：反向指令（"说了 Let me…即判未完成"）必须被移除。"""
        prompt = pb.build_devops_agent_prompt(base_evt, [], "", ["issue"])
        assert '禁止"分析完就结束"' not in prompt
        assert "任务将被判定为未完成并重新执行" not in prompt

    def test_thinking_banned_from_output(self, pb, base_evt):
        """正向：思考过程禁止出现在对外输出。"""
        prompt = pb.build_devops_agent_prompt(base_evt, [], "", ["issue"])
        assert "Thinking must never appear in external output" in prompt
        assert "never in the final answer, a report, or the GitCode comment body" in prompt

    def test_example_section_present(self, pb, base_evt):
        """正向：内部思考 vs 对外结论 正反例范本存在（英文，方向 E）。"""
        prompt = pb.build_devops_agent_prompt(base_evt, [], "", ["issue"])
        assert "## Internal Thinking vs External Conclusion (MUST DISTINGUISH)" in prompt
        assert '❌ "Let me analyze this problem..."' in prompt
        assert '❌ "Now I have a thorough understanding..."' in prompt
        assert '✅ "Done: classification=bug, label=bug/P1, A2A handoff to coding agent."' in prompt

    def test_four_banned_items_in_comment(self, pb, base_evt):
        """正向：评论评论边界保留四类禁止项 + 发布前自检。"""
        prompt = pb.build_devops_agent_prompt(base_evt, [], "", ["issue"])
        assert "Comment content boundary (MUST FOLLOW)" in prompt
        assert "processing-record JSON block (actions/conclusion/...)" in prompt
        assert "HTML comment markers" in prompt
        assert "Pre-publish self-check" in prompt
        assert "read the comment body aloud as a public post" in prompt

    def test_json_pure_structure_rule(self, pb, base_evt):
        """正向：处理记录 JSON 纯结构约束存在。"""
        prompt = pb.build_devops_agent_prompt(base_evt, [], "", ["issue"])
        assert "pure JSON, not mixed with prose" in prompt

    def test_must_execute_action_kept(self, pb, base_evt):
        """正向：必执行动作纪律保留（Skill 段 Execute directly）。"""
        prompt = pb.build_devops_agent_prompt(base_evt, [], "", ["issue"])
        assert "Execute the instructions directly" in prompt
        assert "do not analyze or propose" in prompt


class TestNoCurlTemplates:
    """Issue #219：所有 Agent prompt 移除直接 API 调用（curl 模板），保持中性，
    不含 API/CLI 引导；仍保留认证 token 环境变量名与仓库标识。

    设计文档：docs/weekly-04/issue-219-devops-agent-prompt-optimization-design.md
    """

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

    def test_devops_prompt_no_curl(self, pb, base_evt):
        prompt = pb.build_devops_agent_prompt(base_evt, [], "", ["issue"], agent_type="issue")
        assert "curl" not in prompt

    def test_devops_prompt_no_hardcoded_api_usage(self, pb, base_evt):
        """prompt 不包含硬编码的 API / CLI 调用模板（curl、a2a handoff 命令体）。
        收尾仅以「技能收尾流程」条件化提及，见 TestConditionalClosingFlow。"""
        prompt = pb.build_devops_agent_prompt(base_evt, [], "", ["issue"], agent_type="issue")
        assert "curl" not in prompt
        assert "a2a handoff" not in prompt
        assert "grape a2a handoff" not in prompt

    def test_general_chat_prompt_no_curl(self, pb):
        prompt = pb.build_general_chat_prompt("test query", "owner/repo", [])
        assert "curl" not in prompt

    def test_task_chat_prompt_no_curl(self, pb):
        conv = {"conv_type": "task", "issue_number": 7, "title": "t", "repo_key": "org/repo"}
        prompt = pb.build_issue_chat_prompt("hi", "org/repo", [], conv)
        assert "curl" not in prompt

    def test_devops_prompt_guides_grape_cli_not_token_env(self, pb, base_evt):
        """P2：prompt 引导用 grape CLI + GRAPE_SESSION_TOKEN，不再指示平台 token env。"""
        prompt = pb.build_devops_agent_prompt(base_evt, [], "", ["issue"], agent_type="issue")
        assert "BOT_GITCODE_TOKEN" not in prompt
        assert "USER_GITCODE_TOKEN" not in prompt
        assert "grape" in prompt and "GRAPE_SESSION_TOKEN" in prompt

    def test_devops_prompt_keeps_repo_identifier(self, pb, base_evt):
        prompt = pb.build_devops_agent_prompt(base_evt, [], "", ["issue"], agent_type="issue")
        assert "owner/repo" in prompt

    def test_devops_prompt_no_a2a_section(self, pb, base_evt):
        """Option B：A2A 协议章节整体移除，由技能 SKILL.md 承载。"""
        prompt = pb.build_devops_agent_prompt(base_evt, [], "", ["issue"], agent_type="issue")
        assert "Agent-to-Agent 协议" not in prompt
        assert "字段规则" not in prompt
        assert "### 认证说明" not in prompt

    def test_devops_prompt_no_project_baseline_or_shared_knowledge(self, pb, base_evt):
        """Option B：项目知识基线与共享知识从 prompt 移除，由技能通过 grape-cli/API 获取。"""
        prompt = pb.build_devops_agent_prompt(base_evt, [], "", ["issue"], agent_type="issue")
        assert "项目知识基线" not in prompt
        assert "{{PROJECT_INFO_DIR}}" not in prompt
        assert "共享知识" not in prompt
        assert "{{KNOWLEDGE_DIR}}" not in prompt

    def test_devops_prompt_no_vcs_api_access_section(self, pb, base_evt):
        """Option B：VCS API Access 章节移除，由技能 SKILL.md 承载。"""
        prompt = pb.build_devops_agent_prompt(base_evt, [], "", ["issue"], agent_type="issue")
        assert "API Access" not in prompt
        assert "API base URL" not in prompt


class TestConditionalClosingFlow:
    """Issue #219 + 方案 C：收尾二选一——技能提供 `grape tasks complete` 结构化收尾则
    调完即结束、不再输出 JSON 块；否则按 JSON 块契约输出（docs/weekly-04/issue-219-...md 方向 D）。"""

    @pytest.fixture
    def base_evt(self):
        return {
            "type": "issue_opened",
            "repository": "owner/repo",
            "source": "gitcode",
            "repo_key": "gitcode:owner/repo",
            "issue_id": "1",
            "payload": {
                "project": {"path_with_namespace": "owner/repo"},
                "issue": {"number": 1, "title": "Bug: test", "body": "desc"}
            }
        }

    def _prompt(self, pb, base_evt):
        return pb.build_devops_agent_prompt(base_evt, [], "", ["issue"], agent_type="issue")

    def test_contract_mentions_tasks_complete_branch(self, pb, base_evt):
        p = self._prompt(pb, base_evt)
        assert "follow the closing flow of your current skill (SKILL.md)" in p
        assert "grape tasks complete" in p
        assert "Do NOT repeat the JSON block below" in p

    def test_contract_mentions_json_fallback_branch(self, pb, base_evt):
        p = self._prompt(pb, base_evt)
        assert "Case 2: The skill does not provide" in p
        assert '"actions"' in p and '"issue_summary"' in p

    def test_two_paths_mutually_exclusive(self, pb, base_evt):
        """硬约束：两路二选一，不得同时输出 JSON 块并调 tasks complete。"""
        p = self._prompt(pb, base_evt)
        assert "mutually exclusive" in p
        assert "never output the JSON block AND call" in p

    def test_task_end_section_aligned(self, pb, base_evt):
        """「任务完成」段承载收尾说明，位于「评论答复」之前（方向 D）。"""
        p = self._prompt(pb, base_evt)
        assert "## Task Completion" in p
        assert "finishes the task" in p
        assert p.index("## Task Completion") < p.index("## Comment Reply")


class TestIssueSummaryExcludesCurrent:
    """issue-219 方向 A：当前 Issue 不应出现在 Issue Pipeline 摘要中。"""

    REPO = "gitcode:org/issue219-excl"

    def _seed(self):
        from src.session_manager import db, db_lock
        rows = [
            (1, "1", "issue_one", "r1"),
            (2, "2", "issue_two", "r2"),
            (3, "3", "issue_three", "r3"),
        ]
        with db_lock:
            for i, iid, title, res in rows:
                db.cursor().execute(
                    "INSERT INTO events (seq, repo_key, issue_id, event_type, agent_type, title, status, result, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (i, self.REPO, iid, "issue_opened", "vod", title, "completed", res, f"2026-09-22 0{i}:00:00")
                )
            db.commit()

    def _clean(self):
        from src.session_manager import db, db_lock
        with db_lock:
            db.cursor().execute("DELETE FROM events WHERE repo_key=?", (self.REPO,))
            db.commit()

    def test_excludes_current_issue(self):
        from src.api.event_store import get_issue_summary
        from src.prompt_builder import build_devops_agent_prompt
        self._seed()
        try:
            s = get_issue_summary(self.REPO, exclude_issue="3")
            assert "issue_three" not in s
            assert "issue_one" in s and "issue_two" in s
            evt = {
                "type": "issue_opened", "source": "gitcode", "repository": "org/issue219-excl",
                "repo_key": self.REPO, "issue_id": "3",
                "payload": {"issue": {"number": 3, "title": "issue_three", "body": "b"}},
            }
            p = build_devops_agent_prompt(evt, [], "", ["issue"])
            # 当前 Issue(#3) 不出现在 Issue Pipeline；其它 Issue 仍在
            assert "  3 issue_three" not in p
            assert "  1 issue_one" in p
        finally:
            self._clean()

    def test_no_exclude_includes_all(self):
        from src.api.event_store import get_issue_summary
        self._seed()
        try:
            s = get_issue_summary(self.REPO)
            assert "issue_one" in s and "issue_three" in s
        finally:
            self._clean()

