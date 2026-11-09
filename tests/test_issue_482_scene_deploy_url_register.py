"""Issue #482: SRE 完成后部署 URL 自动登记到需求绑定场景（双路径统一）测试。

覆盖：
- register_scene_deploy_url 共享函数：issue 绑定场景命中登记、结构化/正则提取、
  无场景绑定不写库、非法/超长 URL 拒绝、仅更新绑定场景（其他场景不受影响）、
  幂等覆盖、未知场景不写库。
- complete_task() 远程收尾路径钩子：sre 成功登记场景 + 仓库级（#468 不回退）、
  非 sre / 失败不触发。
- 双路径调用点源码检查：_execute_agent_inner 与 complete_task 均接入
  register_scene_deploy_url（防遗漏回归）。

模式：仿 test_issue_468_sre_deploy_url_register.py（独立测试库 + 种子）。
"""
import inspect
import json
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_i482_scene_deploy_url.db")
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"

import pytest

SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from src.session_manager import db, db_lock
from src.api.db_setup import init_database

init_database()

REPO = "gitcode:i482/sre-repo"
TASK = "t_i482_sre"
SCENE = "scene-i482-bound"
SCENE_OTHER = "scene-i482-other"
ISSUE_NUM = 482


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (TASK,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?", (REPO, ISSUE_NUM))
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (REPO,))
        c.execute("INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name, deploy_url) "
                  "VALUES (?,?,?,?,?)", (REPO, "i482", "sre-repo", "i482/sre-repo", ""))
        c.execute("INSERT INTO issue (repo_key, issue_number, scene_id, scene_user_id, user_id, assignee) "
                  "VALUES (?,?,?,0,1,'')", (REPO, ISSUE_NUM, SCENE))
        for sid in (SCENE, SCENE_OTHER):
            c.execute("INSERT INTO devops_pipeline_scene (scene_id, name, repo_key, is_active, deploy_url) "
                      "VALUES (?,?,?,1,'')", (sid, sid, REPO))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (TASK,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?", (REPO, ISSUE_NUM))
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (REPO,))
        db.commit()


def _scene_url(sid=SCENE):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT deploy_url FROM devops_pipeline_scene WHERE scene_id=? AND repo_key=?",
                  (sid, REPO))
        row = c.fetchone()
    return (row[0] if row else "") or ""


def _register(result_text, issue_key=f"#{ISSUE_NUM}"):
    from src.api.event_pipeline import register_scene_deploy_url
    return register_scene_deploy_url(REPO, issue_key, result_text)


# ===== 绑定场景登记 =====

def test_structured_artifacts_hit():
    text = json.dumps({
        "artifacts": ["deploy_url: https://tunnel482.example.com/abc/"],
        "issue_summary": "done",
    })
    assert _register(text) == "https://tunnel482.example.com/abc/"
    assert _scene_url() == "https://tunnel482.example.com/abc/"


def test_regex_fallback_full_path():
    text = "部署完成，访问：https://s482.example.com:443/t/x/index.html 验收。"
    assert _register(text) == "https://s482.example.com:443/t/x/index.html"
    assert _scene_url() == "https://s482.example.com:443/t/x/index.html"


def test_only_bound_scene_updated():
    _register("done deploy_url: https://only-bound.example.com/app")
    assert _scene_url(SCENE) == "https://only-bound.example.com/app"
    assert _scene_url(SCENE_OTHER) == ""


def test_idempotent_reregister_overwrites():
    assert _register("deploy_url: https://v1.example.com/") == "https://v1.example.com/"
    assert _register("deploy_url: https://v2.example.com/") == "https://v2.example.com/"
    assert _scene_url() == "https://v2.example.com/"


# ===== 无场景绑定 / 校验拒绝 =====

def test_no_scene_binding_noop():
    from src.api.event_pipeline import register_scene_deploy_url
    # issue 未种 scene_id（issue 表无行）→ 无绑定，不登记不报错
    assert register_scene_deploy_url(REPO, "#999999", "deploy_url: https://none.example.com/") == ""
    assert _scene_url() == ""


def test_reject_non_http_scheme():
    assert _register("javascript:alert(1)") == ""
    assert _scene_url() == ""


def test_reject_too_long():
    assert _register("http://" + "a" * 600) == ""
    assert _scene_url() == ""


def test_empty_result_text_noop():
    assert _register("") == ""
    assert _register(None) == ""
    assert _scene_url() == ""


def test_unknown_scene_id_noop():
    from src.api.event_pipeline import register_scene_deploy_url
    # 场景行不存在（rowcount=0）→ 不写库返回空
    assert register_scene_deploy_url(REPO, "#482", "x") == ""
    _seed_issue_scene("scene-not-exists")
    assert register_scene_deploy_url(REPO, "#482", "deploy_url: https://ghost.example.com/") == ""


def _seed_issue_scene(sid):
    with db_lock:
        db.cursor().execute("UPDATE issue SET scene_id=? WHERE repo_key=? AND issue_number=?",
                            (sid, REPO, ISSUE_NUM))
        db.commit()


# ===== complete_task() 远程收尾路径钩子 =====

def _seed_task(agent_type="sre"):
    with db_lock:
        db.cursor().execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, agent_type, prompt, status, issue_id) "
            "VALUES (?, 'sess', 1, ?, ?, 'p', 'running', '#482')",
            (TASK, REPO, agent_type),
        )
        db.commit()


@pytest.fixture()
def _quiet_remote_hooks(monkeypatch):
    """屏蔽 complete_task 远程钩子中的外呼副作用（回帖/评论同步），只保留登记钩子。"""
    monkeypatch.setattr("src.api.event_pipeline._post_result_to_issue", lambda *a, **k: None)
    monkeypatch.setattr("src.api.event_pipeline._sync_issue_comments_to_conversation", lambda *a, **k: None)
    monkeypatch.setattr("src.api.event_pipeline._post_subtask_completion_to_parent", lambda *a, **k: None)
    monkeypatch.setattr("src.api.event_pipeline._post_all_subtasks_done", lambda *a, **k: None)
    monkeypatch.setattr("src.runtime.service._handle_vod_recommend_complete", lambda *a, **k: None)
    monkeypatch.setattr("src.token_resolver.create_provider_from_repo_key", lambda *a, **k: None)


def _repo_url():
    with db_lock:
        c = db.cursor()
        c.execute("SELECT deploy_url FROM managed_repos WHERE repo_key=?", (REPO,))
        row = c.fetchone()
    return (row[0] if row else "") or ""


def test_complete_task_sre_registers_scene_and_repo(_quiet_remote_hooks):
    from src.runtime.service import complete_task
    _seed_task("sre")
    complete_task(TASK, json.dumps({
        "artifacts": ["deploy_url: https://remote482.example.com/t/x"],
    }), exit_code=0)
    assert _scene_url() == "https://remote482.example.com/t/x"
    # Issue #468 仓库级自动登记不回退
    assert _repo_url() == "https://remote482.example.com/t/x"


def test_complete_task_sre_failed_no_register(_quiet_remote_hooks):
    from src.runtime.service import complete_task
    _seed_task("sre")
    complete_task(TASK, "部署失败 deploy_url: https://should-not.example.com/", exit_code=1)
    assert _scene_url() == ""
    assert _repo_url() == ""


def test_complete_task_non_sre_no_register(_quiet_remote_hooks):
    from src.runtime.service import complete_task
    _seed_task("vod")
    complete_task(TASK, "参考 https://doc.example.com/a/b", exit_code=0)
    assert _scene_url() == ""


# ===== 双路径调用点源码检查（防遗漏回归）=====

def test_direct_path_calls_scene_register():
    from src.api.event_pipeline import _execute_agent_inner, register_scene_deploy_url
    src = inspect.getsource(_execute_agent_inner)
    assert "register_scene_deploy_url" in src, (
        "_execute_agent_inner must call register_scene_deploy_url (Issue #482 direct path)")
    assert inspect.isfunction(register_scene_deploy_url)


def test_remote_path_calls_scene_register():
    from src.runtime.service import complete_task
    src = inspect.getsource(complete_task)
    assert "register_scene_deploy_url" in src, (
        "complete_task must call register_scene_deploy_url (Issue #482 remote path)")
