# -*- coding: utf-8 -*-
"""Issue #2118 复审：A2A 阶段推进的节点解析必须按 repo_key 走场景配置。

原实现直读 configs/pipelines.json，仓库级自定义场景（如 org 组织场景）不在
静态文件里 → 取不到节点，stage/pending_review 判断与场景不一致。
"""
from __future__ import annotations

import src.api.pipeline_config as pipeline_config
import src.api.session_store as session_store
from src.domains.runtime.routes.webhook import _scene_node_order


def _patch_scene(monkeypatch, nodes, scene_id="custom-org-2118", calls=None):
    def _fake_cfg(scene_id_arg="default", repo_key=""):
        if calls is not None:
            calls["scene_id"] = scene_id_arg
            calls["repo_key"] = repo_key
        return {"nodes": nodes}

    monkeypatch.setattr(pipeline_config, "get_pipeline_config", _fake_cfg)
    monkeypatch.setattr(session_store, "get_issue_scene",
                        lambda repo_key, issue: (scene_id, 0))


def test_scene_node_order_passes_repo_key(monkeypatch):
    calls = {}
    _patch_scene(monkeypatch, [
        {"id": "org_intake", "next": "org_dispatch"},
        {"id": "org_dispatch", "next": "org_aggregate"},
        {"id": "org_aggregate", "next": "done"},
    ], calls=calls)

    order = _scene_node_order("gitcode:acme/org-repo", 42)

    assert calls == {"scene_id": "custom-org-2118", "repo_key": "gitcode:acme/org-repo"}
    assert order == {
        "org_intake": "org_dispatch",
        "org_dispatch": "org_aggregate",
        "org_aggregate": "done",
    }


def test_scene_node_order_default_scene_still_repo_scoped(monkeypatch):
    calls = {}

    def _boom(repo_key, issue):
        raise RuntimeError("issue_state unavailable")

    _patch_scene(monkeypatch, [{"id": "issue", "next": "coding"}], calls=calls)
    monkeypatch.setattr(session_store, "get_issue_scene", _boom)

    assert _scene_node_order("gitcode:acme/demo", 7) == {"issue": "coding"}
    # 场景查询失败也保持 repo 作用域，避免回退到全局同号场景
    assert calls == {"scene_id": "default", "repo_key": "gitcode:acme/demo"}


def test_scene_node_order_empty_config(monkeypatch):
    _patch_scene(monkeypatch, None, scene_id="")

    assert _scene_node_order("gitcode:acme/demo", 8) == {}