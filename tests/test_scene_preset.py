"""Issue #246 场景预置自定义参数测试。

覆盖：merge 合并（仅追加不覆盖）、get 返回 preset 标记、save preset 保护
（不可删/不可改名/scope 锁定/仅值可更新）、空值沿用 default 或旧值。
"""
import json
import pytest

from src.session_manager import db, db_lock

RK = "gitcode:org/presetenv"


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


def _seed_scene(scene_id, advanced_options=None):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO devops_pipeline_scene (scene_id, name, repo_key, nodes_json, is_active, advanced_options) "
            "VALUES (?,?,?,?,?,?)",
            (scene_id, "Preset Scene", RK, "[]", 1, advanced_options or "{}"))
        db.commit()


def _read_ao(scene_id="s1"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT advanced_options FROM devops_pipeline_scene "
            "WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
            (scene_id, RK))
        row = c.fetchone()
    return json.loads(row[0]) if (row and row[0]) else {}


class _Body:
    def __init__(self, work_branch="main", custom_fields=None):
        self.work_branch = work_branch
        self.custom_fields = custom_fields or []


_PRESET_JSON = json.dumps([
    {"name": "AI_MODEL", "is_secret": False, "scope": ["all"], "default_value": ""},
    {"name": "AI_API_KEY", "is_secret": True, "scope": ["coding"], "default_value": ""},
])


def test_merge_preset_appends_missing_only():
    from src.api.routes.scenes import _merge_preset_custom_fields
    _seed_scene("s1")  # 初始 advanced_options={}
    _merge_preset_custom_fields("s1", RK, _PRESET_JSON)
    ao = _read_ao()
    names = {f["name"] for f in ao["custom_fields"]}
    assert names == {"AI_MODEL", "AI_API_KEY"}
    assert ao["custom_fields"][0]["preset"] is True
    assert "preset_custom_fields" in ao


def test_merge_preset_does_not_overwrite_user_config():
    from src.api.routes.scenes import _merge_preset_custom_fields
    _seed_scene("s1", json.dumps({
        "custom_fields": [{"name": "AI_MODEL", "value": "user-chosen", "is_secret": False, "scope": ["all"], "preset": False}],
    }))
    _merge_preset_custom_fields("s1", RK, _PRESET_JSON)
    ao = _read_ao()
    by_name = {f["name"]: f for f in ao["custom_fields"]}
    assert by_name["AI_MODEL"]["value"] == "user-chosen"  # 不覆盖
    assert "AI_API_KEY" in by_name                          # 缺失项被追加


def test_get_advanced_options_returns_preset_flag():
    from src.api.routes.scenes import get_advanced_options
    _seed_scene("s1", json.dumps({
        "custom_fields": [
            {"name": "AI_MODEL", "value": "gpt", "is_secret": False, "scope": ["all"], "preset": True},
            {"name": "MY_OWN", "value": "v", "is_secret": False, "scope": ["all"], "preset": False},
        ],
    }))
    d = get_advanced_options("s1", user={"user_id": 0}, repo="org/presetenv")
    by_name = {x["name"]: x for x in d["custom_fields"]}
    assert by_name["AI_MODEL"]["preset"] is True
    assert by_name["MY_OWN"]["preset"] is False


def test_save_preset_protected_fields():
    from src.api.routes.scenes import save_advanced_options, get_advanced_options
    _seed_scene("s1", json.dumps({
        "custom_fields": [
            {"name": "AI_MODEL", "value": "gpt", "is_secret": False, "scope": ["all"], "preset": True},
            {"name": "AI_API_KEY", "value": "secret-1", "is_secret": True, "scope": ["coding"], "preset": True},
        ],
        "preset_custom_fields": json.loads(_PRESET_JSON),
    }))
    # 前端提交：preset 项只带 value 变化；试图改名/改 scope 应被后端锁定为声明值
    save_advanced_options(
        "s1",
        _Body("main", [
            {"name": "AI_MODEL", "value": "gpt4", "is_secret": False, "scope": ["sre"], "preset": True},
            {"name": "HACKED", "value": "x", "is_secret": False, "scope": ["all"], "preset": False},
        ]),
        user={"user_id": 0}, repo="org/presetenv")

    # 保存后重新 GET：
    d = get_advanced_options("s1", user={"user_id": 0}, repo="org/presetenv")
    names = {x["name"] for x in d["custom_fields"]}
    by_name = {x["name"]: x for x in d["custom_fields"]}
    # preset 项保留；改名不生效（仍为 AI_MODEL，无改名后的 key）
    assert "AI_MODEL" in names and "AI_API_KEY" in names and "HACKED" in names
    preset_names = {x["name"] for x in d["custom_fields"] if x["preset"]}
    assert preset_names == {"AI_MODEL", "AI_API_KEY"}
    assert by_name["AI_MODEL"]["value"] == "gpt4"       # 值更新生效
    assert by_name["AI_MODEL"]["scope"] == ["all"]      # scope 被锁回声明
    # AI_API_KEY 未被提交仍保留旧值
    assert by_name["AI_API_KEY"]["value"] == "secret-1"


def test_save_preset_cannot_be_deleted():
    from src.api.routes.scenes import save_advanced_options, get_advanced_options
    _seed_scene("s1", json.dumps({
        "custom_fields": [
            {"name": "AI_API_KEY", "value": "secret-1", "is_secret": True, "scope": ["coding"], "preset": True},
        ],
        "preset_custom_fields": json.loads(_PRESET_JSON),
    }))
    # 前端只提交一个自定义项，未带 AI_API_KEY（试图删除）
    save_advanced_options(
        "s1",
        _Body("main", [{"name": "NEW_VAR", "value": "n", "is_secret": False, "scope": ["all"], "preset": False}]),
        user={"user_id": 0}, repo="org/presetenv")
    d = get_advanced_options("s1", user={"user_id": 0}, repo="org/presetenv")
    names = {x["name"] for x in d["custom_fields"]}
    assert "AI_API_KEY" in names      # preset 不可删
    assert names >= {"AI_API_KEY", "NEW_VAR"}


def test_merge_preset_default_value_applied():
    from src.api.routes.scenes import _merge_preset_custom_fields
    preset = json.dumps([
        {"name": "AI_BASE_URL", "is_secret": False, "scope": ["all"], "default_value": "http://localhost:8080"},
    ])
    _seed_scene("s1")
    _merge_preset_custom_fields("s1", RK, preset)
    ao = _read_ao()
    assert ao["custom_fields"][0]["value"] == "http://localhost:8080"
    assert ao["custom_fields"][0]["preset"] is True