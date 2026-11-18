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


# ===== 4) 接线守卫：真实调用路径的静态核查（不用字符串匹配） =====
#
# 教训：上一版守卫用 `assert "pool_engine_ids=_rp_ids_253" in src` 做「接线断言」，
# 结果把一处未定义变量（NameError）当成期望值钉死，16 passed 全是恒绿摆设。
# 现在改成 AST 级核查：解析 events.py，收集函数内真实绑定的名字（赋值/for/with/except/
# 推导式/参数），再检查 `redispatch_event` 里所有被读取的局部名是否都有绑定来源。

import ast
import builtins


def _bound_names(fn):
    """函数内所有「有绑定来源」的名字（保守：任一位置赋值即视为已绑定）。"""
    names = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            for a in getattr(node.args, "args", []) if hasattr(node, "args") else []:
                names.add(a.arg)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.Global) or isinstance(node, ast.Nonlocal):
            names.update(node.names)
    return names


def _loaded_names(fn):
    return {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def test_redispatch_route_has_no_unbound_locals():
    """`redispatch_event` 内每个被读取的名字都必须有绑定来源（否则运行时 NameError）。

    这是回归守卫：曾把 `_rp_ids_253`（从未赋值）当期望值钉进字符串断言，导致终端重建
    分支必抛 NameError 而 16 个用例全绿。
    """
    src_path = __import__("pathlib").Path(events_route.__file__)
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    module_names = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                module_names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            module_names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    module_names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            module_names.add(node.target.id)
    fns = [n for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "redispatch_event"]
    assert fns, "events.py 里找不到 redispatch_event —— 路由被重命名了，守卫要同步"
    fn = fns[0]
    known = _bound_names(fn) | module_names | set(dir(builtins)) | {"__file__", "__name__"}
    missing = sorted(_loaded_names(fn) - known)
    assert not missing, f"redispatch_event 里存在未绑定的局部名（运行时 NameError）: {missing}"


def test_redispatch_enqueue_passes_named_columns():
    """`redispatch_event` 的每个 enqueue_task 都必须用「有绑定的局部名」传池两列。

    这样既堵住未定义变量，也堵住回退成内联 `old.get(...)` 快照透传（曾经的真实缺陷形态）。
    """
    src_path = __import__("pathlib").Path(events_route.__file__)
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    fn = [n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef) and n.name == "redispatch_event"][0]
    known = _bound_names(fn)
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "enqueue_task"]
    assert len(calls) >= 2, f"redispatch_event 里应有两处 enqueue_task（本机重试 + 终态重建），实际 {len(calls)}"
    for i, call in enumerate(calls):
        kws = {kw.arg: kw.value for kw in call.keywords if kw.arg}
        for key in ("pool_selector", "pool_engine_ids"):
            assert key in kws, f"第 {i+1} 处 enqueue_task 缺少 {key} 实参"
            val = kws[key]
            assert isinstance(val, ast.Name), (
                f"第 {i+1} 处 enqueue_task 的 {key} 是 {type(val).__name__}，"
                f"必须传解析后的局部名（禁止内联快照透传）")
            assert val.id in known, (
                f"第 {i+1} 处 enqueue_task 的 {key}={val.id} 在作用域内没有绑定来源（NameError）")
