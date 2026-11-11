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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

REPO = "gitcode:foo/bar"


def _patch_scenes_module(monkeypatch, cfgs, pool_results):
    """patch get_pipeline_config / engine_select 判定函数。

    get_scenes / get_pipeline_config 在路由函数体内 import，须 patch 源模块
    src.api.pipeline_config；engine_select 判定函数同样在函数体内 import，
    patch src.domains.runtime.engine.engine_select。
    """
    import src.api.pipeline_config as pc
    import src.domains.runtime.engine.engine_select as es

    monkeypatch.setattr(pc, "get_pipeline_config",
                        lambda scene_id, repo_key="": cfgs.get(scene_id))

    def _fake_pool(scene_id, agent_type, repo_key="", user_id=0, issue_creator=""):
        return pool_results.get((scene_id, agent_type), "")

    monkeypatch.setattr(es, "get_scene_pool_engine_ids", _fake_pool)


def _scene(sid, nodes):
    return {"name": sid, "nodes": nodes}

    import src.domains.mgmt.routes.scenes as scenes_mod
    res = scenes_mod.list_scenes_with_pipeline(repo="gitcode:foo/bar")
    s = res["scenes"][0]
    assert s["all_offline"] is True
    assert s["offline_nodes"] == ["coding", "qa"]


def test_partial_offline_node_not_flagged_when_pool_ok(monkeypatch):
    """节点 pool 有健康引擎（返回 JSON 串）→ 不进 offline_nodes。"""
    scene_list = [{
        "id": "scene_ok", "name": "ok", "display_name": "ok",
        "description": "", "work_branch": "main", "created_by": "",
        "scene_type": "standard", "pr_subscription_enabled": 0, "linked_repos": [],
        "nodes": [{"tag": "coding", "skill": "s1", "engine_runtime_ids": [1]}],
    }]
    _patch_scenes_module(monkeypatch, scene_list,
                         {("scene_ok", "coding"): "[1]"})
    import src.domains.mgmt.routes.scenes as scenes_mod
    res = scenes_mod.list_scenes_with_pipeline(repo="gitcode:foo/bar")
    s = res["scenes"][0]
    assert s["all_offline"] is False
    assert s["offline_nodes"] == []


def test_no_engine_binding_node_skipped(monkeypatch):
    """无 engine_runtime_ids 的节点（全量广播语义）不参与判定。"""
    scene_list = [{
        "id": "scene_nb", "name": "nb", "display_name": "nb",
        "description": "", "work_branch": "main", "created_by": "",
        "scene_type": "standard", "pr_subscription_enabled": 0, "linked_repos": [],
        "nodes": [{"tag": "coding", "skill": "s1"}],
    }]
    _patch_scenes_module(monkeypatch, scene_list, {})
    import src.domains.mgmt.routes.scenes as scenes_mod
    res = scenes_mod.list_scenes_with_pipeline(repo="gitcode:foo/bar")
    s = res["scenes"][0]
    assert s["all_offline"] is False
    assert s["offline_nodes"] == []


def test_disabled_node_skipped(monkeypatch):
    """enabled=False 的节点跳过校验。"""
    scene_list = [{
        "id": "scene_dis", "name": "dis", "display_name": "dis",
        "description": "", "work_branch": "main", "created_by": "",
        "scene_type": "standard", "pr_subscription_enabled": 0, "linked_repos": [],
        "nodes": [{"tag": "coding", "skill": "s1", "engine_runtime_ids": [1],
                   "enabled": False}],
    }]
    _patch_scenes_module(monkeypatch, scene_list,
                         {("scene_dis", "coding"): None})
    import src.domains.mgmt.routes.scenes as scenes_mod
    res = scenes_mod.list_scenes_with_pipeline(repo="gitcode:foo/bar")
    s = res["scenes"][0]
    assert s["all_offline"] is False


def test_sre_unbound_flagged(monkeypatch):
    """SRE 节点未选机器（无 engine_runtime_ids）→ sre_unbound=True、all_offline=True。"""
    scene_list = [{
        "id": "scene_sre", "name": "sre", "display_name": "sre",
        "description": "", "work_branch": "main", "created_by": "",
        "scene_type": "standard", "pr_subscription_enabled": 0, "linked_repos": [],
        "nodes": [
            # SRE 空绑定：无 engine_runtime_ids（SRE 不支持广播 → 无引擎）
            {"tag": "sre", "skill": "s3"},
            # coding 正常
            {"tag": "coding", "skill": "s1", "engine_runtime_ids": [1]},
        ],
    }]
    _patch_scenes_module(monkeypatch, scene_list,
                         {("scene_sre", "coding"): "[1]"})
    import src.domains.mgmt.routes.scenes as scenes_mod
    res = scenes_mod.list_scenes_with_pipeline(repo="gitcode:foo/bar")
    s = res["scenes"][0]
    assert s["sre_unbound"] is True
    assert s["all_offline"] is True
    assert s["offline_nodes"] == []  # coding 健康，不在离线列表


def test_pool_check_exception_degrades(monkeypatch):
    """判定函数抛异常 → 降级为不标记，不挡创建入口。"""
    scene_list = [{
        "id": "scene_err", "name": "err", "display_name": "err",
        "description": "", "work_branch": "main", "created_by": "",
        "scene_type": "standard", "pr_subscription_enabled": 0, "linked_repos": [],
        "nodes": [{"tag": "coding", "skill": "s1", "engine_runtime_ids": [1]}],
    }]
    import src.domains.runtime.engine.engine_select as es

    def _boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(es, "get_scene_pool_engine_ids", _boom)
    import src.api.pipeline_config as pc
    import src.domains.mgmt.routes.scenes as scenes_mod
    monkeypatch.setattr(pc, "get_scenes", lambda repo_key="": scene_list)
    monkeypatch.setattr(pc, "get_pipeline_config",
                        lambda scene_id, repo_key="": None)
    res = scenes_mod.list_scenes_with_pipeline(repo="gitcode:foo/bar")
    s = res["scenes"][0]
    assert s["all_offline"] is False
    assert s["offline_nodes"] == []
