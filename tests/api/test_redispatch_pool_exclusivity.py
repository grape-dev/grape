"""重派（redispatch）池列三列互斥回归（Issue #1208 补漏，2026-11-18）。

背景：`agent_tasks` 的 `pool_selector` 与 `pool_engine_ids/uids` 三列语义互斥，重派路径此前
「按当前场景重读 id 池」+「原样透传旧任务 pool_selector」，场景改过模式后两者不同源：

  · selector → id/uid 池：新 id 池 + 旧 selector 并存 → claim 第 ③ 级 selector 守卫排除池内
    引擎、Issue #605 池过滤排除池外引擎，无人能认领（实测 t_13f1ac8d9239 / 事件 77063）；
  · id/uid 池 → selector：场景 id 池空、旧快照 selector 也空 → 两列皆空，圈池退化成广播
    （E-04 遗留项在 aw11-8070 观测到的形态）。

覆盖三层：
  1. `resolve_redispatch_pool` 纯函数矩阵（场景权威 / 场景读不到 / 快照自脏 / fail-closed）；
  2. `_redispatch_pool_columns` 粘合层：两方向都以场景现行配置为准，快照只在无场景时兜底；
  3. 落到真实写入闸门：解析结果经 `enqueue_task` 落库后「恰好一列非空」。
"""
import json
import sqlite3
import threading

import pytest

import src.api.db_setup as db_setup_mod
import src.domains.mgmt.user.db as user_db_mod
import src.domains.runtime.engine.engine_select as engine_select_mod
import src.domains.runtime.engine.service as service_mod
from src.api.db_setup import init_database
from src.domains.runtime.engine.engine_select import resolve_redispatch_pool
from src.domains.runtime.routes import events as events_route


# ===== 1) 纯函数矩阵 =====

def test_scene_selector_mode_clears_id_pool():
    """场景现行 = selector 模式 → id/uid 池必须清空（禁止双池并存）。"""
    ids, sel = resolve_redispatch_pool(
        scene_authoritative=True, scene_engine_ids="[]",
        scene_selector={"tags": ["scope:global"]},
        snapshot_engine_ids="[17382]", snapshot_selector="")
    assert ids == "", "selector 模式下 id 池必须为空，否则 claim 双闸门互斥"
    assert json.loads(sel) == {"tags": ["scope:global"]}


def test_scene_engine_list_mode_clears_stale_selector():
    """场景现行 = id/uid 池 → 旧快照 selector 必须丢弃。"""
    ids, sel = resolve_redispatch_pool(
        scene_authoritative=True, scene_engine_ids="[17382]",
        scene_selector=None,
        snapshot_engine_ids="", snapshot_selector=json.dumps({"tags": ["scope:global"]}))
    assert ids == "[17382]"
    assert sel == "", "id 池模式下不得残留快照 selector"


def test_scene_unreadable_falls_back_to_snapshot():
    """场景读不到 → 回落快照（原行为），且快照自身归一。"""
    ids, sel = resolve_redispatch_pool(
        scene_authoritative=False, scene_engine_ids="", scene_selector=None,
        snapshot_engine_ids="", snapshot_selector=json.dumps({"tags": ["scope:repo"]}))
    assert ids == ""
    assert json.loads(sel) == {"tags": ["scope:repo"]}


def test_snapshot_with_both_pools_collapses_to_ids():
    """快照自身带脏双列（历史 bug 产物）→ id 池优先，与 enqueue_task 收口同尺。"""
    ids, sel = resolve_redispatch_pool(
        scene_authoritative=False, scene_engine_ids="", scene_selector=None,
        snapshot_engine_ids="[17382]", snapshot_selector=json.dumps({"tags": ["scope:global"]}))
    assert ids == "[17382]"
    assert sel == ""


def test_scene_all_offline_keeps_fail_closed():
    """场景绑定全灭（None）→ 原样透传 None，由调用方走 #259 报错，不得降级广播。"""
    ids, sel = resolve_redispatch_pool(
        scene_authoritative=True, scene_engine_ids=None,
        scene_selector=None, snapshot_engine_ids="[1]", snapshot_selector="")
    assert ids is None
    assert sel == ""


def test_empty_list_placeholder_is_not_a_pool():
    """`"[]"` 占位不算非空 id 池：快照轨迹下 selector 应保留。"""
    ids, sel = resolve_redispatch_pool(
        scene_authoritative=False, scene_engine_ids="", scene_selector=None,
        snapshot_engine_ids="[]", snapshot_selector=json.dumps({"tags": ["scope:repo"]}))
    assert ids == "[]"
    assert json.loads(sel) == {"tags": ["scope:repo"]}


# ===== 2) 粘合层：场景为准，快照只在无场景时兜底 =====

OLD_ID_MODE = {"repo_key": "gitcode:o/r", "issue_id": "#12", "agent_type": "qa",
               "pool_engine_ids": "[17382]", "pool_selector": ""}
OLD_SELECTOR_MODE = {"repo_key": "gitcode:o/r", "issue_id": "#12", "agent_type": "qa",
                     "pool_engine_ids": "", "pool_selector": json.dumps({"tags": ["scope:global"]})}


def _patch_scene(monkeypatch, scene_id, ids, selector):
    monkeypatch.setattr("src.api.session_store.get_issue_scene",
                        lambda repo_key, issue_id: (scene_id, 0))
    monkeypatch.setattr(engine_select_mod, "get_scene_pool_engine_ids",
                        lambda *a, **kw: ids)
    monkeypatch.setattr(engine_select_mod, "get_scene_engine_selector",
                        lambda *a, **kw: selector)


def test_columns_selector_mode_after_engine_binding(monkeypatch):
    """selector → id 池方向：快照还带旧 selector，场景已切成 id 池 → 只留 id 池。"""
    _patch_scene(monkeypatch, "sc-qa", ids="[17382]", selector=None)
    ids, sel, scene, _uid, auth = events_route._redispatch_pool_columns(OLD_SELECTOR_MODE)
    assert (scene, auth) == ("sc-qa", True)
    assert ids == "[17382]", "场景现行 id 池必须生效"
    assert sel == "", "旧快照 selector 必须被丢弃（否则双闸门互斥、无人认领）"


def test_columns_selector_mode_after_hot_switch(monkeypatch):
    """id 池 → selector 方向（E-04 场景）：快照 selector 为空，场景已按标签 → 只留 selector。"""
    _patch_scene(monkeypatch, "sc-qa", ids="", selector={"tags": ["scope:global"]})
    ids, sel, _scene, _uid, _auth = events_route._redispatch_pool_columns(OLD_ID_MODE)
    assert ids == "", "切到 selector 模式后 id 池必须清空"
    assert json.loads(sel) == {"tags": ["scope:global"]}, (
        "必须读场景现行 selector，不能沿用空快照（否则两列皆空退化成广播）")


def test_columns_without_scene_use_snapshot(monkeypatch):
    """无场景归属 → 仍按任务快照重派（原行为不回归）。"""
    _patch_scene(monkeypatch, "", "", None)
    ids, sel, scene, _uid, auth = events_route._redispatch_pool_columns(OLD_SELECTOR_MODE)
    assert (scene, auth) == ("", False)
    assert ids == ""
    assert json.loads(sel) == {"tags": ["scope:global"]}


def test_columns_scene_read_crash_falls_back(monkeypatch):
    """场景读取抛错 → 回落快照，不阻断重派。"""
    monkeypatch.setattr("src.api.session_store.get_issue_scene",
                        lambda repo_key, issue_id: ("sc-qa", 0))

    def _boom(*a, **kw):
        raise RuntimeError("scene query failed")

    monkeypatch.setattr(engine_select_mod, "get_scene_pool_engine_ids", _boom)
    ids, sel, _scene, _uid, auth = events_route._redispatch_pool_columns(OLD_SELECTOR_MODE)
    assert auth is False, "场景读失败必须标成非权威，交给快照兜底"
    assert ids == ""
    assert json.loads(sel) == {"tags": ["scope:global"]}


# ===== 3) 落库闸门：解析结果经 enqueue_task 写到 agent_tasks 后恰好一列非空 =====

@pytest.fixture()
def env(tmp_path, monkeypatch):
    import src.session_manager as sm

    db_path = str(tmp_path / "redispatch_pool.db")
    monkeypatch.setenv("DEVOPS_DB_BACKEND", "sqlite")
    for _k in ("DEVOPS_DB_HOST", "DEVOPS_DB_PORT", "DEVOPS_DB_USER",
               "DEVOPS_DB_PASSWORD", "DEVOPS_DB_NAME"):
        monkeypatch.delenv(_k, raising=False)
    monkeypatch.setenv("DEVOPS_DB_PATH", db_path)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    lock = threading.RLock()
    for mod in (sm, db_setup_mod, service_mod, user_db_mod):
        monkeypatch.setattr(mod, "db", conn, raising=True)
        monkeypatch.setattr(mod, "db_lock", lock, raising=True)
    init_database()
    yield conn
    conn.close()


def _row(conn, task_id):
    return conn.execute(
        "SELECT pool_engine_ids, pool_engine_uids, pool_selector FROM agent_tasks WHERE id=?",
        (task_id,)).fetchone()


def test_switched_to_selector_writes_only_selector(env, monkeypatch):
    _patch_scene(monkeypatch, "sc-qa", ids="", selector={"tags": ["scope:global"]})
    ids, sel, _scene, _uid, _auth = events_route._redispatch_pool_columns(OLD_ID_MODE)
    tid = service_mod.enqueue_task(
        agent_session_id="sess-rd", runtime_id=0, prompt="redispatch",
        repo_key="gitcode:o/r", agent_type="qa", pool_agent_type="qa", pool_tag="qa",
        pool_engine_ids=ids, pool_selector=sel)
    row_ids, row_uids, row_sel = _row(env, tid)
    assert (row_ids or "") == ""
    assert (row_uids or "") == ""
    assert json.loads(row_sel) == {"tags": ["scope:global"]}


def test_switched_to_engine_list_writes_only_ids(env, monkeypatch):
    _patch_scene(monkeypatch, "sc-qa", ids="[17382]", selector=None)
    ids, sel = events_route._redispatch_pool_columns(OLD_SELECTOR_MODE)[:2]
    tid = service_mod.enqueue_task(
        agent_session_id="sess-rd", runtime_id=0, prompt="redispatch",
        repo_key="gitcode:o/r", agent_type="qa", pool_agent_type="qa", pool_tag="qa",
        pool_engine_ids=ids, pool_selector=sel)
    row_ids, row_uids, row_sel = _row(env, tid)
    assert row_ids == "[17382]"
    assert (row_sel or "") == "", "id 池轨不得残留 selector（claim 双闸门互斥）"


# ===== 4) 接线守卫：非本机重派必须用解析结果，不得回到「快照原样透传」 =====

def test_redispatch_enqueue_uses_resolved_columns():
    """源码级接线断言：终态重建入队不得再直接透传快照池列。

    上两层测的是解析函数；这里钉住「路由真的用了它」——否则函数再对，路径回到
    `pool_selector=old.get("pool_selector")` 就又是同一个 bug（曾经发生过一次）。

    local_only「本机重试」分支例外：它的语义就是钉住原引擎，快照池列与钉住的
    runtime_id 必须同源，故保留一处快照透传（双池互斥由 enqueue_task 收口兜底）。
    """
    src_path = __import__("pathlib").Path(events_route.__file__)
    src = src_path.read_text(encoding="utf-8")
    assert "pool_selector=_rp_sel_253" in src, "终态重建入队未使用解析后的 selector"
    assert "pool_engine_ids=_rp_ids_253" in src, "终态重建入队未使用解析后的 id 池"
    assert "_redispatch_pool_columns(old)" in src, (
        "终态重建分支未按场景成对解析池列")
    snapshot_passthrough = 'pool_selector=old.get("pool_selector")'
    assert src.count(snapshot_passthrough) == 1, (
        "快照透传 selector 只允许留在 local_only「本机重试」分支，其余路径必须走解析")
    assert src.find(snapshot_passthrough) < src.find("pool_selector=_rp_sel_253"), (
        "剩下的快照透传必须位于终态重建解析之前（即 local_only 分支）")
