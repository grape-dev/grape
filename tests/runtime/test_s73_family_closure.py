"""S73 用例（D48，先测后改）：同型家族扫描收口——S70/S64 缺陷形状的剩余实例。

家族 B：int() 强转 issue 号——事件管道未设防（gitee 字符串 ident "IKSZ3I"
回流即 ValueError；云层放行后必炸点）。
家族 A：盖章迁移罩住幂等派生 DDL——（2026-11-12 行式化后口径变更：
动态按卡建列机制退役，白名单行走注册式迁移 202611150001 的
provider/username 两列 + gitee 渠道行查即生效，无需 gitee_username 列）。
家族 C：get_user_token_with_refresh 残留调用点（repo 上下文在作用域
却折叠 gitcode）。

守卫用例沿用 S5 结构守卫先例（grep 形态），行为用例锁 DDL 补列。
"""
import json
import re

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache
from src.domains.mgmt.auth.providers import invalidate_provider_cache


GITEE_CHANNEL = {
    "gitee": {
        "name": "Gitee", "type": "oauth2",
        "auth_url": "https://gitee.com/oauth/authorize",
        "token_url": "https://gitee.com/oauth/token",
        "user_url": "https://gitee.com/api/v5/user",
        "scope": "user_info", "id_field": "id", "login_field": "login",
        "name_field": "name", "pkce_enabled": False, "brand_color": "#c71d23",
        "accept_header": "application/json",
    }
}


@pytest.fixture()
def gitee_channel():
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('oauth_providers_config', ?)",
            (json.dumps(GITEE_CHANNEL),))
        db.commit()
    invalidate_provider_cache()
    invalidate_platform_card_cache()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='oauth_providers_config'")
        db.commit()
    invalidate_provider_cache()


def _drop_whitelist_col(col):
    with db_lock:
        try:
            db.cursor().execute(f"ALTER TABLE login_whitelist DROP COLUMN {col}")
        except Exception:
            pass
        db.commit()


# ------------------------------------------------ 家族 A：白名单行式列每启动补齐

def test_s73_whitelist_column_created_on_every_boot(gitee_channel):
    """行式化（2026-11-12）：注册式迁移补 provider/username 两列幂等；
    gitee 渠道行查白名单零列依赖（卡无需声明任何白名单元数据）。"""
    _drop_whitelist_col("gitee_username")
    from src.api.db_setup import init_database
    init_database()
    with db_lock:
        cols = [r[1] for r in db.cursor().execute("PRAGMA table_info(login_whitelist)")]
    # 行式两列在（注册式迁移幂等补齐）
    assert {"provider", "username"} <= set(cols)
    # gitee 渠道白名单按行查——无需 gitee_username 列即生效
    from src.domains.mgmt.admin.db import add_login_whitelist
    from src.kernel.config import is_user_allowed, invalidate_login_whitelist_cache
    from src.domains.mgmt.user.db import set_meta_value
    _u = "s73wl_user"
    with db_lock:
        db.execute("DELETE FROM login_whitelist WHERE username=?", (_u,))
        db.commit()
    assert add_login_whitelist("gitee", _u) is True
    set_meta_value("login_whitelist_enabled", "true")
    invalidate_login_whitelist_cache()
    try:
        assert is_user_allowed("gitee", _u) is True
        assert is_user_allowed("gitcode", _u) is False
    finally:
        set_meta_value("login_whitelist_enabled", "false")
        invalidate_login_whitelist_cache()
        with db_lock:
            db.execute("DELETE FROM login_whitelist WHERE username=?", (_u,))
            db.commit()


# ------------------------------------------------ 家族 B：事件管道 ident 守卫

def test_s73_pipeline_no_bare_int_ident_cast():
    """event_pipeline/issues_center 不得再出现裸 int(_issue_num)/int(issue_number)
    强转（gitee 字符串 ident 崩）——统一走 _native_issue_number。"""
    offenders = []
    for path, patterns in (
        ("src/api/event_pipeline.py", [r"int\(_issue_num\)", r"int\(issue_number\)"]),
        ("src/api/issues_center.py", [r"int\(issue_number\)"]),
    ):
        src = open(path, encoding="utf-8").read()
        for pat in patterns:
            for m in re.finditer(pat, src):
                offenders.append(f"{path}: {m.group(0)}")
    assert not offenders, f"bare int() ident casts remain: {offenders}"


# ------------------------------------------------ 家族 C：token 派生残留点守卫

def test_s73_repo_context_token_sites_use_provider_derivation():
    """repo 上下文在作用域的取 token 点必须走 get_user_token_for_repo
    （按 repo_key 前缀派生 provider），不得折叠 get_user_token_with_refresh。"""
    files = [
        "src/domains/runtime/routes/issues.py",
        "src/domains/runtime/engine/agent.py",
        "src/domains/runtime/agents/executor.py",
    ]
    for f in files:
        src = open(f, encoding="utf-8").read()
        assert "get_user_token_with_refresh" not in src, (
            f"{f} 仍含 get_user_token_with_refresh（repo 上下文折叠 gitcode）")
        assert "get_user_token_for_repo" in src, (
            f"{f} 未接 get_user_token_for_repo（provider 派生入口）")
