"""GRAPE_SCENE_ID 环境变量注入测试（issue-219：场景标识统一 env 注入）。

覆盖：issue 场景来源 / 仓库级 dev_scene 回退 / 空场景注入空串 /
custom_fields 直接注入（Issue #236：name 直接作为变量名注入 + 冲突/大写兜底 + secret 不回显）。"""
import json
import pytest

from src.session_manager import db, db_lock

RK = "gitcode:org/sceneenv"


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (RK,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (RK,))
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (RK,))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (RK,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (RK,))
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (RK,))
        db.commit()


def _seed_repo(dev_scene=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, dev_scene) "
            "VALUES (?,?,?,?,?)",
            (RK, "org", "sceneenv", "sceneenv", dev_scene))
        db.commit()


def _seed_issue(issue_number, scene_id):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, scene_id) VALUES (?,?,?,?,?)",
            (0, "", RK, issue_number, scene_id))
        db.commit()


def _seed_scene(scene_id, advanced_options=None):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO devops_pipeline_scene (scene_id, name, repo_key, nodes_json, is_active, advanced_options) "
            "VALUES (?,?,?,?,?,?)",
            (scene_id, "DevOps Scene", RK, "[]", 1, advanced_options or "{}"))
        db.commit()


def _inject(issue_id="5"):
    from src.api.event_pipeline import _inject_scene_env
    env_extra = {}
    _inject_scene_env(env_extra, RK, issue_id)
    return env_extra


def test_injects_scene_id_and_custom_fields_from_issue_scene():
    _seed_issue(5, "devops-scene")
    _seed_scene("devops-scene", '{"work_branch": "main", "custom_fields": [{"name": "ENV", "value": "prod"}]}')
    env = _inject()
    assert env["GRAPE_SCENE_ID"] == "devops-scene"
    assert env["ENV"] == "prod"


def test_injects_custom_field_name_as_var_name_directly():
    _seed_issue(5, "devops-scene")
    _seed_scene("devops-scene", '{"custom_fields": [{"name": "MY_API_KEY", "value": "xxx"}]}')
    env = _inject()
    assert env["MY_API_KEY"] == "xxx"
    assert "GRAPE_CF_1" not in env
    assert "GRAPE_CF_MAP" not in env


def test_custom_field_conflict_with_reserved_skipped():
    _seed_issue(5, "devops-scene")
    _seed_scene("devops-scene", '{"custom_fields": [{"name": "PATH", "value": "x"}, {"name": "GRAPE_XXX", "value": "y"}, {"name": "GOOD_KEY", "value": "z"}]}')
    env = _inject()
    assert "PATH" not in env
    assert "GRAPE_XXX" not in env
    assert env["GOOD_KEY"] == "z"


def test_custom_field_lowercase_skipped_at_runtime():
    _seed_issue(5, "devops-scene")
    _seed_scene("devops-scene", '{"custom_fields": [{"name": "my_key", "value": "x"}]}')
    env = _inject()
    assert "my_key" not in env


def test_secret_masked_from_get_but_injected():
    """secret 项：get_advanced_options 不回显明文 value（仅 value_exist），注入仍用明文。"""
    _seed_issue(5, "devops-scene")
    _seed_scene("devops-scene", '{"custom_fields": [{"name": "DEPLOY_SECRET", "value": "ultra-secret", "is_secret": true}]}')
    env = _inject()
    assert env["DEPLOY_SECRET"] == "ultra-secret"

    from src.scene_custom_env import normalize_custom_field
    f = normalize_custom_field({"name": "DEPLOY_SECRET", "value": "ultra-secret", "is_secret": True})
    assert f["is_secret"] is True
    f2 = normalize_custom_field({"name": "DEPLOY_URL", "value": "http://x"})
    assert f2["is_secret"] is False
    f3 = normalize_custom_field({"name": "MY_API_KEY", "value": "k"})
    assert f3["is_secret"] is True  # _KEY 后缀自动识别


def test_save_advanced_options_validates_uppercase_and_reserved():
    from fastapi import HTTPException
    from src.api.routes.scenes import save_advanced_options, get_advanced_options
    _seed_issue(5, "devops-scene")
    _seed_scene("devops-scene", '{}')

    def _save(fields):
        return save_advanced_options("devops-scene",
                                     _Body(work_branch="main", custom_fields=fields),
                                     user={"user_id": 0}, repo="org/sceneenv")

    class _Body:
        def __init__(self, work_branch="", custom_fields=None):
            self.work_branch = work_branch
            self.custom_fields = custom_fields or []

    # 小写拒绝
    try:
        _save([{"name": "bad_key", "value": "1"}])
        assert False, "lowercase name should be rejected"
    except HTTPException as e:
        assert e.status_code == 400
    # 保留变量拒绝
    try:
        _save([{"name": "PATH", "value": "1"}])
        assert False, "reserved name should be rejected"
    except HTTPException as e:
        assert e.status_code == 400
    # 正常保存 secret 项
    _save([{"name": "GOOD_KEY", "value": "1", "is_secret": True}])
    d = get_advanced_options("devops-scene", user={"user_id": 0}, repo="org/sceneenv")
    assert d["custom_fields"][0]["name"] == "GOOD_KEY"
    assert d["custom_fields"][0]["is_secret"] is True
    assert "value" not in d["custom_fields"][0]


def test_secret_blank_value_preserves_old_value():
    from fastapi import HTTPException
    from src.api.routes.scenes import save_advanced_options, get_advanced_options
    _seed_issue(5, "devops-scene")
    _seed_scene("devops-scene", '{}')

    class _Body:
        def __init__(self, work_branch="", custom_fields=None):
            self.work_branch = work_branch
            self.custom_fields = custom_fields or []

    save_advanced_options("devops-scene",
                          _Body(work_branch="main", custom_fields=[{"name": "DEPLOY_SECRET", "value": "orig", "is_secret": True}]),
                          user={"user_id": 0}, repo="org/sceneenv")
    # 掩码已存在 → 前端提交空 value + is_secret → 后端保留旧值
    save_advanced_options("devops-scene",
                          _Body(work_branch="main", custom_fields=[{"name": "DEPLOY_SECRET", "value": "", "is_secret": True}]),
                          user={"user_id": 0}, repo="org/sceneenv")
    d = get_advanced_options("devops-scene", user={"user_id": 0}, repo="org/sceneenv")
    assert d["custom_fields"][0]["name"] == "DEPLOY_SECRET"
    assert d["custom_fields"][0]["value_exist"] is True


def test_unchecking_secret_keeps_value_and_returns_plaintext():
    """取消密钥勾选：keep_value 保留旧值 + is_secret=false → get 返回明文 value。"""
    from src.api.routes.scenes import save_advanced_options, get_advanced_options
    _seed_issue(5, "devops-scene")
    _seed_scene("devops-scene", '{}')

    class _Body:
        def __init__(self, work_branch="", custom_fields=None):
            self.work_branch = work_branch
            self.custom_fields = custom_fields or []

    # 先存为 secret
    save_advanced_options("devops-scene",
                          _Body(work_branch="main", custom_fields=[{"name": "MY_KEY", "value": "real-value", "is_secret": True}]),
                          user={"user_id": 0}, repo="org/sceneenv")
    # 用户取消勾选 + 掩码未改 → keep_value=true, is_secret=false → 值保留、变为明文
    save_advanced_options("devops-scene",
                          _Body(work_branch="main", custom_fields=[{"name": "MY_KEY", "value": "********", "is_secret": False, "keep_value": True}]),
                          user={"user_id": 0}, repo="org/sceneenv")
    d = get_advanced_options("devops-scene", user={"user_id": 0}, repo="org/sceneenv")
    item = d["custom_fields"][0]
    assert item["name"] == "MY_KEY"
    assert item["is_secret"] is False
    assert item["value"] == "real-value"
    # 掩码字面量不应被写入库
    assert item["value"] != "********"


def test_fallback_to_repo_dev_scene():
    _seed_repo("repo-scene")
    _seed_scene("repo-scene", '{"custom_fields": []}')
    env = _inject()
    assert env["GRAPE_SCENE_ID"] == "repo-scene"
    assert "ENV" not in env


def test_empty_when_no_scene():
    env = _inject()
    assert "GRAPE_SCENE_ID" in env
    assert env["GRAPE_SCENE_ID"] == ""


def test_scene_with_work_branch_only_sets_scene_id():
    _seed_issue(5, "devops-scene")
    _seed_scene("devops-scene", '{"work_branch": "main"}')
    env = _inject()
    assert env["GRAPE_SCENE_ID"] == "devops-scene"
    assert "MY_API_KEY" not in env
    assert "GRAPE_CF_MAP" not in env


def test_resolve_scene_id_helper():
    from src.api.event_pipeline import _resolve_scene_id
    assert _resolve_scene_id(RK, "5") == ""          # 无场景
    _seed_issue(5, "devops-scene")
    assert _resolve_scene_id(RK, "#5") == "devops-scene"   # issue 场景（# 前缀兼容）
    assert _resolve_scene_id(RK, "") == ""           # 无 issue → 空