"""D105k 绑定域解耦收口行为锁（无标记版，2026-11-09 用户裁定方案 A）。

背景：uid=24 gitcode 实锤——历史专用字段路径把 PAT 写进 ei.access_token，
`has_oauth_token = bool(access_token)` 把 PAT 行误判 OAuth，安全页出现
"已绑定+解绑"自相矛盾行。

修法（用户裁定，方案 A 彻底解耦，**无过渡标记字段**）：
- ei.access_token 只承载 OAuth token：非空即 OAuth，天然语义，零标记
- PAT 权威存储=凭据域 <p>_pat；专用字段写入路径改写（不再触碰 ei token）
- 存量迁移=搬家：ei 里的历史 PAT 搬进凭据域（等值副本→只清 ei；
  OAuth 痕迹（refresh/expires）→保留；渠道未配置→搬家；歧义→保守保留）
- 供给收敛：get_user_token_for = ①凭据域 <p>_pat ②ei OAuth 兜底——
  搬家/改写全程断不了供
"""
import json

import pytest

from src.domains.mgmt.auth.grape_account_db import (
    backfill_pat_identity_tokens,
    create_external_identity,
    get_user_identities,
)
from src.kernel.session_manager import db, db_lock
from src.domains.mgmt.user.db import set_credential

NS = "d105k"


@pytest.fixture(autouse=True)
def _clean_rows():
    _wipe()
    yield
    _wipe()


def _wipe():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM external_identities WHERE grape_user_id LIKE ?", (f"{NS}%",))
        c.execute("DELETE FROM user_credentials WHERE CAST(user_id AS TEXT) LIKE ?", (f"{NS}%",))
        c.execute("DELETE FROM meta WHERE key LIKE 'oauth_zz%'")
        db.commit()


def _ident(user_id, provider):
    for ident in get_user_identities(str(user_id)):
        if ident["provider"] == provider:
            return ident
    return None


def _raw(user_id, provider):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT access_token, refresh_token, token_expires_at, metadata "
                  "FROM external_identities WHERE grape_user_id=? AND provider=?",
                  (str(user_id), provider))
        return c.fetchone()


def _ensure_password(user_id):
    """给测试账号设密码（登录方式保底检查需要）。"""
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR IGNORE INTO grape_accounts (id, username, password_hash, status) "
                  "VALUES (?, ?, 'x', 1)", (str(user_id), f"{NS}u-{user_id}"))
        c.execute("UPDATE grape_accounts SET password_hash='x' WHERE id=?", (str(user_id),))
        db.commit()


def _cred_token(user_id, provider):
    from src.domains.mgmt.user.db import get_credential
    return str((get_credential(user_id, f"{provider}_pat") or {}).get("token", "") or "")


# ── 存量搬家 ──

def test_legacy_pat_row_moved_to_credential_domain_channel_unconfigured():
    """渠道未配置 + 无 OAuth 痕迹 → 可证明非 OAuth：搬家（凭据域落副本，ei 清空）。"""
    from src.domains.mgmt.user.db import set_meta_value
    uid, prov, tok = f"{NS}m1", "zznch", "legacy-pat-secret"
    set_meta_value(f"oauth_{prov}_client_id", "")  # 渠道未配置
    assert create_external_identity(uid, prov, f"{NS}e1", "legacy-login",
                                    access_token=tok)  # 历史形态：PAT 在 ei
    backfill_pat_identity_tokens()
    row = _raw(uid, prov)
    assert row[0] in ("", None), "搬家后 ei token 未清空"
    assert _cred_token(uid, prov) == tok, "token 未搬入凭据域=断供"
    assert _ident(uid, prov)["has_oauth_token"] is False
    meta = json.loads(row[3] or "{}")
    assert meta.get("source") == "pat", "归属标缺失（排查锚点）"


def test_oauth_trace_row_preserved():
    """refresh/expires 痕迹 → 真 OAuth：保留，has_oauth_token True。"""
    uid, prov = f"{NS}m2", "zznch"
    assert create_external_identity(uid, prov, f"{NS}e2", "lg",
                                    access_token="oauth-tok", refresh_token="ref-tok",
                                    expires_in=3600)
    backfill_pat_identity_tokens()
    row = _raw(uid, prov)
    assert row[0] and row[1], "OAuth 痕迹行被误动"
    assert _ident(uid, prov)["has_oauth_token"] is True
    assert _cred_token(uid, prov) == "", "OAuth token 被误搬入 PAT 域"


def test_equal_copy_row_cleared_not_duplicated():
    """凭据域已有等值副本 → 只清 ei，副本不动。"""
    from src.domains.mgmt.user.db import set_credential
    uid, prov, tok = f"{NS}m3", "zznch", "same-pat"
    set_credential(uid, f"{prov}_pat", {"token": tok, "login": "lg"})
    assert create_external_identity(uid, prov, f"{NS}e3", "lg", access_token=tok)
    backfill_pat_identity_tokens()
    row = _raw(uid, prov)
    assert row[0] in ("", None), "等值副本场景 ei 未清"
    assert _cred_token(uid, prov) == tok, "副本被改写"


def test_ambiguous_row_conservatively_kept():
    """渠道在配、无痕迹 → 不可判定：token 保留（误显示已绑定好过误踢 OAuth）。"""
    from src.domains.mgmt.user.db import set_meta_value
    uid, prov = f"{NS}m4", "zzcfg"
    set_meta_value(f"oauth_{prov}_client_id", "client-xyz")
    assert create_external_identity(uid, prov, f"{NS}e4", "lg", access_token="maybe-oauth")
    backfill_pat_identity_tokens()
    row = _raw(uid, prov)
    assert row[0], "歧义行被误动（可能是真 OAuth）"
    assert _ident(uid, prov)["has_oauth_token"] is True


def test_backfill_idempotent():
    uid, prov, tok = f"{NS}m5", "zznch", "pat-idem"
    assert create_external_identity(uid, prov, f"{NS}e5", "lg", access_token=tok)
    backfill_pat_identity_tokens()
    snap1 = (_raw(uid, prov), _cred_token(uid, prov))
    backfill_pat_identity_tokens()
    snap2 = (_raw(uid, prov), _cred_token(uid, prov))
    assert snap1 == snap2, "二次回填状态漂移（幂等破坏）"


# ── 供给收敛 ──

def test_get_user_token_for_pat_tier_hits_after_move():
    """搬家后供给不断：get_user_token_for ①凭据域命中（user_id 定位）。"""
    from src.api.deps import get_user_token_for
    uid, prov, tok = f"{NS}s1", "zznch", "supply-pat"
    assert create_external_identity(uid, prov, f"{NS}e6", "lg", access_token=tok)
    backfill_pat_identity_tokens()
    assert get_user_token_for("", user_id=uid, provider=prov) == tok, \
        "搬家后统一入口取不到 token=断供"


def test_get_user_token_for_pat_priority_over_ei():
    """PAT 显式配置优先于 ei OAuth（与 fetch_user_token 单序口径一致）。"""
    from src.api.deps import get_user_token_for
    uid, prov = f"{NS}s2", "zznch"
    assert create_external_identity(uid, prov, f"{NS}e7", "lg",
                                    access_token="oauth-tok", refresh_token="r",
                                    expires_in=3600)
    from src.domains.mgmt.user.db import set_credential
    set_credential(uid, f"{prov}_pat", {"token": "explicit-pat", "login": "lg"})
    assert get_user_token_for("", user_id=uid, provider=prov) == "explicit-pat"


def test_oauth_fallback_preserved_when_pat_configured_on_top():
    """能力锁（用户裁定"能力不要丢失"）：OAuth 绑定 + 配置 PAT 共存场景——
    PAT 配置走凭据域，ei 的 OAuth token 不得被清（否则清除 PAT 后无回落）。
    专用路径对 ei 的更新用空 access_token（保留旧值语义）。"""
    from src.domains.mgmt.auth.grape_account_db import update_external_identity_tokens
    uid, prov = f"{NS}s3", "zznch"
    assert create_external_identity(uid, prov, f"{NS}e8", "lg",
                                    access_token="oauth-tok", refresh_token="ref",
                                    expires_in=3600)
    # 模拟专用路径：ei 更新不带明文 token（空参=保留），PAT 另行写凭据域
    assert update_external_identity_tokens(uid, prov, access_token="",
                                           external_name="lg2")
    row = _raw(uid, prov)
    assert row[0] and row[1], "OAuth token/refresh 被清——清除 PAT 后无回落，能力丢失"
    assert _ident(uid, prov)["has_oauth_token"] is True


# ── 接口层守卫：专用字段路径不得再把 PAT 写进 ei ──

def test_structural_dedicated_path_writes_credential_domain():
    """settings.py 专用字段路径结构守卫：gitcode/github PAT 必须写凭据域，
    ei 写入（update/create_external_identity_tokens）不得携带明文 token 实参。"""
    from pathlib import Path
    src = Path("src/domains/mgmt/routes/settings.py").read_text(encoding="utf-8")
    for var, plat in (("_gc_tok", "gitcode"), ("_gh_tok", "github")):
        # ei 写入调用点不得携带明文 token 实参（解耦回潮即红）
        import re
        for m in re.finditer(r"update_external_identity_tokens\([^;]*?\)\n", src, re.S):
            seg = m.group(0)
            if plat in seg and "access_token=\"\"" not in seg:
                assert var not in seg, \
                    f"{plat} 专用路径仍把明文 token 实参写进 ei（解耦回潮）: {seg[:120]}"
        assert f'{plat}_pat"' in src, f"{plat} 缺凭据域写入"


# ── 清除 PAT 四象限级联（_cascade_pat_clear）──

def _wipe_user_repos():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_repos WHERE user_id LIKE ?", (f"{NS}%",))
        db.commit()


def _seed_user_repo(user_id, provider):
    rk = f"{provider}:owner/repo-{user_id}"
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR IGNORE INTO user_repos (user_id, repo_key) VALUES (?, ?)",
                  (str(user_id), rk))
        db.commit()
    return rk


def _user_repo_count(user_id, provider):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM user_repos WHERE user_id=? AND repo_key LIKE ?",
                  (str(user_id), f"{provider}:%"))
        return c.fetchone()[0]


def test_cascade_clear_without_oauth_full_unbind():
    """无 OAuth 清 PAT → 平台级解绑：ei 归属行删、凭据清、user_repos 清（列表不残留）。"""
    from src.domains.mgmt.routes.settings import _cascade_pat_clear
    uid, prov = f"{NS}c1", "zznch"
    set_credential(uid, f"{prov}_pat", {"token": "t1", "login": "lg"})
    assert create_external_identity(uid, prov, f"{NS}ce1", "lg")  # PAT 归属行（token 空）
    _ensure_password(uid)  # 登录方式保底：PAT 行非登录凭据，清后仍有密码可登录
    rk = _seed_user_repo(uid, prov)
    user = {"id": uid, "username": f"{NS}u1"}
    result = _cascade_pat_clear(user, prov)
    assert result["oauth_kept"] is False
    assert _ident(uid, prov) is None, "ei 归属行未删——仓库列表/绑定残留"
    assert _cred_token(uid, prov) == "", "PAT 凭据未清"
    assert _user_repo_count(uid, prov) == 0, "user_repos 缓存残留（实弹：清 PAT 后仓库仍可见）"
    _wipe_user_repos()


def test_cascade_clear_with_oauth_keeps_binding_and_fallback():
    """OAuth 绑定 + PAT 共存 → 清 PAT 只清凭据：ei OAuth 绑定保留（回落能力）。"""
    from src.domains.mgmt.routes.settings import _cascade_pat_clear
    uid, prov = f"{NS}c2", "zznch"
    set_credential(uid, f"{prov}_pat", {"token": "t2", "login": "lg"})
    assert create_external_identity(uid, prov, f"{NS}ce2", "lg",
                                    access_token="oauth-tok", refresh_token="r",
                                    expires_in=3600)
    _ensure_password(uid)
    _seed_user_repo(uid, prov)
    user = {"id": uid, "username": f"{NS}u2"}
    result = _cascade_pat_clear(user, prov)
    assert result["oauth_kept"] is True
    ident = _ident(uid, prov)
    assert ident is not None and ident["has_oauth_token"] is True, \
        "OAuth 绑定被误删——清除 PAT 后无回落，能力丢失"
    assert _cred_token(uid, prov) == "", "PAT 凭据未清"
    assert _user_repo_count(uid, prov) == 1, "OAuth 在：仓库列表应保留（供给②接管）"
    _wipe_user_repos()
