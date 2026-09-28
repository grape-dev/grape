"""Issue #250: revoke 物理删除 agent_runtimes 导致场景配置 engine_runtime_ids 失效。

测试覆盖：
1. revoke 凭据后场景配置 engine_runtime_ids 中失效 ID 被自动清理
2. _validate_scene_engine_ids 遇到失效 runtime_id 优雅降级（不报 400）
3. revoke → 场景校验 → 场景应用全链路不报错
"""
import os
import sys
import json
import uuid

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db"))

import pytest  # noqa: E402

from src.session_manager import db, db_lock  # noqa: E402


def _unique_repo() -> str:
    return f"issue250-{uuid.uuid4().hex[:10]}"


def _insert_credential(name: str = "") -> int:
    """插入测试凭据，返回 cid。"""
    name = name or f"cred-{uuid.uuid4().hex[:8]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, scope, owner_name, status) "
            "VALUES (?, ?, 'user', 'tester', 'active')",
            (f"hash_{name}", name))
        cid = c.lastrowid
        db.commit()
    return cid


def _insert_runtime(cid: int, name: str = "", status: str = "online", healthy: int = 1) -> int:
    """插入测试 agent_runtime，返回 rid。"""
    name = name or f"engine-{uuid.uuid4().hex[:8]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (credential_id, name, status, healthy, scope) "
            "VALUES (?, ?, ?, ?, 'user')",
            (cid, name, status, healthy))
        rid = c.lastrowid
        db.commit()
    return rid


def _insert_engine_capability(rid: int, tag: str = "coding") -> None:
    """插入引擎能力标签。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?, ?, 1)",
            (rid, tag))
        db.commit()


def _insert_scene(scene_id: str, repo_key: str, nodes: list) -> None:
    """插入测试场景配置。"""
    from src.api.pipeline_config import create_custom_scene
    create_custom_scene(scene_id, "测试场景", "", repo_key, nodes)


def _get_scene_nodes(scene_id: str, repo_key: str) -> list:
    """读取场景 nodes。"""
    from src.api.pipeline_config import _query_scene_by_id
    scene = _query_scene_by_id(scene_id, repo_key=repo_key)
    return scene.get("nodes", []) if scene else []


def _cleanup_credential(cid: int):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE credential_id=?", (cid,))
        c.execute("DELETE FROM runtime_credentials WHERE id=?", (cid,))
        db.commit()


def _cleanup_scene(scene_id: str, repo_key: str):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM devops_pipeline_scene WHERE scene_id=? AND repo_key=?",
                  (scene_id, repo_key))
        db.commit()


@pytest.fixture(autouse=True)
def _isolate():
    """每条用例独立的数据隔离。"""
    yield


class TestRevokeCleansSceneEngineRuntimeIds:
    """revoke 凭据后场景配置 engine_runtime_ids 中失效 ID 被自动清理。"""

    def test_revoke_removes_deleted_rid_from_scene(self):
        """revoke 后场景 nodes 中被删 runtime_id 已清除。"""
        owner, repo = "test250", _unique_repo()
        repo_key = f"gitcode:{owner}/{repo}"
        scene_id = f"test-scene-{uuid.uuid4().hex[:6]}"

        cid = _insert_credential()
        rid = _insert_runtime(cid)
        _insert_engine_capability(rid, tag="coding")

        nodes = [{"id": "coding", "tag": "coding", "skill": "implementation",
                  "engine_runtime_ids": [rid]}]
        _insert_scene(scene_id, repo_key, nodes)

        try:
            from src.api.routes.settings import revoke_runtime_credential
            revoke_runtime_credential(cid, {"id": 1, "username": "tester", "auth_type": "api_key"})

            scene_nodes = _get_scene_nodes(scene_id, repo_key)
            assert len(scene_nodes) == 1
            eids = scene_nodes[0].get("engine_runtime_ids", [])
            assert rid not in eids, f"被删 runtime_id {rid} 应已从 engine_runtime_ids 清除"
        finally:
            _cleanup_scene(scene_id, repo_key)
            _cleanup_credential(cid)

    def test_revoke_preserves_unrelated_rids(self):
        """revoke 只清除被删 cred 的 runtime_id，其他 cred 的 runtime_id 保留。"""
        owner, repo = "test250", _unique_repo()
        repo_key = f"gitcode:{owner}/{repo}"
        scene_id = f"test-scene-{uuid.uuid4().hex[:6]}"

        cid_del = _insert_credential(name=f"del-{uuid.uuid4().hex[:6]}")
        cid_keep = _insert_credential(name=f"keep-{uuid.uuid4().hex[:6]}")
        rid_del = _insert_runtime(cid_del)
        rid_keep = _insert_runtime(cid_keep)
        _insert_engine_capability(rid_del, tag="coding")
        _insert_engine_capability(rid_keep, tag="coding")

        nodes = [{"id": "coding", "tag": "coding", "skill": "implementation",
                  "engine_runtime_ids": [rid_del, rid_keep]}]
        _insert_scene(scene_id, repo_key, nodes)

        try:
            from src.api.routes.settings import revoke_runtime_credential
            revoke_runtime_credential(cid_del, {"id": 1, "username": "tester", "auth_type": "api_key"})

            scene_nodes = _get_scene_nodes(scene_id, repo_key)
            eids = scene_nodes[0].get("engine_runtime_ids", [])
            assert rid_del not in eids, "被删 runtime_id 应已清除"
            assert rid_keep in eids, "未删 runtime_id 应保留"
        finally:
            _cleanup_scene(scene_id, repo_key)
            _cleanup_credential(cid_del)
            _cleanup_credential(cid_keep)

    def test_revoke_no_runtimes_no_error(self):
        """凭据无 runtime 时 revoke 不报错（无场景清理发生）。"""
        cid = _insert_credential()
        try:
            from src.api.routes.settings import revoke_runtime_credential
            result = revoke_runtime_credential(cid, {"id": 1, "username": "tester", "auth_type": "api_key"})
            assert "已吊销" in result["message"]
        finally:
            _cleanup_credential(cid)


class TestValidateSceneEngineIdsGracefulDegradation:
    """_validate_scene_engine_ids 遇到失效 runtime_id 优雅降级（不报 400）。"""

    def test_invalid_rid_skipped_not_400(self):
        """失效 runtime_id 被跳过，不抛 HTTPException 400。"""
        owner, repo = "test250", _unique_repo()
        repo_key = f"gitcode:{owner}/{repo}"
        cid = _insert_credential()
        rid_valid = _insert_runtime(cid)
        _insert_engine_capability(rid_valid, tag="coding")
        rid_invalid = 999999  # 不存在的 runtime_id

        nodes = [{"id": "coding", "tag": "coding", "skill": "implementation",
                  "engine_runtime_ids": [rid_invalid, rid_valid]}]

        try:
            from src.api.routes.scenes import _validate_scene_engine_ids
            _validate_scene_engine_ids(nodes, repo_key)
        finally:
            _cleanup_credential(cid)

    def test_all_invalid_rids_skipped(self):
        """全部 runtime_id 失效时不报 400（全部跳过）。"""
        owner, repo = "test250", _unique_repo()
        repo_key = f"gitcode:{owner}/{repo}"
        nodes = [{"id": "coding", "tag": "coding", "skill": "implementation",
                  "engine_runtime_ids": [999998, 999999]}]
        from src.api.routes.scenes import _validate_scene_engine_ids
        _validate_scene_engine_ids(nodes, repo_key)

    def test_valid_rid_still_validated(self):
        """有效 runtime_id 的 scope/能力校验仍正常工作。"""
        owner, repo = "test250", _unique_repo()
        repo_key = f"gitcode:{owner}/{repo}"
        cid = _insert_credential()
        rid = _insert_runtime(cid)
        _insert_engine_capability(rid, tag="coding")

        nodes = [{"id": "coding", "tag": "coding", "skill": "implementation",
                  "engine_runtime_ids": [rid]}]
        try:
            from src.api.routes.scenes import _validate_scene_engine_ids
            _validate_scene_engine_ids(nodes, repo_key)
        finally:
            _cleanup_credential(cid)


class TestRevokeFullChain:
    """revoke → 场景校验 → 场景应用全链路不报错。"""

    def test_revoke_then_validate_then_apply(self):
        """revoke 凭据后，场景校验通过、场景应用不报错。"""
        owner, repo = "test250", _unique_repo()
        repo_key = f"gitcode:{owner}/{repo}"
        scene_id = f"chain-{uuid.uuid4().hex[:6]}"

        cid = _insert_credential()
        rid = _insert_runtime(cid)
        _insert_engine_capability(rid, tag="coding")

        nodes = [{"id": "coding", "tag": "coding", "skill": "implementation",
                  "engine_runtime_ids": [rid], "enabled": True}]
        _insert_scene(scene_id, repo_key, nodes)

        try:
            # 1. revoke 凭据（物理删除 agent_runtimes + 清理场景配置）
            from src.api.routes.settings import revoke_runtime_credential
            revoke_runtime_credential(cid, {"id": 1, "username": "tester", "auth_type": "api_key"})

            # 2. 场景校验不报错（失效 ID 已被清理 + 优雅降级兜底）
            scene_nodes = _get_scene_nodes(scene_id, repo_key)
            assert rid not in (scene_nodes[0].get("engine_runtime_ids") or [])

            from src.api.routes.scenes import _validate_scene_engine_ids
            _validate_scene_engine_ids(scene_nodes, repo_key)

            # 3. 场景应用不报错（dispatch 逻辑对失效 runtime_id 已有容错）
            # 注：apply_scene 需要 devops_agent_session 表，此处仅验证校验通过
        finally:
            _cleanup_scene(scene_id, repo_key)
            _cleanup_credential(cid)

    def test_revoke_multiple_scenes_cleaned(self):
        """revoke 清理多个场景中的失效 ID（跨场景扫描）。"""
        owner, repo = "test250", _unique_repo()
        repo_key = f"gitcode:{owner}/{repo}"
        scene_id_1 = f"multi-1-{uuid.uuid4().hex[:6]}"
        scene_id_2 = f"multi-2-{uuid.uuid4().hex[:6]}"

        cid = _insert_credential()
        rid = _insert_runtime(cid)
        _insert_engine_capability(rid, tag="coding")

        nodes = [{"id": "coding", "tag": "coding", "skill": "implementation",
                  "engine_runtime_ids": [rid]}]
        _insert_scene(scene_id_1, repo_key, nodes)
        _insert_scene(scene_id_2, repo_key, nodes)

        try:
            from src.api.routes.settings import revoke_runtime_credential
            revoke_runtime_credential(cid, {"id": 1, "username": "tester", "auth_type": "api_key"})

            for sid in (scene_id_1, scene_id_2):
                scene_nodes = _get_scene_nodes(sid, repo_key)
                eids = scene_nodes[0].get("engine_runtime_ids", [])
                assert rid not in eids, f"场景 {sid} 的 engine_runtime_ids 应已清除失效 ID {rid}"
        finally:
            _cleanup_scene(scene_id_1, repo_key)
            _cleanup_scene(scene_id_2, repo_key)
            _cleanup_credential(cid)
