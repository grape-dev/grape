"""weekly-09: 市场场景应用独立 scene_id + 可编辑名称.

覆盖验收：
1. 首次应用 → scene_id 为 custom-*（不再是模板名），name 为用户输入
2. 同仓库重复应用同一市场场景（幂等命中）→ 200 复用既有场景（reused=true，scene_id 不变，不新增行）
3. 撞名 → 409，message 引导换名
4. 未传 display_name → 回退模板 display_name
5. 同仓库应用另一模板（模拟另一用户）→ 独立行，互不影响
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
os.environ.setdefault("GRAPE_DB_DIR", tempfile.mkdtemp(prefix="grape-t-apply-"))

from fastapi.testclient import TestClient  # noqa: E402


def _setup() -> int:
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute(
            "CREATE TABLE IF NOT EXISTS scene_market ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, display_name TEXT, "
            "description TEXT, nodes_json TEXT, onboarding TEXT, preset_custom_fields TEXT)")
        c.execute("DELETE FROM scene_market")
        c.execute(
            "INSERT INTO scene_market (name, display_name, description, nodes_json, onboarding) "
            "VALUES ('vod-template', 'VOD 流水线', 'desc', ?, 'repo-onboarding')",
            (json.dumps([{"id": "n1", "agent_type": "vod"}]),))
        market_id = c.lastrowid
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key LIKE 'gitcode:t/%'")
        db.commit()
    return market_id


def test_apply_generates_unique_scene_id_and_editable_name():
    from src.api.main import app
    from src.api.deps import get_current_user_or_api_key
    from src.kernel.session_manager import db, db_lock

    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": 1, "username": "test_user", "auth_type": "api_key"}
    try:
        client = TestClient(app)
        mid = _setup()
        qs = "?owner=t&repo=r1&repo_key=gitcode:t/r1"

        # 1) 首次应用，用户自定义名称
        r1 = client.post(f"/api/scenes/market/{mid}/apply{qs}", json={"display_name": "小明的VOD"})
        assert r1.status_code == 200, r1.text
        sid1 = r1.json()["scene_id"]
        assert sid1.startswith("custom-"), f"scene_id should be custom-*, got {sid1}"
        assert sid1 != "vod-template"

        with db_lock:
            c = db.cursor()
            c.execute("SELECT scene_id, name FROM devops_pipeline_scene WHERE repo_key='gitcode:t/r1'")
            row = c.fetchone()
        assert row and row[0] == sid1 and row[1] == "小明的VOD", row

        # 2) 同仓库重复应用同一市场场景 → 幂等复用（P3 契约对齐 OpenAPI：
        #    Idempotent per market_scene_id + repo_key，200 复用既有场景）
        r2 = client.post(f"/api/scenes/market/{mid}/apply{qs}", json={"display_name": "小明的VOD2号"})
        assert r2.status_code == 200, r2.text
        body2 = r2.json()
        assert body2.get("reused") is True, f"重复 apply 应标记 reused=true: {body2}"
        assert body2["scene_id"] == sid1, f"重复 apply 应复用首个场景, got {body2['scene_id']} vs {sid1}"
        with db_lock:
            c = db.cursor()
            c.execute("SELECT COUNT(*) FROM devops_pipeline_scene WHERE repo_key='gitcode:t/r1'")
            assert c.fetchone()[0] == 1, "同仓库重复 apply 不应新增行"

        # 3) 撞名 → 409（r2 仓库先应用一次造出同名场景，再用另一模板对 r2 应用同名）
        r3a = client.post(f"/api/scenes/market/{mid}/apply?owner=t&repo=r2&repo_key=gitcode:t/r2",
                          json={"display_name": "占用名"})
        assert r3a.status_code == 200, r3a.text
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO scene_market (name, display_name, description, nodes_json, onboarding) "
                "VALUES ('tpl-conflict', 'TPL冲突', 'desc', '[]', 'repo-onboarding')")
            mid_c = c.lastrowid
            db.commit()
        r3 = client.post(f"/api/scenes/market/{mid_c}/apply?owner=t&repo=r2&repo_key=gitcode:t/r2",
                         json={"display_name": "占用名"})
        assert r3.status_code == 409, r3.text
        assert "更换名称" in r3.json()["detail"]["message"]

        # 4) 未传 display_name → 回退模板 display_name
        r4 = client.post(f"/api/scenes/market/{mid}/apply?owner=t&repo=r3&repo_key=gitcode:t/r3")
        assert r4.status_code == 200, r4.text
        sid4 = r4.json()["scene_id"]
        assert sid4 != sid1, "不同仓库应用应为独立行"
        with db_lock:
            c = db.cursor()
            c.execute("SELECT name FROM devops_pipeline_scene WHERE scene_id=?", (sid4,))
            assert c.fetchone()[0] == "VOD 流水线"

        # 5) 同仓库应用另一模板（模拟另一用户），不同名 → 独立行
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO scene_market (name, display_name, description, nodes_json, onboarding) "
                "VALUES ('vod-template-2', 'VOD 流水线2', 'desc', '[]', 'repo-onboarding')")
            mid2 = c.lastrowid
            db.commit()
        r5 = client.post(f"/api/scenes/market/{mid2}/apply{qs}", json={"display_name": "小红的VOD"})
        assert r5.status_code == 200, r5.text
        sid5 = r5.json()["scene_id"]
        assert sid5 != sid1 and sid5.startswith("custom-")
        with db_lock:
            c = db.cursor()
            c.execute("SELECT COUNT(*) FROM devops_pipeline_scene WHERE repo_key='gitcode:t/r1'")
            assert c.fetchone()[0] == 2, "r1 仓库应用2次（同模板1次+另一模板1次）各自独立成行"

        print("ALL PASS")
    finally:
        app.dependency_overrides.clear()
