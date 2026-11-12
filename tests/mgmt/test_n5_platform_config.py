"""N5 配置化平台化收口守卫（OAuth/PAT 全平台配置化 + 白名单列驱动 + display strip）。

UC↔用例映射（requirements UC37）：
- UC37① gitee OAuth 渠道卡在册（providers 内置默认，whitelist_column 派生列）
- UC37② login_whitelist 动态建列（gitee_username 自动出现）
- UC37③ admin 白名单列驱动（add 接受 gitee / list 返回 gitee 列）
- UC37④ display strip 收口（observability 零内联 replace('gitcode:')，统一 stripRepoPrefix）
- UC37⑤ 前端 manual 绑定新平台走 platform_tokens 泛型（提交体形态守卫）
"""
import os
import re as _re

os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

from pathlib import Path

from src.kernel.session_manager import db, db_lock
from src.domains.mgmt.user import db as udb


def test_uc37_1_gitee_channel_card_registered():
    """gitee OAuth 渠道卡在册（providers 内置默认 + whitelist_column）。"""
    from src.domains.mgmt.auth.providers import list_providers, get_provider
    assert "gitee" in list_providers()
    cfg = get_provider("gitee") or {}
    assert cfg.get("whitelist_column") == "gitee_username"
    assert cfg.get("auth_url") == "https://gitee.com/oauth/authorize"
    assert cfg.get("token_url") == "https://gitee.com/oauth/token"


def test_uc37_2_login_whitelist_dynamic_column():
    """login_whitelist 动态建列：gitee_username 随渠道卡自动出现。"""
    from src.api.db_setup import _ensure_whitelist_columns
    _ensure_whitelist_columns()
    with db_lock:
        cols = {r[1] for r in db.execute("PRAGMA table_info(login_whitelist)").fetchall()}
    assert "gitee_username" in cols


def test_uc37_3_admin_whitelist_column_driven():
    """admin 白名单 add/list 列驱动（gitee 可加可查）。"""
    from src.domains.mgmt.admin.db import add_login_whitelist, list_login_whitelist
    uname = "n5_wl_" + uuid.uuid4().hex[:6]
    try:
        assert add_login_whitelist("gitee", uname, "n5") is True
        rows = list_login_whitelist()
        hit = [r for r in rows if r.get("gitee_username") == uname]
        assert hit, f"gitee 白名单行未返回: {rows[:2]}"
    finally:
        with db_lock:
            db.execute("DELETE FROM login_whitelist WHERE gitee_username=?", (uname,))
            db.commit()


def test_uc37_4_display_strip_unified():
    """display strip 收口：observability 零内联 replace('gitcode:')，统一 stripRepoPrefix。"""
    obs = (Path(__file__).resolve().parent.parent.parent
           / "src/api/static/js/pages/observability.js").read_text(encoding="utf-8")
    assert ".replace('gitcode:','')" not in obs, "内联前缀剥离回潮"
    assert obs.count("stripRepoPrefix") >= 8
    # app.js 的 helper 正则清单含 gitee
    app = (Path(__file__).resolve().parent.parent.parent
           / "src/api/static/js/app.js").read_text(encoding="utf-8")
    assert "gitee" in _re.search(r"function stripRepoPrefix\(s\) \{[^}]+\}", app).group(0)


def test_uc37_5_manual_bind_generic_submit():
    """前端 manual 绑定：存量双平台走专用字段，新平台走 platform_tokens 泛型。"""
    acct = (Path(__file__).resolve().parent.parent.parent
            / "src/api/static/js/pages/account.js").read_text(encoding="utf-8")
    assert "_LEGACY_FIELDS" in acct, "存量专用字段映射缺失"
    assert "body.platform_tokens" in acct, "新平台泛型提交缺失"
    # 提交体形态：新平台不写死专用字段
    m = _re.search(r"var field = provider === 'github' \? 'github_token' : 'gitcode_token';", acct)
    assert not m, "manual 绑定仍是写死双平台字段（N5-2 未生效）"


import uuid  # noqa: E402  (供 test_uc37_3 使用)
