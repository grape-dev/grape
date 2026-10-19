# -*- coding: utf-8 -*-
"""Issue #368：派发失败路径统一走 _fail_event（回归测试）。

验证点：
1. _fail_event 直接调用：events 行写入 result/exit_code=1/completed_at 三字段
2. _fail_event 同步收口 agent_tasks → status=failed, exit_code=1, errors 非空
3. _fail_event 内部 DB 异常不外抛（事件仍由调用方控制，不污染派发主流程）
4. PR 订阅 extra 路径（event_pipeline.py:803）：pick_engine=None → 事件三字段完整
5. Pipeline 订阅分发路径（event_pipeline.py:1052）：pick_engine=None → 事件三字段完整

背景：原两处裸 update_event_status(seq, "failed") 落库无 reason/exit_code/completed_at
（无头失败，实测事件 13328/13455），排障需倒查日志。修好后该类无头失败记录清零。
"""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from src.session_manager import db, db_lock

_REPO = "gitcode:test/issue368"


def _clean():
    with db_lock:
        c = db.cursor()
        for t in ("agent_tasks", "events", "devops_agent_session", "devops_pipeline_scene",
                  "managed_repos", "agent_runtimes", "runtime_credentials", "issue_state",
                  "issue", "event_skipped", "event_active_guard", "unified_user_mapping",
                  "user_repos", "engine_capabilities", "task_messages"):
            try:
                c.execute(f"DELETE FROM {t} WHERE repo_key LIKE 'gitcode:test/issue368%' "
                          f"OR repo_key LIKE 'test/issue368%'")
            except Exception:
                pass
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 'issue368-%'")
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'issue368-%'")
        c.execute("DELETE FROM devops_agent_session WHERE id LIKE 'sess-issue368-%'")
        db.commit()


def _mk_event(*, agent_type="qa", status="running", scene_id="") -> int:
    """创建一条 events 行，返回 seq。"""
    from src.api.event_store import append_event
    return append_event(
        repo_key=_REPO, event_type="a2a_event",
        title="issue368-test", issue_id="#0", source="a2a",
        agent_type=agent_type, actor="tester",
        summary="issue368 fixture", payload={}, status=status,
        scene_id=scene_id, entity_type="",
    )


def _mk_task(event_seq, *, status="running", agent_type="qa", errors=None) -> str:
    """创建一条 agent_tasks 行关联到 event_seq，返回 task id。

    errors=None（默认）匹配 _fail_event 的 COALESCE(errors, ?) 填充条件；
    errors="" 匹配 schema DEFAULT ''（COALESCE 不覆盖空串，属既有行为）。
    """
    tid = f"t-issue368-{uuid.uuid4().hex[:8]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, prompt, repo_key, agent_type, skill, "
            "status, runtime_id, started_at, created_at, event_seq, errors) "
            "VALUES (?, 'sess-issue368-x', 'p', ?, ?, 's', ?, 0, ?, ?, ?, ?)",
            (tid, _REPO, agent_type, status,
             datetime.now(timezone.utc).isoformat(),
             datetime.now(timezone.utc).isoformat(), event_seq, errors))
        db.commit()
    return tid


def _event_row(seq):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, exit_code, completed_at, result FROM events WHERE seq=?", (seq,))
        return c.fetchone()


def _task_row(event_seq):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, exit_code, errors FROM agent_tasks WHERE event_seq=? "
                  "ORDER BY created_at DESC LIMIT 1", (event_seq,))
        return c.fetchone()


class TestFailEventUnit:
    """_fail_event 直接调用：三字段完整性 + agent_tasks 收口 + 异常保护。"""

    @pytest.fixture(autouse=True)
    def _setup(self):
        _clean()
        yield
        _clean()

    def test_writes_three_fields(self):
        """_fail_event 写入 result/exit_code=1/completed_at 三字段（无头失败修复核心）。"""
        from src.api.event_pipeline import _fail_event
        seq = _mk_event(status="running")
        _fail_event(seq, "测试失败原因：无可用引擎")
        row = _event_row(seq)
        assert row is not None, "事件行不存在"
        assert row[0] == "failed", f"status 应 failed，实际 {row[0]}"
        assert row[1] == 1, f"exit_code 应 1，实际 {row[1]}"
        assert row[2], f"completed_at 应非空，实际 {row[2]!r}"
        assert "测试失败原因" in (row[3] or ""), f"result 应含原因，实际 {row[3]!r}"

    def test_syncs_agent_tasks_failed(self):
        """_fail_event 同步 agent_tasks → status=failed, exit_code=1（核心收口）。

        errors 字段为 COALESCE(errors, reason)：仅当原值为 NULL 时填充（既有行为）。
        """
        from src.api.event_pipeline import _fail_event
        seq = _mk_event(status="running")
        _mk_task(seq, status="running")
        _fail_event(seq, "派发失败：引擎离线")
        trow = _task_row(seq)
        assert trow is not None, "agent_tasks 行不存在"
        assert trow[0] == "failed", f"agent_tasks.status 应 failed，实际 {trow[0]}"
        assert trow[1] == 1, f"agent_tasks.exit_code 应 1，实际 {trow[1]}"

    def test_syncs_agent_tasks_errors_when_null(self):
        """_fail_event 在 errors=NULL 时填充原因（COALESCE 填充路径）。"""
        from src.api.event_pipeline import _fail_event
        seq = _mk_event(status="running")
        _mk_task(seq, status="queued", errors=None)  # NULL → 可被 COALESCE 填充
        _fail_event(seq, "派发失败：引擎离线")
        trow = _task_row(seq)
        assert trow is not None, "agent_tasks 行不存在"
        assert trow[0] == "failed", f"agent_tasks.status 应 failed，实际 {trow[0]}"
        assert trow[2], f"errors（NULL→填充）应非空，实际 {trow[2]!r}"

    def test_exception_no_propagate(self):
        """_fail_event 内部 update_event_complete 抛错时不外抛（派发主流程不污染）。"""
        from src.api.event_pipeline import _fail_event
        seq = _mk_event(status="running")
        with patch("src.api.event_store.update_event_complete",
                   side_effect=RuntimeError("模拟 DB 异常")):
            # 不得抛异常出去
            _fail_event(seq, "应被吞掉的异常")
        # 事件行仍在（_fail_event 异常保护，不删行；调用方自行控制后续）

    def test_zero_seq_noop(self):
        """seq=0 时 _fail_event 立即返回（边界保护）。"""
        from src.api.event_pipeline import _fail_event
        _fail_event(0, "不应执行")  # 不得抛异常


class TestPRSubscriptionExtraFailPath:
    """event_pipeline.py:803 PR 订阅 extra 路径：pick_engine=None → 三字段完整。

    直接调用 _dispatch_pr_subscription_extra，mock pick_engine=None，
    断言 a2a_event 行三字段完整（无头失败修复）。
    """

    @pytest.fixture(autouse=True)
    def _setup(self):
        _clean()
        yield
        _clean()

    def test_pr_subscription_no_engine_writes_reason(self):
        """PR 订阅 extra 派发：pick_engine=None → a2a_event 三字段完整。"""
        import src.api.event_pipeline as ep
        evt = {
            "repo_key": _REPO, "issue_id": "#0", "actor": "tester",
            "payload": {"object_attributes": {"iid": 99}},
        }
        match = ("sc368-a", "qa-cli", [], "", [])  # (scene_id, qa_skill, qa_engine_ids, sre_*, sre_*)
        with patch("src.runtime.engine_select.pick_engine", return_value=None):
            ep._dispatch_pr_subscription_extra(_REPO, evt, "pull_request_opened", match)
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT status, exit_code, completed_at, result, scene_id, agent_type "
                "FROM events WHERE repo_key=? AND event_type='a2a_event' "
                "ORDER BY seq DESC LIMIT 1", (_REPO,))
            row = c.fetchone()
        assert row is not None, "未生成 a2a_event 行"
        status, exit_code, completed_at, result, scene_id, agent_type = row
        assert status == "failed", f"status 应 failed，实际 {status}"
        assert exit_code == 1, f"exit_code 应 1，实际 {exit_code}"
        assert completed_at, f"completed_at 应非空，实际 {completed_at!r}"
        assert result and "无可用引擎" in result, f"result 应含可读原因，实际 {result!r}"
        assert scene_id == "sc368-a", f"scene_id 应 sc368-a，实际 {scene_id}"
        assert agent_type == "qa", f"agent_type 应 qa，实际 {agent_type}"


class TestSubscriptionAgentFailPath:
    """event_pipeline.py:1052 Pipeline 订阅分发路径：pick_engine=None → 三字段完整。

    直接调用 _dispatch_subscription_agent，mock pick_engine=None，断言事件三字段完整。
    """

    @pytest.fixture(autouse=True)
    def _setup(self):
        _clean()
        self._seed_session()
        yield
        _clean()

    def _seed_session(self):
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT OR REPLACE INTO managed_repos "
                "(repo_key, repo_owner, repo_name, full_name, webhook_id, dev_scene, "
                "bot_gitcode_token, gitcode_webhook_token, bot_username) "
                "VALUES (?, 'test', 'issue368', 'test/issue368', 0, 'default', ?, 'wt', 'grape_bot')",
                (_REPO, "bot-token"))
            # 为 sre agent 建 session（含 skill），让 _dispatch_subscription_agent 能解析 skill
            c.execute(
                "INSERT INTO devops_agent_session "
                "(id, repo_key, agent_type, runtime_id, status, enable, skill, repo_gitcode_token) "
                "VALUES (?, ?, 'sre', 0, 'idle', 1, 'sre-skill', ?)",
                ("sess-issue368-sre", _REPO, "bot-token"))
            db.commit()

    def test_subscription_agent_no_engine_writes_reason(self):
        """Pipeline 订阅分发：pick_engine=None → 事件三字段完整（无头失败修复）。"""
        import src.api.event_pipeline as ep
        evt = {
            "repo_key": _REPO, "issue_id": "#0", "payload": {"issue_number": 0},
            "actor": "tester", "repository": "test/issue368", "source": "gitcode",
            "type": "issues_opened",
        }
        with patch("src.runtime.engine_select.pick_engine", return_value=None):
            ep._dispatch_subscription_agent(_REPO, evt, "issues_opened", "sre", "qa")
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT status, exit_code, completed_at, result, agent_type "
                "FROM events WHERE repo_key=? AND agent_type='sre' "
                "ORDER BY seq DESC LIMIT 1", (_REPO,))
            row = c.fetchone()
        assert row is not None, "未生成 sre 订阅事件行"
        status, exit_code, completed_at, result, agent_type = row
        assert status == "failed", f"status 应 failed，实际 {status}"
        assert exit_code == 1, f"exit_code 应 1，实际 {exit_code}"
        assert completed_at, f"completed_at 应非空，实际 {completed_at!r}"
        assert result and "无可用引擎" in result, f"result 应含可读原因，实际 {result!r}"
        assert agent_type == "sre", f"agent_type 应 sre，实际 {agent_type}"
