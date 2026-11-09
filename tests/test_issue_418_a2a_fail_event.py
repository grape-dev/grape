# -*- coding: utf-8 -*-
"""Issue #418：webhook A2A 早退分支统一走 _fail_event（完整诊断回写）。

验证点（对应 Spec Acceptance Criteria）：
1. pick_engine=None 早退：事件 result 非空、exit_code=1、completed_at 非空
2. auto-select 空集早退：三字段完整，reason 含「auto-select 空集」
3. auto-select 异常早退：reason 含「auto-select 异常」而非裸 failed
4. try 兜底异常早退：reason 含「A2A 派发启动异常」
5. 幂等：重复触发同一 seq 早退，终态诊断字段不被二次覆盖
6. #369 任务侧判死（runtime/service.py）：_fail_task_pool_unreachable →
   事件三字段完整（_sync_event_failed 通路回归）

背景：原 4 处早退调裸 update_event_status(seq,"failed")，事件
exit_code=NULL / result 空 / 无 completed_at（无头失败，排障只能倒查日志）。
修统一走 event_pipeline._fail_event（#368 已验证模式）。
"""
import os
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from src.session_manager import db, db_lock

_REPO = "gitcode:issue418-owner/repo"
_REPO_FULL = "issue418-owner/repo"


def _clean():
    with db_lock:
        c = db.cursor()
        for t in ("agent_tasks", "events", "devops_agent_session", "managed_repos",
                  "agent_runtimes", "runtime_credentials", "issue",
                  "event_active_guard"):
            try:
                c.execute(f"DELETE FROM {t} WHERE repo_key=?", (_REPO,))
            except Exception:
                pass
        try:
            c.execute("DELETE FROM agent_runtimes WHERE credential_id IN "
                      "(SELECT id FROM runtime_credentials WHERE name LIKE 'issue418-cred-%')")
        except Exception:
            pass
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'issue418-cred-%'")
        # 模式配置以 env 为准（防其它用例残留 meta 行干扰 plugin/legacy 判定）
        try:
            c.execute("DELETE FROM meta WHERE key IN ('pipeline_mode', 'execution_mode')")
        except Exception:
            pass
        db.commit()


def _seed_managed_repo():
    """托管记录种子（无 bot token → A2A 鉴权走 defer 通道，与 test_a2a_queue 一致）。"""
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
                "VALUES (?, 'issue418-owner', 'repo', ?)",
                (_REPO, _REPO_FULL))
            db.commit()
        except Exception:
            pass


def _a2a_body(agent="coding", issue_number="7"):
    return {
        "agent": agent,
        "payload": {
            "repository": _REPO_FULL,
            "account": _REPO_FULL,
            "issue_number": issue_number,
            "pr_number": "",
            "context": "issue418 测试 A2A 早退回写",
        },
    }


def _latest_a2a_event():
    """取该仓库最新 a2a_event 行： (seq, status, exit_code, completed_at, result)。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT seq, status, exit_code, completed_at, result FROM events "
            "WHERE repo_key=? AND event_type='a2a_event' ORDER BY seq DESC LIMIT 1",
            (_REPO,))
        return c.fetchone()


def _mk_event(status="running") -> int:
    from src.api.event_store import append_event
    return append_event(
        repo_key=_REPO, event_type="a2a_event",
        title="issue418-test", issue_id="#0", source="a2a",
        agent_type="coding", actor="tester",
        summary="issue418 fixture", payload={}, status=status,
        scene_id="", entity_type="",
    )


def _post(client, body):
    return client.post("/api/a2a", json=body)


class _BoomCursor:
    """对命中特征 SQL 的 execute 抛错（模拟 auto-select 查询异常），其余透传。"""

    def __init__(self, inner):
        self._inner = inner

    def execute(self, sql, *a, **k):
        if "CASE name WHEN 'opencode'" in (sql or ""):
            raise RuntimeError("模拟 auto-select 查询异常")
        return self._inner.execute(sql, *a, **k)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _BoomDb:
    def __init__(self, inner):
        self._inner = inner

    def cursor(self):
        return _BoomCursor(self._inner.cursor())

    def __getattr__(self, name):
        return getattr(self._inner, name)


class TestA2AEarlyExitFailEvent:
    """webhook.py 4 处 A2A 早退分支 → _fail_event 完整诊断回写。"""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        _clean()
        _seed_managed_repo()
        # legacy 模式：绕开 plugin event-driven 路径，直达 4 处早退分支
        monkeypatch.setenv("GRAPE_PIPELINE_MODE", "legacy")
        monkeypatch.delenv("DEVOPS_EXECUTION_MODE", raising=False)
        yield
        _clean()

    def test_pick_engine_none_writes_reason(self):
        """早退① pick_engine=None → 三字段完整（无头失败修复核心）。"""
        from fastapi.testclient import TestClient
        from src.api.main import app
        with patch("src.runtime.engine_select.pick_engine", return_value=None):
            client = TestClient(app)
            r = _post(client, _a2a_body())
        assert r.status_code == 200, r.text
        assert r.json().get("status") == "failed", r.text
        row = _latest_a2a_event()
        assert row is not None, "未生成 a2a_event 行"
        _seq, status, exit_code, completed_at, result = row
        assert status == "failed", f"status 应 failed，实际 {status}"
        assert exit_code == 1, f"exit_code 应 1，实际 {exit_code}"
        assert completed_at, f"completed_at 应非空，实际 {completed_at!r}"
        assert result and "无可用引擎" in result and "pick_engine=None" in result, \
            f"result 应含具体原因，实际 {result!r}"

    def test_auto_select_empty_writes_reason(self):
        """早退② auto-select 空集（无在线本机引擎）→ 三字段完整。"""
        from fastapi.testclient import TestClient
        from src.api.main import app
        with patch("src.runtime.engine_select.pick_engine", return_value=0):
            client = TestClient(app)
            r = _post(client, _a2a_body())
        assert r.status_code == 200, r.text
        row = _latest_a2a_event()
        assert row is not None, "未生成 a2a_event 行"
        _seq, status, exit_code, completed_at, result = row
        assert status == "failed", f"status 应 failed，实际 {status}"
        assert exit_code == 1, f"exit_code 应 1，实际 {exit_code}"
        assert completed_at, f"completed_at 应非空，实际 {completed_at!r}"
        assert result and "auto-select 空集" in result, f"result 应含空集原因，实际 {result!r}"

    def test_auto_select_exception_writes_reason(self):
        """早退③ auto-select 查询异常 → reason 含「auto-select 异常」而非裸 failed。"""
        from fastapi.testclient import TestClient
        from src.api.main import app
        with patch("src.runtime.engine_select.pick_engine", return_value=0), \
             patch("src.session_manager.db", _BoomDb(db)):
            client = TestClient(app)
            r = _post(client, _a2a_body())
        assert r.status_code == 200, r.text
        row = _latest_a2a_event()
        assert row is not None, "未生成 a2a_event 行"
        _seq, status, exit_code, completed_at, result = row
        assert status == "failed", f"status 应 failed，实际 {status}"
        assert exit_code == 1, f"exit_code 应 1，实际 {exit_code}"
        assert completed_at, f"completed_at 应非空，实际 {completed_at!r}"
        assert result and "auto-select 异常" in result, f"result 应含异常原因，实际 {result!r}"

    def test_dispatch_fallback_exception_writes_reason(self):
        """早退④ try 兜底异常（pick_engine 抛错）→ reason 含「A2A 派发启动异常」。"""
        from fastapi.testclient import TestClient
        from src.api.main import app
        with patch("src.runtime.engine_select.pick_engine",
                   side_effect=RuntimeError("模拟派发启动异常")):
            client = TestClient(app)
            r = _post(client, _a2a_body())
        assert r.status_code == 200, r.text
        assert r.json().get("seq") == 0, r.text
        row = _latest_a2a_event()
        assert row is not None, "未生成 a2a_event 行"
        _seq, status, exit_code, completed_at, result = row
        assert status == "failed", f"status 应 failed，实际 {status}"
        assert exit_code == 1, f"exit_code 应 1，实际 {exit_code}"
        assert completed_at, f"completed_at 应非空，实际 {completed_at!r}"
        assert result and "A2A 派发启动异常" in result, f"result 应含启动异常原因，实际 {result!r}"


class TestFailEventIdempotent:
    """终态幂等：重复触发同一 seq 早退，诊断字段不被二次覆盖。"""

    @pytest.fixture(autouse=True)
    def _setup(self):
        _clean()
        yield
        _clean()

    def test_terminal_not_overwritten(self):
        """同一 seq 两次 _fail_event：第二次不覆盖第一次的 result/completed_at。"""
        from src.api.event_pipeline import _fail_event
        seq = _mk_event(status="running")
        _fail_event(seq, "第一次原因：无可用引擎")
        with db_lock:
            c = db.cursor()
            first = c.execute(
                "SELECT status, exit_code, completed_at, result FROM events WHERE seq=?",
                (seq,)).fetchone()
        _fail_event(seq, "第二次原因：不应覆盖")
        with db_lock:
            c = db.cursor()
            second = c.execute(
                "SELECT status, exit_code, completed_at, result FROM events WHERE seq=?",
                (seq,)).fetchone()
        assert first[0] == "failed" and first[1] == 1 and first[2] and "第一次原因" in first[3]
        assert second[3] == first[3], f"终态 result 不应被覆盖：{first[3]!r} → {second[3]!r}"
        assert second[2] == first[2], "终态 completed_at 不应被覆盖"


class TestTaskSideFailWriteback369:
    """#369 任务侧判死路径：_fail_task_pool_unreachable → 事件三字段完整。"""

    @pytest.fixture(autouse=True)
    def _setup(self):
        _clean()
        yield
        _clean()

    def test_pool_unreachable_syncs_event(self):
        """绑定引擎全部不可达判死 → 任务 failed 且关联事件三字段完整。"""
        from src.runtime import service
        seq = _mk_event(status="running")
        tid = f"t-issue418-{uuid.uuid4().hex[:8]}"
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO agent_tasks (id, agent_session_id, prompt, repo_key, agent_type, skill, "
                "status, runtime_id, started_at, created_at, event_seq, errors) "
                "VALUES (?, 'sess-issue418-x', 'p', ?, 'coding', 's', 'queued', 0, NULL, ?, ?, NULL)",
                (tid, _REPO, datetime.now(timezone.utc).isoformat(), seq))
            db.commit()
            service._fail_task_pool_unreachable(c, tid, [123], pool_tag="coding")
        with db_lock:
            c = db.cursor()
            trow = c.execute(
                "SELECT status, exit_code, errors FROM agent_tasks WHERE id=?", (tid,)).fetchone()
            erow = c.execute(
                "SELECT status, exit_code, completed_at, result FROM events WHERE seq=?",
                (seq,)).fetchone()
        assert trow[0] == "failed", f"任务应 failed，实际 {trow[0]}"
        assert trow[1] == 1, f"任务 exit_code 应 1，实际 {trow[1]}"
        assert erow[0] == "failed", f"事件应 failed，实际 {erow[0]}"
        assert erow[1] == 1, f"事件 exit_code 应 1，实际 {erow[1]}"
        assert erow[2], f"事件 completed_at 应非空，实际 {erow[2]!r}"
        assert erow[3] and "不可达" in erow[3], f"事件 result 应含判死原因，实际 {erow[3]!r}"
