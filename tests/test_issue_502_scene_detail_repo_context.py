"""Issue #502: 场景详情接口无仓库上下文时静默回退默认场景并回显请求 id 的读侧缺陷测试。

覆盖验收标准：
- `GET /api/scenes/{scene_id}` 支持 `?repo=`（query 参数优先，会话上下文兜底）
- 任意 (repo, scene_id)：detail 的 name/nodes 与 `scenes list --repo` 中该 id 条目一致
- 不存在的 (repo, scene_id) → 404（不再静默回退默认场景 nodes）
- 响应 `id` 恒等于解析到的真实 scene_id，不回显请求值
- 无仓库上下文（无 repo 参数 + 无会话上下文）→ 按全局/官方解析，仓库级 uuid 场景不误回退默认
- get_pipeline_config：repo_key 为空时仍允许按 scene_id 做全局/官方解析；file default 兜底带 fallback 标记
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api.main import app  # noqa: E402
from src.auth import grape_account_db  # noqa: E402
from src.session_manager import db, db_lock  # noqa: E402

_TEST_REPO = "issue502/demo"
_TEST_REPO_KEY = f"gitcode:{_TEST_REPO}"

client = TestClient(app)

_uid = None
_headers = {}


def _jwt_access_token(user_id, username="test"):
    from src.api.jwt_utils import create_access_token
    return create_access_token(user_id, username, 0)


def setup_module():
    global _uid, _headers
    _uid = grape_account_db.create_grape_account("test_issue502", email=None, password="test123")
    if not _uid:
        _uid = grape_account_db.get_grape_account_by_username("test_issue502")
    _token = _jwt_access_token(_uid["id"], _uid["username"])
    _headers = {"Authorization": f"Bearer {_token}"}


def teardown_module():
    with db_lock:
        try:
            db.execute("DELETE FROM devops_pipeline_scene WHERE repo_key LIKE 'gitcode:issue502/%'")
        except Exception:
            pass
        db.commit()


@pytest.fixture(autouse=True)
def _clean_scenes():
    """每个用例前清空测试场景并失效场景缓存，保证幂等。"""
    from src.api.pipeline_config import invalidate_scene_cache
    invalidate_scene_cache()
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key LIKE 'gitcode:issue502/%'")
        except Exception:
            pass
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key LIKE 'gitcode:issue502/%'")
        except Exception:
            pass
        db.commit()
    invalidate_scene_cache()


def _seed_repo_scene(scene_id, name="仓库自定义场景", nodes=None, repo_key=_TEST_REPO_KEY):
    from src.api.pipeline_config import create_custom_scene
    return create_custom_scene(
        scene_id=scene_id, name=name, description="desc", repo_key=repo_key,
        nodes=nodes or [{"id": "issue", "skill": "issue-v2", "mode": "emit", "next": "done"}],
    )


def _seed_global_scene(scene_id, name="全局场景"):
    from src.api.pipeline_config import create_custom_scene
    return create_custom_scene(
        scene_id=scene_id, name=name, description="desc", repo_key="",
        nodes=[{"id": "issue", "skill": "issue-v2", "mode": "emit", "next": "done"}],
    )


class TestSceneDetailRepoContext:
    def test_detail_with_repo_matches_list(self):
        """?repo= 指定仓库：detail 的 name/既有 nodes 字段与 list --repo 该 id 条目一致。"""
        sid = f"uuid-{uuid.uuid4().hex[:8]}"
        _seed_repo_scene(sid, name="仓库自定义场景")

        list_resp = client.get(f"/api/scenes?repo={_TEST_REPO}", headers=_headers)
        assert list_resp.status_code == 200
        entries = [s for s in list_resp.json()["scenes"] if s["id"] == sid]
        assert len(entries) == 1

        resp = client.get(f"/api/scenes/{sid}?repo={_TEST_REPO}", headers=_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == sid
        assert body["name"] == entries[0]["name"] == "仓库自定义场景"
        # Issue #1721：detail 节点新增 resolvable/scope_check 增量字段，
        # 既有字段保持与 list 一致。
        list_nodes = entries[0]["nodes"]
        detail_nodes = body["nodes"]
        assert len(detail_nodes) == len(list_nodes)
        for detail_node, list_node in zip(detail_nodes, list_nodes):
            for key, value in list_node.items():
                assert detail_node[key] == value
            assert "resolvable" in detail_node
            assert "scope_check" in detail_node

    def test_detail_with_repo_scene_name(self):
        """?repo= 指定仓库：按场景名（非 uuid）也能解析到仓库内场景。"""
        _seed_repo_scene("my-scene-name", name="场景名解析")
        resp = client.get(f"/api/scenes/my-scene-name?repo={_TEST_REPO}", headers=_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "my-scene-name"
        assert body["name"] == "场景名解析"

    def test_detail_with_repo_unknown_scene_404(self):
        """?repo= 指定仓库 + 仓库内不存在的场景 id → 404，不得返回默认场景 nodes。"""
        sid = f"nope-{uuid.uuid4().hex[:8]}"
        resp = client.get(f"/api/scenes/{sid}?repo={_TEST_REPO}", headers=_headers)
        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert detail["code"] == 404

    def test_detail_no_repo_context_uuid_scene_404(self):
        """无仓库上下文（不带 ?repo=）请求仓库级 uuid 场景 → 404，不再静默回退默认场景。"""
        sid = f"uuid-{uuid.uuid4().hex[:8]}"
        _seed_repo_scene(sid, name="某仓库自定义")
        resp = client.get(f"/api/scenes/{sid}", headers=_headers)
        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert detail["code"] == 404

    def test_detail_official_scene_resolves_globally(self):
        """无仓库上下文 + 官方模板名 → 仍可按 scene_id 全局解析，id 与请求一致。"""
        resp = client.get("/api/scenes/skill-dev", headers=_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "skill-dev"
        assert body["name"] == "skill-dev"
        assert body["display_name"] == "华为云Skill开发"
        assert body["nodes"]

    def test_detail_default_official_template_resolves(self):
        """无仓库上下文 + default 官方模板 → 合法解析（不是静默回退），id=default。"""
        resp = client.get("/api/scenes/default", headers=_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "default"
        assert body["name"] == "default"
        assert body["display_name"] == "通用开发"

    def test_detail_session_context_fallback_no_repo(self):
        """无 repo 参数且无会话上下文：与不带 repo 行为一致，解析不到 → 404。"""
        sid = f"uuid-{uuid.uuid4().hex[:8]}"
        _seed_repo_scene(sid)
        resp = client.get(f"/api/scenes/{sid}", headers=_headers)
        assert resp.status_code == 404


class TestPipelineConfigResolution:
    def test_get_pipeline_config_empty_repo_key_queries_global_row(self):
        """repo_key 为空时允许按 scene_id 查全局/官方作用域行（devops_pipeline_scene repo_key=''）。"""
        from src.api.pipeline_config import get_pipeline_config
        _seed_global_scene("global-xyz", name="全局场景行")
        cfg = get_pipeline_config("global-xyz", repo_key="")
        assert cfg is not None
        assert cfg["name"] == "全局场景行"
        assert cfg["scene_id"] == "global-xyz"
        assert not cfg.get("fallback")

    def test_get_pipeline_config_repo_key_missing_falls_to_global_row(self):
        """repo 作用域无该场景时，仍可按 scene_id 解析到全局官方场景。"""
        from src.api.pipeline_config import get_pipeline_config
        _seed_global_scene("global-abc", name="全局官方")
        cfg = get_pipeline_config("global-abc", repo_key=_TEST_REPO_KEY)
        assert cfg is not None
        assert cfg["scene_id"] == "global-abc"
        assert not cfg.get("fallback")

    def test_get_pipeline_config_unknown_scene_marked_fallback(self):
        """不存在的 scene_id：get_pipeline_config 对内部调用方保留 default 兜底，但必须带 fallback 标记。"""
        from src.api.pipeline_config import get_pipeline_config
        cfg = get_pipeline_config("totally-unknown-xyz", repo_key="")
        assert cfg is not None  # file default 兜底（内部执行链路兼容）
        assert cfg.get("fallback") is True
        src = cfg.get("fallback_source") or {}
        assert src.get("scene_id") == "default"
        assert src.get("name")

    def test_get_pipeline_config_exact_file_scene_not_fallback(self):
        """configs/pipelines.json 中显式存在的场景名不是 fallback（精确解析）。"""
        from src.api.pipeline_config import get_pipeline_config
        cfg = get_pipeline_config("cloud-native", repo_key="")
        assert cfg is not None
        assert cfg["scene_id"] == "cloud-native"
        assert not cfg.get("fallback")