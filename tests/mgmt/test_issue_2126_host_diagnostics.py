"""Issue #2126: 托管流程诊断字段、结构化缺失项与 partial 超时回收测试。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from src.kernel.session_manager import db, db_lock  # noqa: E402

RK = "gitcode:diagnose/test"


@pytest.fixture(scope="session", autouse=True)
def _ensure_tables():
    from src.api import db_setup
    db_setup.init_database()


@pytest.fixture(autouse=True)
def _clean_repo():
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM managed_repos WHERE repo_key LIKE 'gitcode:diagnose/%'")
        except Exception:
            pass
        db.commit()
    yield


def _add_repo(webhook_id=0):
    from src.domains.mgmt.user.db import add_managed_repo
    add_managed_repo(RK, "diagnose", "test", webhook_id=webhook_id)


def test_managed_repos_has_diagnostic_columns():
    with db_lock:
        c = db.cursor()
        cols = {r[1] for r in c.execute("PRAGMA table_info(managed_repos)").fetchall()}
    assert {"host_status", "host_step", "host_error", "host_updated_at"} <= cols


def test_set_and_get_host_detail():
    from src.domains.mgmt.user.db import set_host_status, get_host_detail
    _add_repo()
    set_host_status(RK, "partial", "create_webhook", "webhook 创建失败，请检查权限后重试")
    detail = get_host_detail(RK)
    assert detail["status"] == "partial"
    assert detail["step"] == "create_webhook"
    assert detail["error"] == "webhook 创建失败，请检查权限后重试"
    assert detail["updated_at"]


def test_success_clears_host_detail():
    from src.domains.mgmt.user.db import set_host_status, get_host_detail
    _add_repo()
    set_host_status(RK, "bot_missing", "check_bot", "限流")
    set_host_status(RK, "")
    detail = get_host_detail(RK)
    assert detail["status"] == ""
    assert detail["step"] == ""
    assert detail["error"] == ""


def test_check_host_status_recovers_stale_partial():
    from src.domains.mgmt.user.db import set_host_status, get_host_detail
    from src.domains.insight.routes.dashboard import _check_host_status
    from src.api.time_utils import db_now_str
    _add_repo(webhook_id=12345)
    set_host_status(RK, "partial", "create_webhook", "超时未完成")
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE managed_repos SET host_updated_at=? WHERE repo_key=?",
                  (db_now_str(-3600), RK))
        db.commit()
    assert _check_host_status("diagnose", "test", RK) == ""
    assert get_host_detail(RK)["status"] == ""


def test_check_host_status_keeps_fresh_partial():
    from src.domains.mgmt.user.db import set_host_status, get_host_detail
    from src.domains.insight.routes.dashboard import _check_host_status
    _add_repo(webhook_id=12345)
    set_host_status(RK, "partial", "create_webhook", "刚失败")
    assert _check_host_status("diagnose", "test", RK) == "partial"
    detail = get_host_detail(RK)
    assert detail["step"] == "create_webhook"
    assert detail["error"] == "刚失败"


def test_missing_items_mapping():
    from src.domains.insight.routes.dashboard import _host_missing_items
    assert _host_missing_items("bot_missing") == ["bot"]
    assert _host_missing_items("webhook_missing") == ["webhook"]
    assert _host_missing_items("partial") == ["bot"]
    assert _host_missing_items("partial", "path_b_fallback") == ["bot"]
    assert _host_missing_items("partial", "create_webhook") == ["webhook"]
    assert _host_missing_items("partial", "user_token") == ["token"]
    assert _host_missing_items("") == []

def test_unknown_updated_at_is_not_reclaimed():
    """Spec：旧数据空值视为未知，存量 partial（host_updated_at 空）不应首次访问即被清空。"""
    from src.domains.mgmt.user.db import set_host_status, get_host_detail
    from src.domains.insight.routes.dashboard import _check_host_status, _host_partial_stale
    _add_repo(webhook_id=12345)
    set_host_status(RK, "partial", "path_b_fallback", "历史遗留")
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE managed_repos SET host_updated_at='' WHERE repo_key=?", (RK,))
        db.commit()
    assert _host_partial_stale("") is False
    assert _check_host_status("diagnose", "test", RK) == "partial"
    assert get_host_detail(RK)["status"] == "partial"


def test_stale_partial_bot_step_becomes_bot_missing():
    """超时复查按诊断步骤判定：bot 相关（path_b_fallback）不得直接收敛为完整。"""
    from src.domains.mgmt.user.db import set_host_status, get_host_detail
    from src.domains.insight.routes.dashboard import _check_host_status
    from src.api.time_utils import db_now_str
    _add_repo(webhook_id=12345)
    set_host_status(RK, "partial", "path_b_fallback", "全局 bot 不可用")
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE managed_repos SET host_updated_at=? WHERE repo_key=?",
                  (db_now_str(-3600), RK))
        db.commit()
    assert _check_host_status("diagnose", "test", RK) == "bot_missing"
    detail = get_host_detail(RK)
    assert detail["status"] == "bot_missing"
    assert detail["step"] == "path_b_fallback"
    assert detail["error"] == "全局 bot 不可用"


def test_stale_partial_unknown_step_not_recovered():
    """无诊断步骤时按保守策略收敛为 bot_missing，而不是无条件判完整。"""
    from src.domains.mgmt.user.db import set_host_status, get_host_detail
    from src.domains.insight.routes.dashboard import _check_host_status
    from src.api.time_utils import db_now_str
    _add_repo(webhook_id=12345)
    set_host_status(RK, "partial")
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE managed_repos SET host_updated_at=? WHERE repo_key=?",
                  (db_now_str(-3600), RK))
        db.commit()
    assert _check_host_status("diagnose", "test", RK) == "bot_missing"
    detail = get_host_detail(RK)
    assert detail["status"] == "bot_missing"
    assert detail["step"] == "verify_bot"


def test_get_all_managed_repos_includes_host_status():
    """降级分支读取 host_status，主查询必须 SELECT 该列。"""
    from src.domains.mgmt.user.db import set_host_status, get_all_managed_repos
    _add_repo(webhook_id=1)
    set_host_status(RK, "partial", "invite_bot", "x")
    rows = {r["repo_key"]: r for r in get_all_managed_repos()}
    assert rows[RK]["host_status"] == "partial"


def _upstream_err(code):
    return Exception(f"GitCode API POST https://example.invalid/hooks failed ({code}): boom")


def test_host_error_classification_and_text():
    from src.domains.mgmt.routes.repos import (
        _classify_host_error, _host_error_text, _host_http_error_code, _host_502_detail,
    )
    assert _classify_host_error(_upstream_err(401)) == "permission_denied"
    assert _classify_host_error(_upstream_err(403)) == "permission_denied"
    assert _classify_host_error(_upstream_err(404)) == "repo_not_found"
    assert _classify_host_error(_upstream_err(429)) == "rate_limited"
    assert _classify_host_error(_upstream_err(500)) == "upstream_unavailable"
    assert _classify_host_error(Exception("network down")) == "upstream_unavailable"
    assert _host_error_text("rate_limited") == "代码托管平台限流，请稍后重试"
    assert _host_error_text("unknown_code") == _host_error_text("host_failed")
    assert _host_http_error_code({"error_code": "TOKEN_PERMISSION_INSUFFICIENT"}) == "permission_denied"
    assert _host_http_error_code({"error_code": "REPO_NOT_FOUND"}) == "repo_not_found"
    assert _host_http_error_code({"error_code": "WEBHOOK_URL_NOT_CONFIGURED"}) == "webhook_url_missing"
    assert _host_http_error_code({"code": 404}) == "repo_not_found"
    assert _host_http_error_code(None) == "host_failed"
    detail = _host_502_detail("host_setup", _upstream_err(500))
    assert detail["host_status"] == "partial"
    assert detail["step"] == "host_setup"
    assert detail["missing"] == []


def _patch_repair(monkeypatch, state, calls, setup_exc=None, bot_token="bot-token", secret=""):
    import src.api.deps as deps
    import src.domains.mgmt.user.repo_manager as rm
    import src.domains.mgmt.user.db as udb

    def fake_member(_token, _owner, _repo, _user):
        return {"access_level": 40 if state["bot_ok"] else 0}

    def fake_add(*_a, **_k):
        calls["add"] += 1
        state["bot_ok"] = True
        return (True, "")

    def fake_find(*_a, **_k):
        return state["webhook"]

    def fake_setup(*_a, **_k):
        calls["setup"] += 1
        if setup_exc:
            raise setup_exc
        state["webhook"] = 4242
        return 4242

    def fake_remove(*_a, **_k):
        calls["remove"] += 1
        return True

    monkeypatch.setattr(deps, "get_user_token_with_refresh", lambda *_a, **_k: "tok")
    # B 批接缝迁移：token 解析已统一到 repos.get_user_token_for（模块级绑定）
    import src.domains.mgmt.routes.repos as _repos_mod
    monkeypatch.setattr(_repos_mod, "get_user_token_for", lambda *_a, **_k: "tok")
    monkeypatch.setattr(deps, "check_repo_access", lambda *_a, **_k: None)
    monkeypatch.setattr(udb, "get_webhook_url", lambda: "https://x.example/webhook/gitcode")
    monkeypatch.setattr(udb, "get_webhook_secret", lambda *_a, **_k: secret)
    monkeypatch.setattr(udb, "set_host_status", lambda *_a, **_k: None)
    monkeypatch.setattr(udb, "add_managed_repo", lambda *_a, **_k: None)
    monkeypatch.setattr(rm, "get_member_permission", fake_member)
    monkeypatch.setattr(rm, "add_collaborator", fake_add)
    monkeypatch.setattr(rm, "accept_invite_for_repo", lambda *_a, **_k: True)
    monkeypatch.setattr(rm, "get_hosting_provider_config", lambda _p: {
        "bot_token": bot_token,
        "bot_username": "grape_bot",
        "find_webhook": fake_find,
        "setup_webhook": fake_setup,
        "remove_webhook": fake_remove,
        "webhook_url_suffix": "/webhook/gitcode",
        "db_token_field": "gitcode_webhook_token",
        "db_bot_token_field": "bot_gitcode_token",
        # B 批契约新增：建 hook 鉴权参数按方言派生（gitee 系=webhook_password）
        "setup_webhook_kwargs": lambda secret, wh_token, use_path_b: {
            "webhook_password": (secret if use_path_b else wh_token)},
    })


def test_repair_host_idempotent(monkeypatch):
    """AC：重复 repair 不重复邀请 bot / 创建 webhook。"""
    from src.domains.mgmt.routes import repos as repos_mod
    state = {"bot_ok": False, "webhook": None}
    calls = {"add": 0, "setup": 0, "remove": 0}
    _patch_repair(monkeypatch, state, calls)

    r1 = repos_mod.repair_host("repair", "idem", {"username": "u", "id": 1})
    r2 = repos_mod.repair_host("repair", "idem", {"username": "u", "id": 1})
    assert r1["status"] == "ok" and r1["missing"] == []
    assert r2["status"] == "ok"
    assert calls == {"add": 1, "setup": 1, "remove": 0}


def test_repair_host_webhook_failure_is_structured(monkeypatch):
    """失败原因分类 + 结构化返回（step/missing/host_status）。"""
    from src.domains.mgmt.routes import repos as repos_mod
    state = {"bot_ok": True, "webhook": None}
    calls = {"add": 0, "setup": 0, "remove": 0}
    _patch_repair(monkeypatch, state, calls, setup_exc=_upstream_err(403))

    res = repos_mod.repair_host("repair", "fail", {"username": "u", "id": 1})
    assert res["status"] == "error"
    assert res["host_status"] == "webhook_missing"
    assert res["missing"] == ["webhook"]
    assert res["step"] == "create_webhook"
    assert res["message"] == "权限不足：请使用仓库所有者凭据，或授予机器人管理员权限"


def test_stale_partial_github_host_step_not_recovered():
    """github_host 为未知/兜底步骤，超时后保守收敛为 bot_missing，不得误清空。"""
    from src.domains.mgmt.user.db import set_host_status, get_host_detail
    from src.domains.insight.routes.dashboard import _check_host_status
    from src.api.time_utils import db_now_str
    _add_repo(webhook_id=12345)
    set_host_status(RK, "partial", "github_host", "GitHub 托管失败")
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE managed_repos SET host_updated_at=? WHERE repo_key=?",
                  (db_now_str(-3600), RK))
        db.commit()
    assert _check_host_status("diagnose", "test", RK) == "bot_missing"
    detail = get_host_detail(RK)
    assert detail["status"] == "bot_missing"
    assert detail["step"] == "github_host"


def test_stale_partial_user_token_step_kept_for_relogin():
    """user_token 缺失需用户重新登录，不得按 webhook 完成收敛。"""
    from src.domains.mgmt.user.db import set_host_status, get_host_detail
    from src.domains.insight.routes.dashboard import _check_host_status, _host_missing_items
    from src.api.time_utils import db_now_str
    _add_repo(webhook_id=12345)
    set_host_status(RK, "partial", "user_token", "Token 缺失")
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE managed_repos SET host_updated_at=? WHERE repo_key=?",
                  (db_now_str(-3600), RK))
        db.commit()
    assert _check_host_status("diagnose", "test", RK) == "partial"
    detail = get_host_detail(RK)
    assert detail["status"] == "partial"
    assert detail["step"] == "user_token"
    assert _host_missing_items("partial", detail["step"]) == ["token"]


def test_stale_partial_host_setup_step_not_recovered():
    """通用 host_setup 步骤不应被误判为 webhook 完成。"""
    from src.domains.mgmt.user.db import set_host_status
    from src.domains.insight.routes.dashboard import _check_host_status
    from src.api.time_utils import db_now_str
    _add_repo(webhook_id=12345)
    set_host_status(RK, "partial", "host_setup", "托管失败")
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE managed_repos SET host_updated_at=? WHERE repo_key=?",
                  (db_now_str(-3600), RK))
        db.commit()
    assert _check_host_status("diagnose", "test", RK) == "bot_missing"


def test_gitcode_webhook_502_detail_is_structured():
    """GitCode webhook 401/404 的 502 必须携带 host_status/missing/step。"""
    from src.domains.mgmt.routes.repos import _gitcode_webhook_502_detail
    d401 = _gitcode_webhook_502_detail(401)
    assert d401["code"] == 502
    assert d401["error_code"] == "WEBHOOK_TOKEN_INVALID"
    assert d401["host_status"] == "webhook_missing"
    assert d401["missing"] == ["webhook"]
    assert d401["step"] == "create_webhook"
    d404 = _gitcode_webhook_502_detail(404, "owner", "repo")
    assert d404["code"] == 502
    assert d404["error_code"] == "REPO_NOT_FOUND"
    assert d404["host_status"] == "webhook_missing"
    assert d404["missing"] == ["webhook"]
    assert d404["step"] == "create_webhook"
    assert "owner/repo" in d404["message"]


def test_repair_host_path_b_reuses_existing_webhook(monkeypatch):
    """路径 B 已存在 webhook 且 DB 有密钥时复用，不删不建（幂等）。"""
    from src.domains.mgmt.routes import repos as repos_mod
    state = {"bot_ok": True, "webhook": 555}
    calls = {"add": 0, "setup": 0, "remove": 0}
    _patch_repair(monkeypatch, state, calls, bot_token="", secret="stored-secret")

    res = repos_mod.repair_host("repair", "pathb", {"username": "u", "id": 1})
    assert res["status"] == "ok"
    assert res["webhook_id"] == 555
    assert calls == {"add": 0, "setup": 0, "remove": 0}
    assert state["webhook"] == 555


def test_repair_host_path_b_rebuilds_when_secret_mismatched(monkeypatch):
    """路径 B DB 无密钥（无法验证一致）时才删旧重建。"""
    from src.domains.mgmt.routes import repos as repos_mod
    state = {"bot_ok": True, "webhook": 555}
    calls = {"add": 0, "setup": 0, "remove": 0}
    _patch_repair(monkeypatch, state, calls, bot_token="", secret="")

    res = repos_mod.repair_host("repair", "pathb-rebuild", {"username": "u", "id": 1})
    assert res["status"] == "ok"
    assert res["webhook_id"] == 4242
    assert calls == {"add": 0, "setup": 1, "remove": 1}


def test_filter_repos_exposes_webhook_missing_status():
    """仓库列表在 webhook 未创建（非 hosted）时仍透出 host_status。"""
    from src.domains.mgmt.routes.repos import _filter_repos
    from src.domains.mgmt.user.db import set_host_status
    _add_repo(webhook_id=0)
    set_host_status(RK, "webhook_missing", "create_webhook", "webhook 未创建")
    rows = _filter_repos(1, [{
        "full_name": "diagnose/test", "owner": "diagnose", "name": "test",
        "repo_key": RK, "is_admin": True,
    }], 0)
    assert len(rows) == 1
    assert rows[0]["is_hosted"] is False
    assert rows[0]["host_status"] == "webhook_missing"


def test_list_managed_repos_exposes_host_status(monkeypatch):
    """托管列表接口透出 host_status（供侧边栏徽标渲染）。"""
    import src.domains.mgmt.user.db as udb
    import src.domains.mgmt.routes.repos as repos_mod
    monkeypatch.setattr(udb, "get_managed_repos", lambda *_a, **_k: [{
        "repo_key": RK, "full_name": "diagnose/test", "is_admin": True,
        "webhook_id": 0, "bot_gitcode_token": "", "host_status": "webhook_missing",
    }])
    out = repos_mod.list_managed_repos({"id": 1, "org_id": 0})
    assert out["repos"][0]["host_status"] == "webhook_missing"


def test_get_managed_repos_includes_host_status():
    """get_managed_repos 主查询必须 SELECT host_status。"""
    from src.domains.mgmt.user.db import set_host_status, get_managed_repos, mark_user_repo_join
    _add_repo(webhook_id=0)
    set_host_status(RK, "webhook_missing", "create_webhook", "x")
    mark_user_repo_join(1, RK, is_join=True, is_admin=True)
    rows = {r["repo_key"]: r for r in get_managed_repos(1)}
    assert rows[RK]["host_status"] == "webhook_missing"
