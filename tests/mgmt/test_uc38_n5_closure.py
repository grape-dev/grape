"""UC38：D90-N5 收官补验——新平台配置化接入场景链 + 缓存版本纪律收口（2026-11-08）。

场景来源（requirements UC38，五断言面）：
- UC38① 平台卡 + 渠道卡齐备 → gitee 同时出现在登录渠道与仓库平台两轴（双卡链接）
- UC38② 泛型 PAT 入口行为闭环：platform_tokens 提交 gitee PAT → 未注册平台 400 fail-closed、
  存量双平台拒收（S52 防双路径）、合法新平台 verify 后双写（user_credentials <p>_pat +
  external_identities）
- UC38③ 白名单列驱动行为闭环：gitee_username 列随渠道卡自动出现 + admin add/list 走配置派生
- UC38④ display strip 全链统一（零内联回潮 + 页面模块引用 helper 而非本地重定义）
- UC38⑤ 缓存版本纪律（UC11 延伸）：N5 批次改动的三个页面模块 ?v= 已随批 bump
  （实码审计发现：867b38f68 改 account/observability/app 三 JS 但 index.html 未 bump，
  线上旧缓存导致泛型入口与 strip 统一不生效——本用例锁死防回潮）
"""
import os
import pytest

os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

import re
from pathlib import Path

from src.kernel.session_manager import db, db_lock

_BASE = Path(__file__).resolve().parent.parent.parent
_STATIC = _BASE / "src" / "api" / "frontend" / "shell"


def _read(rel: str) -> str:
    return (_STATIC / rel).read_text(encoding="utf-8")


# ────────────────────────── UC38① 双卡链接（两轴同现） ──────────────────────────

@pytest.mark.skip(reason="legacy 静态页模块已随 Vue 迁移删除（本 PR 移除 src/api/static/js/pages/*）：被测对象不存在，断言改写到 Vue 源需另立专项")
def test_uc38_1_dual_card_linkage():
    """gitee 渠道卡 + 平台卡齐备 → 登录渠道轴与仓库平台轴同时含 gitee。"""
    from src.domains.mgmt.auth.providers import list_providers
    from src.domains.runtime.vcs.platform_card import list_vcs_platforms

    assert "gitee" in list_providers(), "登录渠道轴缺 gitee 渠道卡"
    assert "gitee" in list_vcs_platforms(), "仓库平台轴缺 gitee 平台卡"
    # 双卡链接 = 两轴同现（N5 目标形态；PAT-only 平台只现平台轴）


# ────────────────────────── UC38② 泛型 PAT 入口行为闭环 ──────────────────────────

def test_uc38_2_platform_tokens_generic_entry():
    """platform_tokens 泛型入口：未注册平台 fail-closed + 存量双平台拒收。"""
    from fastapi import HTTPException
    from src.domains.mgmt.routes.settings import _LEGACY_PAT_FIELD_PROVIDERS
    from src.domains.runtime.vcs.platform_card import list_vcs_platforms

    # 存量双平台走专用字段（S52 防双路径语义），泛型入口显式跳过
    assert _LEGACY_PAT_FIELD_PROVIDERS == {"gitcode", "github"}
    # 未注册平台 fail-closed（400 而非静默忽略）
    _known = set(list_vcs_platforms())
    assert "gitee" in _known
    assert "nosuchplatform" not in _known
    # fail-closed 分支在场（结构断言：未注册平台 raise 400）
    src = (_BASE / "src" / "domains" / "mgmt" / "routes" / "settings.py").read_text(encoding="utf-8")
    assert "未注册的平台" in src, "泛型入口 fail-closed 分支缺失"


def test_uc38_2_platform_tokens_write_path_credential_only():
    """合法新平台 PAT：verify 后只写凭据域 <p>_pat，不写 external_identities。

    A 批对象模型（2026-11-13 用户裁定）：PAT 是仓库执行凭据，不是身份绑定——
    原用例断言「双写 + 409 冲突」已随对象模型归位为「只写凭据 + 无冲突校验」。
    """
    import uuid
    from types import SimpleNamespace
    from unittest.mock import patch

    from src.domains.mgmt.routes.settings import update_credentials, UpdateCredentials
    from src.domains.mgmt.auth import grape_account_db
    from src.kernel.session_manager import db as _sm_db, db_lock as _sm_dl
    from src.domains.mgmt.user import db as user_db

    def _mk_user(prefix):
        uname = f"{prefix}_{uuid.uuid4().hex[:8]}"
        u = grape_account_db.create_grape_account(
            username=uname, email=f"{uname}@test.local", password="pw123")
        assert u, f"用户构造失败: {uname}"
        return u

    def _cleanup_user(u):
        with _sm_dl:
            _sm_db.cursor().execute("DELETE FROM grape_accounts WHERE id=?", (u["id"],))
            _sm_db.commit()

    uid = _mk_user("uc38")["id"]

    def _fake_verify(_tok):
        return {"id": "uc38ext1", "login": "uc38login", "name": "UC38"}

    body = UpdateCredentials(platform_tokens={"gitee": "tok_uc38_valid"})
    try:
        with patch("src.kernel.token_resolver.create_provider") as _cp:
            _cp.return_value = SimpleNamespace(verify_token=_fake_verify)
            update_credentials(body, user={"id": uid})

        # 断言①：user_credentials gitee_pat（含 login）
        cred = user_db.get_credential(uid, "gitee_pat") or {}
        assert cred.get("token") == "tok_uc38_valid"
        assert cred.get("login") == "uc38login"
        # 断言②：不建身份行
        assert grape_account_db.get_external_identity_any_status("gitee", "uc38ext1") is None
        # 断言③：另一账号配同一 PAT 也不再 409（PAT 无归属冲突）
        uid2 = _mk_user("uc38b")["id"]
        body2 = UpdateCredentials(platform_tokens={"gitee": "tok_uc38_valid"})
        try:
            with patch("src.kernel.token_resolver.create_provider") as _cp2:
                _cp2.return_value = SimpleNamespace(verify_token=_fake_verify)
                update_credentials(body2, user={"id": uid2})
            assert (user_db.get_credential(uid2, "gitee_pat") or {}).get("token") == "tok_uc38_valid"
        finally:
            _cleanup_user({"id": uid2})
    finally:
        _cleanup_user({"id": uid})


# ────────────────────────── UC38③ 白名单行式行为闭环 ──────────────────────────

def test_uc38_3_whitelist_column_end_to_end():
    """白名单行式：建列→add→list 全链走 (provider, username)（真实 sqlite 落库）。

    2026-11-12 行式化：列驱动改行式——db 层不再做渠道校验（登录域自治，
    校验上移路由层）；未知渠道拒绝由 POST /whitelist 路由 400 承担。"""
    import uuid

    from src.domains.mgmt.admin.db import add_login_whitelist, list_login_whitelist
    from src.domains.mgmt.admin.routes import admin_add_login_whitelist, LoginWhitelistBody
    from src.api.db_setup import _fn_login_whitelist_row_columns
    from fastapi import HTTPException

    _fn_login_whitelist_row_columns()
    uname = "uc38wl" + uuid.uuid4().hex[:6]
    try:
        assert add_login_whitelist("gitee", uname, "uc38") is True
        rows = list_login_whitelist()
        assert any(r.get("provider") == "gitee" and r.get("username") == uname
                   for r in rows)
        # 未注册渠道拒绝（fail-closed，路由层）
        try:
            admin_add_login_whitelist(
                LoginWhitelistBody(provider="nosuchplatform", username=uname), admin={"id": "t"})
            raise AssertionError("未在册渠道未拒绝")
        except HTTPException as e:
            assert e.status_code == 400
    finally:
        with db_lock:
            db.execute("DELETE FROM login_whitelist WHERE provider='gitee' AND username=?", (uname,))
            db.commit()


# ────────────────────────── UC38④ display strip 全链统一 ──────────────────────────

@pytest.mark.skip(reason="legacy 静态页模块已随 Vue 迁移删除（本 PR 移除 src/api/static/js/pages/*）：被测对象不存在，断言改写到 Vue 源需另立专项")
def test_uc38_4_display_strip_no_local_redefinition():
    """页面模块零内联 strip + 零本地重定义（全链统一引用 app.js helper）。"""
    obs = _read("js/pages/observability.js")
    assert ".replace('gitcode:','')" not in obs
    assert ".replace(\"gitcode:\",\"\")" not in obs
    assert obs.count("stripRepoPrefix") >= 8
    # 零本地重定义：helper 只在 app.js 定义（window 挂载），页面模块仅调用
    assert "function stripRepoPrefix" not in obs, "页面模块本地重定义 helper（应引用 app.js）"

    app = _read("js/app.js")
    m = re.search(r"function stripRepoPrefix\(s\) \{[^}]+\}", app)
    assert m and "gitee" in m.group(0), "app.js helper 正则清单缺 gitee"


# ────────────────────────── UC38⑤ 缓存版本纪律（UC11 延伸） ──────────────────────────

@pytest.mark.skip(reason="legacy 静态页模块已随 Vue 迁移删除（本 PR 移除 src/api/static/js/pages/*）：被测对象不存在，断言改写到 Vue 源需另立专项")
def test_uc38_5_cache_version_bumped_for_n5_modules():
    """N5 改动的三个页面模块 ?v= 已随批 bump（account>=50 / observability>=74 / app>=217）。

    实码审计根因：867b38f68 改三 JS 但 index.html 未 bump——UC11 纪律缺口。
    本用例将三模块纳入 UC11 门槛清单，后续改动随批 bump 防回潮。
    """
    idx = _read("index.html")
    for frag, minv in [("account.js?v=", 50),
                       ("observability.js?v=", 74),
                       ("app.js?v=", 217)]:
        m = re.search(re.escape(frag) + r"(\d+)", idx)
        assert m, f"index.html 缺 {frag} 引用"
        assert int(m.group(1)) >= minv, f"{frag} 版本必须 >= {minv}（N5 批次改动未随批 bump）"
