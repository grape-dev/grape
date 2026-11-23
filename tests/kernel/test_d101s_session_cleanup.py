"""D101 方案三：session 死列清理 + 策略链第 1 级删除——行为锁。

方案稿 docs/weekly-09/d101-task-token-snapshot-retirement-design.md §二。

RED 语义（改前失败，改后转绿）：
- D101S① 策略链四改三：DevopsAgentSessionToken 不再在链中
- D101S② 类删除：DevopsAgentSessionToken 符号消亡（防复活守卫）
- D101S③ detect_and_mark_expired 退役（T8 b 案）：函数+error_code 消亡，
  消费方直调 RepoBot 级检测
- D101S④ session 列零写入：upsert 形参删除 + 解绑清理段删除
"""
import os

os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

import inspect


# ──────────────── D101S① 策略链四改三 ────────────────

def test_d101s_1_strategy_chain_three_levels():
    """策略链 = [显式 rc 行, RepoBot, GlobalBot, UserToken]（session 级已删除）。

    Issue #3713 在链首加了 `ExplicitRepoBotToken`（只读 rc 行、不做 path-B 守卫、
    不触碰 global bot）：本守卫的目标没变——**session 级不得复活**；
    变的是目标态形状，故此处显式列出四级并锁住顺序（链序是身份语义的一部分）。
    """
    from src.kernel.token_resolver import ExplicitRepoBotToken, GlobalBotToken, RepoBotToken, TokenResolver, UserToken
    from src.domains.mgmt.vcs import create_provider
    resolver = TokenResolver(create_provider("gitcode"))
    levels = [type(s).__name__ for s in resolver.strategies]
    assert levels == ["ExplicitRepoBotToken", "RepoBotToken", "GlobalBotToken", "UserToken"], \
        f"策略链不是目标态（显式 rc 行 + 三级）: {levels}"
    # 链序注释同步（架构常量注释更新）
    src = inspect.getsource(type(resolver))
    assert "1 session 专属" not in src, "链序注释仍声明四级（T1 未完成）"


# ──────────────── D101S② 类删除防复活 ────────────────

def test_d101s_2_class_removed():
    """DevopsAgentSessionToken 类符号消亡（含 _is_expired/mark_expired）。"""
    import src.kernel.token_resolver as tr
    assert not hasattr(tr, "DevopsAgentSessionToken"), \
        "DevopsAgentSessionToken 复活（T2 未删）"
    src = inspect.getsource(tr)
    assert "class DevopsAgentSessionToken" not in src


# ──────────────── D101S③ detect_and_mark_expired 退役（T8 b 案） ────────────────

def test_d101s_3_detect_mark_expired_retired():
    """detect_and_mark_expired 函数退役 + SESSION_TOKEN_EXPIRED error_code 消亡。

    消费方（vod_handler/handler_async）改直调 RepoBot 级检测——
    本用例锁消费方不再引用 token_service.detect_and_mark_expired。
    """
    import src.kernel.token_service as ts
    assert not hasattr(ts, "detect_and_mark_expired"), \
        "detect_and_mark_expired 未退役（T8 b 案未实施）"
    # error_code 消亡
    from src.domains.runtime.engine import service
    ssrc = inspect.getsource(service)
    assert "SESSION_TOKEN_EXPIRED" not in ssrc, "service.py:24 文案映射未删"
    # 消费方迁移（精确 import 引用检测——不含注释中的历史提法）
    import re as _re
    from src.domains.runtime.im import vod_handler_async as vod
    from src.domains.runtime.im import handler_async as ha
    for _name, _mod in (("vod_handler", vod), ("handler_async", ha)):
        _msrc = inspect.getsource(_mod)
        _imports = _re.findall(r"from src\.token_service import ([^\n]+)", _msrc)
        for _imp in _imports:
            assert "detect_and_mark_expired" not in _imp.replace(
                "detect_and_mark_repo_token_expired", ""), \
                f"{_name} 仍 import 已退役函数（消费方未迁移）: {_imp}"


# ──────────────── D101S④ session 列零写入 + 死代码清除 ────────────────

def test_d101s_4_session_upsert_param_removed():
    """upsert_agent_session 的 repo_gitcode_token 形参删除（调用方全传 None 已证死参数）。"""
    import inspect
    from src.api import session_store
    sig = inspect.signature(session_store.upsert_agent_session)
    assert "repo_gitcode_token" not in sig.parameters, \
        "upsert_agent_session 仍有 repo_gitcode_token 形参（T6 未删）"


def test_d101s_4b_unbind_cleanup_removed():
    """解绑清理段删除（db.py 清三列的 UPDATE 语句消亡——列已死无物可清）。"""
    import inspect
    from src.domains.mgmt.user import db
    src = inspect.getsource(db)
    assert "UPDATE devops_agent_session SET repo_gitcode_token=''" not in src, \
        "解绑清理段仍在（T5 未删）"


def test_d101s_4c_admin_panel_column_derivation_removed():
    """agents.py 面板 session_token_field 列派生段删除（T4）。"""
    import inspect
    from src.domains.mgmt.routes import agents
    src = inspect.getsource(agents)
    assert "session_token_field()" not in src, \
        "agents.py 仍按 session_token_field 派生查询列（T4 未删）"
