"""Issue-45 M2：会话级临时 token 生命周期测试.

覆盖：
- enqueue_task 签发 GRAPE_API_TOKEN（session_key=task_id，devops 无 user_id）
- chat 任务绑定 USER_ID
- 任务终态（complete/fail/cancel）→ token 即时失效
- direct 模式签发 + finally 失效
- env 中 GRAPE_API_TOKEN 不落 agent_tasks 明文之外（sha256 落库）
"""
import uuid
from unittest.mock import patch

from src.api import auth_token
from src.runtime.service import enqueue_task, update_task_status, complete_task, cancel_task
from src.session_manager import db, db_lock


def _seed_session_and_runtime(repo_key="gitcode:o/r", agent_type="vod"):
    with db_lock:
        c = db.cursor()
        sid = "m2sess-%s" % uuid.uuid4().hex[:8]
        c.execute(
            "INSERT OR REPLACE INTO devops_agent_session (id, repo_key, agent_type, status, skill) "
            "VALUES (?,?,?,?,'m2skill')",
            (sid, repo_key, agent_type, "idle"))
        c.execute("INSERT INTO agent_runtimes (name, display_name, credential_id, installed, healthy, status, scope, owner_id) "
                  "VALUES ('m2rt','m2rt',0,1,1,'online','global','')")
        rid = c.lastrowid
        db.commit()
    return sid, rid


def _task_env(task_id):
    with db_lock:
        row = db.cursor().execute("SELECT env_extra FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
    import json
    return json.loads(row[0]) if row else {}


def _cleanup():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM session_api_tokens")
        c.execute("DELETE FROM agent_tasks WHERE id LIKE 't_%'")
        c.execute("DELETE FROM devops_agent_session WHERE id LIKE 'm2sess-%'")
        c.execute("DELETE FROM agent_runtimes WHERE name='m2rt'")
        db.commit()


def _new_task(repo_key="gitcode:o/r", agent_type="vod", user_id=0, env_extra=None):
    sid, rid = _seed_session_and_runtime(repo_key, agent_type)
    return enqueue_task(
        agent_session_id=sid, runtime_id=rid, prompt="p", cwd=".",
        event_type="test", account="", user_id=user_id,
        repo_key=repo_key, agent_type=agent_type, skill="m2skill",
        env_extra=dict(env_extra or {}),
    )


def _session_row(task_id):
    with db_lock:
        return db.cursor().execute(
            "SELECT session_key, repo_key, agent_type, user_id, token_sha256 "
            "FROM session_api_tokens WHERE session_key=?", (task_id,)).fetchone()


def test_enqueue_issues_token_and_injects_env():
    _cleanup()
    tid = _new_task(repo_key="gitcode:o/r", agent_type="vod")
    env = _task_env(tid)
    plain = env.get("GRAPE_API_TOKEN", "")
    assert plain.startswith("tk_")
    row = _session_row(tid)
    assert row is not None and row[1] == "gitcode:o/r" and row[2] == "vod" and row[3] == 0
    assert row[4] == auth_token._sha256(plain)  # 落库 sha256，非明文


def test_chat_task_binds_user_id():
    _cleanup()
    tid = _new_task(repo_key="chat:123", agent_type="chat", user_id=123,
                    env_extra={"USER_ID": "123", "CONVERSATION_ID": "conv-x"})
    plain = _task_env(tid)["GRAPE_API_TOKEN"]
    assert plain.startswith("tk_")
    row = _session_row(tid)
    assert row[2] == "chat" and row[3] == 123
    info = auth_token.verify_session_token(plain)
    assert info is not None and info["user_id"] == 123 and info["repo_key"] == "chat:123"


def test_task_terminal_revokes_token():
    _cleanup()
    tid = _new_task()
    plain = _task_env(tid)["GRAPE_API_TOKEN"]
    assert auth_token.verify_session_token(plain) is not None
    complete_task(tid, "done", exit_code=0)
    assert auth_token.verify_session_token(plain) is None
    assert _session_row(tid) is None


def test_task_fail_revokes_token():
    _cleanup()
    tid = _new_task()
    plain = _task_env(tid)["GRAPE_API_TOKEN"]
    update_task_status(tid, "failed", exit_code=1)
    assert auth_token.verify_session_token(plain) is None


def test_task_cancel_revokes_token():
    _cleanup()
    tid = _new_task()
    plain = _task_env(tid)["GRAPE_API_TOKEN"]
    cancel_task(tid)
    assert auth_token.verify_session_token(plain) is None


def test_direct_mode_issues_and_revokes():
    _cleanup()
    from src.api import event_pipeline

    captured = {}

    def _fake_stream(prompt, cwd, session_id="", **kw):
        env = kw.get("env_extra", {})
        token = env.get("GRAPE_API_TOKEN", "")
        captured["token"] = token
        captured["verify"] = auth_token.verify_session_token(token) if token else None
        yield ("step_start", "s")
        yield ("done", {"exit_code": 0, "text": "ok"})

    with patch("src.prompt_builder.build_prompt", return_value="prompt"), \
         patch("src.runtime.executor.stream_devops_agent", side_effect=_fake_stream):
        event_pipeline._execute_agent(
            "gitcode:o/r", 424242, "vod", "opencode", "m2skill",
            {"repository": "o/r", "type": "vod_signal", "issue_id": "#0"},
            runtime_id=1, task_id="",
        )

    assert captured["token"].startswith("tk_")
    assert captured["verify"] is not None           # 执行期内有效
    assert _session_row("d_424242") is None          # 执行结束 → 失效