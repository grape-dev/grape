# -*- coding: utf-8 -*-
"""确定性流水线推进兜底：不以技能/收尾钩子为真源。"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest

import src.domains.runtime.engine.pipeline_reconcile as pr
import src.domains.runtime.engine.service as engine_service
import src.domains.runtime.public as runtime_public


def _task(agent="coding", completed_at=None, issue_id="#9"):
    return {
        "id": "t_reconcile",
        "repo_key": "gitcode:acme/svc",
        "issue_id": issue_id,
        "agent_type": agent,
        "event_seq": 321,
        "completed_at": completed_at or datetime.now(timezone.utc).isoformat(),
    }


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(pr, "_last_attempt", {})


def _patch(monkeypatch, *, task, state, inflight=False, result=None):
    calls = []
    monkeypatch.setattr(pr, "_candidate_tasks", lambda limit: [task])
    monkeypatch.setattr(pr, "_issue_state", lambda repo, num: dict(state))
    monkeypatch.setattr(runtime_public, "has_inflight",
                        lambda repo, issue: inflight)
    monkeypatch.setattr(
        engine_service, "run_auto_advance_hook",
        lambda *a, **kw: calls.append((a, kw)) or (result or {"action": "none"}))
    return calls


def test_advances_completed_task_whose_stage_did_not_move(monkeypatch):
    calls = _patch(
        monkeypatch, task=_task("coding"),
        state={"status": "processing", "stage": "coding"},
        result={"action": "a2a", "next_role": "qa", "seq": 9001})

    stats = pr.reconcile_pipeline_progress()

    assert stats["advanced"] == 1
    assert stats["actions"][0]["next"] == "qa"
    assert len(calls) == 1
    args = calls[0][0]
    assert args[:5] == ("t_reconcile", "gitcode:acme/svc", "#9", "coding", 321)


def test_skips_when_stage_already_advanced(monkeypatch):
    calls = _patch(
        monkeypatch, task=_task("coding"),
        state={"status": "processing", "stage": "qa"})

    stats = pr.reconcile_pipeline_progress()

    assert stats["advanced"] == 0
    assert calls == []


def test_skips_when_downstream_inflight(monkeypatch):
    calls = _patch(
        monkeypatch, task=_task("coding"),
        state={"status": "processing", "stage": "coding"}, inflight=True)

    stats = pr.reconcile_pipeline_progress()

    assert stats["advanced"] == 0
    assert calls == []


def test_skips_issue_already_pending_review(monkeypatch):
    calls = _patch(
        monkeypatch, task=_task("qa"),
        state={"status": "pending_review", "stage": "qa"})

    stats = pr.reconcile_pipeline_progress()

    assert stats["advanced"] == 0
    assert calls == []


def test_skips_stale_completed_task(monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
    calls = _patch(
        monkeypatch, task=_task("coding", completed_at=old),
        state={"status": "processing", "stage": "coding"})

    stats = pr.reconcile_pipeline_progress(lookback_hours=6)

    assert stats["advanced"] == 0
    assert calls == []


def test_skips_non_issue_and_unparseable_timestamp(monkeypatch):
    calls = _patch(
        monkeypatch, task=_task("coding", completed_at="not-a-time"),
        state={"status": "processing", "stage": "coding"})

    stats = pr.reconcile_pipeline_progress()

    assert stats["advanced"] == 0
    assert calls == []


def test_reconcile_reads_real_db_rows(monkeypatch):
    """真表谓词：只看 completed 任务 + issue.stage 仍等于该 agent 的行。"""
    import sqlite3
    import threading

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript("""
    CREATE TABLE agent_tasks (
        id TEXT PRIMARY KEY, repo_key TEXT, issue_id TEXT, agent_type TEXT,
        event_seq INTEGER, status TEXT, completed_at TEXT
    );
    CREATE TABLE issue (
        id INTEGER PRIMARY KEY, repo_key TEXT, issue_number INTEGER,
        status TEXT, stage TEXT
    );
    """)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO issue (repo_key, issue_number, status, stage) "
                 "VALUES ('gitcode:acme/svc', 9, 'processing', 'coding')")
    conn.execute("INSERT INTO agent_tasks VALUES "
                 "('t_db', 'gitcode:acme/svc', '#9', 'coding', 7, 'completed', ?)",
                 (now,))
    conn.commit()

    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", conn, raising=False)
    monkeypatch.setattr(sm, "db_lock", threading.RLock(), raising=False)

    # 读侧走免锁 bulk_read 通道；测试内存库需把 reader 指向同一连接。
    import src.kernel.bulk_read as bulk_read
    from contextlib import contextmanager

    @contextmanager
    def _reader(domain=""):
        cur = conn.cursor()
        try:
            yield cur
        finally:
            cur.close()

    monkeypatch.setattr(bulk_read, "reader", _reader)

    calls = []
    monkeypatch.setattr(runtime_public, "has_inflight", lambda *a: False)
    monkeypatch.setattr(engine_service, "run_auto_advance_hook",
                        lambda *a, **kw: calls.append(a) or {"action": "a2a",
                                                             "next_role": "qa"})

    stats = pr.reconcile_pipeline_progress()

    assert stats["scanned"] == 1
    assert stats["advanced"] == 1
    assert calls[0][:4] == ("t_db", "gitcode:acme/svc", "#9", "coding")


def test_retry_gate_limits_attempt_frequency(monkeypatch):
    calls = _patch(
        monkeypatch, task=_task("coding"),
        state={"status": "processing", "stage": "coding"},
        result={"action": "none"})

    pr.reconcile_pipeline_progress()
    pr.reconcile_pipeline_progress()

    # 首轮尝试一次；第二轮被 10 分钟重试闸挡住，避免每 5 分钟刷日志
    assert len(calls) == 1