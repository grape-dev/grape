"""Issue #3621: 建单端点 trigger_pipeline 可选参数（只建单不触发）回归测试。

覆盖：
1. 默认调用（不传 trigger_pipeline）：行为与现状完全一致——同步触发 issue_opened、stage=vod
2. 显式 trigger_pipeline=true：与默认一致（零破坏）
3. trigger_pipeline=false：平台单创建成功、无任何流水线事件、stage=created、status=active
4. 对 stage=created 挂单执行 trigger_pending_issue_pipeline：正常入链（与直接建单同路径）、幂等不重复入链
5. 非 created stage 触发被拒（409，不重复入链）
6. 触发失败/未落事件 → 回滚 stage=created（可重试，不卡死）
7. HTTP 入口层：POST /api/issues/{issue_id}/pipeline/trigger 接线 + create 入口布尔显式解析
8. 复审修复：status 终态校验并入同锁认领（CAS 带 status 条件，并发取消窗口下 400）
9. 复审修复：无事件回滚时恢复 status=active（run_pipeline 已同步 processing）；终态不复活
10. 复审修复：事件计数仅统计触发类事件（仓库订阅等旁路事件不误判 409）
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

import asyncio
import functools
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import require_no_session_token
from src.domains.runtime.routes.issues import require_org_member
from src.kernel.session_manager import db, db_lock

REPO = "gitcode:owner/issue3621-test"
USER = {"id": 1, "username": "tester", "auth_type": "api_key"}

# run_pipeline 同步桩调用记录（入参 raw + headers；不落事件——
# issue_opened 事件的落库属 run_pipeline 职责，回归由既有 issue_89 用例覆盖；
# 本文件只断言触发与否、调用次数与 payload/header 形态，避免桩事件被后台线程异步改写）
PIPELINE_CALLS = []


def _pipeline_stub(raw, headers, account=None, repo_key_override=None):
    PIPELINE_CALLS.append((raw, headers))
    return {"seq": 100}


def _cleanup():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM event_skipped WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO,))
        try:
            c.execute("DELETE FROM issue_state WHERE repo_key=?", (REPO,))
        except Exception:
            pass
        db.commit()
    del PIPELINE_CALLS[:]


def _fresh(test_func):
    """用例自清理（不依赖 pytest fixture）：执行前后清本仓残留与桩调用记录。"""
    @functools.wraps(test_func)
    def wrapper(self):
        _cleanup()
        try:
            return test_func(self)
        finally:
            _cleanup()
    return wrapper


def _stage_row(issue_id: str):
    from src.api.session_store import get_issue_stage
    return get_issue_stage(REPO, issue_id) or {}


def _stage_of(issue_id: str) -> str:
    return str(_stage_row(issue_id).get("stage") or "")


def _event_count(issue_id: str) -> int:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM events WHERE repo_key=? AND issue_id=?", (REPO, issue_id))
        return int(c.fetchone()[0] or 0)


def _create(body_overrides: dict) -> dict:
    """直调 create_task_via_issue（统一建单逻辑，与 POST /api/issues/{owner}/{repo} 同实现）。

    以同步桩替换 run_pipeline（记录调用），避免真实流水线异步派生事件污染后续用例。
    """
    from src.api.issues_center import TaskCreateBody, create_task_via_issue
    body = TaskCreateBody(repo_key=REPO, title="Issue3621 Test Task",
                          description="Test description", **body_overrides)
    with patch("src.api.deps.get_user_token_for_repo", return_value="fake_token"), \
         patch("src.api.issues_center._find_open_issue_by_title", return_value=None), \
         patch("src.domains.runtime.vcs.api_client.VcsApiClient.create_issue",
               return_value={"number": 200}), \
         patch("src.api.issues_center._get_user_id_by_gitcode", return_value=1), \
         patch("src.api.issues_center._get_issue_title", return_value=""), \
         patch("src.domains.runtime.pipeline.event_pipeline.run_pipeline",
               side_effect=_pipeline_stub):
        return asyncio.run(create_task_via_issue(body, USER))


@contextmanager
def _http_client():
    """HTTP 入口层测试：覆盖用户态鉴权依赖（create=require_org_member，写操作=user_only）。"""
    app.dependency_overrides[require_org_member] = lambda: dict(USER)
    app.dependency_overrides[require_no_session_token] = lambda: dict(USER)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


class TestDefaultCreateUnchanged:
    @_fresh
    def test_default_creates_event_and_vod_stage(self):
        """不传 trigger_pipeline：同步触发 issue_opened + stage=vod（回归：现状不变）"""
        result = _create({})
        assert result["created"] is True
        assert result["trigger_pipeline"] is True
        assert _stage_of("#200") == "vod"
        assert len(PIPELINE_CALLS) == 1
        _raw, _headers = PIPELINE_CALLS[0]
        assert b'"object_kind": "issue"' in _raw
        assert _headers.get("X-Gitcode-Event") == "issue_opened"

    @_fresh
    def test_explicit_true_same_as_default(self):
        """显式 trigger_pipeline=true：与默认一致"""
        result = _create({"trigger_pipeline": True})
        assert result["created"] is True
        assert result["trigger_pipeline"] is True
        assert _stage_of("#200") == "vod"
        assert len(PIPELINE_CALLS) == 1


class TestCreateWithoutTrigger:
    @_fresh
    def test_false_skips_pipeline_and_marks_created(self):
        """trigger_pipeline=false：建单成功、无流水线事件、stage=created 哨兵归属、status=active"""
        result = _create({"trigger_pipeline": False})
        assert result["created"] is True
        assert result["trigger_pipeline"] is False
        assert "未触发流水线" in result["message"]
        # run_pipeline 未被调用，无任何流水线事件
        assert len(PIPELINE_CALLS) == 0
        assert _event_count("#200") == 0
        # stage=created 挂单标记；owner_agent 为哨兵（非真实节点 "created"）
        _stage = _stage_row("#200")
        assert _stage.get("stage") == "created"
        assert _stage.get("owner_agent") == "pending_trigger"
        # 平台单可见（status=active）
        with db_lock:
            c = db.cursor()
            c.execute("SELECT status, created_by FROM issue WHERE repo_key=? AND issue_number=?",
                      (REPO, "200"))
            row = c.fetchone()
        assert row is not None, "挂单后 issue 卡片应可列表可见"
        assert row[0] == "active"
        # 创建人身份与直接建单一致（_creator_login 落 created_by），供后续触发复用
        assert row[1] == "tester"


class TestLaterTriggerOfCreatedIssue:
    @_fresh
    def test_trigger_created_issue_enters_pipeline_once(self):
        """对 stage=created 挂单触发：入链（stage=vod + issue_opened 事件），重复触发 409 幂等"""
        from src.api.issues_center import trigger_pending_issue_pipeline

        assert _create({"trigger_pipeline": False})["created"] is True
        assert _stage_of("#200") == "created"
        calls = []

        def _fake_run_pipeline(raw, headers, account=None, repo_key_override=None):
            calls.append((raw, headers))
            # 模拟 run_pipeline 落一条 issue_opened 事件（终态，避免后台线程改写）
            with db_lock:
                c = db.cursor()
                c.execute("INSERT INTO events (repo_key, issue_id, event_type, agent_type, status, title, created_at) "
                          "VALUES (?,?,?,?,?,?,datetime('now'))",
                          (REPO, "#200", "issue_opened", "vod", "completed", "Issue3621 Test Task"))
                db.commit()
            return {"seq": 101}

        with patch("src.domains.runtime.pipeline.event_pipeline.run_pipeline",
                   side_effect=_fake_run_pipeline):
            # 首次触发：入链成功，payload 为 issue_opened 原生形态（与直接建单同路径）
            result = asyncio.run(trigger_pending_issue_pipeline(REPO, 200, actor="tester"))
            assert result["ok"] is True
            assert result["stage"] == "vod"
            assert result["seq"] == 101
            assert len(calls) == 1
            _raw, _headers = calls[0]
            assert b'"object_kind": "issue"' in _raw
            assert _headers.get("X-Gitcode-Event") == "issue_opened"
        assert _stage_of("#200") == "vod"
        assert _event_count("#200") == 1

        # 重复触发：已有事件/stage=vod → 409，不重复入链
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(trigger_pending_issue_pipeline(REPO, 200, actor="tester"))
        assert exc_info.value.status_code == 409
        assert _event_count("#200") == 1

    @_fresh
    def test_trigger_non_created_stage_rejected(self):
        """stage=vod（已入链）触发 → 409 拒绝"""
        from src.api.issues_center import trigger_pending_issue_pipeline
        from src.api.session_store import set_issue_stage

        set_issue_stage(REPO, "#300", "vod", "vod")
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(trigger_pending_issue_pipeline(REPO, 300, actor="tester"))
        assert exc_info.value.status_code == 409

    @_fresh
    def test_trigger_failure_rolls_back_to_created(self):
        """run_pipeline 抛错：回滚 stage=created（无事件），可再次触发而不卡死"""
        from src.api.issues_center import trigger_pending_issue_pipeline

        assert _create({"trigger_pipeline": False})["created"] is True
        with patch("src.domains.runtime.pipeline.event_pipeline.run_pipeline",
                   side_effect=RuntimeError("boom")):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(trigger_pending_issue_pipeline(REPO, 200, actor="tester"))
        assert exc_info.value.status_code == 502
        assert _stage_of("#200") == "created"
        assert _event_count("#200") == 0

    @_fresh
    def test_trigger_failure_after_event_landed_is_success(self):
        """run_pipeline 落事件后才抛错：事件已存在视为入链成功（不误报 502/不卡死）"""
        from src.api.issues_center import trigger_pending_issue_pipeline

        assert _create({"trigger_pipeline": False})["created"] is True

        def _raise_after_event(raw, headers, account=None, repo_key_override=None):
            with db_lock:
                c = db.cursor()
                c.execute("INSERT INTO events (repo_key, issue_id, event_type, agent_type, status, title, created_at) "
                          "VALUES (?,?,?,?,?,?,datetime('now'))",
                          (REPO, "#200", "issue_opened", "vod", "completed", "Issue3621 Test Task"))
                db.commit()
            raise RuntimeError("after event")

        with patch("src.domains.runtime.pipeline.event_pipeline.run_pipeline",
                   side_effect=_raise_after_event):
            result = asyncio.run(trigger_pending_issue_pipeline(REPO, 200, actor="tester"))
        assert result["ok"] is True
        assert result["stage"] == "vod"
        assert _stage_of("#200") == "vod"
        assert _event_count("#200") == 1

    @_fresh
    def test_trigger_without_event_rolls_back_and_retryable(self):
        """run_pipeline 返回但未落事件：回滚 created，随后可成功重试（不永久 409）"""
        from src.api.issues_center import trigger_pending_issue_pipeline

        assert _create({"trigger_pipeline": False})["created"] is True

        def _no_event(raw, headers, account=None, repo_key_override=None):
            return {"seq": 0}

        with patch("src.domains.runtime.pipeline.event_pipeline.run_pipeline",
                   side_effect=_no_event):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(trigger_pending_issue_pipeline(REPO, 200, actor="tester"))
        assert exc_info.value.status_code == 502
        assert _stage_of("#200") == "created"

        def _with_event(raw, headers, account=None, repo_key_override=None):
            with db_lock:
                c = db.cursor()
                c.execute("INSERT INTO events (repo_key, issue_id, event_type, agent_type, status, title, created_at) "
                          "VALUES (?,?,?,?,?,?,datetime('now'))",
                          (REPO, "#200", "issue_opened", "vod", "completed", "Issue3621 Test Task"))
                db.commit()
            return {"seq": 102}

        with patch("src.domains.runtime.pipeline.event_pipeline.run_pipeline",
                   side_effect=_with_event):
            result = asyncio.run(trigger_pending_issue_pipeline(REPO, 200, actor="tester"))
        assert result["stage"] == "vod"
        assert result["seq"] == 102
        assert _event_count("#200") == 1


class TestHttpEntryLayer:
    @_fresh
    def test_trigger_route_wires_pending_trigger(self):
        """POST /api/issues/{issue_id}/pipeline/trigger 正确解析卡片并调用触发实现"""
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, "
                      "task_description, status, org_id, created_by, stage, owner_agent) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (USER["id"], "tester", REPO, 200, "Issue3621 Test Task", "desc",
                       "active", "", "tester", "created", "pending_trigger"))
            db.commit()
            issue_id = c.lastrowid
        with _http_client() as client, \
                patch("src.domains.runtime.issues_center.trigger_pending_issue_pipeline",
                      new=AsyncMock(return_value={"ok": True, "issue_number": 200,
                                                  "stage": "vod", "seq": 7})) as m:
            resp = client.post(f"/api/issues/{issue_id}/pipeline/trigger")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "issue_number": 200, "stage": "vod",
                               "seq": 7, "issue_id": issue_id}
        m.assert_awaited_once()
        assert m.await_args.args[0] == REPO
        assert int(m.await_args.args[1]) == 200
        assert m.await_args.kwargs["actor"] == "tester"

    @_fresh
    def test_trigger_route_missing_issue_404(self):
        with _http_client() as client:
            resp = client.post("/api/issues/999999/pipeline/trigger")
        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == 404

    @_fresh
    def test_create_route_parses_string_false(self):
        """HTTP 建单入口：字符串 "false" 必须解析为 False（不能 bool("false") → True）"""
        with _http_client() as client, \
                patch("src.api.issues_center.create_task_via_issue",
                      new=AsyncMock(return_value={"ok": True})) as m:
            resp = client.post("/api/issues/owner/issue3621-test",
                               json={"repo_key": REPO, "title": "T", "description": "D",
                                     "trigger_pipeline": "false"})
        assert resp.status_code == 200, resp.text
        body = m.await_args.args[0]
        assert body.trigger_pipeline is False

    @_fresh
    def test_create_route_parses_string_true(self):
        with _http_client() as client, \
                patch("src.api.issues_center.create_task_via_issue",
                      new=AsyncMock(return_value={"ok": True})) as m:
            resp = client.post("/api/issues/owner/issue3621-test",
                               json={"repo_key": REPO, "title": "T", "description": "D",
                                     "trigger_pipeline": "true"})
        assert resp.status_code == 200, resp.text
        body = m.await_args.args[0]
        assert body.trigger_pipeline is True

    @_fresh
    def test_create_route_rejects_invalid_bool(self):
        with _http_client() as client:
            resp = client.post("/api/issues/owner/issue3621-test",
                               json={"repo_key": REPO, "title": "T", "description": "D",
                                     "trigger_pipeline": "maybe"})
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == 400

class TestReviewFixStatusGuardInClaim:
    """复审修复 1：status 终态校验并入同锁认领（routes 先读后触发的并发取消窗口关闭）"""

    def _insert_created_issue(self, status: str) -> int:
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, "
                      "task_description, status, org_id, created_by, stage, owner_agent) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (USER["id"], "tester", REPO, 400, "Guard Test", "desc",
                       status, "", "tester", "created", "pending_trigger"))
            db.commit()
            return c.lastrowid

    def _issue_status(self, issue_number) -> str:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT status FROM issue WHERE repo_key=? AND issue_number=?",
                      (REPO, str(issue_number)))
            return str(c.fetchone()[0] or "")

    @_fresh
    def test_cancelled_created_issue_rejected_400(self):
        """stage=created + status=cancelled：直接调用触发实现 → 400（终态守卫在核心层生效）"""
        from src.api.issues_center import trigger_pending_issue_pipeline

        self._insert_created_issue("cancelled")
        with patch("src.domains.runtime.pipeline.event_pipeline.run_pipeline") as m:
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(trigger_pending_issue_pipeline(REPO, 400, actor="tester"))
        assert exc_info.value.status_code == 400
        m.assert_not_called()
        assert self._issue_status(400) == "cancelled"

    @_fresh
    def test_completed_created_issue_rejected_400(self):
        from src.api.issues_center import trigger_pending_issue_pipeline

        self._insert_created_issue("completed")
        with patch("src.domains.runtime.pipeline.event_pipeline.run_pipeline") as m:
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(trigger_pending_issue_pipeline(REPO, 400, actor="tester"))
        assert exc_info.value.status_code == 400
        m.assert_not_called()

    @_fresh
    def test_cancel_race_window_blocked_by_cas(self):
        """并发取消窗口（读取 status 通过后、CAS 前被取消）→ CAS 0 行 → 400，不触发"""
        import src.domains.runtime.issues_center as ic
        from src.api.issues_center import trigger_pending_issue_pipeline

        self._insert_created_issue("active")

        class _RaceCursor:
            """读 status 的 SELECT 返回 active 后立刻并发取消（窗口模拟）。"""

            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, params=()):
                cur = self._inner.execute(sql, params)
                if str(sql).startswith("SELECT stage, title"):
                    # 独立 cursor 执行并发取消，避免清掉本 cursor 的 SELECT 结果集
                    with db_lock:
                        _c2 = db.cursor()
                        _c2.execute(
                            "UPDATE issue SET status='cancelled' WHERE repo_key=? AND issue_number=?",
                            (REPO, "400"))
                        db.commit()
                return cur

            def __getattr__(self, item):
                return getattr(self._inner, item)

        class _RaceDb:
            def cursor(self):
                return _RaceCursor(db.cursor())

            def __getattr__(self, item):
                return getattr(db, item)

            def commit(self):
                db.commit()

        with patch.object(ic, "_rt_db", _RaceDb()), \
             patch("src.domains.runtime.pipeline.event_pipeline.run_pipeline") as m:
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(trigger_pending_issue_pipeline(REPO, 400, actor="tester"))
        assert exc_info.value.status_code == 400
        m.assert_not_called()
        assert self._issue_status(400) == "cancelled"

    @_fresh
    def test_http_route_cancelled_issue_400_from_core(self):
        """HTTP 入口：取消单触发 → 400（校验由核心同锁认领返回，路由不再先读后判）"""
        issue_id = self._insert_created_issue("cancelled")
        with _http_client() as client:
            resp = client.post(f"/api/issues/{issue_id}/pipeline/trigger")
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == 400


class TestReviewFixRollbackRestoresStatus:
    """复审修复 2：无事件回滚时恢复 status（processing→active；终态保留）"""

    def _create_pending(self, number=500):
        assert _create({"trigger_pipeline": False})["created"] is True
        # create 固定用 200 号；直接改号保持用例隔离
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE issue SET issue_number=? WHERE repo_key=? AND issue_number=?",
                      (str(number), REPO, "200"))
            db.commit()

    def _issue_row(self, number=500):
        with db_lock:
            c = db.cursor()
            c.execute("SELECT stage, status FROM issue WHERE repo_key=? AND issue_number=?",
                      (REPO, str(number)))
            return c.fetchone()

    @_fresh
    def test_no_event_rollback_restores_active_status(self):
        """run_pipeline 已把 status 同步为 processing 但未落事件 → 回滚后 status=active"""
        from src.api.issues_center import trigger_pending_issue_pipeline

        self._create_pending()

        def _sync_status_no_event(raw, headers, account=None, repo_key_override=None):
            # 模拟 event_pipeline set_issue_stage 同步 status=processing（Issue #85）
            with db_lock:
                c = db.cursor()
                c.execute("UPDATE issue SET status='processing' WHERE repo_key=? AND issue_number=?",
                          (REPO, "500"))
                db.commit()
            return {"seq": 0}

        with patch("src.domains.runtime.pipeline.event_pipeline.run_pipeline",
                   side_effect=_sync_status_no_event):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(trigger_pending_issue_pipeline(REPO, 500, actor="tester"))
        assert exc_info.value.status_code == 502
        assert self._issue_row() == ("created", "active")

    @_fresh
    def test_rollback_preserves_cancelled_status(self):
        """认领后、触发过程中被取消（status=cancelled）→ 回滚保留终态，不复活为 active"""
        from src.api.issues_center import trigger_pending_issue_pipeline

        self._create_pending()

        def _cancel_during_pipeline(raw, headers, account=None, repo_key_override=None):
            with db_lock:
                c = db.cursor()
                c.execute("UPDATE issue SET status='cancelled' WHERE repo_key=? AND issue_number=?",
                          (REPO, "500"))
                db.commit()
            raise RuntimeError("pipeline failed after cancel")

        with patch("src.domains.runtime.pipeline.event_pipeline.run_pipeline",
                   side_effect=_cancel_during_pipeline):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(trigger_pending_issue_pipeline(REPO, 500, actor="tester"))
        assert exc_info.value.status_code == 502
        stage, status = self._issue_row()
        assert stage == "created"
        assert status == "cancelled"


class TestReviewFixTriggerEventCount:
    """复审修复 3：事件计数仅统计触发类事件，仓库订阅等旁路事件不误判 409"""

    def _insert_bypass_event(self, issue_id="#600", event_type="repo_watched"):
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO events (repo_key, issue_id, event_type, agent_type, status, title, created_at) "
                      "VALUES (?,?,?,?,?,?,datetime('now'))",
                      (REPO, issue_id, event_type, "", "completed", "bypass"))
            db.commit()

    @_fresh
    def test_bypass_event_does_not_block_trigger(self):
        from src.api.issues_center import trigger_pending_issue_pipeline

        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, "
                      "task_description, status, org_id, created_by, stage, owner_agent) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (USER["id"], "tester", REPO, 600, "Bypass Test", "desc",
                       "active", "", "tester", "created", "pending_trigger"))
            db.commit()
        # 仓库订阅等旁路事件挂在同一 issue_id 下，不得误判 409
        self._insert_bypass_event()

        def _with_event(raw, headers, account=None, repo_key_override=None):
            with db_lock:
                c = db.cursor()
                c.execute("INSERT INTO events (repo_key, issue_id, event_type, agent_type, status, title, created_at) "
                          "VALUES (?,?,?,?,?,?,datetime('now'))",
                          (REPO, "#600", "issue_opened", "vod", "completed", "Bypass Test"))
                db.commit()
            return {"seq": 103}

        with patch("src.domains.runtime.pipeline.event_pipeline.run_pipeline",
                   side_effect=_with_event):
            result = asyncio.run(trigger_pending_issue_pipeline(REPO, 600, actor="tester"))
        assert result["ok"] is True
        assert result["stage"] == "vod"

    @_fresh
    def test_pipeline_event_still_blocks_retrigger(self):
        """触发类事件存在 → 仍 409（幂等不破坏）"""
        from src.api.issues_center import trigger_pending_issue_pipeline

        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, "
                      "task_description, status, org_id, created_by, stage, owner_agent) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (USER["id"], "tester", REPO, 600, "Block Test", "desc",
                       "active", "", "tester", "created", "pending_trigger"))
            db.commit()
        self._insert_bypass_event(event_type="issue_opened")
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(trigger_pending_issue_pipeline(REPO, 600, actor="tester"))
        assert exc_info.value.status_code == 409
