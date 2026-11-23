# -*- coding: utf-8 -*-
"""Issue #3914 E：场景节点「存名必存 id」。

验收：场景保存后节点 `skill_id ≥ 1`；解析不到保持原样（派发时按名降级）；
存量回填脚本默认 dry-run 出对照清单（见 scripts/backfill_scene_nodes_skill_id.py，用例覆盖解析口径）。
"""
import json
import os
import sqlite3
import sys
import threading

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)


def _resolve_fake(skill_name, repo_key="", user_id=0):
    return {"repo-qa": 2756, "coding-cli": 2754}.get(skill_name, 0)


class TestFillSceneNodesSkillId:
    def test_name_only_node_gets_id(self):
        from src.domains.mgmt.pipeline_config import fill_scene_nodes_skill_id
        nodes = [{"id": "qa", "tag": "qa", "skill": "repo-qa"},
                 {"id": "coding", "tag": "coding", "skill": "coding-cli"}]
        out = fill_scene_nodes_skill_id(nodes, "gitcode:o/r", resolver=_resolve_fake)
        assert out[0]["skill_id"] == 2756
        assert out[1]["skill_id"] == 2754
        assert out[0]["skill"] == "repo-qa"  # 名字保留（显示 + 降级兜底）

    def test_existing_id_preserved(self):
        from src.domains.mgmt.pipeline_config import fill_scene_nodes_skill_id
        nodes = [{"tag": "qa", "skill": "repo-qa", "skill_id": 999}]
        out = fill_scene_nodes_skill_id(nodes, "gitcode:o/r", resolver=_resolve_fake)
        assert out[0]["skill_id"] == 999

    def test_unresolvable_keeps_name_only(self):
        from src.domains.mgmt.pipeline_config import fill_scene_nodes_skill_id
        nodes = [{"tag": "qa", "skill": "ghost-skill"}]
        out = fill_scene_nodes_skill_id(nodes, "gitcode:o/r", resolver=_resolve_fake)
        assert "skill_id" not in out[0] or not out[0].get("skill_id")
        assert out[0]["skill"] == "ghost-skill"

    def test_node_without_skill_untouched(self):
        from src.domains.mgmt.pipeline_config import fill_scene_nodes_skill_id
        nodes = [{"tag": "sre", "skill": ""}, "not-a-dict"]
        out = fill_scene_nodes_skill_id(nodes, "gitcode:o/r", resolver=_resolve_fake)
        assert out[0] == {"tag": "sre", "skill": ""}
        assert out[1] == "not-a-dict"

    def test_does_not_mutate_input(self):
        from src.domains.mgmt.pipeline_config import fill_scene_nodes_skill_id
        nodes = [{"tag": "qa", "skill": "repo-qa"}]
        fill_scene_nodes_skill_id(nodes, "gitcode:o/r", resolver=_resolve_fake)
        assert "skill_id" not in nodes[0]


@pytest.fixture()
def scene_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript("""
    CREATE TABLE devops_pipeline_scene (
        id INTEGER PRIMARY KEY AUTOINCREMENT, scene_id TEXT, name TEXT DEFAULT '',
        description TEXT DEFAULT '', nodes_json TEXT DEFAULT '[]', onboarding TEXT DEFAULT '',
        is_active INTEGER DEFAULT 1, repo_key TEXT DEFAULT '', updated_at TEXT DEFAULT '',
        created_at TEXT DEFAULT ''
    );
    """)
    import src.domains.mgmt.pipeline_config as pc
    monkeypatch.setattr(pc, "_SCENE_CACHE", {})
    monkeypatch.setattr(pc, "_db", lambda: (conn, threading.RLock()))
    import src.domains.mgmt.user.db as udb
    monkeypatch.setattr(udb, "resolve_skill_id", _resolve_fake)
    yield conn
    conn.close()


def test_update_custom_scene_persists_skill_id(scene_db):
    """仓库级场景保存：只存名的节点写库后带 skill_id（≥1）。"""
    from src.domains.mgmt.pipeline_config import create_custom_scene, update_custom_scene
    create_custom_scene("sc-3914", "s", "", "gitcode:o/r",
                        [{"tag": "qa", "skill": "repo-qa"}])
    update_custom_scene("sc-3914", "gitcode:o/r",
                        [{"tag": "qa", "skill": "repo-qa", "enabled": True}])
    row = scene_db.execute(
        "SELECT nodes_json FROM devops_pipeline_scene WHERE scene_id='sc-3914'").fetchone()
    node = json.loads(row[0])[0]
    assert node["skill"] == "repo-qa"
    assert int(node.get("skill_id") or 0) >= 1


def test_create_custom_scene_persists_skill_id(scene_db):
    from src.domains.mgmt.pipeline_config import create_custom_scene
    create_custom_scene("sc-3914b", "s2", "", "gitcode:o/r",
                        [{"tag": "qa", "skill": "repo-qa"},
                         {"tag": "coding", "skill": "ghost-skill"}])
    row = scene_db.execute(
        "SELECT nodes_json FROM devops_pipeline_scene WHERE scene_id='sc-3914b'").fetchone()
    nodes = json.loads(row[0])
    assert nodes[0]["skill_id"] == 2756
    assert not nodes[1].get("skill_id")  # 解析不到保持原样


def test_zero_skill_id_node_rebackfilled():
    """`skill_id=0`（存量/老前端）→ 按名回填；只有非 0 的既有 id 才视为「已选」保留。"""
    from src.domains.mgmt.pipeline_config import fill_scene_nodes_skill_id
    nodes = [{"tag": "qa", "skill": "repo-qa", "skill_id": 0}]
    out = fill_scene_nodes_skill_id(nodes, "gitcode:o/r", resolver=_resolve_fake)
    assert out[0]["skill_id"] == 2756


# ── 存量回填脚本：apply 后重新 collect 校验（部分成功不静默）────────────

def _load_backfill_module():
    import importlib.util
    from pathlib import Path
    script = (Path(__file__).resolve().parents[2]
              / "scripts" / "backfill_scene_nodes_skill_id.py")
    spec = importlib.util.spec_from_file_location("backfill_3914_under_test", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_backfill_apply_verifies_no_pending_rows_left(monkeypatch, capsys):
    """apply 成功且复检待写行=0 → 退出码 0，并打印校验结果。"""
    import sys
    mod = _load_backfill_module()
    pending = [("devops_pipeline_scene", 1, "[]")]
    calls = {"n": 0}

    def _collect():
        calls["n"] += 1
        return ([], pending if calls["n"] == 1 else [])

    monkeypatch.setattr(mod, "collect", _collect)
    monkeypatch.setattr(mod, "apply", lambda p: len(p))
    monkeypatch.setattr(sys, "argv", ["backfill", "--apply"])
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "校验：回填后待写行 0" in out


def test_backfill_apply_reports_partial_failure(monkeypatch, capsys):
    """部分行写失败（复检仍有待写行）→ 非 0 退出 + stderr 提示复核，不静默成功。"""
    import sys
    mod = _load_backfill_module()
    pending = [("devops_pipeline_scene", 1, "[]")]
    monkeypatch.setattr(mod, "collect", lambda: ([], pending))
    monkeypatch.setattr(mod, "apply", lambda p: 0)
    monkeypatch.setattr(sys, "argv", ["backfill", "--apply"])
    assert mod.main() == 1
    err = capsys.readouterr().err
    assert "仍有 1 行待写" in err
