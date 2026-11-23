"""Issue #3713 任务面：建树失败时把凭据 stage 带进 task errors。

原症状：token 为空 → mirror refs=0 → `git worktree add` 失败 → 平台只看到
「git dir not found」→ `GRAPE_GIT_WRITABLE=false` → agent 自述「git 元数据只读」
→ 零交付仍 completed（与 #3711 同一条收口通道）。

本文件锁三件事：
  1. `repogit.worktree_credential_error()` 用**与换券面同一个选择器**归因（过期降级不判失败），
     错误串带 `credential_<stage>`，且不再出现「git 元数据只读」措辞；
  2. token 可用时不归因（建树失败但凭据没问题 → 保持原告警路径，语义不变）；
  3. `agent.credential_block_result()` 是纯构造（便于单测），执行流程引用它。
"""
from __future__ import annotations

import pytest

import inspect

from issue_3713_fakes import CredWorld, install, restore_ports, snapshot_ports
from src.domains.runtime.engine import agent as agent_mod
from src.domains.runtime.engine import repogit

RK = "gitee:acme/demo"



@pytest.fixture(autouse=True)
def _ports_baseline():
    """端口是进程级绑定：每个用例结束还原到进入时的快照。

    不能用「全清」：装配层绑的 `vcs_cards` 会被抹掉，同进程后续用例造不出 provider。
    """
    snapshot = snapshot_ports()
    yield
    restore_ports(snapshot)

def test_expired_rc_row_degrades_instead_of_failing(monkeypatch):
    """C3：过期 rc 行 → 跳过本级，落到归属者；建树不被判成凭据失败。"""
    install(monkeypatch, CredWorld(rc_value="rc-bot-token", bot_status="expired",
                                   host_user_id="15962",
                                   user_tokens={"15962": "owner-pat"}))
    assert repogit.worktree_credential_error(RK) == ""


def test_expired_rc_row_without_fallback_is_reported_not_configured(monkeypatch):
    """C3 的另一半：过期 + 下一档也空 → 仍如实归因（不是静默失败）。"""
    install(monkeypatch, CredWorld(rc_value="rc-bot-token", bot_status="expired",
                                   host_user_id="15962", user_tokens={}))
    err = repogit.worktree_credential_error(RK)
    assert err.startswith("credential_not_configured"), err


def test_empty_rc_row_is_not_attributed_to_credentials(monkeypatch):
    """空值行 = 无行（840431a8b）：落到归属者后凭据可用 → 不得归因到凭据。"""
    install(monkeypatch, CredWorld(rc_value="", host_user_id="15962",
                                   user_tokens={"15962": "owner-pat"}))
    assert repogit.worktree_credential_error(RK) == ""


def test_empty_repository_is_reported_as_empty_repo(monkeypatch):
    """空仓（远端 0 分支）必须单独分类：凭据再好也建不出树，不能报成凭据问题。"""
    install(monkeypatch, CredWorld(rc_value=None, host_user_id="15962"))
    monkeypatch.setattr(repogit, "repo_base_has_refs", lambda rk: False)
    err = repogit.worktree_credential_error(RK)
    assert err.startswith("empty_repo"), err
    assert "凭据" in err  # 明确澄清"这不是凭据问题"


def test_nonempty_repository_without_credential_is_still_attributed(monkeypatch):
    """有分支 + 无凭据 → 仍按凭据归因（空仓判定不得吞掉真正的凭据问题）。"""
    install(monkeypatch, CredWorld(rc_value=None, host_user_id="15962"))
    monkeypatch.setattr(repogit, "repo_base_has_refs", lambda rk: True)
    assert repogit.worktree_credential_error(RK).startswith("credential_not_configured")


def test_undecryptable_rc_degrades_to_the_next_level(monkeypatch):
    """2026-11-22 二次收敛：unknown kid / 解不开 = 不可用 → 跳过本级。

    这里没有下一档可用凭据，所以最终如实报 not_configured；
    关键在于它**不再**把“解不开”升级成独立的硬失败分类（gitee 毒行事故）。
    """
    install(monkeypatch, CredWorld(rc_undecryptable=True, host_user_id="15962"))
    err = repogit.worktree_credential_error(RK)
    assert err.startswith("credential_not_configured"), err
    assert not err.startswith("credential_undecryptable")


def test_no_credential_at_all_reports_not_configured(monkeypatch):
    install(monkeypatch, CredWorld(rc_value=None, host_user_id="15962"))
    assert repogit.worktree_credential_error(RK).startswith("credential_not_configured")


def test_usable_token_is_not_blamed_for_worktree_failure(monkeypatch):
    install(monkeypatch, CredWorld(rc_value="rc-bot-token", host_user_id="15962"))
    assert repogit.worktree_credential_error(RK) == "", (
        "凭据可用 → 建树失败原因在 git 侧，不得凭空归因到凭据")


def test_empty_repo_key_is_silent(monkeypatch):
    install(monkeypatch, CredWorld(rc_value="rc-bot-token"))
    assert repogit.worktree_credential_error("") == ""


def test_credential_block_result_shape(monkeypatch):
    """阻断结果是纯构造：{"_error": "credential_<stage>: ...", "_started": False}。"""
    install(monkeypatch, CredWorld(rc_undecryptable=True, host_user_id="15962"))
    blk = agent_mod.credential_block_result("t_1", RK)
    assert blk is not None
    assert blk["_started"] is False
    assert blk["_error"].startswith("credential_not_configured")


def test_credential_block_result_none_when_usable(monkeypatch):
    install(monkeypatch, CredWorld(rc_value="rc-bot-token", host_user_id="15962"))
    assert agent_mod.credential_block_result("t_1", RK) is None


def test_execute_flow_wires_the_credential_block():
    """流程接线守卫：建树返回空且凭据不可用时必须阻断（源码级锁，防被删）。"""
    src = inspect.getsource(agent_mod._execute_task_impl)
    assert "ensure_issue_worktree(" in src
    assert "credential_block_result(" in src, (
        "建树失败后必须按凭据归因阻断，否则又回到「只读降级交付」")
    assert "_started" in src