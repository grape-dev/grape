"""P9：/repos/managed has_bot_token 平台通用化（卡派生，列/单表两路径）。

缺陷实锤：列表项写死 has_bot_token=bool(r.get('bot_gitcode_token'))——
gitee 卡 token_storage_field 为空（凭据只落 grape_t_repo_credentials 单表），
gitee 仓凭据有值也恒 false。修复：列表项与 /repos/bot-token 单仓接口统一走
repo_has_bot_credential（repo_key 前缀 → 卡：列平台查 managed_repos 列，
无列平台查 grape_t_repo_credentials；列→rc 回落与 get_bot_token_by_repo 读序一致）。

用例：
- ① gitee（无列平台）rc 单表有凭据 → 列表 has_bot_token=True（修复前恒 false）
- ② gitee 同场景单仓 /repos/bot-token 与列表同口径（True）
- ③ gitcode 列路径回归：列有密文 token → True（不回潮）
- ④ github 列路径回归：bot_github_token 列有密文 token → True
- ⑤ 无凭据 gitee 仓 → False
"""
import os

os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

import uuid

from src.kernel.session_manager import db, db_lock


def _iso(provider: str) -> str:
    return f"{provider}:p9/{uuid.uuid4().hex[:10]}"


def _seed(provider: str, repo_key: str, *, col_token: str = "", rc_token: str = ""):
    """播种托管行（路径 A：host_user_id=0）+ 可选凭据（列 / rc 单表）。"""
    from src.domains.mgmt.user.db import add_managed_repo, mark_user_repo_join, _encrypt_repo_secret
    owner, name = repo_key.split(":", 1)[1].split("/")
    ok = add_managed_repo(repo_key, owner, name, host_user_id=0, webhook_id=0)
    assert ok, f"播种托管行失败: {repo_key}"
    with db_lock:
        c = db.cursor()
        # 冷启动测试库可能未建 rc 单表（生产由盖章迁移建）——幂等补建（uc39 同款）
        c.execute(
            "CREATE TABLE IF NOT EXISTS grape_t_repo_credentials ("
            "id TEXT, repo_key TEXT, cred_type TEXT, value TEXT, updated_at TEXT)")
        if col_token:
            _col = f"bot_{provider}_token"
            # QA 第7轮整改（2026-11-13）：F4/D106 终态后冷启动库不再建 gitee/gitlab
            # 存量列（_ensure_platform_storage_columns 对卡 token_storage_field 空串
            # 平台跳过）；⑥/⑥b 模拟的是「存量部署」形态，须先幂等补列再 UPDATE
            # （对齐 S30 idiom：test_login_whitelist_row_model.py:52 手动 ALTER 模拟
            # 存量库）。PRAGMA 探测列存在性——已建列库（gitcode/github 存量环境、
            # 或同批先跑的用例）零副作用。
            _have_col = _col in {
                r[1] for r in c.execute("PRAGMA table_info(managed_repos)").fetchall()}
            if not _have_col:
                c.execute(
                    f"ALTER TABLE managed_repos ADD COLUMN {_col} TEXT DEFAULT ''")
            c.execute(
                f"UPDATE managed_repos SET {_col}=? WHERE repo_key=?",
                (_encrypt_repo_secret(col_token), repo_key))
        if rc_token:
            from src.domains.mgmt.user.db import _upsert_repo_credential
            _upsert_repo_credential(c, repo_key, "bot", rc_token)
        db.commit()
    mark_user_repo_join(1, repo_key, is_join=True)


def _cleanup(repo_key: str):
    with db_lock:
        db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (repo_key,))
        db.cursor().execute("DELETE FROM grape_t_repo_credentials WHERE repo_key=?", (repo_key,))
        db.cursor().execute("DELETE FROM user_repos WHERE repo_key=?", (repo_key,))
        db.commit()


def _client():
    from fastapi.testclient import TestClient
    from src.api.main import app
    from src.api.deps import require_org_member
    app.dependency_overrides[require_org_member] = lambda: {"id": 1, "org_id": 0}
    return TestClient(app)


# ──────────────── ① gitee rc 单表路径（缺陷主场景） ────────────────

def test_p9_gitee_rc_credential_list_has_bot_token_true():
    """gitee 仓凭据在 grape_t_repo_credentials → /repos/managed has_bot_token=True
    （修复前写死 bot_gitcode_token 列恒 false）。"""
    rk = _iso("gitee")
    _seed("gitee", rk, rc_token="p9_gitee_tok")
    client = _client()
    try:
        r = client.get("/api/repos/managed", headers={"Authorization": "Bearer test"})
        assert r.status_code == 200, r.text[:300]
        hit = [x for x in r.json()["repos"] if x["repo_key"] == rk]
        assert hit, f"仓库应在列表: {r.json()['repos'][:2]}"
        assert hit[0]["has_bot_token"] is True, \
            "gitee 仓 rc 凭据有值应 True（写死 bot_gitcode_token 列的缺陷回潮）"
    finally:
        client.app.dependency_overrides.clear()
        _cleanup(rk)


# ──────────────── ② 单仓接口同口径 ────────────────

def test_p9_gitee_single_repo_endpoint_same_answer():
    """gitee 仓 /repos/bot-token/{o}/{r} 与列表同一判定（True），消除双口径。"""
    from src.api.deps import require_no_session_token
    from fastapi.testclient import TestClient
    from src.api.main import app
    rk = _iso("gitee")
    _seed("gitee", rk, rc_token="p9_gitee_tok2")
    app.dependency_overrides[require_no_session_token] = lambda: {"id": 1, "username": "p9"}
    client = TestClient(app)
    try:
        owner, name = rk.split(":", 1)[1].split("/")
        r = client.get(f"/api/repos/bot-token/{owner}/{name}",
                       headers={"Authorization": "Bearer test"})
        assert r.status_code == 200, r.text[:300]
        assert r.json()["has_bot_token"] is True, \
            "单仓接口应与列表同口径（gitee rc 凭据 → True）"
    finally:
        app.dependency_overrides.clear()
        _cleanup(rk)


# ──────────────── ③④ 列平台回归（gitcode/github 不变） ────────────────

def test_p9_gitcode_column_path_regression():
    """gitcode：managed_repos.bot_gitcode_token 列有密文 token → True（存量回归）。"""
    rk = _iso("gitcode")
    _seed("gitcode", rk, col_token="p9_gitcode_tok")
    client = _client()
    try:
        r = client.get("/api/repos/managed", headers={"Authorization": "Bearer test"})
        assert r.status_code == 200, r.text[:300]
        hit = [x for x in r.json()["repos"] if x["repo_key"] == rk]
        assert hit, f"仓库应在列表: {r.json()['repos'][:2]}"
        assert hit[0]["has_bot_token"] is True, "gitcode 列路径应保持 True"
    finally:
        client.app.dependency_overrides.clear()
        _cleanup(rk)


def test_p9_github_column_path_regression():
    """github：managed_repos.bot_github_token 列有密文 token → True（存量回归）。"""
    rk = _iso("github")
    _seed("github", rk, col_token="p9_github_tok")
    client = _client()
    try:
        r = client.get("/api/repos/managed", headers={"Authorization": "Bearer test"})
        assert r.status_code == 200, r.text[:300]
        hit = [x for x in r.json()["repos"] if x["repo_key"] == rk]
        assert hit, f"仓库应在列表: {r.json()['repos'][:2]}"
        assert hit[0]["has_bot_token"] is True, "github 列路径应保持 True"
    finally:
        client.app.dependency_overrides.clear()
        _cleanup(rk)


# ──────────────── ⑥ 存量列回落（live-w10 实锤场景） ────────────────

def test_p9_gitee_legacy_column_fallback_true():
    """⑥ gitee（空卡列平台）rc 单表无凭据、但旧列 bot_gitee_token 有值
    （live-w10 存量数据形态：老部署无 rc 表、凭据落旧列）→ True。

    修复前恒 False（终态口径只查 rc，存量列数据失真——与白名单行式化
    「行式双读存量列」同教训）。"""
    rk = _iso("gitee")
    _seed("gitee", rk, col_token="p9_legacy_gitee_tok")
    client = _client()
    try:
        r = client.get("/api/repos/managed", headers={"Authorization": "Bearer test"})
        assert r.status_code == 200, r.text[:300]
        hit = [x for x in r.json()["repos"] if x["repo_key"] == rk]
        assert hit, f"仓库应在列表: {r.json()['repos'][:2]}"
        assert hit[0]["has_bot_token"] is True, \
            "gitee 存量列（bot_gitee_token）有值应经回落路径判 True"
    finally:
        client.app.dependency_overrides.clear()
        _cleanup(rk)


def test_p9_gitee_legacy_column_token_value_via_get_bot_token():
    """⑥b get_bot_token_by_repo 对 gitee 存量列同样取得到值（同一读序实现）。"""
    from src.domains.mgmt.user.db import get_bot_token_by_repo
    rk = _iso("gitee")
    _seed("gitee", rk, col_token="p9_legacy_gitee_tok2")
    try:
        owner, name = rk.split(":", 1)[1].split("/")
        assert get_bot_token_by_repo(owner, name) == "p9_legacy_gitee_tok2", \
            "取值接口应与存在性判定同一读序（存量列回落）"
    finally:
        _cleanup(rk)


# ──────────────── ⑤ 无凭据 → False（不虚报） ────────────────

def test_p9_gitee_no_credential_false():
    """gitee 仓无任何凭据 → has_bot_token=False。"""
    rk = _iso("gitee")
    _seed("gitee", rk)
    client = _client()
    try:
        r = client.get("/api/repos/managed", headers={"Authorization": "Bearer test"})
        assert r.status_code == 200, r.text[:300]
        hit = [x for x in r.json()["repos"] if x["repo_key"] == rk]
        assert hit, f"仓库应在列表: {r.json()['repos'][:2]}"
        assert hit[0]["has_bot_token"] is False, "无凭据应 False"
    finally:
        client.app.dependency_overrides.clear()
        _cleanup(rk)
