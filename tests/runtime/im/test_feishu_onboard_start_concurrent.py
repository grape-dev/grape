"""飞书扫码 /start 的并发回归（PR !1929 架构守护 N1）。

`start_onboard` 必须保持 async def（要 create_task 起扫码协程），但原来的
「并发闸门」与「任务落库」是两次独立 `asyncio.to_thread` —— 两个 await 之间
是事件循环的调度点，两个并发 /start 都能在闸门的 SELECT 里读到空结果、
然后各自 INSERT，DB 出现两条 pending，违背端点注释声明的单任务约束。

本用例用「在闸门返回后 sleep」把那个窗口放大成确定性的：修复后闸门与
INSERT 在同一把 db_lock 内，sleep 发生在锁内 → 第二个请求被挡住 → 409；
若回退成两次 to_thread，sleep 落在两个 await 之间 → 两个请求都成功 → 断言失败。
"""
import asyncio
import sqlite3
import time
import uuid

import pytest

_DDL = """
CREATE TABLE IF NOT EXISTS feishu_onboard_task (
    task_id TEXT PRIMARY KEY,
    bot_type TEXT,
    repo_key TEXT,
    user_id TEXT,
    status TEXT,
    app_id TEXT,
    app_secret TEXT,
    qr_url TEXT,
    expire_in INTEGER,
    error TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    finished_at TEXT
);
"""


@pytest.fixture()
def onboard_db(tmp_path, monkeypatch):
    path = str(tmp_path / "onboard.db")
    monkeypatch.setenv("DEVOPS_DB_PATH", path)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_DDL)
    conn.commit()
    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", conn, raising=False)
    yield conn
    conn.close()


def _pending_count(conn, user_id) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM feishu_onboard_task WHERE user_id=? AND status='pending'",
        (user_id,),
    ).fetchone()[0]


def test_concurrent_start_onboard_keeps_single_pending_task(onboard_db, monkeypatch):
    from fastapi import HTTPException

    from src.domains.mgmt.routes import feishu_onboard as fo

    async def _noop(*a, **k):        # 不真的去飞书建应用
        return None

    monkeypatch.setattr(fo, "_run_registration", _noop)

    real_guard = fo._guard_active_onboard

    def slow_guard(user_id):
        real_guard(user_id)          # 真查重
        time.sleep(0.05)             # 放大「闸门返回 → INSERT」之间的窗口

    monkeypatch.setattr(fo, "_guard_active_onboard", slow_guard)

    body = fo.StartBody(bot_type="chat_bot", repo_key="")
    user = {"id": 4321, "username": "race"}

    async def race():
        return await asyncio.gather(
            fo.start_onboard(body, user),
            fo.start_onboard(body, user),
            return_exceptions=True,
        )

    results = asyncio.run(race())
    ok = [r for r in results if isinstance(r, dict)]
    conflicts = [r for r in results
                 if isinstance(r, HTTPException) and r.status_code == 409]

    assert len(ok) == 1, f"并发 /start 应当只有一个成功，实际 {len(ok)}：{results}"
    assert len(conflicts) == 1, f"另一个请求应拿到 409，实际 {results}"
    assert _pending_count(onboard_db, user["id"]) == 1, "DB 里只能有一条 pending 扫码任务"
    assert uuid.UUID(ok[0]["task_id"]).hex == ok[0]["task_id"]   # 返回值形状没变