"""Issue #528: PR 订阅场景 ISSUE_NUMBER 注入非数字值

PR 订阅（PR 无关联 Issue）触发任务时，事件 issue_id 为伪 id（#pr_{PR号}_{scene_id}），
服务端曾将其原样注入 ISSUE_NUMBER，下游 A2A/CLI 要求 issue_number 为数字全部被拒。

验收：
1. 无关联 Issue 的 PR 事件 → env 不注入非数字 ISSUE_NUMBER / GRAPE_ISSUE_ID，
   PR_NUMBER 注入纯数字 PR 号（payload.pr_number 或 evt._pr_sub_pr_number）。
2. 关联 Issue 的 PR/Issue 事件 → ISSUE_NUMBER 仍注入真实数字编号（不回归）。
3. build_task_env（重派路径）同样做纯数字校验。
4. 两个入队/直连执行注入点均改为调用统一 helper（防第三处旁路注入回归）。
"""
import inspect

from src.api.event_pipeline import (
    _inject_issue_pr_env,
    build_task_env,
    resolve_issue_card_id,
)

_TEST_REPO = "gitcode:owner/repo"


def _pr_sub_evt(pr_num=257, scene_id="default", issue_id=None, payload_pr_number=None):
    """构造 PR 订阅派发事件（无关联 Issue → issue_id 为伪 id）。"""
    evt = {
        "type": "pull_request_opened",
        "issue_id": issue_id if issue_id is not None else f"#pr_{pr_num}_{scene_id}",
        "payload": {"pr_number": payload_pr_number} if payload_pr_number is not None else {},
    }
    if payload_pr_number is None:
        evt["_pr_sub_pr_number"] = pr_num
    return evt


class TestInjectIssuePrEnv:
    def test_pr_sub_no_linked_issue_skips_issue_number(self):
        """无关联 Issue 的 PR 订阅事件：ISSUE_NUMBER/GRAPE_ISSUE_ID 不注入（伪 id 非数字）"""
        env = {}
        _inject_issue_pr_env(env, _pr_sub_evt(), _TEST_REPO)
        assert "ISSUE_NUMBER" not in env, f"不应注入非数字 ISSUE_NUMBER: {env.get('ISSUE_NUMBER')}"
        assert "GRAPE_ISSUE_ID" not in env

    def test_pr_sub_injects_numeric_pr_number(self):
        """PR 订阅事件：PR_NUMBER 由 _pr_sub_pr_number 兜底注入纯数字 PR 号"""
        env = {}
        _inject_issue_pr_env(env, _pr_sub_evt(pr_num=257), _TEST_REPO)
        assert env.get("PR_NUMBER") == "257"
        assert env["PR_NUMBER"].isdigit()

    def test_pr_sub_payload_pr_number_preferred(self):
        """payload.pr_number 存在时优先于 _pr_sub_pr_number"""
        env = {}
        _inject_issue_pr_env(env, _pr_sub_evt(payload_pr_number=99), _TEST_REPO)
        assert env.get("PR_NUMBER") == "99"

    def test_numeric_issue_id_still_injected(self):
        """关联 Issue 的 PR 事件（issue_id 为真实编号）：ISSUE_NUMBER 仍注入（不回归）"""
        env = {}
        _inject_issue_pr_env(env, _pr_sub_evt(issue_id="#528", payload_pr_number=257), _TEST_REPO)
        assert env.get("ISSUE_NUMBER") == "528"
        assert env.get("PR_NUMBER") == "257"

    def test_plain_issue_number_from_payload(self):
        """普通 Issue 事件（payload.issue_number）：ISSUE_NUMBER 正常注入"""
        env = {}
        _inject_issue_pr_env(env, {"issue_id": "", "payload": {"issue_number": "66"}}, _TEST_REPO)
        assert env.get("ISSUE_NUMBER") == "66"

    def test_non_numeric_pr_number_not_injected(self):
        """PR 号本身非数字（异常数据）→ PR_NUMBER 也不注入"""
        env = {}
        _inject_issue_pr_env(env, _pr_sub_evt(issue_id="#100", payload_pr_number="pr_abc"), _TEST_REPO)
        assert env.get("ISSUE_NUMBER") == "100"
        assert "PR_NUMBER" not in env

    def test_hash_prefixed_numeric_pr_number_normalized(self):
        """带 # 前缀的数字 PR 号 → 去前缀后注入"""
        env = {}
        _inject_issue_pr_env(env, _pr_sub_evt(issue_id="#100", payload_pr_number="#257"), _TEST_REPO)
        assert env.get("PR_NUMBER") == "257"

    def test_empty_evt_injects_nothing(self):
        """空事件 → 三个变量均不注入"""
        env = {}
        _inject_issue_pr_env(env, {}, _TEST_REPO)
        assert not any(k in env for k in ("ISSUE_NUMBER", "GRAPE_ISSUE_ID", "PR_NUMBER"))


class TestBuildTaskEnvNumericGuard:
    """重派路径（build_task_env）同样只注入纯数字编号（Issue #528 防御）"""

    def test_pseudo_issue_number_not_injected(self):
        env = build_task_env(_TEST_REPO, "qa", "#pr_257_default",
                             issue_number="#pr_257_default", pr_number="")
        assert "ISSUE_NUMBER" not in env, f"重派路径不应注入伪 issue 编号: {env.get('ISSUE_NUMBER')}"

    def test_numeric_injected(self):
        env = build_task_env(_TEST_REPO, "qa", "#528",
                             issue_number="#528", pr_number="257")
        assert env.get("ISSUE_NUMBER") == "528"
        assert env.get("PR_NUMBER") == "257"

    def test_non_numeric_pr_number_not_injected(self):
        env = build_task_env(_TEST_REPO, "qa", "#528",
                             issue_number="528", pr_number="pr_257_default")
        assert env.get("ISSUE_NUMBER") == "528"
        assert "PR_NUMBER" not in env


class TestInjectionCallSitesUseHelper:
    """两个 env 注入点（入队/直连执行）必须统一走 _inject_issue_pr_env，防旁路注入回归"""

    def test_enqueue_pipeline_task_uses_helper(self):
        import src.api.event_pipeline as ep
        src_enqueue = inspect.getsource(ep._enqueue_pipeline_task)
        assert "_inject_issue_pr_env(" in src_enqueue, \
            "_enqueue_pipeline_task 应调用 _inject_issue_pr_env（Issue #528）"
        # 不允许残留裸注入（伪 id 会绕过数字校验）
        assert 'env_extra["ISSUE_NUMBER"] = str(_issue_num)' not in src_enqueue

    def test_execute_agent_inner_uses_helper(self):
        import src.api.event_pipeline as ep
        src_exec = inspect.getsource(ep._execute_agent_inner)
        assert "_inject_issue_pr_env(" in src_exec, \
            "_execute_agent_inner 应调用 _inject_issue_pr_env（Issue #528）"
        assert 'env["ISSUE_NUMBER"] = str(issue_number)' not in src_exec
        assert 'env_extra["ISSUE_NUMBER"] = str(_issue_num)' not in src_exec


class TestResolveIssueCardIdGuard:
    """resolve_issue_card_id 对伪 id 返回空串（既有先例，保持不回归）"""

    def test_pseudo_id_returns_empty(self):
        assert resolve_issue_card_id(_TEST_REPO, "#pr_257_default") == ""

    def test_numeric_returns_card_id_or_empty(self):
        # Issue #667：_inject_issue_pr_env 现会兜底补建缺行（同文件上方用例可能已建 owner/repo#528 行），
        # 先清行再断言纯查询语义：无行 → 空串；但不抛异常
        from src.session_manager import db as _db, db_lock as _dl
        with _dl:
            _db.cursor().execute("DELETE FROM issue WHERE repo_key=? AND issue_number=528", (_TEST_REPO,))
            _db.commit()
        assert resolve_issue_card_id(_TEST_REPO, "#528") == ""
