"""Issue #468: SRE 完成后 deploy_url 自动登记（双路径统一）测试。

覆盖：
- register_sre_deploy_url 共享函数：结构化 artifacts/issue_summary 命中、
  完整 URL 正则回落（资源路径不截断）、非法/超长 URL 拒绝、
  未知 repo_key / 空 result_text 不写库、幂等。
- complete_task() 远程收尾路径钩子：sre 成功登记、非 sre / 失败不触发。

模式：仿 test_task_complete.py（独立测试库 + agent_tasks 种子）。
"""
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_i468_deploy_url.db")
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"

import pytest

SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from src.session_manager import db, db_lock
from src.api.db_setup import init_database

init_database()

REPO = "gitcode:i468/sre-repo"
TASK = "t_i468_sre"


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (TASK,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO,))
        c.execute("INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name, deploy_url) "
                  "VALUES (?,?,?,?,?)", (REPO, "i468", "sre-repo", "i468/sre-repo", ""))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (TASK,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO,))
        db.commit()


def _deploy_url():
    with db_lock:
        c = db.cursor()
        c.execute("SELECT deploy_url FROM managed_repos WHERE repo_key=?", (REPO,))
        row = c.fetchone()
    return (row[0] if row else "") or ""


def _register(result_text):
    from src.api.event_pipeline import register_sre_deploy_url
    return register_sre_deploy_url(REPO, result_text)


# ===== 结构化优先 =====

def test_structured_artifacts_hit():
    import json
    text = json.dumps({
        "actions": ["deployed"],
        "conclusion": "ok",
        "artifacts": ["deploy_url: https://tunnel.example.com/abc123/"],
        "issue_summary": "done",
    })
    assert _register(text) == "https://tunnel.example.com/abc123/"
    assert _deploy_url() == "https://tunnel.example.com/abc123/"


def test_structured_issue_summary_hit():
    import json
    text = json.dumps({
        "issue_summary": "部署完成 deploy_url: http://100.1.2.3:5000/app 其他说明",
    })
    assert _register(text) == "http://100.1.2.3:5000/app"
    assert _deploy_url() == "http://100.1.2.3:5000/app"


def test_structured_artifacts_string_hit():
    import json
    text = json.dumps({"artifacts": "deploy_url: https://a.example.com/x"})
    assert _register(text) == "https://a.example.com/x"


# ===== 正则回落：完整路径不截断 =====

def test_regex_fallback_full_path_not_truncated():
    """旧正则会把 https://proxy.example.com:443/tunnel/abc/index.html 截成
    https://proxy.example.com:443/ —— 修复后必须保留完整资源路径。"""
    text = "部署完成，访问地址：https://proxy.example.com:443/tunnel/abc/index.html 请验收。"
    assert _register(text) == "https://proxy.example.com:443/tunnel/abc/index.html"
    assert _deploy_url() == "https://proxy.example.com:443/tunnel/abc/index.html"


def test_regex_fallback_plain_host():
    assert _register("done http://1.2.3.4:8080") == "http://1.2.3.4:8080"
    assert _deploy_url() == "http://1.2.3.4:8080"


# ===== 校验拒绝 =====

def test_reject_non_http_scheme():
    assert _register("javascript:alert(1)") == ""
    assert _deploy_url() == ""


def test_reject_too_long():
    assert _register("http://" + "a" * 600) == ""
    assert _deploy_url() == ""


def test_no_url_noop():
    assert _register("部署失败，无产出") == ""
    assert _deploy_url() == ""


def test_empty_result_text_noop():
    assert _register("") == ""
    assert _register(None) == ""
    assert _deploy_url() == ""


def test_unknown_repo_key_returns_empty():
    from src.api.event_pipeline import register_sre_deploy_url
    assert register_sre_deploy_url("gitcode:nobody/none", "http://x.example.com") == ""


# ===== 幂等 =====

def test_idempotent_reregister():
    url = "https://stable.example.com/app"
    assert _register(f"deploy_url: {url}") == url
    assert _register(f"deploy_url: {url}") == url
    assert _deploy_url() == url


# ===== complete_task() 远程收尾路径钩子 =====

def _seed_task(agent_type="sre"):
    with db_lock:
        db.cursor().execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, agent_type, prompt, status, issue_id) "
            "VALUES (?, 'sess', 1, ?, ?, 'p', 'running', '#468')",
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


def test_complete_task_sre_registers(_quiet_remote_hooks):
    import json as _json
    from src.runtime.service import complete_task
    _seed_task("sre")
    complete_task(TASK, _json.dumps({
        "actions": ["deploy"],
        "conclusion": "ok",
        "artifacts": ["deploy_url: https://remote.example.com/tunnel/x"],
    }), exit_code=0)
    assert _deploy_url() == "https://remote.example.com/tunnel/x"


def test_complete_task_sre_regex_fallback(_quiet_remote_hooks):
    from src.runtime.service import complete_task
    _seed_task("sre")
    complete_task(TASK, "部署完成 http://r.example.com:9000/p/q.html", exit_code=0)
    assert _deploy_url() == "http://r.example.com:9000/p/q.html"


def test_complete_task_non_sre_no_register(_quiet_remote_hooks):
    from src.runtime.service import complete_task
    _seed_task("vod")
    complete_task(TASK, "分析完成，参考 https://doc.example.com/a/b/c", exit_code=0)
    assert _deploy_url() == ""


def test_complete_task_sre_failed_no_register(_quiet_remote_hooks):
    from src.runtime.service import complete_task
    _seed_task("sre")
    complete_task(TASK, "部署失败 deploy_url: https://should-not-register.example.com/", exit_code=1)
    assert _deploy_url() == ""
