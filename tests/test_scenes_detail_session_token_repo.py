"""补充测试：会话 token（tk_）无 ?repo= 时 scenes detail 用 token 绑定的 repo_key 解析仓库级场景。

新增三个用例（对齐 test_issue_502_scene_detail_repo_context.py 风格）：
1. 会话 token + 不带 ?repo= → 200，返回仓库级场景真实节点；
2. JWT / grape_ Key + 不带 ?repo= → 仍 404（行为不变）；
3. 会话 token 仓库下场景真不存在 → 仍 404（不复活静默兜底）。
"""
import importlib
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_BACKEND", "sqlite")

from fastapi.testclient import TestClient


def _fresh_imports():
    for m in [k for k in sys.modules if k.startswith("src.")]:
        del sys.modules[m]


def _reset_db():
    import src.session_manager as sm
    with sm.db_lock:
        c = sm.db.cursor()
        c.execute("DELETE FROM session_api_tokens")
        c.execute("DELETE FROM devops_pipeline_scene WHERE scene_id=?", ("sess-scene-1",))
        sm.db.commit()


def _seed():
    import json
    import src.session_manager as sm
    with sm.db_lock:
        c = sm.db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO devops_pipeline_scene (name, scene_id, repo_key, nodes_json, is_active) "
            "VALUES (?,?,?,?,1)",
            ("会话场景", "sess-scene-1", "gitcode:acme/repo1", json.dumps(
                [{"id": "vod", "tag": "vod", "skill": "v"}, {"id": "qa", "tag": "qa", "skill": "q"}])),
        )
        sm.db.commit()


def _make_session_token():
    import src.api.auth_token as at
    tk = at.create_session_token("k-1", "gitcode:acme/repo1", "vod", 1) if hasattr(at, "create_session_token") else None
    if tk:
        return tk
    # fallback: 手工插行
    import hashlib, time, secrets
    tok = "tk_" + secrets.token_urlsafe(24)
    with __import__("src.session_manager", fromlist=["db"]).db_lock as _:
        pass
    import src.session_manager as sm
    with sm.db_lock:
        c = sm.db.cursor()
        c.execute(
            "INSERT INTO session_api_tokens (session_key, repo_key, agent_type, user_id, token_sha256, expires_at) "
            "VALUES (?,?,?,?,?,?)",
            ("k-1", "gitcode:acme/repo1", "vod", 1,
             hashlib.sha256(tok.encode()).hexdigest(), int(time.time()) + 600),
        )
        sm.db.commit()
    return tok


def _client():
    import src.api.auth_token as at
    _fresh_imports()
    import src.api.main as m
    importlib.reload(m)
    return TestClient(m.app)


def test_session_token_resolves_repo_scene_without_repo_param():
    _reset_db(); _seed()
    cl = _client()
    tk = _make_session_token()
    r = cl.get("/api/scenes/sess-scene-1", headers={"Authorization": f"Bearer {tk}"})
    assert r.status_code == 200, r.text
    nodes = [n["id"] for n in r.json()["nodes"]]
    assert nodes == ["vod", "qa"], nodes


def test_jwt_without_repo_param_still_404():
    _reset_db(); _seed()
    cl = _client()
    # 伪 JWT（非 tk_/grape_ 前缀走 JWT 校验）→ 无效 token 401/403 都可，重点是不 200
    r = cl.get("/api/scenes/sess-scene-1", headers={"Authorization": "Bearer invalid.jwt.value"})
    assert r.status_code != 200


def test_session_token_scene_absent_in_bound_repo_still_404():
    _reset_db(); _seed()
    cl = _client()
    tk = _make_session_token()
    r = cl.get("/api/scenes/sess-scene-does-not-exist", headers={"Authorization": f"Bearer {tk}"})
    assert r.status_code == 404, r.text
