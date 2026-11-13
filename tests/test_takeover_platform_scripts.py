"""跨平台适配：脚本资产 / prompt 变体 / host_os 落库。

覆盖：
  · _script_posix / _script_windows 三件套的文件名、扩展名、调用形式、内容要点
  · normalize_host_os 归一（win32/darwin/未知 → windows/darwin/linux）
  · render_takeover_prompt 两个平台给出各自的载入/上报/交材料命令
  · start 请求带 host_os 时落库（缺省 linux，旧客户端行为不变）
"""

import json

import pytest
from fastapi.testclient import TestClient

from src.api.deps import get_current_user_or_api_key
from src.api.main import app
from src.domains.runtime.engine import takeover as svc

REPO = "gitcode:o/r"


@pytest.fixture(autouse=True)
def _bind_db():
    global db, db_lock
    db, db_lock = svc.db, svc.db_lock


def _cleanup():
    with db_lock:
        c = db.cursor()
        for sql, args in (
            ("DELETE FROM takeover_sessions WHERE repo_key=?", (REPO,)),
            ("DELETE FROM agent_tasks WHERE repo_key=?", (REPO,)),
            ("DELETE FROM events WHERE repo_key=?", (REPO,)),
            ("DELETE FROM issue WHERE repo_key=?", (REPO,)),
            ("DELETE FROM session_api_tokens WHERE session_key LIKE 'takeover:%'", ()),
        ):
            try:
                c.execute(sql, args)
            except Exception:
                pass
        db.commit()


def _seed(issue_number=77, task_id="t_plat_1", seq=9077):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, event_seq, issue_id, repo_key, agent_type, "
            " skill, status, runtime_id, cwd, env_extra, prompt, created_at, started_at) "
            "VALUES (?,?,?,?,?,?,?,'running',15,'/tmp/plat',?,'接管测试 prompt','2026-11-12T00:00:00+00:00',"
            "'2026-11-12T00:00:01+00:00')",
            (task_id, "sess-plat", seq, f"#{issue_number}", REPO, "coding", "implementation",
             json.dumps({"REPO_FULL_NAME": "o/r", "GRAPE_WORK_BRANCH": "feat/x"})),
        )
        c.execute(
            "INSERT INTO events (seq, repo_key, issue_id, event_type, agent_type, status, created_at, payload) "
            "VALUES (?,?,?,?,?,'running','2026-11-12T00:00:00','{}')",
            (seq, REPO, f"#{issue_number}", "issue_opened", "coding"),
        )
        db.commit()


@pytest.fixture()
def client():
    _seed()
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": 1, "username": "u", "auth_type": "api_key", "scope": "user"}
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user_or_api_key, None)
    _cleanup()


def test_normalize_host_os():
    assert svc.normalize_host_os("") == "linux"
    assert svc.normalize_host_os("Windows") == "windows"
    assert svc.normalize_host_os("win32") == "windows"
    assert svc.normalize_host_os("darwin") == "darwin"
    assert svc.normalize_host_os("macOS") == "darwin"
    assert svc.normalize_host_os("freebsd") == "linux"


def test_posix_script_assets():
    assets = svc._script_posix(env={"REPO_FULL_NAME": "o/r", "GRAPE_TAKEOVER_ID": "tkos_x"})
    by = {a["name"]: a for a in assets}
    assert set(by) == {"env", "report", "request_finish"}
    assert by["env"]["filename"].endswith(".sh") and by["env"]["mode"] == "0600"
    assert "export REPO_FULL_NAME='o/r'" in by["env"]["content"]
    assert svc.SCRIPT_CWD_MARK in by["env"]["content"]      # 插件替换为本地 cwd
    assert "GRAPE_AUTH_FILE" in by["env"]["content"]
    assert "progress %{http_code}" in by["report"]["content"]
    assert "request-finish %{http_code}" in by["request_finish"]["content"]
    # 结构化结论字段（与 worker 的 tasks/complete 同构）
    assert "actions" in by["request_finish"]["content"]
    assert "artifacts" in by["request_finish"]["content"]


def test_windows_script_assets():
    assets = svc._script_windows(env={"REPO_FULL_NAME": "o/r"})
    by = {a["name"]: a for a in assets}
    assert set(by) == {"env", "report", "request_finish"}
    assert by["env"]["filename"].endswith(".ps1") and by["env"]["exec"] == "dot-source"
    assert "$env:REPO_FULL_NAME = 'o/r'" in by["env"]["content"]
    assert "$env:GRAPE_AUTH_FILE" in by["env"]["content"]
    assert "Invoke-RestMethod" in by["report"]["content"]
    assert "/progress" in by["report"]["content"]
    assert "request-finish" in by["request_finish"]["content"]
    assert "files_written" in by["request_finish"]["content"]
    # 单引号转义（PowerShell 里 ' → ''）
    assert svc._ps_q("a'b") == "'a''b'"


def test_prompt_variants_by_host_os():
    kw = dict(repo="o/r", issue_number="7", agent_type="coding", skill="implementation",
              cancel_ok=True, cancel_error="", task_id="t1", workspace_created=True,
              cwd="/tmp/ws", skill_ok=True, has_references=True, messages=[],
              original_prompt="")
    posix = svc.render_takeover_prompt(host_os="linux", **kw)
    win = svc.render_takeover_prompt(host_os="windows", **kw)
    assert "source .grape-takeover-env.sh" in posix
    assert "sh .grape-takeover-report.sh" in posix
    assert "sh .grape-takeover-request-finish.sh" in posix
    assert ". .\\.grape-takeover-env.ps1" in win
    assert "pwsh -ExecutionPolicy Bypass -File .\\.grape-takeover-report.ps1" in win
    assert "pwsh -ExecutionPolicy Bypass -File .\\.grape-takeover-request-finish.ps1" in win
    assert "source .grape-takeover-env.sh" not in win


def test_start_persists_host_os(client):
    res = client.post("/api/takeover/sessions", json={
        "repo_key": REPO, "issue_number": "77", "agent_type": "coding",
        "platform": "dsh", "host_os": "windows", "host_arch": "x64", "host_shell": "powershell",
    })
    assert res.status_code == 200, res.text
    body = res.json()
    bundle = body["bundle"] if "bundle" in body else body
    assert bundle["platform"]["host_os"] == "windows"
    names = {a["name"]: a for a in bundle["assets"]["scripts"]}
    assert names["env"]["filename"] == ".grape-takeover-env.ps1"
    sid = (body.get("session") or body.get("takeover") or {}).get("id") or body.get("takeover_id")
    with db_lock:
        c = db.cursor()
        c.execute("SELECT host_os, host_arch, host_shell FROM takeover_sessions WHERE id=?", (sid,))
        row = c.fetchone()
    assert row == ("windows", "x64", "powershell")


def test_start_defaults_to_linux(client):
    res = client.post("/api/takeover/sessions", json={
        "repo_key": REPO, "issue_number": "77", "agent_type": "coding"})
    assert res.status_code == 200, res.text
    bundle = res.json()["bundle"]
    assert bundle["platform"]["host_os"] == "linux"
    names = {a["name"]: a for a in bundle["assets"]["scripts"]}
    assert names["env"]["filename"] == ".grape-takeover-env.sh"