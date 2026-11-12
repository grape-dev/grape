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

os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

import re
from pathlib import Path

from src.kernel.session_manager import db, db_lock

_BASE = Path(__file__).resolve().parent.parent.parent
_STATIC = _BASE / "src" / "api" / "static"


def _read(rel: str) -> str:
    return (_STATIC / rel).read_text(encoding="utf-8")


# ────────────────────────── UC38① 双卡链接（两轴同现） ──────────────────────────

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


def test_uc38_2_platform_tokens_write_path_dual_write():
    """合法新平台 PAT：verify 后双写形态（user_credentials <p>_pat + external_identities）。

    行为实弹：mock verify_token 返回平台用户信息，走 update_credentials 真函数，
    断言双写落库 + 409 冲突拒绝（他人已绑）。
    用户构造走 grape_accounts 权威路径（user_accounts 写入冻结，对齐 test_user_db）。
    """
    import uuid
    from types import SimpleNamespace
    from unittest.mock import patch

    from fastapi import HTTPException
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

        # 双写断言①：user_credentials gitee_pat
        cred = user_db.get_credential(uid, "gitee_pat") or {}
        assert cred.get("token") == "tok_uc38_valid"
        assert cred.get("login") == "uc38login"
        # 双写断言②：external_identities 归属源
        ident = grape_account_db.get_external_identity("gitee", "uc38ext1")
        assert ident and ident["grape_user_id"] == uid

        # 409 冲突：另一 grape 账号绑同一 gitee 身份 → 拒绝
        uid2 = _mk_user("uc38b")["id"]
        body2 = UpdateCredentials(platform_tokens={"gitee": "tok_uc38_valid"})
        try:
            with patch("src.kernel.token_resolver.create_provider") as _cp2:
                _cp2.return_value = SimpleNamespace(verify_token=_fake_verify)
                update_credentials(body2, user={"id": uid2})
            raise AssertionError("他人已绑同一平台身份应 409 拒绝")
        except HTTPException as e:
            assert e.status_code == 409
        finally:
            grape_account_db.delete_external_identity(uid2, "gitee")
            _cleanup_user({"id": uid2})
    finally:
        grape_account_db.delete_external_identity(uid, "gitee")
        _cleanup_user({"id": uid})


# ────────────────────────── UC38③ 白名单列驱动行为闭环 ──────────────────────────

def test_uc38_3_whitelist_column_end_to_end():
    """白名单列驱动：建列→add→list 全链走配置派生（真实 sqlite 落库）。"""
    import uuid

    from src.domains.mgmt.admin.db import add_login_whitelist, list_login_whitelist
    from src.api.db_setup import _ensure_whitelist_columns

    _ensure_whitelist_columns()
    uname = "uc38wl" + uuid.uuid4().hex[:6]
    try:
        assert add_login_whitelist("gitee", uname, "uc38") is True
        rows = list_login_whitelist()
        assert any(r.get("gitee_username") == uname for r in rows)
        # 未注册渠道拒绝（fail-closed）
        assert add_login_whitelist("nosuchplatform", uname, "uc38") is False
    finally:
        with db_lock:
            db.execute("DELETE FROM login_whitelist WHERE gitee_username=?", (uname,))
            db.commit()


# ────────────────────────── UC38④ display strip 全链统一 ──────────────────────────

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
