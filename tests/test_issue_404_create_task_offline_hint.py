"""Issue #404（重做版）：需求中心事前拦截——场景引擎全灭提示。

用户思路：不做事后告警/重派链路，在创建需求的场景下拉框里直接提示
「场景绑定的引擎全部离线」，引导用户去换绑。

Issue #501 迁移：探测从 /scenes/with-pipeline（N 场景 × M 节点，拉取即全量预探测）
移到按需接口 GET /scenes/{scene_id}/engine-status（用户选中场景时 1 × M）。
探测语义与原 with-pipeline 内联版完全一致，本用例集改为锚定新接口。

验收：
1. /scenes/{scene_id}/engine-status 对引擎全灭的场景返回 all_offline=True + offline_nodes
2. 部分离线（非全部）不在 offline_nodes 里（只有该节点全灭才标记）
3. 无引擎绑定的节点不参与判定
4. enabled=False 的节点跳过
5. 校验异常降级为不标记（不挡创建入口）
6. SRE 空绑定 → sre_unbound=True（「未选择机器」话术，区别于引擎全灭）
7. 场景不存在 → all_offline=False（不拦创建入口）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = "gitcode:foo/bar"


def _patch_scenes_module(monkeypatch, cfgs, pool_results):
    """patch get_pipeline_config / engine_select 判定函数。

    engine-status（Issue #501 后）经 get_pipeline_config 取节点
    （DB-first 单行查询），判定函数在函数体内 import，须 patch 源模块
    src.api.pipeline_config / src.runtime.engine_select。
    cfgs: {scene_id: {"name":..., "nodes": [...]} 或 None(不存在)}
    """
    import src.api.pipeline_config as pc
    import src.runtime.engine_select as es

    monkeypatch.setattr(pc, "get_pipeline_config",
                        lambda scene_id, repo_key="": cfgs.get(scene_id))

    def _fake_pool(scene_id, agent_type, repo_key="", user_id=0, issue_creator=""):
        return pool_results.get((scene_id, agent_type), "")

    monkeypatch.setattr(es, "get_scene_pool_engine_ids", _fake_pool)


def _scene(sid, nodes):
    return {"name": sid, "nodes": nodes}


def _engine_status(monkeypatch, nodes, pool_results, scene_id):
    _patch_scenes_module(monkeypatch, {scene_id: _scene(scene_id, nodes)},
                         pool_results)
    import src.api.routes.scenes as scenes_mod
    return scenes_mod.get_scene_engine_status(
        scene_id, user={"auth_type": "session_token"}, repo=REPO)


def test_all_offline_scene_flagged(monkeypatch):
    nodes = [
        {"tag": "coding", "skill": "s1", "engine_runtime_ids": [1, 2]},
        {"tag": "qa", "skill": "s2", "engine_runtime_ids": [3]},
    ]
    # coding/qa 两个节点全灭
    st = _engine_status(
        monkeypatch, nodes,
        {("scene_dead", "coding"): None, ("scene_dead", "qa"): None},
        "scene_dead")
    assert st["all_offline"] is True
    assert st["offline_nodes"] == ["coding", "qa"]
    assert st["scene_id"] == "scene_dead"


def test_partial_offline_node_not_flagged_when_pool_ok(monkeypatch):
    """节点 pool 有健康引擎（返回 JSON 串）→ 不进 offline_nodes。"""
    nodes = [{"tag": "coding", "skill": "s1", "engine_runtime_ids": [1]}]
    st = _engine_status(monkeypatch, nodes,
                        {("scene_ok", "coding"): "[1]"}, "scene_ok")
    assert st["all_offline"] is False
    assert st["offline_nodes"] == []


def test_no_engine_binding_node_skipped(monkeypatch):
    """无 engine_runtime_ids 的节点（全量广播语义）不参与判定。"""
    nodes = [{"tag": "coding", "skill": "s1"}]
    st = _engine_status(monkeypatch, nodes, {}, "scene_nb")
    assert st["all_offline"] is False
    assert st["offline_nodes"] == []


def test_disabled_node_skipped(monkeypatch):
    """enabled=False 的节点跳过校验。"""
    nodes = [{"tag": "coding", "skill": "s1", "engine_runtime_ids": [1],
              "enabled": False}]
    st = _engine_status(monkeypatch, nodes,
                        {("scene_dis", "coding"): None}, "scene_dis")
    assert st["all_offline"] is False


def test_sre_unbound_flagged(monkeypatch):
    """SRE 节点未选机器（无 engine_runtime_ids）→ sre_unbound=True、all_offline=True。"""
    nodes = [
        # SRE 空绑定：无 engine_runtime_ids（SRE 不支持广播 → 无引擎）
        {"tag": "sre", "skill": "s3"},
        # coding 正常
        {"tag": "coding", "skill": "s1", "engine_runtime_ids": [1]},
    ]
    st = _engine_status(monkeypatch, nodes,
                        {("scene_sre", "coding"): "[1]"}, "scene_sre")
    assert st["sre_unbound"] is True
    assert st["all_offline"] is True
    assert st["offline_nodes"] == []  # coding 健康，不在离线列表


def test_pool_check_exception_degrades(monkeypatch):
    """判定函数抛异常 → 降级为不标记，不挡创建入口。"""
    import src.runtime.engine_select as es

    def _boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(es, "get_scene_pool_engine_ids", _boom)
    import src.api.pipeline_config as pc
    import src.api.routes.scenes as scenes_mod
    monkeypatch.setattr(pc, "get_pipeline_config", lambda scene_id, repo_key="": {
        "name": "scene_err",
        "nodes": [{"tag": "coding", "skill": "s1", "engine_runtime_ids": [1]}]})
    st = scenes_mod.get_scene_engine_status(
        "scene_err", user={"auth_type": "session_token"}, repo=REPO)
    assert st["all_offline"] is False
    assert st["offline_nodes"] == []


def test_unknown_scene_no_warning(monkeypatch):
    """场景不存在（DB 与 fallback 均查不到）→ all_offline=False，不拦创建入口。"""
    _patch_scenes_module(monkeypatch, {}, {})
    import src.api.routes.scenes as scenes_mod
    st = scenes_mod.get_scene_engine_status(
        "scene_missing", user={"auth_type": "session_token"}, repo=REPO)
    assert st["all_offline"] is False
