"""双池互斥回归：agent_tasks 不得同时带 id/uid 池与 selector（Issue #1208 补漏）。

背景：重派（redispatch）终态重建路径会「重读场景 id 池 + 原样透传旧任务 pool_selector」，
两条派发路径各自成立、拼在一条任务行上就变成死锁：
  - claim 第③级 selector 守卫（_selector_guard_blocks）把池内引擎判为不匹配 → skip；
  - claim 池过滤（Issue #605）把池外引擎判为不在名单 → skip。
两把尺子同时生效 = 没有任何引擎能认领，事件长挂 running（实测 t_13f1ac8d9239：
池=[17382(scope:repo)] + 选择器 {"tags":["scope:global"]}，每 4s 两条 skip 日志）。

修法口径：三列语义互斥（selector 轨恒空 id 池），enqueue_task 作为唯一落库口收口——
id 池非空即视为最新解析结果，丢弃与之冲突的 selector 并留 WARNING。
"""
import json
import sqlite3
import threading

import pytest

import src.api.db_setup as db_setup_mod
import src.domains.mgmt.user.db as user_db_mod
import src.domains.runtime.engine.service as service_mod
from src.api.db_setup import init_database


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """真 init_database 建库（全表），db/db_lock 统一指向测试库（对齐 test_issue_1011_integration）。"""
    import src.session_manager as sm

    db_path = str(tmp_path / "exclusivity.db")
    monkeypatch.setenv("DEVOPS_DB_BACKEND", "sqlite")
    for _k in ("DEVOPS_DB_HOST", "DEVOPS_DB_PORT", "DEVOPS_DB_USER",
               "DEVOPS_DB_PASSWORD", "DEVOPS_DB_NAME"):
        monkeypatch.delenv(_k, raising=False)
    monkeypatch.setenv("DEVOPS_DB_PATH", db_path)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    lock = threading.RLock()
    monkeypatch.setattr(sm, "db", conn, raising=True)
    monkeypatch.setattr(sm, "db_lock", lock, raising=True)
    monkeypatch.setattr(db_setup_mod, "db", conn, raising=True)
    monkeypatch.setattr(db_setup_mod, "db_lock", lock, raising=True)
    monkeypatch.setattr(service_mod, "db", conn, raising=True)
    monkeypatch.setattr(service_mod, "db_lock", lock, raising=True)
    monkeypatch.setattr(user_db_mod, "db", conn, raising=True)
    monkeypatch.setattr(user_db_mod, "db_lock", lock, raising=True)
    init_database()
    yield conn
    conn.close()


def _enqueue(pool_engine_ids="", pool_selector=""):
    return service_mod.enqueue_task(
        agent_session_id="sess-excl", runtime_id=0, prompt="exclusivity",
        repo_key="gitcode:x/y", agent_type="qa", skill="",
        pool_agent_type="qa", pool_tag="qa",
        pool_engine_ids=pool_engine_ids, pool_selector=pool_selector)


def _row(conn, task_id):
    return conn.execute(
        "SELECT pool_engine_ids, pool_selector FROM agent_tasks WHERE id=?",
        (task_id,)).fetchone()


def test_both_pools_collapse_to_id_pool(env):
    """两个池同时传入 → 保留 id 池、丢弃 selector（否则 claim 双闸门互斥死锁）。"""
    tid = _enqueue(pool_engine_ids="[17382]",
                   pool_selector=json.dumps({"tags": ["scope:global"]}))
    ids, sel = _row(env, tid)
    assert ids == "[17382]"
    assert sel == "", "id 池非空时必须丢弃残留 selector（双闸门互斥）"


def test_pure_selector_untouched(env):
    """selector 轨正常形态（id 池为空）行为零变化。"""
    sel_in = json.dumps({"tags": ["scope:repo"]})
    tid = _enqueue(pool_engine_ids="", pool_selector=sel_in)
    ids, sel = _row(env, tid)
    assert (ids or "") == ""
    assert json.loads(sel) == {"tags": ["scope:repo"]}


def test_empty_pool_placeholders_do_not_trigger_drop(env):
    """id 池为 "[]"/空串的占位形态不算「非空」，selector 仍保留。"""
    sel_in = json.dumps({"tags": ["scope:repo"]})
    tid = _enqueue(pool_engine_ids="[]", pool_selector=sel_in)
    ids, sel = _row(env, tid)
    assert (ids or "") == "[]"
    assert json.loads(sel) == {"tags": ["scope:repo"]}
