"""Issue #455：0→1 安装引导（OSS 主线）改进 —— 测试。

覆盖：
- M1-① 启动版本检查（ensure_python_version < 3.10 显式退出）
- M1-② 对外基址配置判定与告警（is_server_base_url_configured / get_server_base_url_warning / health 字段）
- M1-④ SQLite 建表补齐 pipeline_instances + 清理存在性探测
- M1-⑤ GitHub OAuth 落库读回校验（读不到账号/绑定 → 失败响应而非静默 302）
"""
import os
import sqlite3
import sys
import uuid

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

from src.api.main import app
from src.session_manager import db, db_lock
from src.user import db as user_db

client = TestClient(app)


# ---------------------------------------------------------------------------
# M1-① 启动版本检查
# ---------------------------------------------------------------------------

class TestPythonVersionCheck:
    def test_3_9_exits(self):
        from src.api.version_check import ensure_python_version
        with pytest.raises(SystemExit) as ei:
            ensure_python_version(version_info=(3, 9, 19))
        assert ei.value.code == 1

    def test_3_10_ok(self):
        from src.api.version_check import ensure_python_version
        ensure_python_version(version_info=(3, 10, 0))  # 不抛异常

    def test_3_12_ok(self):
        from src.api.version_check import ensure_python_version
        ensure_python_version(version_info=(3, 12, 1))  # 不抛异常

    def test_declared_min_is_310(self):
        """requirements/README 声明下限与代码一致：>= 3.10"""
        from src.api.version_check import _MIN_PYTHON
        assert _MIN_PYTHON == (3, 10)

    def test_requirements_declares_310(self):
        """requirements.txt 注释声明 >= 3.10"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "requirements.txt"), encoding="utf-8") as f:
            assert ">= 3.10" in f.read()

    def test_doctor_rejects_39(self):
        """scripts/doctor.py 对 3.9 判 error（阻断）"""
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "doctor_preflight", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "doctor.py"))
        # 仅静态断言函数文本包含 3.10 下限（避免高耦合 import doctor 全链路）
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "doctor.py"), encoding="utf-8") as f:
            src = f.read()
        assert ">= 3.10" in src
        assert "主服务需要 >= 3.10" in src


# ---------------------------------------------------------------------------
# M1-② 对外基址配置判定 + 健康检查字段
# ---------------------------------------------------------------------------

class TestServerBaseUrl:
    def test_configured_public(self):
        from src.user.db import is_server_base_url_configured, get_server_base_url_warning
        with patch("src.config.read_server_config",
                   return_value={"server_base_url": "https://grape.example.com"}):
            assert is_server_base_url_configured() is True
            assert get_server_base_url_warning() == ""

    def test_loopback_not_configured(self):
        from src.user.db import is_server_base_url_configured, get_server_base_url_warning
        with patch("src.config.read_server_config",
                   return_value={"server_base_url": "http://localhost:3000"}):
            assert is_server_base_url_configured() is False
            assert "回环" in get_server_base_url_warning() or "localhost" in get_server_base_url_warning()

    def test_empty_not_configured(self):
        from src.user.db import is_server_base_url_configured, get_server_base_url_warning
        with patch("src.config.read_server_config", return_value={}), \
             patch("src.user.db.get_meta_value", return_value=""):
            assert is_server_base_url_configured() is False
            assert get_server_base_url_warning() != ""

    def test_meta_fallback_configured(self):
        from src.user.db import is_server_base_url_configured
        with patch("src.config.read_server_config", return_value={}), \
             patch("src.user.db.get_meta_value", return_value="https://worker.example.com"):
            assert is_server_base_url_configured() is True

    def test_health_reports_configuration(self):
        """/api/health 返回 server_base_url_configured 字段"""
        with patch("src.config.read_server_config",
                   return_value={"server_base_url": "https://grape.example.com"}):
            r = client.get("/api/health")
        data = r.json()
        assert data["server_base_url_configured"] is True
        assert data["server_base_url_warning"] == ""

    def test_health_warns_when_unconfigured(self):
        with patch("src.config.read_server_config", return_value={}), \
             patch("src.user.db.get_meta_value", return_value=""):
            r = client.get("/api/health")
        data = r.json()
        assert data["server_base_url_configured"] is False
        assert data["server_base_url_warning"]


# ---------------------------------------------------------------------------
# M1-④ SQLite 建表补齐 pipeline_instances + 清理存在性探测
# ---------------------------------------------------------------------------

class TestDbTables:
    def test_pipeline_instances_in_new_tables(self):
        from src.api import db_setup
        names = [n for n, _ in db_setup._NEW_TABLES]
        assert "pipeline_instances" in names
        assert "issue_state" in names  # 被清理逻辑引用，确认在清单

    def test_pipeline_instances_created(self):
        """init_database 后表真实存在（新实例不再 'no such table'）"""
        with db_lock:
            rows = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='pipeline_instances'").fetchall()
        assert rows, "pipeline_instances 表应已由 db_setup 创建"

    def test_pipeline_instances_has_repo_key_column(self):
        with db_lock:
            cols = [r[1] for r in db.execute("PRAGMA table_info(pipeline_instances)").fetchall()]
        assert "repo_key" in cols

    def test_probe_existing_tables_skips_missing(self):
        """清理探测：缺表不进入待删集合（降级不报错）"""
        from src.api.onboarding import _probe_existing_tables
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE events (repo_key TEXT)")
        existing = _probe_existing_tables(conn.cursor())
        assert "events" in existing
        assert "pipeline_instances" not in existing  # 内存库未建该表 → 探测应排除

    def test_cleanup_stale_repo_data_no_crash(self):
        """真实测试库上清理流程完整执行不抛错（含 pipeline_instances/issue_state）"""
        from src.api.onboarding import cleanup_stale_repo_data
        rk = f"gitcode:issue455-{uuid.uuid4().hex[:6]}"
        cleanup_stale_repo_data(rk)  # 不应抛异常（缺表已降级/建表已补齐）


# ---------------------------------------------------------------------------
# M1-⑤ GitHub OAuth 落库读回校验
# ---------------------------------------------------------------------------

class _FakeReqResp:
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def _save_state(provider="github"):
    state = "test-state-455-" + uuid.uuid4().hex[:8]
    user_db.save_oauth_state(state, provider, {})
    return state


def _gid():
    return str(20_000_000 + uuid.uuid4().int % 100_000_000)


def _cleanup_gh(username: str, gid: str):
    from src.auth.grape_account_db import get_grape_account_by_username, soft_delete_account, delete_external_identity
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM unified_user_mapping WHERE github_login=?", (username,))
        db.commit()
    acc = get_grape_account_by_username(username)
    if acc:
        delete_external_identity(acc["id"], "github")
        soft_delete_account(acc["id"])


class TestGithubOAuthReadback:
    def test_account_readback_failure_redirects_error(self):
        """创建账号后读回不到 → 失败响应（302 oauth_error），不再静默登录"""
        uname = "gh_rb_" + uuid.uuid4().hex[:6]
        gid = _gid()
        state = _save_state("github")
        # create_grape_account_oauth 返回 dict，但紧随其后的读回返回 None → 模拟落库无痕
        with patch("src.user.db.get_meta_value", return_value="x"), \
             patch("requests.post", return_value=_FakeReqResp({"access_token": "at", "refresh_token": "rt"})), \
             patch("requests.get", return_value=_FakeReqResp({"id": gid, "login": uname, "name": "新用户"})), \
             patch("src.config.is_user_allowed", return_value=True), \
             patch("src.api.routes.grape_auth.create_grape_account_oauth",
                   return_value={"id": 900002, "username": uname, "email": ""}), \
             patch("src.api.routes.grape_auth.get_grape_account_by_id", return_value=None):
            r = client.get("/api/auth/callback/github", params={"state": state, "code": "c"}, follow_redirects=False)
        assert r.status_code == 302
        assert "oauth_error" in r.headers.get("location", "")

    def test_binding_readback_failure_rolls_back_and_redirects_error(self):
        """绑定写库后读回不到 → 回滚账号 + 失败响应"""
        uname = "gh_rb_bind_" + uuid.uuid4().hex[:6]
        gid = _gid()
        state = _save_state("github")
        real_create = pytest.importorskip("src.api.routes.grape_auth").create_grape_account_oauth
        with patch("src.user.db.get_meta_value", return_value="x"), \
             patch("requests.post", return_value=_FakeReqResp({"access_token": "at", "refresh_token": "rt"})), \
             patch("requests.get", return_value=_FakeReqResp({"id": gid, "login": uname, "name": "新用户"})), \
             patch("src.config.is_user_allowed", return_value=True), \
             patch("src.api.routes.grape_auth.create_grape_account_oauth") as m_create, \
             patch("src.auth.grape_account_db.get_external_identity", return_value=None), \
             patch("src.api.routes.grape_auth.soft_delete_account") as m_rollback:
            # 模拟账号真实创建成功（返回带 id），但绑定读回始终 None
            m_create.return_value = None
            # 用真实 create_grape_account_oauth 写入（确保后续软删有对象），再让绑定读回失败
            from src.auth.grape_account_db import create_grape_account_oauth as _real_create
            _acc = _real_create(username=uname, email="", provider="github")
            assert _acc, "预置账号创建失败"
            m_create.return_value = _acc
            r = client.get("/api/auth/callback/github", params={"state": state, "code": "c"}, follow_redirects=False)
        assert r.status_code == 302
        assert "oauth_error" in r.headers.get("location", "")
        m_rollback.assert_called_once()  # 绑定落库失败 → 回滚账号

    def test_success_github_auto_create_readback_ok(self):
        """正常 GitHub 自动创建：读回通过 → 302 登录成功（exchange_code）"""
        uname = "gh_rb_ok_" + uuid.uuid4().hex[:6]
        gid = _gid()
        state = _save_state("github")
        with patch("src.user.db.get_meta_value", return_value="x"), \
             patch("requests.post", return_value=_FakeReqResp({"access_token": "at", "refresh_token": "rt"})), \
             patch("requests.get", return_value=_FakeReqResp({"id": gid, "login": uname, "name": "新用户"})), \
             patch("src.config.is_user_allowed", return_value=True):
            r = client.get("/api/auth/callback/github", params={"state": state, "code": "c"}, follow_redirects=False)
        assert r.status_code == 302
        assert "exchange_code=" in r.headers.get("location", "")
        _cleanup_gh(uname, gid)

    def test_readback_only_for_github(self):
        """GitCode 路径不受读回校验影响（保持原语义）"""
        uname = "gc_rb_" + uuid.uuid4().hex[:6]
        gid = _gid()
        state = _save_state("gitcode")
        with patch("src.user.db.get_meta_value", return_value="x"), \
             patch("requests.post", return_value=_FakeReqResp({"access_token": "at", "refresh_token": "rt"})), \
             patch("requests.get", return_value=_FakeReqResp({"id": gid, "login": uname, "nickname": "新用户"})), \
             patch("src.config.is_user_allowed", return_value=True):
            r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"}, follow_redirects=False)
        assert r.status_code == 302
        assert "exchange_code=" in r.headers.get("location", "")
        _cleanup_gh(uname, gid)

# ---------------------------------------------------------------------------
# M2-⑥/⑦ 托管接口 502 上下文 + 非所有者 403 前置校验
# ---------------------------------------------------------------------------

class TestHost502Detail:
    """_host_502_detail：上游上下文与可重试性标记"""

    def test_500_retryable(self):
        from src.api.routes.repos import _host_502_detail
        d = _host_502_detail("github_host", RuntimeError("GitHub API POST https://api.github.com/x failed (500): boom"))
        assert d["code"] == 502
        assert d["retryable"] is True
        assert d["upstream"]["status"] == 500
        assert d["upstream"]["stage"] == "github_host"
        assert "UPSTREAM_GITHUB_HOST_500" == d["error_code"]
        assert "retry_hint" in d

    def test_403_not_retryable(self):
        from src.api.routes.repos import _host_502_detail
        d = _host_502_detail("gitcode_user", RuntimeError("GitCode API GET https://gitcode.com/x failed (403): no"))
        assert d["retryable"] is False
        assert d["upstream"]["status"] == 403

    def test_network_retryable(self):
        from src.api.routes.repos import _host_502_detail
        d = _host_502_detail("host_setup", RuntimeError("Cannot reach GitHub API: timed out"))
        assert d["retryable"] is True
        assert d["upstream"]["status"] is None

    def test_upstream_status_extractor(self):
        from src.api.routes.repos import _upstream_status_code
        assert _upstream_status_code(RuntimeError("GitHub API GET u failed (502): x")) == 502
        assert _upstream_status_code(RuntimeError("random message")) == 0


class _FakeHostResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class TestGithubHostPermissionPrecheck:
    """GitHub 托管前归属/权限前置校验：403/404 明确返回，网络错误不阻断"""

    def _base_mocks(self, monkeypatch, invoke_precheck=True):
        import src.api.routes.repos as repos_mod
        monkeypatch.setattr("src.user.db.get_webhook_url", lambda: "https://grape.topxtopx.com/webhook/gitcode")
        monkeypatch.setattr("src.api.session_store.ensure_default_agent_sessions", lambda *a, **k: None)
        if invoke_precheck:
            # 模拟 with_oauth_token 真正调用 fn(token)（预检查使用同一刷新机制）
            monkeypatch.setattr("src.api.routes.repos.with_oauth_token",
                                lambda user, provider, fn, *a, **k: fn("real_tok"))
        else:
            monkeypatch.setattr("src.api.routes.repos.with_oauth_token", lambda *a, **k: None)
        monkeypatch.setattr("src.user.db.get_global_github_bot_token", lambda: "")
        monkeypatch.setattr("src.user.db.get_global_github_bot_username", lambda: "")
        monkeypatch.setattr("src.api.routes.repos.get_user_github_token_with_refresh",
                           lambda u, user_id: "gh_tok")
        monkeypatch.setattr("src.user.repo_manager.setup_github_webhook", lambda *a, **k: 101)
        monkeypatch.setattr("src.user.repo_manager.find_existing_github_webhook", lambda *a, **k: None)
        # host_repo 内局部 import repo_manager._github_req —— patch 模块属性生效
        return repos_mod

    def _host(self):
        from src.api.deps import require_no_session_token
        app.dependency_overrides[require_no_session_token] = lambda: {
            "id": 2, "username": "Jason7602", "login_provider": "gitcode"}
        try:
            return client.post("/api/repos/host?provider=github",
                               json={"full_name": "Jason7602/permtest"},
                               headers={"Authorization": "Bearer fake-jwt"})
        finally:
            app.dependency_overrides.clear()

    def test_upstream_403_returns_403_permission_denied(self, monkeypatch):
        self._base_mocks(monkeypatch)

        def _gh_req(method, url, token=""):
            raise RuntimeError("GitHub API GET " + url + " failed (403): Forbidden")
        monkeypatch.setattr("src.user.repo_manager._github_req", _gh_req)
        r = self._host()
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["error_code"] == "PERMISSION_DENIED"

    def test_upstream_404_returns_404(self, monkeypatch):
        self._base_mocks(monkeypatch)

        def _gh_req(method, url, token=""):
            raise RuntimeError("GitHub API GET " + url + " failed (404): Not Found")
        monkeypatch.setattr("src.user.repo_manager._github_req", _gh_req)
        r = self._host()
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["error_code"] == "REPO_NOT_FOUND"

    def test_non_owner_non_admin_returns_403(self, monkeypatch):
        self._base_mocks(monkeypatch)

        def _gh_req(method, url, token=""):
            if url.endswith("/user"):
                return {"login": "Jason7602"}
            return {"owner": {"login": "SomeoneElse"}, "permissions": {"admin": False, "push": True}}
        monkeypatch.setattr("src.user.repo_manager._github_req", _gh_req)
        r = self._host()
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["error_code"] == "PERMISSION_DENIED"

    def test_owner_continues_success(self, monkeypatch):
        self._base_mocks(monkeypatch)

        def _gh_req(method, url, token=""):
            if url.endswith("/user"):
                return {"login": "Jason7602"}
            return {"owner": {"login": "Jason7602"}, "permissions": {"admin": True, "push": True}}
        monkeypatch.setattr("src.user.repo_manager._github_req", _gh_req)
        r = self._host()
        assert r.status_code == 200, r.text

    def test_network_error_does_not_block(self, monkeypatch):
        self._base_mocks(monkeypatch)

        def _gh_req(method, url, token=""):
            raise RuntimeError("Cannot reach GitHub API: timed out")
        monkeypatch.setattr("src.user.repo_manager._github_req", _gh_req)
        r = self._host()
        assert r.status_code == 200, r.text  # 网络类：无法确认权限，交由下游既有流程

    def test_unknown_permission_does_not_block(self, monkeypatch):
        """with_oauth_token 未产出权限信息（None）→ 不阻断既有流程（兼容老测试/降级）"""
        self._base_mocks(monkeypatch, invoke_precheck=False)
        r = self._host()
        assert r.status_code == 200, r.text

    def test_i18n_keys_present(self):
        import json as _json
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for lang in ("zh", "en"):
            with open(os.path.join(root, "src", "api", "static", "locales", lang, "repos.json"), encoding="utf-8") as f:
                data = _json.load(f)
            assert data["host"]["permission_denied"]
            assert data["host"]["upstream_error"]
            assert data["host"]["repo_not_found"]


# ---------------------------------------------------------------------------
# M3-⑧ 拉取路径有限次重试 + M3-⑨ 自检清单文档
# ---------------------------------------------------------------------------

def _proc(returncode, stderr=""):
    import subprocess
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


class TestRepogitRetry:
    def test_network_error_detected(self):
        from src.runtime.repogit import _is_git_network_error
        assert _is_git_network_error(_proc(128, "fatal: unable to access 'https://x': Failed to connect")) is True
        assert _is_git_network_error(_proc(128, "fatal: unable to access: HTTP/2 stream 0 was not cleanly closed")) is True
        assert _is_git_network_error(_proc(128, "fatal: early EOF")) is True
        assert _is_git_network_error(_proc(128, "fatal: Could not read from remote repository")) is True

    def test_auth_error_not_retried(self):
        from src.runtime.repogit import _is_git_network_error
        assert _is_git_network_error(_proc(128, "fatal: unable to access: The requested URL returned error: 403")) is False
        assert _is_git_network_error(_proc(128, "fatal: Authentication failed for 'https://x/'")) is False

    def test_success_immediate(self):
        from src.runtime.repogit import _run_git_retry
        with patch("src.runtime.repogit._run_git") as m:
            m.return_value = _proc(0)
            r = _run_git_retry(["fetch"], token="t", retries=3, base_delay=0)
        assert r.returncode == 0
        assert m.call_count == 1

    def test_network_error_retries_then_success(self):
        from src.runtime.repogit import _run_git_retry
        with patch("src.runtime.repogit._run_git") as m:
            m.side_effect = [
                _proc(128, "fatal: unable to access 'https://x': Failed to connect"),
                _proc(128, "fatal: early EOF"),
                _proc(0),
            ]
            r = _run_git_retry(["fetch"], token="t", retries=3, base_delay=0)
        assert r.returncode == 0
        assert m.call_count == 3

    def test_network_error_exhausts_returns_last(self):
        from src.runtime.repogit import _run_git_retry
        with patch("src.runtime.repogit._run_git") as m:
            m.return_value = _proc(128, "fatal: unable to access: Connection timed out")
            r = _run_git_retry(["fetch"], token="t", retries=2, base_delay=0)
        assert r.returncode == 128
        assert m.call_count == 2

    def test_4xx_fast_fail_single_attempt(self):
        from src.runtime.repogit import _run_git_retry
        with patch("src.runtime.repogit._run_git") as m:
            m.return_value = _proc(128, "fatal: unable to access: The requested URL returned error: 403")
            r = _run_git_retry(["fetch"], token="t", retries=3, base_delay=0)
        assert r.returncode == 128
        assert m.call_count == 1

    def test_ensure_repo_base_uses_retry(self):
        """拉取主路径（fetch）已切换到重试版本"""
        import inspect
        from src.runtime import repogit
        src = inspect.getsource(repogit.ensure_repo_base)
        assert "_run_git_retry" in src


class TestWorkerPackageRetry:
    def test_network_error_retries_then_success(self):
        import subprocess as _sp
        from src.worker_package import _git_retry
        with patch("src.worker_package._git") as m:
            m.side_effect = [
                _sp.CalledProcessError(128, ["git"], stderr="fatal: unable to access 'https://x': Failed to connect"),
                _sp.CalledProcessError(128, ["git"], stderr="fatal: early EOF"),
                None,  # 第三次成功
            ]
            _git_retry(["clone", "https://x", "/tmp/x"], retries=3, base_delay=0)
        assert m.call_count == 3

    def test_4xx_fast_fail(self):
        import subprocess as _sp
        import pytest as _pytest
        from src.worker_package import _git_retry
        with patch("src.worker_package._git") as m:
            m.side_effect = _sp.CalledProcessError(
                128, ["git"], stderr="fatal: unable to access: The requested URL returned error: 403")
            with _pytest.raises(_sp.CalledProcessError):
                _git_retry(["clone", "https://x", "/tmp/x"], retries=3, base_delay=0)
        assert m.call_count == 1


class TestChecklistDoc:
    def test_docs_checklist_exists(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "docs", "0-to-1-checklist.md")
        assert os.path.isfile(path), "docs/0-to-1-checklist.md 应存在"
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert "Python" in content
        assert "server_base_url" in content
        assert "oauth-providers" in content

    def test_readme_links_checklist(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("README.md", "README.zh.md"):
            with open(os.path.join(root, name), encoding="utf-8") as f:
                assert "docs/0-to-1-checklist.md" in f.read(), f"{name} 应链接自检清单"
