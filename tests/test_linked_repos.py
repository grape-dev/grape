"""Issue #242: 跨仓库需求开发 — linked_repos 字段测试。

覆盖：
- DB schema: linked_repos 列存在
- pipeline_config: create/get/update linked_repos
- scenes API Request 模型含 linked_repos
- event_pipeline: GRAPE_LINKED_REPOS 环境变量注入
- repogit: ensure_repo_base_multi / ensure_linked_repo_worktrees 函数签名
- vcs/api_client: create_pr_for_repo 函数存在且可调用
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from src.session_manager import db, db_lock

RK = "gitcode:org/linked-repos-test"


@pytest.fixture(autouse=True)
def _clean():
    """每用例前清空场景表（限定 repo_key，不污染其他测试）。"""
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (RK,))
        c.execute("DELETE FROM issue WHERE repo_key=?", (RK,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (RK,))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (RK,))
        c.execute("DELETE FROM issue WHERE repo_key=?", (RK,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (RK,))
        db.commit()


@pytest.fixture(scope="session", autouse=True)
def _ensure_tables():
    from src.api import db_setup
    db_setup.init_database()


# ===== DB schema =====

def test_linked_repos_column_exists():
    """devops_pipeline_scene 表含 linked_repos 列（init_database 迁移后）。"""
    from src.api import db_setup
    db_setup.init_database()
    with db_lock:
        c = db.cursor()
        cols = {r[1] for r in c.execute("PRAGMA table_info(devops_pipeline_scene)").fetchall()}
    assert "linked_repos" in cols, f"linked_repos column missing, got {cols}"


# ===== pipeline_config: create + get =====

def test_create_scene_with_linked_repos():
    """create_custom_scene 写入 linked_repos，get_scenes 读取返回。"""
    from src.api.pipeline_config import create_custom_scene, get_scenes
    linked = [
        {"repo_key": "gitcode:org/repo-a", "branch": "main"},
        {"repo_key": "gitcode:org/repo-b", "branch": "dev"},
    ]
    create_custom_scene(
        "test-lr-scene", "LR Scene", "", RK,
        [{"id": "issue", "skill": "issue-v2"}],
        linked_repos=linked)
    scenes = get_scenes(repo_key=RK)
    assert len(scenes) == 1
    assert scenes[0]["linked_repos"] == linked


def test_create_scene_without_linked_repos_defaults_empty():
    """不传 linked_repos 时默认空列表。"""
    from src.api.pipeline_config import create_custom_scene, get_scenes
    create_custom_scene("test-lr-empty", "Empty", "", RK, [{"id": "issue"}])
    scenes = get_scenes(repo_key=RK)
    assert len(scenes) == 1
    assert scenes[0]["linked_repos"] == []


# ===== pipeline_config: update =====

def test_update_scene_linked_repos():
    """update_custom_scene 更新 linked_repos。"""
    from src.api.pipeline_config import create_custom_scene, update_custom_scene, get_scenes
    create_custom_scene("test-lr-update", "Update", "", RK, [{"id": "issue"}])
    new_linked = [{"repo_key": "gitcode:org/repo-c", "branch": "feature"}]
    update_custom_scene("test-lr-update", RK, [{"id": "issue"}], linked_repos=new_linked)
    scenes = get_scenes(repo_key=RK)
    assert len(scenes) == 1
    assert scenes[0]["linked_repos"] == new_linked


def test_update_scene_clear_linked_repos():
    """传 linked_repos=[] 清空关联仓库。"""
    from src.api.pipeline_config import create_custom_scene, update_custom_scene, get_scenes
    create_custom_scene("test-lr-clear", "Clear", "", RK, [{"id": "issue"}],
                        linked_repos=[{"repo_key": "gitcode:org/repo-a", "branch": "main"}])
    update_custom_scene("test-lr-clear", RK, [{"id": "issue"}], linked_repos=[])
    scenes = get_scenes(repo_key=RK)
    assert len(scenes) == 1
    assert scenes[0]["linked_repos"] == []


def test_update_scene_without_linked_repos_preserves():
    """不传 linked_repos（None）时不修改已有值。"""
    from src.api.pipeline_config import create_custom_scene, update_custom_scene, get_scenes
    original = [{"repo_key": "gitcode:org/repo-a", "branch": "main"}]
    create_custom_scene("test-lr-preserve", "Preserve", "", RK, [{"id": "issue"}],
                        linked_repos=original)
    update_custom_scene("test-lr-preserve", RK, [{"id": "issue"}])
    scenes = get_scenes(repo_key=RK)
    assert len(scenes) == 1
    assert scenes[0]["linked_repos"] == original


# ===== pipeline_config: _query_scene_by_id =====

def test_query_scene_by_id_returns_linked_repos():
    """_query_scene_by_id 返回 linked_repos 字段。"""
    from src.api.pipeline_config import create_custom_scene, _query_scene_by_id
    linked = [{"repo_key": "gitcode:org/repo-x", "branch": "main"}]
    create_custom_scene("test-lr-byid", "ByID", "", RK, [{"id": "issue"}],
                        linked_repos=linked)
    scene = _query_scene_by_id("test-lr-byid", repo_key=RK)
    assert scene is not None
    assert scene["linked_repos"] == linked


# ===== pipeline_config: 幂等更新（reactivate）=====

def test_create_scene_reactivate_updates_linked_repos():
    """create_custom_scene 对已存在（is_active=0）的场景做 reactivate 时更新 linked_repos。"""
    from src.api.pipeline_config import create_custom_scene, get_scenes
    create_custom_scene("test-lr-react", "React", "", RK, [{"id": "issue"}],
                        linked_repos=[{"repo_key": "gitcode:org/old", "branch": "main"}])
    # 第二次 create（reactivate 路径）更新 linked_repos
    new_linked = [{"repo_key": "gitcode:org/new", "branch": "dev"}]
    create_custom_scene("test-lr-react", "React", "", RK, [{"id": "issue"}],
                        linked_repos=new_linked)
    scenes = get_scenes(repo_key=RK)
    assert len(scenes) == 1
    assert scenes[0]["linked_repos"] == new_linked


# ===== event_pipeline: GRAPE_LINKED_REPOS 注入 =====

def _seed_repo(dev_scene=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, dev_scene) "
            "VALUES (?,?,?,?,?)",
            (RK, "org", "linked-repos-test", "linked-repos-test", dev_scene))
        db.commit()


def _seed_issue(issue_number, scene_id):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, scene_id) VALUES (?,?,?,?,?)",
            (0, "", RK, issue_number, scene_id))
        db.commit()


def _seed_scene(scene_id, linked_repos=None, advanced_options=None):
    lr_json = json.dumps(linked_repos or [], ensure_ascii=False) if linked_repos else "[]"
    ao = advanced_options or "{}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO devops_pipeline_scene "
            "(scene_id, name, repo_key, nodes_json, is_active, linked_repos, advanced_options) "
            "VALUES (?,?,?,?,?,?,?)",
            (scene_id, "LR Scene", RK, "[]", 1, lr_json, ao))
        db.commit()


def test_read_scene_linked_repos_from_column():
    """_read_scene_linked_repos 优先读独立列。"""
    from src.api.event_pipeline import _read_scene_linked_repos
    _seed_repo("lr-scene")
    _seed_issue(10, "lr-scene")
    linked = [{"repo_key": "gitcode:org/repo-a", "branch": "main"}]
    _seed_scene("lr-scene", linked_repos=linked)
    result = _read_scene_linked_repos(RK, "#10")
    assert result == linked


def test_read_scene_linked_repos_fallback_to_advanced_options():
    """独立列为空时回退读 advanced_options JSON。"""
    from src.api.event_pipeline import _read_scene_linked_repos
    _seed_repo("lr-scene-ao")
    _seed_issue(11, "lr-scene-ao")
    linked = [{"repo_key": "gitcode:org/repo-b", "branch": "dev"}]
    ao = json.dumps({"work_branch": "main", "linked_repos": linked})
    # linked_repos 列为 NULL，强制走 advanced_options 回退路径
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO devops_pipeline_scene "
            "(scene_id, name, repo_key, nodes_json, is_active, linked_repos, advanced_options) "
            "VALUES (?,?,?,?,?,?,?)",
            ("lr-scene-ao", "LR Scene", RK, "[]", 1, None, ao))
        db.commit()
    result = _read_scene_linked_repos(RK, "#11")
    assert result == linked


def test_read_scene_linked_repos_empty_when_no_scene():
    """无场景时返回空列表。"""
    from src.api.event_pipeline import _read_scene_linked_repos
    result = _read_scene_linked_repos(RK, "#999")
    assert result == []


def test_inject_linked_repos_env():
    """_enqueue_pipeline_task 注入 GRAPE_LINKED_REPOS（通过 _read_scene_linked_repos 验证）。"""
    from src.api.event_pipeline import _read_scene_linked_repos
    _seed_repo("lr-scene-env")
    _seed_issue(12, "lr-scene-env")
    linked = [{"repo_key": "gitcode:org/repo-c", "branch": "feature"}]
    _seed_scene("lr-scene-env", linked_repos=linked)
    # 模拟 event_pipeline 中的注入逻辑
    lr = _read_scene_linked_repos(RK, "#12")
    assert lr == linked
    env_val = json.dumps(lr, ensure_ascii=False)
    assert json.loads(env_val) == linked


# ===== repogit: 函数签名验证 =====

def test_repogit_ensure_repo_base_multi_exists():
    """ensure_repo_base_multi 函数存在且可调用。"""
    from src.runtime import repogit
    assert callable(repogit.ensure_repo_base_multi)
    # 空列表返回空 dict
    result = repogit.ensure_repo_base_multi([], token="")
    assert result == {}


def test_repogit_ensure_linked_repo_worktrees_exists():
    """ensure_linked_repo_worktrees 函数存在且可调用。"""
    from src.runtime import repogit
    assert callable(repogit.ensure_linked_repo_worktrees)
    # 非法 agent_type 返回空列表
    result = repogit.ensure_linked_repo_worktrees([], "invalid-agent", "999", token="")
    assert result == []


def test_repogit_ensure_linked_repo_worktrees_non_git_agent():
    """非 git agent 类型返回空列表（is_git_agent 守卫）。"""
    from src.runtime import repogit
    linked = [{"repo_key": "gitcode:org/repo-a", "branch": "main"}]
    result = repogit.ensure_linked_repo_worktrees(linked, "issue", "999", token="")
    assert result == []


# ===== vcs/api_client: create_pr_for_repo =====

def test_create_pr_for_repo_exists():
    """create_pr_for_repo 函数存在且可调用。"""
    from src.vcs.api_client import create_pr_for_repo
    assert callable(create_pr_for_repo)


def test_vcs_api_client_class_intact():
    """VcsApiClient 类方法完整（api_client.py 结构完整性回归）。"""
    from src.vcs.api_client import VcsApiClient
    client = VcsApiClient("gitcode", "fake-token", "owner", "repo")
    # 验证所有方法存在（防止类被意外拆分）
    assert hasattr(client, "create_pr")
    assert hasattr(client, "get_pr")
    assert hasattr(client, "merge_pr")
    assert hasattr(client, "get_repo_info")
    assert hasattr(client, "post_issue_comment")
    assert hasattr(client, "get_issue")
    assert hasattr(client, "close_issue")
    assert hasattr(client, "_gitcode_request")
    assert hasattr(client, "_github_request")
    assert hasattr(client, "_gitlab_request")
    assert hasattr(client, "_do_request")


# ===== scenes API Request 模型 =====

def test_create_scene_request_has_linked_repos():
    """CreateSceneRequest 模型含 linked_repos 字段。"""
    from src.api.routes.scenes import CreateSceneRequest
    req = CreateSceneRequest(name="test", nodes=[])
    assert hasattr(req, "linked_repos")
    assert req.linked_repos == []


def test_update_scene_request_has_linked_repos():
    """UpdateSceneRequest 模型含 linked_repos 字段。"""
    from src.api.routes.scenes import UpdateSceneRequest
    req = UpdateSceneRequest()
    assert hasattr(req, "linked_repos")
    assert req.linked_repos is None  # 默认 None=不更新


def test_advanced_options_request_has_linked_repos():
    """AdvancedOptionsRequest 模型含 linked_repos 字段。"""
    from src.api.routes.scenes import AdvancedOptionsRequest
    req = AdvancedOptionsRequest()
    assert hasattr(req, "linked_repos")
    assert req.linked_repos == []


def test_create_market_scene_request_has_linked_repos():
    """CreateMarketSceneRequest 模型含 linked_repos 字段。"""
    from src.api.routes.scenes import CreateMarketSceneRequest
    req = CreateMarketSceneRequest(name="test", nodes=[])
    assert hasattr(req, "linked_repos")


def test_update_market_scene_request_has_linked_repos():
    """UpdateMarketSceneRequest 模型含 linked_repos 字段。"""
    from src.api.routes.scenes import UpdateMarketSceneRequest
    req = UpdateMarketSceneRequest()
    assert hasattr(req, "linked_repos")
