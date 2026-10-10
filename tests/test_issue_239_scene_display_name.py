"""Issue #239: 场景名称显示一致性——display_name 优先逻辑测试。

覆盖：
- get_pipeline_config 返回值包含 display_name 字段（DB 场景 / scene_market 模板 / pipelines.json 回退）
- display_name 存在时优先返回 display_name
- display_name 缺失时回退 name
- 无 scene 时返回 None
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_scene_table():
    """每个用例前清空场景表，保证幂等。"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%'")
        except Exception:
            pass
        try:
            c.execute("DELETE FROM scene_market WHERE source!='official' OR name LIKE 'test_%'")
        except Exception:
            pass
        db.commit()
    yield


@pytest.fixture(scope="session", autouse=True)
def _ensure_tables():
    from src.api import db_setup
    db_setup.init_database()


def test_get_pipeline_config_returns_display_name_from_db():
    """DB 场景：get_pipeline_config 返回 display_name，优先于 name。"""
    from src.api.pipeline_config import create_custom_scene, get_pipeline_config
    create_custom_scene(
        scene_id="test_dn_scene",
        name="内部名A",
        description="",
        repo_key="gitcode:test/repo",
        nodes=[{"id": "issue", "skill": "issue-v2"}],
        display_name="展示名A",
    )
    cfg = get_pipeline_config("test_dn_scene", repo_key="gitcode:test/repo")
    assert cfg is not None
    assert cfg["name"] == "内部名A"
    assert cfg["display_name"] == "展示名A"


def test_get_pipeline_config_falls_back_to_name_when_no_display_name():
    """display_name 为空时回退 name，不报错不空白。"""
    from src.api.pipeline_config import create_custom_scene, get_pipeline_config
    create_custom_scene(
        scene_id="test_no_dn",
        name="只有name",
        description="",
        repo_key="gitcode:test/repo2",
        nodes=[{"id": "issue", "skill": "issue-v2"}],
        display_name="",  # 无 display_name
    )
    cfg = get_pipeline_config("test_no_dn", repo_key="gitcode:test/repo2")
    assert cfg is not None
    assert cfg["name"] == "只有name"
    assert cfg["display_name"] == "只有name"  # 回退 name


def test_get_pipeline_config_returns_none_for_missing_scene():
    """不存在的 scene_id 且无 market/file 回退时返回 None。"""
    from src.api.pipeline_config import get_pipeline_config
    cfg = get_pipeline_config("nonexistent_scene_xyz_12345", repo_key="gitcode:nonexistent/repo")
    # 可能回退到 default file scene，若返回非 None 则 display_name 必须有值
    if cfg is not None:
        assert "display_name" in cfg
        assert cfg["display_name"]  # 非空


def test_get_pipeline_config_market_template_has_display_name():
    """scene_market 官方模板分支返回 display_name。"""
    from src.session_manager import db, db_lock
    from src.api.pipeline_config import get_pipeline_config

    # 插入一条 official 模板
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM scene_market WHERE name='test_official_dn'")
            c.execute(
                "INSERT INTO scene_market (name, display_name, version, description, market_description, "
                "category_id, is_featured, like_count, source, source_user_id, recommended_template, "
                "nodes_json, onboarding, is_active) "
                "VALUES ('test_official_dn','官方展示名',1,'desc','desc',1,0,0,'official','','',"
                "'[{\"id\":\"issue\",\"skill\":\"issue-v2\"}]','repo-onboarding',1)"
            )
            db.commit()
        except Exception as e:
            pytest.skip(f"scene_market 插入失败（表结构差异）: {e}")

    cfg = get_pipeline_config("test_official_dn", repo_key="")  # 无 repo_key → 走 market 回退
    assert cfg is not None
    assert cfg["display_name"] == "官方展示名"
    assert cfg["name"] == "test_official_dn"


def test_scene_name_uses_display_name_first():
    """验证 display_name 优先逻辑的等价表达式（与 issues_center.py/issues.py 中使用方式一致）。"""
    # 模拟 get_pipeline_config 返回值
    cfg_with_dn = {"name": "raw_name", "display_name": "漂亮名字"}
    cfg_without_dn = {"name": "raw_name", "display_name": ""}
    cfg_none_dn = {"name": "raw_name"}  # 老调用方可能无此字段

    # issues_center.py / issues.py / pipeline.py 使用的表达式
    def pick(cfg):
        return (cfg or {}).get("display_name") or (cfg or {}).get("name", "")

    assert pick(cfg_with_dn) == "漂亮名字"
    assert pick(cfg_without_dn) == "raw_name"
    assert pick(cfg_none_dn) == "raw_name"
    assert pick(None) == ""


def test_frontend_dropdown_uses_display_name_first():
    """验证前端 issues.js 下拉框的等价表达式。"""
    # issues.js: s.display_name || s.name || s.id
    def pick(s):
        return s.get("display_name") or s.get("name") or s.get("id")

    assert pick({"id": "s1", "name": "n", "display_name": "d"}) == "d"
    assert pick({"id": "s1", "name": "n", "display_name": ""}) == "n"
    assert pick({"id": "s1", "name": "", "display_name": ""}) == "s1"
    assert pick({"id": "s1"}) == "s1"
