"""Issue #3580 PR #1868 复审修复的回归用例。

覆盖四项修复：
1. GitLab webhook 密钥：必须使用共享密钥，禁止回落 api_token（凭据外泄 + 投递全 404）；
2. gitlab_v4 家族强制路径 B（随机密钥），即使配置了全局 bot token；
3. remove_gitlab_webhook 上游 404 幂等；
4. _run_git 累加调用方已有 GIT_CONFIG_*，不覆盖。
"""
import json

import pytest

from src.domains.mgmt.user import repo_manager as rm
from src.domains.runtime.engine import repogit
from src.kernel import vcs_gitlab_hooks as hooks


def test_gitlab_webhook_secret_required(monkeypatch):
    seen = {}

    def fake_req(method, url, token="", body=None, timeout=30):
        seen["body"] = body
        return {"id": 11}

    monkeypatch.setattr(hooks, "_request", fake_req)

    with pytest.raises(RuntimeError, match="secret required"):
        rm.setup_gitlab_webhook("PAT-must-not-leak", "g", "r",
                                "https://gw/webhook/gitlab-corp",
                                webhook_password="",
                                api_base="https://gl.corp/api/v4")

    rid = rm.setup_gitlab_webhook("PAT-must-not-leak", "g", "r",
                                  "https://gw/webhook/gitlab-corp",
                                  webhook_password="shared-secret",
                                  api_base="https://gl.corp/api/v4")
    assert rid == 11
    assert seen["body"]["token"] == "shared-secret"
    assert seen["body"]["token"] != "PAT-must-not-leak"


def test_gitlab_family_forces_secret_path(monkeypatch):
    """gitlab_v4 的 webhook 必须走路径 B（随机密钥），但 bot_token 不再被丢弃。

    2026-11-23：原实现用「bot_token 置空」来表达路径 B，连带关掉了「邀请 bot
    协作者 / bot 回帖凭据」。改为显式 webhook_path_b 后，两者互不影响。
    """
    import src.domains.mgmt.user.db as _udb
    monkeypatch.setattr(_udb, "get_global_bot_token_for", lambda provider: "global-bot-token")
    cfg = rm.get_hosting_provider_config("gitlab")
    assert cfg["webhook_path_b"] is True
    assert cfg["bot_token"] == "global-bot-token"


def test_remove_gitlab_webhook_404_idempotent(monkeypatch):
    def not_found(method, url, token="", body=None, timeout=30):
        raise hooks.GitLabHookError(method, url, 404, "404 Not Found")

    monkeypatch.setattr(hooks, "_request", not_found)
    assert rm.remove_gitlab_webhook("tok", "g", "r", 9, api_base="https://gl.corp/api/v4") is True

    def forbidden(method, url, token="", body=None, timeout=30):
        raise hooks.GitLabHookError(method, url, 403, "403 Forbidden")

    monkeypatch.setattr(hooks, "_request", forbidden)
    with pytest.raises(hooks.GitLabHookError):
        rm.remove_gitlab_webhook("tok", "g", "r", 9, api_base="https://gl.corp/api/v4")


def test_run_git_accumulates_git_config(monkeypatch):
    captured = {}

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, env=None, **kw):
        captured["cmd"] = cmd
        captured["env"] = env
        return _R()

    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "grape")
    monkeypatch.setattr(repogit.subprocess, "run", fake_run)

    repogit._run_git(["--version"], token="t0ken", timeout=5, ca_bundle="/etc/ca.pem")

    env = captured["env"]
    assert env["GIT_CONFIG_KEY_0"] == "user.name"          # 既有项保留
    assert env["GIT_CONFIG_COUNT"] == "4"                  # 1 + 3（auth/CA/followRedirects）
    pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
             for i in range(int(env["GIT_CONFIG_COUNT"]))}
    assert pairs["http.sslCAInfo"] == "/etc/ca.pem"
    assert pairs["http.followRedirects"] == "false"
    assert any(k.lower() == "http.extraheader" for k in pairs)
    assert "t0ken" not in " ".join(captured["cmd"])

# ===== Issue #3580 复审（MySQL 独有 P0）：收件箱 last_error 的默认值 =====
# 三个断言分别锁住：新装库 DDL 可空、入队语句显式写该列、存量库有收敛迁移。
# SQLite 对 `TEXT NOT NULL DEFAULT ''` 完全合法，所以整族缺陷只有 MySQL 严格模式
# 才现形（实测 1364 → 通用 webhook 每次 500），必须靠静态锁 + 真库演练双保险。

def test_webhook_inbox_ddl_last_error_nullable():
    from src.domains.runtime import schema_ddl

    ddl = schema_ddl._WEBHOOK_INBOX_DDL
    assert "last_error MEDIUMTEXT DEFAULT NULL" in ddl
    assert "last_error TEXT NOT NULL DEFAULT ''" not in ddl


def test_webhook_enqueue_writes_last_error_explicitly():
    import inspect

    from src.domains.runtime import webhook_inbox

    src = inspect.getsource(webhook_inbox.enqueue_webhook)
    insert = src.split("INSERT INTO grape_t_webhook_inbox", 1)[1]
    assert "last_error" in insert, "入队 INSERT 必须显式写 last_error（MySQL 无列默认）"


def test_webhook_inbox_last_error_migration_registered():
    import src.api.db_setup as ds

    by_version = {m.version: m.name for m in ds._MIGRATIONS}
    assert by_version.get("202611200004") == "webhook-inbox-last-error-nullable"
    # 部署线已占用 202611200002/202611200003（ca_bundle / create-webhook-inbox-table），
    # 本迁移必须另占号，否则按号跳过 = 列永远收敛不了
    assert by_version.get("202611200002") != "webhook-inbox-last-error-nullable"
    assert by_version.get("202611200003") != "webhook-inbox-last-error-nullable"
    names = [m.name for m in ds._MIGRATIONS]
    assert names.count("webhook-inbox-last-error-nullable") == 1


def test_webhook_inbox_last_error_migration_sqlite_idempotent(monkeypatch):
    """SQLite 侧：建表后连跑两次零异常（早退分支），且入队仍可省略该列。"""
    import sqlite3

    import src.api.db_setup as ds
    from src.domains.runtime.schema_ddl import _WEBHOOK_INBOX_DDL

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_WEBHOOK_INBOX_DDL)

    monkeypatch.setattr(ds, "_mysql_backend", lambda: False)
    monkeypatch.setattr(ds, "_migration_conn",
                        lambda domain, default=None: conn)
    monkeypatch.setattr("src.kernel.session_manager.db", conn, raising=False)

    ds._fn_webhook_inbox_last_error_nullable()
    ds._fn_webhook_inbox_last_error_nullable()   # 幂等：二次调用无副作用

    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(grape_t_webhook_inbox)")}
    assert "last_error" in cols
