"""Issue #1841: 场景注入 work_branch 存在性校验 / 回退 / prompt 标注 / 存量校正。

覆盖：
1. resolve_work_branch：有效分支原样返回；失效分支回退仓库默认分支并告警（含原配置值）；
   分支列表不可核实（None）时保持原配置不误判。
2. read_effective_scene_work_branch：场景注入读取处统一返回生效分支。
3. event_pipeline.build_task_env：注入的 GRAPE_WORK_BRANCH 为回退后的默认分支。
4. scheduler autopilot：task.work_branch 失效时 env 注入默认分支。
5. prompt_builder：prompt 可见实际生效分支与回退原因。
6. 场景高级选项保存 API：失效 work_branch 返回明确 400 校验错误。
7. scripts/audit_scene_work_branch.py：存量失效清单识别 + 批量校正。
"""
import importlib.util
import json
import logging
import os
import subprocess

import pytest

from src.runtime import repogit
from src.session_manager import db, db_lock

RK = "gitcode:org/wb1841"
REPO = "org/wb1841"


@pytest.fixture(autouse=True)
def _clean():
    repogit.invalidate_branch_list_cache()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (RK,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (RK,))
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (RK,))
        c.execute("DELETE FROM autopilot_tasks WHERE name LIKE 'wb1841_%'")
        db.commit()
    yield
    repogit.invalidate_branch_list_cache()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (RK,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (RK,))
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (RK,))
        c.execute("DELETE FROM autopilot_tasks WHERE name LIKE 'wb1841_%'")
        db.commit()


def _patch_branch_list(monkeypatch, branches, default="main", target=None):
    mod = target or repogit

    def _fake(repo_key, token="", refresh=False):
        if branches is None:
            return None, ""
        return set(branches), default

    monkeypatch.setattr(mod, "list_repo_branches", _fake)


def _seed_scene(scene_id="scene-1841", work_branch="weekly-ghost"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO devops_pipeline_scene "
            "(scene_id, name, repo_key, nodes_json, is_active, advanced_options) "
            "VALUES (?,?,?,?,?,?)",
            (scene_id, "Scene 1841", RK, "[]", 1,
             json.dumps({"work_branch": work_branch, "custom_fields": []})))
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, scene_id) "
            "VALUES (?,?,?,?,?)", (0, "", RK, 5, scene_id))
        db.commit()


def test_resolve_valid_branch_is_kept(monkeypatch):
    _patch_branch_list(monkeypatch, ["main", "weekly-10"])
    effective, note = repogit.resolve_work_branch(RK, "weekly-10")
    assert effective == "weekly-10"
    assert note == ""


def test_resolve_invalid_branch_falls_back_and_warns(monkeypatch, caplog):
    _patch_branch_list(monkeypatch, ["main", "weekly-10"], default="weekly-10")
    with caplog.at_level(logging.WARNING):
        effective, note = repogit.resolve_work_branch(RK, "weekly-1102-zrr")
    assert effective == "weekly-10"
    assert "weekly-1102-zrr" in note and RK in note
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("weekly-1102-zrr" in m and RK in m and "weekly-10" in m for m in msgs), msgs


def test_resolve_unverifiable_keeps_configured(monkeypatch):
    _patch_branch_list(monkeypatch, None)
    effective, note = repogit.resolve_work_branch(RK, "weekly-1102-zrr")
    assert effective == "weekly-1102-zrr"
    assert note == ""


def test_read_effective_scene_work_branch(monkeypatch):
    _seed_scene()
    _patch_branch_list(monkeypatch, ["main", "weekly-10"], default="weekly-10")
    assert repogit.read_effective_scene_work_branch(RK, "#5") == "weekly-10"


def test_build_task_env_injects_effective_branch(monkeypatch):
    _seed_scene()
    _patch_branch_list(monkeypatch, ["main", "weekly-10"], default="weekly-10")
    from src.api.event_pipeline import build_task_env
    env = build_task_env(RK, "coding", "#5")
    assert env["GRAPE_WORK_BRANCH"] == "weekly-10"


def test_autopilot_env_injects_effective_branch(monkeypatch, tmp_path, caplog):
    _patch_branch_list(monkeypatch, ["main", "weekly-10"], default="weekly-10")
    from src.runtime import scheduler
    import src.runtime.service as service

    captured = {}
    monkeypatch.setattr(service, "runtime_execution_kind", lambda _id: "direct")
    monkeypatch.setattr(scheduler, "_build_autopilot_prompt", lambda *a, **k: "prompt")
    monkeypatch.setattr("src.runtime.engine_select.pick_engine", lambda *a, **k: 1)
    monkeypatch.setattr("src.runtime.engine_select.engine_name", lambda _id: "engine-1")
    monkeypatch.setattr("src.runtime.instances.runtime_instance_name", lambda _id: "inst-1")
    monkeypatch.setattr("src.api.event_pipeline.get_agent_workdir", lambda *a, **k: str(tmp_path))
    monkeypatch.setattr("src.api.event_pipeline.get_agent_display_name", lambda _at: "Issue Agent")
    monkeypatch.setattr("src.api.event_pipeline._resolve_bot_username", lambda _rk: "grape_bot")
    monkeypatch.setattr("src.api.event_pipeline._materialize_direct_workdir", lambda *a, **k: "prompt")
    monkeypatch.setattr("src.prompt_builder.resolve_task_server_env", lambda *a, **k: {})
    monkeypatch.setattr("src.user.db.get_gitcode_token", lambda *a, **k: "")

    def _capture(*args, **kwargs):
        captured["env_extra"] = args[12]

    monkeypatch.setattr(scheduler, "_execute_autopilot_direct", _capture)

    with caplog.at_level(logging.WARNING):
        scheduler._execute_autopilot_agent(
            RK, {"id": 1841, "skill_name": "s", "work_branch": "weekly-1102-zrr"}, 1841)

    assert captured["env_extra"]["GRAPE_WORK_BRANCH"] == "weekly-10"
    assert any("weekly-1102-zrr" in r.getMessage()
               for r in caplog.records if r.levelno >= logging.WARNING)


def test_prompt_marks_effective_branch_and_reason(monkeypatch):
    _seed_scene()
    _patch_branch_list(monkeypatch, ["main", "weekly-10"], default="weekly-10")
    from src.prompt_builder import _build_scene_advanced_options
    import src.prompt_builder as pb
    pb._advanced_options_cache.clear()
    text = _build_scene_advanced_options(RK, "#5")
    assert "实际生效分支: weekly-10" in text
    assert "weekly-ghost" in text


def test_save_advanced_options_rejects_missing_branch(monkeypatch):
    from fastapi import HTTPException
    from src.api.routes.scenes import save_advanced_options

    _seed_scene()
    _patch_branch_list(monkeypatch, ["main", "weekly-10"], default="weekly-10")

    class _Body:
        def __init__(self, work_branch, custom_fields=None, linked_repos=None):
            self.work_branch = work_branch
            self.custom_fields = custom_fields or []
            self.linked_repos = linked_repos or []

    with pytest.raises(HTTPException) as ei:
        save_advanced_options("scene-1841", _Body("weekly-1102-zrr"),
                              user={"user_id": 0}, repo=REPO)
    assert ei.value.status_code == 400
    assert "weekly-1102-zrr" in ei.value.detail["message"]
    assert ei.value.detail["field"] == "work_branch"

    # 有效分支可正常保存
    save_advanced_options("scene-1841", _Body("weekly-10"),
                          user={"user_id": 0}, repo=REPO)
    with db_lock:
        row = db.cursor().execute(
            "SELECT advanced_options FROM devops_pipeline_scene WHERE scene_id=? AND repo_key=?",
            ("scene-1841", RK)).fetchone()
    assert json.loads(row[0])["work_branch"] == "weekly-10"


def _load_script():
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    path = os.path.join(repo, "scripts", "audit_scene_work_branch.py")
    spec = importlib.util.spec_from_file_location("audit_scene_work_branch", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_audit_script_lists_and_fixes_stale_branch(monkeypatch):
    _seed_scene()
    script = _load_script()
    _patch_branch_list(monkeypatch, ["main", "weekly-10"], default="weekly-10", target=script)

    result = script.scan(repo_key=RK)
    assert result["checked"] == 1
    assert len(result["invalid"]) == 1
    item = result["invalid"][0]
    assert item["work_branch"] == "weekly-ghost"
    assert item["suggested_branch"] == "weekly-10"

    assert script.apply_fix(result["invalid"]) == 1
    with db_lock:
        row = db.cursor().execute(
            "SELECT advanced_options FROM devops_pipeline_scene WHERE scene_id=? AND repo_key=?",
            ("scene-1841", RK)).fetchone()
    opts = json.loads(row[0])
    assert opts["work_branch"] == "weekly-10"
    assert opts["custom_fields"] == []
    # 幂等：再扫描不再报告 invalid
    assert script.scan(repo_key=RK)["invalid"] == []

# ===================== Issue #1841 QA 复审：真实分支列表 / 分页 / 缓存 / 建分支刷新 =====================

def _git(args, cwd):
    return subprocess.run(["git", "-C", str(cwd)] + args, capture_output=True, text=True)


def _make_source_repo(tmp_path, branch="main"):
    src = tmp_path / f"src-{branch.replace('/', '-')}"
    src.mkdir()
    _git(["init", "-b", branch], src)
    _git(["config", "user.email", "test@example.com"], src)
    _git(["config", "user.name", "Test User"], src)
    (src / "a.txt").write_text("v1", encoding="utf-8")
    _git(["add", "-A"], src)
    _git(["commit", "-m", "init"], src)
    return src


def test_local_repo_branch_names_reads_mirror_and_origin_head(tmp_path, monkeypatch):
    src = _make_source_repo(tmp_path)
    _git(["branch", "weekly-10"], src)
    base = tmp_path / "codebase"
    _git(["init", "--bare", str(base)], tmp_path)
    _git(["remote", "add", "origin", str(src)], base)
    _git(["fetch", "origin", "+refs/heads/*:refs/remotes/origin/*"], base)
    _git(["symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/weekly-10"], base)
    monkeypatch.setattr(repogit, "repo_base_dir", lambda _rk: str(base))

    branches, default = repogit._local_repo_branch_names(RK)
    assert branches == {"main", "weekly-10"}
    assert default == "weekly-10"


def test_ensure_repo_base_records_origin_head(tmp_path, monkeypatch):
    src = _make_source_repo(tmp_path)
    monkeypatch.setattr("src.config.WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr(repogit, "clone_url_for", lambda _rk: str(src))

    base = repogit.ensure_repo_base(RK)
    assert base and os.path.isdir(base)
    r = _git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], base)
    assert r.stdout.strip() == "origin/main"
    branches, default = repogit._local_repo_branch_names(RK)
    assert default == "main" and "main" in branches


def test_fetch_remote_branch_names_paginates_and_reads_default(monkeypatch):
    pages = {
        1: [{"name": f"b{i}"} for i in range(100)],
        2: [{"name": f"c{i}"} for i in range(100)],
        3: [{"name": "weekly-10"}],
    }

    def _fake_req(method, url, token):
        if f"{RK.split(':', 1)[1]}" not in url:
            return {}
        if "/branches?" in url:
            page = int(url.rsplit("page=", 1)[1])
            return pages.get(page, [])
        if url.endswith(f"/repos/{RK.split(':', 1)[1]}"):
            return {"default_branch": "weekly-10"}
        return {}

    monkeypatch.setattr(repogit, "_branch_lookup_token", lambda _rk: "tok")
    monkeypatch.setattr("src.user.repo_manager._req", _fake_req)
    repogit.invalidate_branch_list_cache(force_remote=False)

    branches, default = repogit._fetch_remote_branch_names(RK)
    assert len(branches) == 201
    assert "weekly-10" in branches
    assert default == "weekly-10"


def test_fetch_remote_branch_names_truncation_is_unverifiable(monkeypatch):
    def _fake_req(method, url, token):
        if "/branches?" in url:
            return [{"name": f"b{url.rsplit('page=', 1)[1]}_{i}"} for i in range(100)]
        return {"default_branch": "main"}

    monkeypatch.setattr(repogit, "_branch_lookup_token", lambda _rk: "tok")
    monkeypatch.setattr("src.user.repo_manager._req", _fake_req)
    repogit.invalidate_branch_list_cache(force_remote=False)

    branches, default = repogit._fetch_remote_branch_names(RK)
    assert branches is None
    assert default == ""


def test_branch_cache_success_and_failure_ttl(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(repogit.time, "time", lambda: clock["t"])
    repogit.invalidate_branch_list_cache(force_remote=False)

    repogit._branch_list_cache_put(RK, {"main"}, "main")
    assert repogit._branch_list_cache_get(RK) == ({"main"}, "main")
    clock["t"] += 91
    assert repogit._branch_list_cache_get(RK) is None

    repogit._branch_list_cache_put(RK, None, "")
    clock["t"] += 29
    assert repogit._branch_list_cache_get(RK) == (None, "")
    clock["t"] += 2
    assert repogit._branch_list_cache_get(RK) is None


def test_invalidate_forces_remote_lookup(monkeypatch):
    monkeypatch.setattr(repogit, "_local_repo_branch_names",
                        lambda _rk: ({"stale"}, "stale"))
    monkeypatch.setattr(repogit, "_fetch_remote_branch_names",
                        lambda _rk, token="": ({"new"}, "new"))
    monkeypatch.setattr(repogit, "_fetch_remote_default_branch", lambda *a, **k: "new")
    repogit.invalidate_branch_list_cache(force_remote=False)

    repogit.invalidate_branch_list_cache(RK)  # 建分支后失效
    assert repogit.list_repo_branches(RK) == ({"new"}, "new")
    # 标记已消费，后续命中缓存，不再穿透
    assert repogit.list_repo_branches(RK) == ({"new"}, "new")


def test_refresh_repo_branch_mirror_makes_new_branch_visible(tmp_path, monkeypatch):
    src = _make_source_repo(tmp_path)
    monkeypatch.setattr("src.config.WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr(repogit, "clone_url_for", lambda _rk: str(src))
    repogit.ensure_repo_base(RK)

    _git(["branch", "feat/new"], src)
    assert "feat/new" not in repogit._local_repo_branch_names(RK)[0]
    assert repogit.refresh_repo_branch_mirror(RK, "feat/new") is True
    assert "feat/new" in repogit._local_repo_branch_names(RK)[0]


def test_resolve_main_validates_when_branch_list_available(monkeypatch):
    """Spec 5：main/master 不再无条件放行，列表可核实时按实际存在性判定。"""
    _patch_branch_list(monkeypatch, ["develop", "release"], default="develop")
    effective, note = repogit.resolve_work_branch(RK, "main")
    assert effective == "develop"
    assert "main" in note

    ok, msg, suggestion = repogit.validate_work_branch_exists(RK, "master")
    assert ok is False
    assert suggestion == "develop" and "master" in msg


@pytest.fixture
def autopilot_client():
    from fastapi.testclient import TestClient
    from src.api.deps import get_current_user
    from src.api.main import app
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 0, "username": "alice", "auth_type": "session_token"}
    try:
        with TestClient(app) as cli:
            yield cli
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def _task_body(work_branch, name="wb1841_task"):
    return {
        "name": name,
        "repo_key": RK,
        "instruction": "run daily check",
        "cron": "0 9 * * *",
        "max_retry": 3,
        "work_branch": work_branch,
    }


class TestAutopilotWorkBranchValidation:
    """autopilot 创建/更新任务的 work_branch 校验（QA 指出的零测试点）。"""

    def test_create_rejects_missing_branch(self, autopilot_client, monkeypatch):
        _patch_branch_list(monkeypatch, ["main", "weekly-10"], default="weekly-10")
        r = autopilot_client.post("/api/autopilot/tasks", json=_task_body("weekly-ghost"))
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert detail["field"] == "work_branch"
        assert "weekly-ghost" in detail["message"]

    def test_create_accepts_existing_branch(self, autopilot_client, monkeypatch):
        _patch_branch_list(monkeypatch, ["main", "weekly-10"], default="weekly-10")
        r = autopilot_client.post("/api/autopilot/tasks", json=_task_body("weekly-10"))
        assert r.status_code == 200, r.text

    def test_update_rejects_missing_branch(self, autopilot_client, monkeypatch):
        _patch_branch_list(monkeypatch, ["main", "weekly-10"], default="weekly-10")
        r = autopilot_client.post("/api/autopilot/tasks", json=_task_body("weekly-10"))
        assert r.status_code == 200, r.text
        task_id = r.json()["id"]
        r2 = autopilot_client.put(f"/api/autopilot/tasks/{task_id}",
                                  json={"work_branch": "weekly-ghost"})
        assert r2.status_code == 400, r2.text
        assert r2.json()["detail"]["field"] == "work_branch"

    def test_update_clearing_work_branch_skips_validation(self, autopilot_client, monkeypatch):
        _patch_branch_list(monkeypatch, ["main", "weekly-10"], default="weekly-10")
        r = autopilot_client.post("/api/autopilot/tasks", json=_task_body("weekly-10"))
        assert r.status_code == 200, r.text
        task_id = r.json()["id"]
        r2 = autopilot_client.put(f"/api/autopilot/tasks/{task_id}",
                                  json={"work_branch": ""})
        assert r2.status_code == 200, r2.text
