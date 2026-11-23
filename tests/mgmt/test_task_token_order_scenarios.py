"""取令牌顺序用例 S1–S7（Issue #21 R1 顺序 + 评审 A1/A2）。

契约（顺序，优先级从高到低）：
  ① task.repo_gitcode_token（历史行值）
  ② **显式配置的仓库 bot 行（链首 rc_bot）** ← A1 修的就是这一条：必须排在派发人令牌之前
  ③ 派发身份个人令牌
  ④ 原链（github: get_github_token / 其它 get_repo_token_with_refresh）

A2：插件建表迁移折入分类种子后，种子失败必须抛出（不盖章、下次重试）。
"""
from __future__ import annotations

import pytest

import src.domains.mgmt.user.db as user_db
import src.domains.mgmt.user.task_token_identity as tti
import src.kernel.token_resolver as tr
from src.api import db_setup


@pytest.fixture()
def wiring(monkeypatch):
    """把 rc 行、派发身份、原链三个来源都换成可观测的桩。"""
    state = {"rc": ("", ""), "user": ("", ""), "chain": "chain-token", "calls": []}

    class _Head:
        def __init__(self, provider):
            pass

        def resolve(self, repo_key):
            state["calls"].append("rc")
            return state["rc"]

    monkeypatch.setattr(tr, "ExplicitRepoBotToken", _Head)
    monkeypatch.setattr(tr, "create_provider_from_repo_key", lambda rk: object())
    monkeypatch.setattr(tti, "user_identity_token",
                        lambda task, repo_key: (state["calls"].append("user") or state["user"]))
    monkeypatch.setattr(user_db, "get_github_token", lambda repo_key="": state["chain"])
    monkeypatch.setattr(user_db, "get_repo_token_with_refresh", lambda repo_key: state["chain"])
    return state


RK = "github:yangaiwu/hello-blog"


def test_s1_explicit_repo_bot_wins_over_dispatch_identity(wiring):
    """S1（A1 核心）：rc 行可用 → 直接用它，不再看派发人令牌。"""
    wiring["rc"] = ("rc-bot-token", "")
    wiring["user"] = ("person-token", "account")
    tok = user_db.get_task_token({"repo_key": RK, "user_id": "2"})
    assert tok == "rc-bot-token"
    assert wiring["calls"] == ["rc"], "rc 可用时不得再走派发身份"


def test_s2_unusable_rc_row_falls_to_dispatch_identity(wiring):
    """S2：rc 行过期 → 继续看派发人令牌（不是立刻失败，也不下沉到全局 bot）。"""
    wiring["rc"] = ("", "expired")
    wiring["user"] = ("person-token", "account")
    tok = user_db.get_task_token({"repo_key": RK, "user_id": "2"})
    assert tok == "person-token"
    assert wiring["calls"] == ["rc", "user"]


def test_s3_no_rc_row_uses_dispatch_identity(wiring):
    """S3：没有 rc 行 → 派发人令牌。"""
    wiring["rc"] = ("", "")
    wiring["user"] = ("person-token", "account")
    assert user_db.get_task_token({"repo_key": RK, "user_id": "2"}) == "person-token"


def test_s4_no_identity_token_falls_back_to_chain(wiring):
    """S4：rc 不可用 + 派发人无令牌 → 原链（repo bot/平台 bot/归属者）。"""
    wiring["rc"] = ("", "expired")
    wiring["user"] = ("", "account")
    assert user_db.get_task_token({"repo_key": RK, "user_id": "2"}) == "chain-token"
    assert wiring["calls"] == ["rc", "user"]


def test_s5_task_row_token_skips_everything(wiring):
    """① 任务行历史 token 仍然最优先（既有语义不变）。"""
    tok = user_db.get_task_token({"repo_key": RK, "repo_gitcode_token": "row-token"})
    assert tok == "row-token"
    assert wiring["calls"] == []


def test_s6_undecryptable_rc_row_also_falls_through(wiring):
    """S6：rc 行解不开 → 与过期同路径（继续看派发身份）。"""
    wiring["rc"] = ("", "undecryptable")
    wiring["user"] = ("person-token", "account")
    assert user_db.get_task_token({"repo_key": RK, "user_id": "2"}) == "person-token"


def test_s7_plugin_seed_failure_propagates(monkeypatch):
    """S7（A2）：建表迁移折入的种子失败必须抛出，不能被吞掉后照样盖章。"""
    monkeypatch.setattr(db_setup, "_fn_create_plugin_tables_b1", lambda: None)

    def _boom():
        raise RuntimeError("seed exploded")

    monkeypatch.setattr(db_setup, "_fn_plugin_category_seed_b1", _boom)
    with pytest.raises(RuntimeError):
        db_setup._fn_plugin_tables_and_seed_b1()