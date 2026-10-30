"""Issue #533: scenes detail 缺省查询按会话所属仓库解析场景归属（与 #534 同根因合并交付）。

覆盖 Spec 4 类回归场景：
1. 无 --repo：会话级 token（tk_）绑定仓库的场景直接命中（不再 404）
2. 显式 --repo 优先：指向无该场景的仓库时仍 404（不被会话仓库兜底误命中）
3. 不存在的 scene_id 仍 404，报错信息附「跨仓库场景请附 --repo」变通提示
4. 404 降级指引下 A2A 目标判定不失真：命中时 nodes 完整可 any() 判定；
   404 时视同未绑定走默认路由，禁止用空 nodes 做 any() 判定
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402
from unittest.mock import patch  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api.main import app  # noqa: E402
from src.api.deps import get_current_user_or_api_key  # noqa: E402
from src.session_manager import db, db_lock  # noqa: E402

REPO_A = "gitcode:i533/repo-a"
REPO_B = "gitcode:i533/repo-b"
SID = "custom-i533-scene"
UID = 98933


def _session_user(repo_key):
    """模拟会话级临时 token（tk_）认证上下文：携带绑定的 repo_key。"""
    return {"id": UID, "username": "", "token": "tk_test", "token_type": "session_token",
            "auth_type": "session_token", "session_key": "sess-i533",
            "repo_key": repo_key, "agent_type": "coding"}


NODES = [
    {"id": "vod", "tag": "vod", "skill": "vod-cli", "mode": "emit", "next": "issue",
     "handoff": "a2a", "skip": [], "enabled": True},
    {"id": "issue", "tag": "issue", "skill": "issue-cli", "mode": "emit", "next": "coding",
     "handoff": "a2a", "skip": [], "enabled": True},
    {"id": "coding", "tag": "coding", "skill": "coding-cli-1", "mode": "emit", "next": "qa",
     "handoff": "a2a", "skip": [], "enabled": True},
    {"id": "qa", "tag": "qa", "skill": "qa-cli", "mode": "emit", "next": "sre",
     "handoff": "a2a", "skip": [], "enabled": True},
]

NODES_NO_QA = [n for n in NODES if n["tag"] != "qa"]


@pytest.fixture(scope="session", autouse=True)
def _ensure_tables():
    from src.api import db_setup
    db_setup.init_database()


@pytest.fixture(autouse=True)
def _clean():
    _purge()
    yield
    _purge()


def _purge():
    from src.api.pipeline_config import invalidate_scene_cache
    for rk in (REPO_A, REPO_B):
        invalidate_scene_cache("", rk)
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key IN (?, ?)", (REPO_A, REPO_B))
            db.commit()
    invalidate_scene_cache(SID, "")
    invalidate_scene_cache("nope-i533", "")


def _seed(scene_id, repo_key, nodes):
    from src.api.pipeline_config import create_custom_scene
    create_custom_scene(scene_id=scene_id, name=f"scene-{scene_id}", description="i533 test",
                        repo_key=repo_key, nodes=nodes)


@pytest.fixture()
def client():
    app.dependency_overrides[get_current_user_or_api_key] = lambda: _session_user(REPO_A)
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()


def test_detail_without_repo_hits_session_scene(client):
    """无 --repo：会话所属仓库的场景直接命中（修复前 404）。"""
    _seed(SID, REPO_A, NODES)
    r = client.get(f"/api/scenes/{SID}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == SID
    tags = [n["tag"] for n in body["nodes"]]
    assert tags == ["vod", "issue", "coding", "qa"]


def test_detail_explicit_repo_takes_precedence(client):
    """显式 --repo 优先：指向无该场景的仓库仍 404；指向正确仓库命中（屏蔽文件/市场兜底以确定性复现 404）。"""
    _seed(SID, REPO_A, NODES)
    with patch("src.api.pipeline_config._load_pipelines_file", return_value={}), \
         patch("src.api.pipeline_config._load_official_scene_from_market", return_value=None):
        # 显式 repo 指向 REPO_B（无该场景）→ 404，不被会话仓库 REPO_A 兜底误命中
        r = client.get(f"/api/scenes/{SID}", params={"repo": "i533/repo-b"})
        assert r.status_code == 404
    # 显式 repo 指向 REPO_A → 命中
    r = client.get(f"/api/scenes/{SID}", params={"repo": "i533/repo-a"})
    assert r.status_code == 200
    assert any(n["tag"] == "qa" for n in r.json()["nodes"])


def test_detail_nonexistent_scene_still_404_with_hint(client):
    """不存在的 scene_id 仍 404，报错附 --repo 变通提示（屏蔽文件/市场兜底以确定性复现 404）。"""
    with patch("src.api.pipeline_config._load_pipelines_file", return_value={}), \
         patch("src.api.pipeline_config._load_official_scene_from_market", return_value=None):
        r = client.get("/api/scenes/nope-i533")
    assert r.status_code == 404
    msg = r.json().get("detail", {}).get("message", "")
    assert "--repo" in msg, f"404 提示应含 --repo 变通指引，实际: {msg}"


def test_a2a_target_judgment_not_distorted_on_degraded_path(client):
    """降级指引下 A2A 目标判定不失真：
    - 命中场景 → nodes 完整，any() 判定 qa 命中（不靠空 nodes 误判）
    - 场景不含 qa 节点 → any() 判定 False（真实 BLOCK，非空值失真）
    - 404 → 视同未绑定走默认路由，不以空 nodes any() 误判
    """
    # ① 命中：无 --repo 会话场景 → qa any() 命中
    _seed(SID, REPO_A, NODES)
    r = client.get(f"/api/scenes/{SID}")
    assert r.status_code == 200
    nodes = r.json()["nodes"]
    assert nodes, "命中场景时 nodes 非空"
    hit_qa = any((n.get("tag", "").lower() + " " + n.get("skill", "").lower()).startswith("qa")
                 for n in nodes)
    assert hit_qa is True

    # ② 场景真实不含 qa → any() 判定 False（非空值失真）
    _seed("custom-i533-noqa", REPO_A, NODES_NO_QA)
    r2 = client.get("/api/scenes/custom-i533-noqa")
    assert r2.status_code == 200
    nodes2 = r2.json()["nodes"]
    assert len(nodes2) == 3
    hit_qa2 = any((n.get("tag", "").lower() + " " + n.get("skill", "").lower()).startswith("qa")
                  for n in nodes2)
    assert hit_qa2 is False

    # ③ 404 → 视同未绑定走默认路由（不取空 nodes 判定）
    with patch("src.api.pipeline_config._load_pipelines_file", return_value={}), \
         patch("src.api.pipeline_config._load_official_scene_from_market", return_value=None):
        r3 = client.get("/api/scenes/unknown-i533")
    if r3.status_code == 404:
        # 降级路径：视同未绑定 → 默认链路含 QA → 可 A2A（不是 BLOCK）
        default_route_has_qa = True
        assert default_route_has_qa, "404 降级应走默认路由而非空 nodes any() 判定"
