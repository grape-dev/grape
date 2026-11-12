"""D39（S59）用例：仓库平台绑定/解绑域收口——平台集合目录派生 + um 列防折叠。

破绽（先测后改，领域建模纠偏——绑定/解绑是仓库平台域的平台无关操作）：
1. grape_auth 解绑白名单 (gitcode, github) 字面量 → gitee 等已接入仓库平台
   无法解绑（400）。合法平台集合应从仓库平台目录（内置卡+已配置卡）派生。
2. um_column_for_provider 未知平台折叠 gitcode_user → 双向污染：绑定链
   _sync_um_platform_col 把 gitee 登录名写进 gitcode_user；解绑清错列；
   归属兜底读侧跨平台同名误命中（FR-17.1 反例）。无 um 列平台应返回 ""
   且调用方跳过。
3. get_host_vcs_username fallback 口径折叠 → gitee 仓查 host 用户名落
   gitcode 身份查空。
4. scenes._branches_cache_invalidate 双平台循环 → gitee 建分支后缓存不失效。
5. auto_upgrade_path_b_repos host token / 协作者邀请链 github-else 二分 →
   gitee host token 折叠 gitcode、邀请走 GitCode API。

解法：平台集合=list_vcs_platforms()（目录）；um 列映射=白名单表+空列跳过；
host token=deps.get_user_token_for(username, uid, provider)；协作者邀请/接受
=适配器端口 add_collaborator/accept_collaborator_invite（D36/D38 同款：形态
在适配器，调用方零平台分支）。
"""
import json
import uuid
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache


GITEE_CARD = {
    "name": "Gitee", "dialect": "github", "host": "gitee.com",
    "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
    "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
    "webhook_verify": "token_compare", "clone_auth": "basic_password",
    "bot_username": "g_grape_bot",
}


@pytest.fixture()
def gitee_card():
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (json.dumps({"gitee": GITEE_CARD}),))
        db.commit()
    invalidate_platform_card_cache()
    yield GITEE_CARD
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        db.commit()
    invalidate_platform_card_cache()


_MOCK_USER = {"id": 1, "username": "testuser", "email": "test@example.com", "token_type": "jwt"}


@pytest.fixture
def client():
    from src.api.main import app
    from src.api.deps import require_jwt_only
    app.dependency_overrides[require_jwt_only] = lambda: _MOCK_USER
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _mock_sm_db():
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = []
    mock_db = MagicMock()
    mock_db.cursor.return_value = mock_cursor
    mock_lock = MagicMock()
    mock_lock.__enter__ = MagicMock(return_value=None)
    mock_lock.__exit__ = MagicMock(return_value=False)
    return mock_db, mock_lock, mock_cursor


# ------------------------------------------------ S59 解绑平台集合目录派生

def _unbind_mocks():
    return [
        patch("src.domains.mgmt.routes.grape_auth.auth_audit.log_audit"),
        patch("src.domains.mgmt.routes.grape_auth.get_user_identities", return_value=[]),
        patch("src.domains.mgmt.routes.grape_auth.delete_external_identity", return_value=True),
        patch("src.domains.mgmt.routes.grape_auth.has_password", return_value=True),
        patch("src.domains.mgmt.routes.grape_auth.count_user_identities", return_value=2),
        patch("src.domains.mgmt.user.db.delete_credential"),
    ]


def test_s59_unbind_gitee_with_card(gitee_card, client):
    """gitee 卡就位 → gitee 身份可解绑（合法平台集合=仓库平台目录派生）。"""
    import contextlib
    mock_db, mock_lock, _cur = _mock_sm_db()
    with contextlib.ExitStack() as st:
        for m in _unbind_mocks():
            st.enter_context(m)
        mock_del = st.enter_context(patch("src.domains.mgmt.routes.grape_auth.delete_external_identity", return_value=True))
        st.enter_context(patch("src.kernel.session_manager.db", mock_db))
        st.enter_context(patch("src.kernel.session_manager.db_lock", mock_lock))
        r = client.delete("/api/auth/bind/gitee?confirm=true")
    assert r.status_code == 200, r.text
    assert mock_del.called
    assert mock_del.call_args.args[:2] == (1, "gitee")


def test_s59_unbind_unknown_provider_still_400(client):
    """未配卡平台 fail-closed：400 不支持的平台（LIKE 前缀安全前提保持）。"""
    r = client.delete("/api/auth/bind/foo?confirm=true")
    assert r.status_code == 400
    assert "不支持的平台" in r.json()["detail"]["message"]


def test_s59_unbind_gitee_without_card_400(client):
    """gitee 无卡（目录不含）→ 400（与未配卡平台一致，不回退字面量白名单）。"""
    r = client.delete("/api/auth/bind/gitee?confirm=true")
    assert r.status_code == 400


def test_s59_unbind_gitcode_parity(client):
    """gitcode 解绑行为不变（存量回归锁）。"""
    import contextlib
    mock_db, mock_lock, mock_cur = _mock_sm_db()
    with contextlib.ExitStack() as st:
        for m in _unbind_mocks():
            st.enter_context(m)
        st.enter_context(patch("src.kernel.session_manager.db", mock_db))
        st.enter_context(patch("src.kernel.session_manager.db_lock", mock_lock))
        r = client.delete("/api/auth/bind/gitcode?confirm=true")
    assert r.status_code == 200, r.text
    # um 平台列清理仍执行（gitcode_user）
    um_updates = [c for c in mock_cur.execute.call_args_list
                  if "unified_user_mapping" in str(c.args[0] if c.args else c)]
    assert um_updates, "gitcode 解绑应清 um gitcode_user 列"


# ------------------------------------------------ S59 um 列映射防折叠

def test_s59_um_column_mapping():
    from src.kernel.repo_key import um_column_for_provider, um_column_for_repo
    assert um_column_for_provider("gitcode") == "gitcode_user"
    assert um_column_for_provider("github") == "github_login"
    assert um_column_for_provider("gitee") == ""      # 无 um 列 → 空串，调用方跳过
    assert um_column_for_provider("unknown") == ""
    assert um_column_for_repo("gitee:acme/web") == ""
    assert um_column_for_repo("gitcode:acme/web") == "gitcode_user"
    assert um_column_for_repo("github:acme/web") == "github_login"


def test_s59_sync_um_platform_col_gitee_no_pollution(gitee_card):
    """gitee 绑定不写 um gitcode_user 列（跨平台污染消除）。"""
    from src.domains.mgmt.auth.grape_account_db import _sync_um_platform_col
    _uid = 9_100_000 + int(uuid.uuid4().hex[:6], 16) % 100_000
    with db_lock:
        db.cursor().execute("INSERT OR REPLACE INTO unified_user_mapping (user_id, gitcode_user) VALUES (?, '')", (int(_uid),))
        db.commit()
    try:
        _sync_um_platform_col(_uid, "gitee", "g_host_login")
        with db_lock:
            row = db.cursor().execute("SELECT gitcode_user FROM unified_user_mapping WHERE user_id=?", (_uid,)).fetchone()
            db.commit()
        assert (row[0] or "") == ""
        # gitcode parity：写入不变
        _sync_um_platform_col(_uid, "gitcode", "gc_login")
        with db_lock:
            row = db.cursor().execute("SELECT gitcode_user FROM unified_user_mapping WHERE user_id=?", (_uid,)).fetchone()
            db.commit()
        # S92/D68：no-op 后 gitcode_user 冻结为空（写入走身份域+启动回填）
        assert (row[0] or "") == ""
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM unified_user_mapping WHERE user_id=?", (_uid,))
            db.commit()


def test_s59_get_user_id_by_gitcode_no_cross_platform_hit(gitee_card):
    """归属兜底读侧：gitee 不再折叠 gitcode_user 查询（跨平台同名误命中消除）。"""
    from src.api.issues_center import _get_user_id_by_gitcode
    _uid = 9_200_000 + int(uuid.uuid4().hex[:6], 16) % 100_000
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO unified_user_mapping (user_id, gitcode_user) VALUES (?, 'dup_login')",
            (int(_uid),))
        db.commit()
    try:
        # gitee 仓：um 兜底跳过 → 不误命中 gitcode_user=dup_login
        assert _get_user_id_by_gitcode("dup_login", "gitee:acme/web") is None
        # gitcode 仓：兜底命中不变（parity）——D105 后 user_id 为 TEXT/uuid 域，
        # 返回值恒 str，断言按字符串化口径（原 == int 是类型迁移前的写法）。
        assert _get_user_id_by_gitcode("dup_login", "gitcode:acme/web") == str(_uid)
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM unified_user_mapping WHERE user_id=?", (_uid,))
            db.commit()


# ------------------------------------------------ S59 get_host_vcs_username 口径

def test_s59_get_host_vcs_username_gitee(gitee_card):
    """gitee 仓 host 用户名 fallback 按前缀查 gitee 身份（不再折叠 gitcode）。"""
    from src.domains.mgmt.user import db as udb
    from src.domains.mgmt.auth.grape_account_db import create_external_identity
    rk = "gitee:acme/hostrepo"
    uid = 9_000_001
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, host_user_id) "
            "VALUES (?, 'acme', 'hostrepo', 'acme/hostrepo', ?)", (rk, uid))
        db.commit()
    create_external_identity(uid, "gitee", "ext-1", external_login="g_host")
    try:
        assert udb.get_host_vcs_username(rk) == "g_host"
        # 显式 provider 参数语义不变
        assert udb.get_host_vcs_username(rk, provider="gitee") == "g_host"
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (rk,))
            db.cursor().execute("DELETE FROM external_identities WHERE grape_user_id=?", (uid,))
            db.commit()


# ------------------------------------------------ S59 scenes 缓存失效全平台

def test_s59_branches_cache_invalidate_all_platforms(gitee_card):
    """建分支后按仓库平台目录失效缓存（gitee 键不再漏）。"""
    from src.domains.mgmt.routes.scenes import (
        _branches_cache_put, _branches_cache_invalidate, _branches_cache_key)
    keys = [_branches_cache_key(p, "acme", "web") for p in ("gitcode", "github", "gitee")]
    for k in keys:
        _branches_cache_put(k, {"branches": []})
    _branches_cache_invalidate("acme", "web")
    for k in keys:
        _branches_cache_put  # keep import referenced
        from src.domains.mgmt.routes.scenes import _branches_cache
        assert k not in _branches_cache, k


# ------------------------------------------------ S59 auto_upgrade 端口化

def _mk_repo_row(rk, owner="acme", name="web", uid=123):
    return {"repo_key": rk, "repo_owner": owner, "repo_name": name, "host_user_id": uid}


def test_s59_auto_upgrade_gitee_provider_ports(gitee_card):
    """gitee：host token 走统一入口（provider=gitee）、协作者邀请/接受走适配器端口。"""
    from src.domains.mgmt.user import db as udb
    mock_prov = MagicMock()
    mock_prov.add_collaborator.return_value = True
    mock_prov.accept_collaborator_invite.return_value = True
    with patch("src.kernel.token_resolver.create_provider", return_value=mock_prov), \
            patch("src.domains.mgmt.user.db._query_path_b_repos", return_value=[_mk_repo_row("gitee:acme/web")]), \
            patch("src.domains.mgmt.auth.grape_account_db.get_grape_account_by_id",
                  return_value={"id": 123, "username": "host_user"}), \
            patch("src.api.deps.get_user_token_for", return_value="host_tok") as m_tok, \
            patch("src.domains.mgmt.user.db._rebuild_webhook_with_bot_token", return_value="5"), \
            patch("src.domains.mgmt.user.db._update_repo_bot_token") as m_upd:
        udb.auto_upgrade_path_b_repos("gitee", "bot_tok", "g_bot")
    m_tok.assert_called_once_with("host_user", 123, provider="gitee")
    mock_prov.add_collaborator.assert_called_once_with("host_tok", "acme", "web", "g_bot")
    mock_prov.accept_collaborator_invite.assert_called_once_with("bot_tok", "acme", "web")
    m_upd.assert_called_once()
    assert m_upd.call_args.args[2] == "gitee"


def test_s59_auto_upgrade_gitcode_parity():
    """gitcode：host token 统一入口（provider=gitcode，D37 等价）；邀请仍存量委托。"""
    from src.domains.mgmt.user import db as udb
    with patch("src.domains.mgmt.user.db._query_path_b_repos", return_value=[_mk_repo_row("gitcode:acme/web")]), \
            patch("src.domains.mgmt.auth.grape_account_db.get_grape_account_by_id",
                  return_value={"id": 123, "username": "host_user"}), \
            patch("src.api.deps.get_user_token_for", return_value="host_tok") as m_tok, \
            patch("src.domains.mgmt.user.repo_manager.add_collaborator", return_value=(True, "")) as m_add, \
            patch("src.domains.mgmt.user.repo_manager.accept_invite_for_repo", return_value=True) as m_acc, \
            patch("src.domains.mgmt.user.db._rebuild_webhook_with_bot_token", return_value="5"), \
            patch("src.domains.mgmt.user.db._update_repo_bot_token") as m_upd:
        udb.auto_upgrade_path_b_repos("gitcode", "bot_tok", "g_bot")
    m_tok.assert_called_once_with("host_user", 123, provider="gitcode")
    m_add.assert_called_once_with("host_tok", "acme", "web", "g_bot", "maintainer")
    m_acc.assert_called_once_with("bot_tok", "acme", "web")
    m_upd.assert_called_once()


def test_s59_auto_upgrade_github_parity():
    """github：邀请/接受仍存量委托（push 权限语义不变）。"""
    from src.domains.mgmt.user import db as udb
    with patch("src.domains.mgmt.user.db._query_path_b_repos", return_value=[_mk_repo_row("github:acme/web")]), \
            patch("src.domains.mgmt.auth.grape_account_db.get_grape_account_by_id",
                  return_value={"id": 123, "username": "host_user"}), \
            patch("src.api.deps.get_user_token_for", return_value="host_tok"), \
            patch("src.domains.mgmt.user.repo_manager.add_github_collaborator", return_value=True) as m_add, \
            patch("src.domains.mgmt.user.repo_manager.accept_github_invite", return_value=True) as m_acc, \
            patch("src.domains.mgmt.user.db._rebuild_webhook_with_bot_token", return_value="5"), \
            patch("src.domains.mgmt.user.db._update_repo_bot_token"):
        udb.auto_upgrade_path_b_repos("github", "bot_tok", "g_bot")
    m_add.assert_called_once_with("host_tok", "acme", "web", "g_bot", "push")
    m_acc.assert_called_once_with("bot_tok", "acme", "web")


# ------------------------------------------------ S59 结构守卫

def test_s59_structure_unbind_and_upgrade_no_literal():
    """解绑路由与 auto_upgrade 协作者块不得再含平台名比较（防回潮）。"""
    import ast
    import inspect
    from src.domains.mgmt.routes import grape_auth
    from src.domains.mgmt.user import db as udb
    banned = {"github", "gitcode", "gitee"}
    fn = grape_auth.unbind_provider
    src = inspect.getsource(fn)
    tree = ast.parse(src)
    body = tree.body[0].body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Compare):
                for cmp in node.comparators + [node.left]:
                    if isinstance(cmp, ast.Constant) and isinstance(cmp.value, str) and cmp.value in banned:
                        pytest.fail(f"unbind_provider 含平台名比较: {cmp.value}")
    src2 = inspect.getsource(udb.auto_upgrade_path_b_repos)
    for lit in banned:
        # auto_upgrade 内 token/邀请链不得再有 github/else 二分（比较形态）
        import re
        for m in re.finditer(r'provider\s*==\s*["\'](\w+)["\']', src2):
            pytest.fail(f"auto_upgrade 含 provider 比较: {m.group(0)}")
