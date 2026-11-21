"""Issue #3713 守卫用例 1-6：换券链与建树链对 rc bot 行必须是同一口径。

背景（Issue 正文 + 评审 #202773482/#202773660，验收口径以 #202773660 为准）：
  · 链① 换券 `_repo_platform_token` 自己直读 `runtime_credentials(repo_key,'bot')`，
    行可用直取、空/解不开/过期 424；
  · 链② 建树 `get_task_token → get_repo_token_with_refresh → TokenResolver`，
    而 `RepoBotToken._fetch` 的 path-B 守卫**在 rc 读取之前** → path-B 仓根本读不到 rc 行。
同一份数据两个答案 → 私有 path-B 仓换券 200、建树空 token、refs=0、零交付仍 completed。

本文件把六条守卫都写成**跨入口断言**（同一份 CredWorld，两个入口各取一次）：
  1. 同仓两入口 token 相同；
  2. rc 行空值 → 按「无行」处理（840431a8b），两入口同一下一档、答案一致；
  3. path-B + rc 有值 → 两入口都命中 `rc_bot`；
  4. path-B + 无 rc 行 → 两入口都回落归属者，**绝不落全局 bot**（正向断言等于谁）；
  5. rc 过期 → 跳过本级、两入口落到同一档（不硬失败、不返回过期旧值）；
  6. 幂等 → 连续两次 token/source 均不变。

判据：这些用例在旧实现上必须红（打在分叉点上），新实现全绿。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from issue_3713_fakes import CredWorld, install, restore_ports, snapshot_ports
from src.domains.mgmt.routes.repos import _repo_platform_token
from src.domains.mgmt.user.db import get_task_token, get_repo_bot_token_no_global
from src.domains.runtime.engine import repogit
from src.kernel.token_resolver import ExplicitRepoBotToken, resolve_exec_token

SESSION_USER = {"auth_type": "session_token", "id": "0", "username": "",
                "agent_type": "coding"}
RK = "gitee:acme/demo"
OWNER, REPO = "acme", "demo"
RC_BOT = "rc-bot-token-0123456789abcdef01234567"
OWNER_PAT = "owner-pat-0123456789abcdef01234567"
GLOBAL_BOT = "global-bot-token-should-never-leak"


def _exchange():
    """入口 A：换券路由。"""
    return _repo_platform_token(OWNER, REPO, dict(SESSION_USER), "gitee")


def _worktree_chain():
    """入口 B：建树链取 token（worker 建树前调的就是它）。"""
    return get_task_token({"repo_key": RK})


def _exchange_stage():
    """换券面的 stage 分类（424 detail）。"""
    with pytest.raises(HTTPException) as ei:
        _exchange()
    assert ei.value.status_code == 424, ei.value.detail
    return ei.value.detail.get("stage")


# ── 守卫 1：同仓两入口 token 相同（path-A） ────────────────────────────────

@pytest.fixture(autouse=True)
def _ports_baseline():
    """端口是进程级绑定：每个用例结束还原到进入时的快照。

    不能用「全清」：装配层绑的 `vcs_cards` 会被抹掉，同进程后续用例造不出 provider。
    """
    snapshot = snapshot_ports()
    yield
    restore_ports(snapshot)

def test_guard1_same_token_both_entries_path_a(monkeypatch):
    install(monkeypatch, CredWorld(rc_value=RC_BOT, global_bot=GLOBAL_BOT))

    assert _exchange() == RC_BOT
    assert _worktree_chain() == RC_BOT, "建树链看不见 rc 行 → 同一份数据两个答案"


# ── 守卫 2：rc 行存在但值为空 = 没有这行（840431a8b），两入口同一下一档 ─────
def test_guard2_empty_rc_row_is_treated_as_missing_both_entries(monkeypatch):
    """空值行不得判成显式失败：写侧已改「空 → 删行」，存量空行必须交回原链。

    否则 gitee 那 6 个仓（历史空占位行）会 424 / credential_empty —— 
    平台 bot 有效也救不回来，等于把 840431a8b 的修复打回。
    """
    install(monkeypatch, CredWorld(rc_value="", global_bot=GLOBAL_BOT))

    assert _exchange() == GLOBAL_BOT
    assert _worktree_chain() == GLOBAL_BOT
    sel = resolve_exec_token(RK)
    assert sel.stage == "" and sel.token == GLOBAL_BOT and sel.level == "global_bot"
    assert repogit.worktree_credential_error(RK) == ""


def test_guard2b_empty_rc_row_on_path_b_falls_back_to_owner(monkeypatch):
    """空值行 + path-B：回落归属者 PAT，绝不落全局 bot。"""
    install(monkeypatch, CredWorld(rc_value="", host_user_id="15962",
                                   user_tokens={"15962": OWNER_PAT},
                                   global_bot=GLOBAL_BOT))
    assert _exchange() == OWNER_PAT
    assert _worktree_chain() == OWNER_PAT
    assert resolve_exec_token(RK).level == "user"


# ── 守卫 3：path-B + rc 行有值 → 两入口都用 rc_bot ────────────────────────
def test_guard3_path_b_with_rc_uses_rc_bot_both_entries(monkeypatch):
    install(monkeypatch, CredWorld(rc_value=RC_BOT, host_user_id="15962",
                                   user_tokens={"15962": OWNER_PAT},
                                   global_bot=GLOBAL_BOT))

    assert _exchange() == RC_BOT
    assert _worktree_chain() == RC_BOT, (
        "path-B 守卫在 rc 读取之前 → 归属于宿主用户的私仓仍读不到显式 bot 行")
    sel = resolve_exec_token(RK)
    assert sel.token == RC_BOT and sel.level == "rc_bot" and sel.stage == ""


# ── 守卫 4：path-B + 无 rc 行 → 回落归属者，绝不落全局 bot ─────────────────
def test_guard4_path_b_without_rc_falls_back_to_owner_never_global_bot(monkeypatch):
    install(monkeypatch, CredWorld(rc_value=None, host_user_id="15962",
                                   user_tokens={"15962": OWNER_PAT},
                                   global_bot=GLOBAL_BOT))

    assert _exchange() == OWNER_PAT
    assert _worktree_chain() == OWNER_PAT
    assert _exchange() != GLOBAL_BOT, "path-B 不得因「无 rc 行」拿到全局 bot 身份"
    sel = resolve_exec_token(RK)
    assert sel.level == "user" and sel.token == OWNER_PAT


# ── 守卫 5：rc 过期 → 两入口都短路失败，不得一边返回旧值 ───────────────────
def test_guard5_expired_rc_row_skips_to_next_level_both_entries(monkeypatch):
    """C3（2026-11-22 回滚）：过期 ≠ 配置坏了 —— 跳过本级、落到下一档，两入口答案一致。

    基线语义就是「跳过这一级」（RepoBotToken 遇 expired return ""，继续 GlobalBot/归属者）；
    PR 1953 曾改成硬失败，一旦标记滞后就把本来能跑的任务判 failed，故恢复。
    """
    world = CredWorld(rc_value=RC_BOT, bot_status="expired", host_user_id="15962",
                      user_tokens={"15962": OWNER_PAT}, global_bot=GLOBAL_BOT)
    install(monkeypatch, world)

    assert _exchange() == OWNER_PAT
    assert _worktree_chain() == OWNER_PAT
    sel = resolve_exec_token(RK)
    assert sel.stage == "" and sel.level == "user"
    assert repogit.worktree_credential_error(RK) == ""


# ── 守卫 6：幂等 —— 连续两次 token/source 均不变 ──────────────────────────
def test_guard6_idempotent_both_entries(monkeypatch):
    install(monkeypatch, CredWorld(rc_value=RC_BOT, host_user_id="15962",
                                   user_tokens={"15962": OWNER_PAT},
                                   global_bot=GLOBAL_BOT))

    first = (_exchange(), _worktree_chain(), resolve_exec_token(RK))
    second = (_exchange(), _worktree_chain(), resolve_exec_token(RK))

    assert first[0] == second[0] == RC_BOT
    assert first[1] == second[1] == RC_BOT
    assert (first[2].token, first[2].level) == (second[2].token, second[2].level)


# ── 实现红线：不得用 bypass_path_b=True 落地「统一」 ───────────────────────
def test_explicit_level_exposes_no_bypass_switch():
    """链首新增的一级不得带 bypass 开关——它会同时解锁 global bot（身份越界）。"""
    import inspect

    params = inspect.signature(ExplicitRepoBotToken.__init__).parameters
    assert "bypass_path_b" not in params, (
        "ExplicitRepoBotToken 只读 rc 行，不得提供 bypass 参数")
    source = inspect.getsource(ExplicitRepoBotToken)
    assert "bypass_path_b=" not in source and "bypass_path_b =" not in source, (
        "该类不得通过 bypass 落地（会把 global bot 一并解锁）")


def test_identity_face_unchanged_with_explicit_row(monkeypatch):
    """回帖身份路径（get_bot_identity_token，bypass_path_b=True）回归不受影响。"""
    install(monkeypatch, CredWorld(rc_value=RC_BOT, host_user_id="15962",
                                   user_tokens={"15962": OWNER_PAT},
                                   global_bot=GLOBAL_BOT))
    from src.kernel.token_resolver import get_bot_identity_token

    assert get_bot_identity_token(RK) == RC_BOT


def test_no_global_face_drops_expired_explicit_value(monkeypatch):
    """守卫 7（A2A/tasks 鉴权面）：rc 过期不得把旧值当可用 token 发出去。"""
    install(monkeypatch, CredWorld(rc_value=RC_BOT, bot_status="expired",
                                   host_user_id="15962",
                                   user_tokens={"15962": OWNER_PAT},
                                   global_bot=GLOBAL_BOT))

    got = get_repo_bot_token_no_global(RK)
    assert got != RC_BOT, "read_stored_token 不查过期 → 旧值必须在这里被挡住"
    assert got == OWNER_PAT