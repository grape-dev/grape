"""Issue #1011 完整派发链集成测试（不 mock service 层——验收 17 的固化）。

模块级 db 绑定的 patch 清单（缺一不可，漏 patch 的模块会把数据写去别库）：
rt / service / db_setup / user/db（engine_ref_snapshot 快照双写）/ session_manager（health_scorer._db 延迟引用）。

与 test_issue_1011_claim_v2.py 的差别：那份是路由层契约测试（service 层打桩），
本文件走「路由 → uid 解析 → service 层 SQL → 任务状态机」的完整链路：
- 真 init_database schema（非手写子集表）
- 真 enqueue_task 入队（三键快照 engine_uid/engine_name_snapshot/pool_engine_uids 天然双写）
- 真 service.claim_by_tag / claim 的 SQL 认领（池过滤、healthy guard 真实执行）
- 健康分直接插 runtime_health_scores 表（绕开 5min 扫描 + 100s 心跳窗口的 wall-clock 依赖，
  见 #1011 E2E 检查报告 198418584 二.2 的时序坑）
- v2 路由函数直调（HTTP 层由 fastapi TestClient 层面等价，路由无业务逻辑）

覆盖（spec 验收 4/7/8/9 的集成面 + UC-C1 uid 池轨）：
- I-1  v2 claim-by-tag(uid) 完整链路领到任务：status=running + runtime_id 落定 + payload 双带
- I-2  v2 claim-by-tag 未知 uid → 404 UNKNOWN_ENGINE_UID（全链 fail-closed）
- I-3  uid 池轨等价守卫（UC-C1）：pool_engine_uids 非空时 uid 在池内放行（ids 镜像缺失不误杀）
- I-4  v1 并存：同任务 v1 claim-by-tag(id) 行为等价
- I-5  v2 claim(uid) 确定路由（显式 runtime_id 任务）完整链路
"""
import json
import sqlite3
import threading
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

import src.domains.runtime.routes.runtime as rt
import src.api.db_setup as db_setup_mod
import src.domains.runtime.engine.service as service_mod
import src.domains.mgmt.user.db as user_db_mod
from src.api.db_setup import init_database

UID_A = "aaaa1111-1111-5111-8111-111111111111"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """真 init_database 建库（全表），db/db_lock 三处（rt/service/health_scorer）统一指向。
    health_scorer 走 _db() 延迟 import src.session_manager.db——统一在 session_manager 层替换。"""
    import src.session_manager as sm

    db_path = str(tmp_path / "integration.db")
    # 对齐 conftest：强制 SQLite 后端（防 configs/db-backend.json 误连 MySQL）
    monkeypatch.setenv("DEVOPS_DB_BACKEND", "sqlite")
    for _k in ("DEVOPS_DB_HOST", "DEVOPS_DB_PORT", "DEVOPS_DB_USER", "DEVOPS_DB_PASSWORD", "DEVOPS_DB_NAME"):
        monkeypatch.delenv(_k, raising=False)
    monkeypatch.setenv("DEVOPS_DB_PATH", db_path)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    lock = threading.RLock()
    monkeypatch.setattr(sm, "db", conn, raising=True)
    monkeypatch.setattr(sm, "db_lock", lock, raising=True)
    # db_setup 顶层绑定了 session_manager.db——init_database 也要落到测试库
    monkeypatch.setattr(db_setup_mod, "db", conn, raising=True)
    monkeypatch.setattr(db_setup_mod, "db_lock", lock, raising=True)
    monkeypatch.setattr(service_mod, "db", conn, raising=True)
    monkeypatch.setattr(service_mod, "db_lock", lock, raising=True)
    monkeypatch.setattr(rt, "db", conn, raising=True)
    monkeypatch.setattr(rt, "db_lock", lock, raising=True)
    # user/db.py 顶层绑定 db（engine_ref_snapshot 快照双写用它）——同样落到测试库
    monkeypatch.setattr(user_db_mod, "db", conn, raising=True)
    monkeypatch.setattr(user_db_mod, "db_lock", lock, raising=True)
    init_database()
    yield conn
    conn.close()


def _seed_engine(conn, credential_id=1, name="engine-a", uid=UID_A, tag="all"):
    """真实形态引擎行 + 凭据 + capabilities（claim_by_tag 的 capabilities JOIN 需要）。"""
    conn.execute(
        "INSERT INTO runtime_credentials (id, token_hash, name, device_name, scope, status) "
        "VALUES (?, 'x', 'cred', 'dev', 'global', 'active')", (credential_id,))
    # last_seen_at 必须新鲜：heartbeat_alive（远端口径）= last_seen 距今 ≤100s，
    # 否则 health_scorer 强制 score=0 → #253 拒派（E2E 检查报告 198418584 二.2 的时序坑）
    # probe_status='ok'：arch 线幽灵引擎闸门（2026-11-06）要求探活通过才可认领
    conn.execute(
        "INSERT INTO agent_runtimes (id, credential_id, name, engine_uid, status, healthy, "
        "max_concurrent, agent_tags, last_seen_at, probe_status, worker_version) "
        "VALUES (?, ?, ?, ?, 'online', 1, 20, ?, ?, 'ok', 'v-test')",
        (2, credential_id, name, uid, json.dumps([tag]),
         datetime.now(timezone.utc).isoformat()))
    conn.execute(
        "INSERT INTO engine_capabilities (runtime_id, engine_uid, tag, enabled) "
        "VALUES (2, ?, ?, 1)", (uid, tag))
    # 健康分直接插正分（绕开 5min 扫描 + 心跳 100s 窗口的 wall-clock 依赖）
    conn.execute(
        "INSERT OR REPLACE INTO runtime_health_scores "
        "(runtime_id, health_score, total_tasks, failed_tasks, completed_tasks, "
        " consecutive_failures, failure_rate, last_status, window_days, updated_at, "
        " failed_tasks_effective, last_failure_at) "
        "VALUES (?, 100, 0, 0, 0, 0, 0.0, '', 7, ?, 0, '')",
        (2, datetime.now(timezone.utc).isoformat()))
    conn.commit()


def _enqueue_via_pipeline(conn, runtime_id, pool_ids, pool_uids):
    """真 enqueue_task 入队（三键快照双写），返回 task_id。"""
    task_id = service_mod.enqueue_task(
        agent_session_id="sess-int", runtime_id=runtime_id, prompt="integration-e2e",
        repo_key="gitcode:x/y", agent_type="coder", skill="",
        pool_agent_type="coder", pool_tag="all",
        pool_engine_ids=json.dumps(pool_ids), pool_engine_source="manual")
    # pool_engine_uids 镜像：enqueue_task 依据 pool_engine_ids 现算快照——校验 uid 轨也在
    row = conn.execute(
        "SELECT engine_uid, pool_engine_uids FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
    assert row and row[1], "enqueue_task 未双写 pool_engine_uids（#646 三键快照缺失）"
    assert json.loads(row[1]) == pool_uids, "pool_engine_uids 快照内容与入队 uid 池不一致"
    return task_id


def _reset_queued(conn, task_id):
    conn.execute("UPDATE agent_tasks SET status='queued', runtime_id=0 WHERE id=?", (task_id,))
    conn.commit()


# ---------- I-1: v2 claim-by-tag(uid) 完整链路领到任务 ----------

def test_i1_v2_claim_by_tag_full_chain(env):
    conn, = env,
    _seed_engine(conn)
    task_id = _enqueue_via_pipeline(conn, runtime_id=0, pool_ids=[2], pool_uids=[UID_A])

    # v2 路由直调（uid 寻址）→ 服务层真实认领
    r = rt.runtime_claim_by_tag_v2(UID_A, "all", None, credential_id=1)
    task = r.get("task") or {}
    assert task.get("id") == task_id, f"v2 claim-by-tag 未领到任务: {r}"
    # payload 池轨断言：pool_engine_uids（入队快照）与寻址 uid 一致；
    # engine_uid 快照对池任务为 None 是 #646 语义（入队时无确定引擎，快照在
    # 定向任务路径才有值——v1 池任务同行为，本用例不引入新语义）
    assert json.loads(task.get("pool_engine_uids") or "[]") == [UID_A]
    # 状态机真实落定
    row = conn.execute(
        "SELECT status, runtime_id FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
    assert row[0] == "running" and row[1] == 2


# ---------- I-2: 未知 uid 全链 fail-closed ----------

def test_i2_v2_claim_unknown_uid_404(env):
    conn, = env,
    _seed_engine(conn)
    task_id = _enqueue_via_pipeline(conn, runtime_id=0, pool_ids=[2], pool_uids=[UID_A])
    _reset_queued(conn, task_id)
    count_before = conn.execute("SELECT COUNT(*) FROM agent_tasks").fetchone()[0]
    with pytest.raises(HTTPException) as ei:
        rt.runtime_claim_by_tag_v2("ffffffff-0000-0000-0000-000000000000", "all", None, credential_id=1)
    assert ei.value.status_code == 404
    assert ei.value.detail.get("code") == "UNKNOWN_ENGINE_UID"
    # 任务未被误领/未造行
    row = conn.execute("SELECT status, runtime_id FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
    assert row[0] == "queued"
    assert conn.execute("SELECT COUNT(*) FROM agent_tasks").fetchone()[0] == count_before


# ---------- I-3: uid 池轨等价守卫（UC-C1）----------

def test_i3_uid_pool_track_equivalence(env):
    """pool_engine_uids 非空 + ids 镜像缺失（不同步）时，uid 在池内 → 放行不误杀。"""
    conn, = env,
    _seed_engine(conn)
    task_id = _enqueue_via_pipeline(conn, runtime_id=0, pool_ids=[2], pool_uids=[UID_A])
    # 模拟 ids 镜像缺失（同步失败窗口）：ids 清空但 uids 保留
    conn.execute("UPDATE agent_tasks SET pool_engine_ids='' WHERE id=?", (task_id,))
    conn.commit()
    r = rt.runtime_claim_by_tag_v2(UID_A, "all", None, credential_id=1)
    task = r.get("task") or {}
    assert task.get("id") == task_id, "uid 池轨等价守卫未放行（ids 镜像缺失被误杀）"


# ---------- I-4: v1 并存行为等价 ----------

def test_i4_v1_claim_by_tag_equivalent(env):
    conn, = env,
    _seed_engine(conn)
    task_id = _enqueue_via_pipeline(conn, runtime_id=0, pool_ids=[2], pool_uids=[UID_A])
    # v1 端点（id 寻址）领同一形态的新任务
    r = rt.runtime_claim_by_tag(2, "all", None, credential_id=1)  # v1 签名 (runtime_id, tag, body, credential_id)
    task = r.get("task") or {}
    assert task.get("id") == task_id, f"v1 claim-by-tag 未领到同任务（并存行为漂移）: {r}"
    row = conn.execute("SELECT status, runtime_id FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
    assert row[0] == "running" and row[1] == 2


# ---------- I-5: v2 claim(uid) 确定路由完整链路 ----------

def test_i5_v2_claim_direct_route(env):
    conn, = env,
    _seed_engine(conn)
    # 显式 runtime_id 任务（确定路由）——不带 pool（#653 拒绝空池）
    task_id = service_mod.enqueue_task(
        agent_session_id="sess-int", runtime_id=2, prompt="integration-e2e",
        repo_key="gitcode:x/y", agent_type="coder", skill="")
    r = rt.runtime_claim_v2(UID_A, None, credential_id=1)
    task = r.get("task") or {}
    assert task.get("id") == task_id, f"v2 claim 确定路由未领到任务: {r}"
    row = conn.execute("SELECT status, runtime_id FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
    assert row[0] == "running" and row[1] == 2
