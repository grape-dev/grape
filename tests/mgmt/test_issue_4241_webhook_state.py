"""Issue #4241: webhook_ok 不能只判 bool(webhook_id) —— 地址漂移三态回归。

覆盖验收口径：
- 落盘注册地址 == 当前权威 webhook_url 前缀 → ok，webhook_ok=true
- 权威地址改成另一域名 → stale，webhook_ok=false / webhook_stale=true
- webhook_id=0 → absent（webhook_ok=false，与既有行为一致）
- 存量行落盘地址为空 → unknown（不误报 stale）
- get_webhook_url 以 DB meta 为权威，server-config.json 仅一次性种子
- 仪表盘 _check_host_status 同口径输出 webhook_stale，并在修复回写后自动恢复
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest  # noqa: E402

from src.kernel.session_manager import db, db_lock  # noqa: E402

RK = "gitcode:wh4241/test"
AUTHORITY = "https://grape.example.com"


@pytest.fixture(scope="session", autouse=True)
def _ensure_tables():
    from src.api import db_setup
    db_setup.init_database()


@pytest.fixture(autouse=True)
def _clean_repo():
    from src.domains.mgmt.user.db import set_meta_value
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM managed_repos WHERE repo_key LIKE 'gitcode:wh4241/%'")
            c.execute("DELETE FROM user_repos WHERE repo_key LIKE 'gitcode:wh4241/%'")
        except Exception:
            pass
        db.commit()
    # 权威地址统一到本用例固定值；用例结束清空，避免污染其它用例
    set_meta_value("webhook_url", AUTHORITY)
    yield
    set_meta_value("webhook_url", "")


def _add_row(repo_key=RK, webhook_id=424100, webhook_url=AUTHORITY + "/webhook/gitcode"):
    from src.domains.mgmt.user.db import add_managed_repo
    add_managed_repo(repo_key, "wh4241", "test", webhook_id=webhook_id, webhook_url=webhook_url)


def test_managed_repos_has_webhook_url_column():
    with db_lock:
        c = db.cursor()
        cols = {r[1] for r in c.execute("PRAGMA table_info(managed_repos)").fetchall()}
    assert "webhook_url" in cols


def test_add_managed_repo_persists_and_preserves_registered_url():
    from src.domains.mgmt.user.db import add_managed_repo, get_managed_webhook_url
    _add_row()
    assert get_managed_webhook_url(RK) == AUTHORITY + "/webhook/gitcode"
    # 后续不带地址的写入（如仅更新 webhook_id）不得冲掉已落盘地址
    add_managed_repo(RK, "wh4241", "test", webhook_id=424101)
    assert get_managed_webhook_url(RK) == AUTHORITY + "/webhook/gitcode"


class TestWebhookState:
    def test_ok_when_registered_address_matches_authority(self):
        from src.domains.mgmt.user.db import webhook_state
        _add_row(webhook_url=AUTHORITY + "/webhook/gitcode")
        assert webhook_state(RK) == "ok"

    def test_ok_for_bare_authority_registered_address(self):
        from src.domains.mgmt.user.db import webhook_state_for_row
        assert webhook_state_for_row(1, AUTHORITY + "/", AUTHORITY) == "ok"

    def test_stale_when_authority_domain_changed(self):
        """验收 1：meta 的 webhook_url 改成另一域名 → stale。"""
        from src.domains.mgmt.user.db import webhook_state
        _add_row(webhook_url="https://old-tunnel.example.net/webhook/gitcode")
        assert webhook_state(RK) == "stale"

    def test_absent_when_webhook_id_missing(self):
        from src.domains.mgmt.user.db import webhook_state
        _add_row(webhook_id=0, webhook_url="")
        assert webhook_state(RK) == "absent"

    def test_unknown_for_legacy_row_without_registered_url(self):
        """存量行无落盘地址 → unknown，不误报 stale。"""
        from src.domains.mgmt.user.db import webhook_state
        _add_row(webhook_url="")
        assert webhook_state(RK) == "unknown"

    def test_unknown_when_authority_unset(self, monkeypatch):
        from src.domains.mgmt.user.db import set_meta_value, webhook_state
        set_meta_value("webhook_url", "")
        monkeypatch.setattr("src.kernel.config.read_server_config", lambda *a, **k: {})
        _add_row(webhook_url=AUTHORITY + "/webhook/gitcode")
        assert webhook_state(RK) == "unknown"

    def test_normalize_ignores_trailing_slash_default_port_and_case(self):
        from src.domains.mgmt.user.db import normalize_webhook_address
        assert (normalize_webhook_address("HTTPS://Grape.Example.com:443/webhook/gitcode/")
                == "https://grape.example.com/webhook/gitcode")


class TestAuthoritySource:
    def test_meta_wins_over_server_config(self, monkeypatch):
        """权威 = DB meta；server-config 的旧值不参与运行时判定。"""
        from src.domains.mgmt.user.db import get_webhook_url
        monkeypatch.setattr(
            "src.kernel.config.read_server_config",
            lambda *a, **k: {"webhook_url": "https://old-tunnel.example.net"})
        assert get_webhook_url() == AUTHORITY

    def test_server_config_seeds_meta_when_meta_empty(self, monkeypatch):
        from src.domains.mgmt.user.db import get_meta_value, get_webhook_url, set_meta_value
        set_meta_value("webhook_url", "")
        monkeypatch.setattr("src.domains.mgmt.user.db._WEBHOOK_URL_SEEDED", False)
        monkeypatch.setattr(
            "src.kernel.config.read_server_config",
            lambda *a, **k: {"webhook_url": "https://seed.example.com"})
        assert get_webhook_url() == "https://seed.example.com"
        assert get_meta_value("webhook_url") == "https://seed.example.com"


class TestManagedReposListOutput:
    def test_list_reports_three_state_fields(self):
        from src.domains.mgmt.routes.repos import list_managed_repos
        from src.domains.mgmt.user.db import add_managed_repo, mark_user_repo_join
        uid = 940241
        mark_user_repo_join(uid, RK, is_join=True, is_admin=True)
        add_managed_repo(RK, "wh4241", "test", webhook_id=424100,
                         webhook_url="https://old-tunnel.example.net/webhook/gitcode")
        rows = list_managed_repos(user={"id": uid, "org_id": ""})["repos"]
        row = next(r for r in rows if r["repo_key"] == RK)
        assert row["webhook_ok"] is False
        assert row["webhook_stale"] is True
        assert row["webhook_state"] == "stale"

    def test_list_ok_when_address_matches(self):
        from src.domains.mgmt.routes.repos import list_managed_repos
        from src.domains.mgmt.user.db import add_managed_repo, mark_user_repo_join
        uid = 940242
        mark_user_repo_join(uid, RK, is_join=True, is_admin=True)
        add_managed_repo(RK, "wh4241", "test", webhook_id=424100,
                         webhook_url=AUTHORITY + "/webhook/gitcode")
        rows = list_managed_repos(user={"id": uid, "org_id": ""})["repos"]
        row = next(r for r in rows if r["repo_key"] == RK)
        assert row["webhook_ok"] is True
        assert row["webhook_stale"] is False
        assert row["webhook_state"] == "ok"

    def test_legacy_row_keeps_webhook_ok_true(self):
        """存量行（无落盘地址）不因新增判据变成 false —— 不误报。"""
        from src.domains.mgmt.routes.repos import list_managed_repos
        from src.domains.mgmt.user.db import add_managed_repo, mark_user_repo_join
        uid = 940243
        mark_user_repo_join(uid, RK, is_join=True, is_admin=True)
        add_managed_repo(RK, "wh4241", "test", webhook_id=424100)
        rows = list_managed_repos(user={"id": uid, "org_id": ""})["repos"]
        row = next(r for r in rows if r["repo_key"] == RK)
        assert row["webhook_ok"] is True
        assert row["webhook_stale"] is False
        assert row["webhook_state"] == "unknown"


class TestDashboardHostStatus:
    def test_drift_reports_webhook_stale_and_recovers_after_repair(self):
        """验收 3：健康检查同口径 —— 漂移报 stale；地址回写后自动恢复。"""
        from src.domains.insight.routes.dashboard import _check_host_status
        from src.domains.mgmt.user.db import add_managed_repo, get_host_status
        _add_row(webhook_url="https://old-tunnel.example.net/webhook/gitcode")
        assert _check_host_status("wh4241", "test", RK) == "webhook_stale"
        assert get_host_status(RK) == "webhook_stale"
        # 模拟 repair-host 成功回写实际注册地址 → 状态自愈
        add_managed_repo(RK, "wh4241", "test", webhook_id=424100,
                         webhook_url=AUTHORITY + "/webhook/gitcode")
        assert _check_host_status("wh4241", "test", RK) == ""
        assert get_host_status(RK) == ""

    def test_partial_diagnostic_is_not_overridden_by_drift(self):
        from src.domains.insight.routes.dashboard import _check_host_status
        from src.domains.mgmt.user.db import get_host_status, set_host_status
        _add_row(webhook_url="https://old-tunnel.example.net/webhook/gitcode")
        set_host_status(RK, "partial", "check_bot", "bot 未确认加入")
        assert _check_host_status("wh4241", "test", RK) == "partial"
        assert get_host_status(RK) == "partial"