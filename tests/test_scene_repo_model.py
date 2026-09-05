"""Issue-39 方案 B：场景表迁移测试（旧 scope/user_id 结构 → 仅 repo_key 结构）。

覆盖：
- T1 表结构无 scope/user_id 列，唯一约束含 (scene_id, repo_key)
- T3 存量 user 场景经 user_repos 并入所属仓库
- T4 存量 repo 场景 scene_id 去 'repo:' 前缀
- T5 存量 official 记录被清理（不入仓库场景表）
- apply_default_scene_to_repo 默认场景复刻（T6/T7/T8/T9）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

# 与 conftest 共用测试库（conftest 已 setdefault DEVOPS_DB_PATH=data/test_sessions.db）
# 不在此覆盖 env，避免 db 实例绑定不一致


@pytest.fixture(autouse=True)
def _clean_scene_table():
    """每个用例前清空场景表与 user_repos，保证幂等（不触碰 scene_market，避免跨文件污染）。"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM devops_pipeline_scene")
        except Exception:
            pass
        try:
            c.execute("DELETE FROM user_repos")
        except Exception:
            pass
        try:
            c.execute("DELETE FROM managed_repos")
        except Exception:
            pass
        db.commit()
    yield


def _mk_old_table(c):
    """构造旧结构 devops_pipeline_scene（含 scope/user_id 列）。"""
    try:
        c.execute("DROP TABLE IF EXISTS devops_pipeline_scene")
    except Exception:
        pass
    c.execute("""
CREATE TABLE devops_pipeline_scene (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_id    TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    scope       TEXT DEFAULT 'official',
    repo_key    TEXT DEFAULT '',
    user_id     INTEGER DEFAULT 0,
    nodes_json  TEXT DEFAULT '[]',
    onboarding  TEXT DEFAULT 'repo-onboarding',
    is_active   INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (scene_id, scope, repo_key, user_id)
)""")


@pytest.fixture(scope="session", autouse=True)
def _ensure_tables():
    """先 init 全部基础表（含 user_repos / managed_repos），供迁移测试依赖。"""
    from src.api import db_setup
    db_setup.init_database()


def test_scene_table_has_no_scope_userid():
    """新表结构：无 scope / user_id，唯一约束含 (scene_id, repo_key)。"""
    from src.api import db_setup
    db_setup.init_database()
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        cols = {r[1] for r in c.execute("PRAGMA table_info(devops_pipeline_scene)").fetchall()}
    assert "scope" not in cols, f"scope column should be removed, got {cols}"
    assert "user_id" not in cols, f"user_id column should be removed, got {cols}"
    assert "repo_key" in cols


def test_dedupe_repo_scene_across_repos():
    """同 scene_id 可存在于不同仓库；同仓库内不重复（UNIQUE）。"""
    from src.api.pipeline_config import create_custom_scene
    create_custom_scene("default", "通用开发", "", "gitcode:a/r", [{"id": "issue", "skill": "issue-v2"}])
    create_custom_scene("default", "通用开发", "", "gitcode:b/r", [{"id": "issue", "skill": "issue-v2"}])
    from src.api.pipeline_config import get_scenes
    assert len(get_scenes(repo_key="gitcode:a/r")) == 1
    assert len(get_scenes(repo_key="gitcode:b/r")) == 1


def test_migrate_user_scene_to_repo():
    """T3：存量 user 场景经 user_repos 反查 repo_key 并入仓库。"""
    from src.api import db_setup
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        _mk_old_table(c)
        c.execute("INSERT INTO devops_pipeline_scene (scene_id, name, description, scope, repo_key, user_id, nodes_json) "
                  "VALUES ('custom-scene', '自定义', '', 'user', '', 42, '[]')")
        c.execute("INSERT INTO user_repos (user_id, repo_key, owner, name) VALUES (42, 'gitcode:jason/myrepo', 'jason', 'myrepo')")
        db.commit()
    db_setup._migrate_scene_table_to_repo_model()
    from src.api.pipeline_config import get_scenes
    scenes = get_scenes(repo_key="gitcode:jason/myrepo")
    assert any(s["id"] == "custom-scene" for s in scenes), f"user scene not migrated to repo: {scenes}"


def test_migrate_repo_scene_drop_prefix():
    """T4：存量 repo 场景 scene_id 去 'repo:' 前缀。"""
    from src.api import db_setup
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        _mk_old_table(c)
        c.execute("INSERT INTO devops_pipeline_scene (scene_id, name, description, scope, repo_key, user_id, nodes_json) "
                  "VALUES ('repo:default', '通用开发', '', 'repo', 'gitcode:o/r', 0, '[]')")
        db.commit()
    db_setup._migrate_scene_table_to_repo_model()
    from src.api.pipeline_config import get_scenes
    scenes = get_scenes(repo_key="gitcode:o/r")
    assert any(s["id"] == "default" for s in scenes), f"repo: prefix not stripped: {scenes}"


def test_migrate_official_scene_removed():
    """T5：存量 official 记录被清理（不入仓库场景表）。"""
    from src.api import db_setup
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        _mk_old_table(c)
        c.execute("INSERT INTO devops_pipeline_scene (scene_id, name, description, scope, repo_key, user_id, nodes_json) "
                  "VALUES ('default', '官方默认', '', 'official', '', 0, '[]')")
        db.commit()
    db_setup._migrate_scene_table_to_repo_model()
    from src.api.pipeline_config import get_scenes
    assert get_scenes(repo_key="gitcode:any/repo") == [], "official record should be removed"


def test_apply_default_scene_from_market():
    """T6：从 scene_market 复刻默认「通用开发」到仓库。"""
    from src.api import db_setup
    db_setup.init_database()
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT 1 FROM scene_market WHERE name='default' AND source='official' LIMIT 1")
        if not c.fetchone():
            c.execute(
                "INSERT INTO scene_market (name, display_name, version, description, market_description, category_id, is_featured, like_count, source, source_user_id, recommended_template, nodes_json, onboarding, is_active) "
                "VALUES ('default','通用开发','1.0.0','','',1,1,0,'official',0,'hello-flask','[]','repo-onboarding',1)")
            db.commit()
    from src.api.pipeline_config import get_scenes, apply_default_scene_to_repo
    from src.user.db import add_managed_repo
    add_managed_repo("gitcode:new/repo", "new", "repo")
    ok = apply_default_scene_to_repo("gitcode:new/repo")
    assert ok is True
    scenes = get_scenes(repo_key="gitcode:new/repo")
    assert any(s["id"] == "default" for s in scenes), f"default scene not applied: {scenes}"
    # display_name 应展示为「通用开发」而非标识符 default（issue-39 display_name 修复）
    _def = next((s for s in scenes if s["id"] == "default"), None)
    assert _def, "default scene missing in get_scenes"
    assert _def.get("display_name") == "通用开发", f"display_name should be 通用开发, got {_def.get('display_name')!r}"
    from src.user.db import get_repo_scene
    assert get_repo_scene("gitcode:new/repo") == "default"


def test_apply_default_scene_disabled():
    """T7：default_scene_market_name 为空时托管不自动配场景。"""
    from src.api import db_setup
    db_setup.init_database()
    from src.api import pipeline_config
    orig = os.environ.get("DEFAULT_SCENE_MARKET_NAME")
    os.environ["DEFAULT_SCENE_MARKET_NAME"] = ""
    try:
        ok = pipeline_config.apply_default_scene_to_repo("gitcode:empty/repo")
        assert ok is False
        from src.api.pipeline_config import get_scenes
        assert get_scenes(repo_key="gitcode:empty/repo") == []
    finally:
        if orig is not None:
            os.environ["DEFAULT_SCENE_MARKET_NAME"] = orig
        else:
            os.environ.pop("DEFAULT_SCENE_MARKET_NAME", None)


def test_apply_custom_default_scene():
    """T8：配置 default_scene_market_name='iac' 时复刻 iac 场景。"""
    from src.api import db_setup
    db_setup.init_database()
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT 1 FROM scene_market WHERE name='iac' AND source='official' LIMIT 1")
        if not c.fetchone():
            c.execute(
                "INSERT INTO scene_market (name, display_name, version, description, market_description, category_id, is_featured, like_count, source, source_user_id, recommended_template, nodes_json, onboarding, is_active) "
                "VALUES ('iac','IaC开发','1.0.0','','',1,1,0,'official',0,'tf-infra','[]','repo-onboarding',1)")
            db.commit()
    from src.api import pipeline_config
    orig = os.environ.get("DEFAULT_SCENE_MARKET_NAME")
    os.environ["DEFAULT_SCENE_MARKET_NAME"] = "iac"
    try:
        ok = pipeline_config.apply_default_scene_to_repo("gitcode:iac/repo")
        assert ok is True
        from src.api.pipeline_config import get_scenes
        scenes = get_scenes(repo_key="gitcode:iac/repo")
        assert any(s["id"] == "iac" for s in scenes), f"iac scene not applied: {scenes}"
    finally:
        if orig is not None:
            os.environ["DEFAULT_SCENE_MARKET_NAME"] = orig
        else:
            os.environ.pop("DEFAULT_SCENE_MARKET_NAME", None)


def test_dev_scene_default_empty():
    """T10：新建 managed_repos dev_scene 默认空。"""
    from src.api import db_setup
    db_setup.init_database()
    from src.user.db import add_managed_repo
    add_managed_repo("gitcode:devempty/repo", "devempty", "repo")
    from src.user.db import get_repo_scene
    assert get_repo_scene("gitcode:devempty/repo") == ""


def test_set_repo_scene_empty_not_default():
    """T11：set_repo_scene 空值写入空串（非 default）。"""
    from src.api import db_setup
    db_setup.init_database()
    from src.user.db import add_managed_repo, set_repo_scene, get_repo_scene
    add_managed_repo("gitcode:setempty/repo", "setempty", "repo")
    set_repo_scene("gitcode:setempty/repo", "")
    assert get_repo_scene("gitcode:setempty/repo") == ""