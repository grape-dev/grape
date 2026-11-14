"""D109 用户↔仓库平台关系表：纯关系语义 + link manual 优先 + 绑定路径写关系。

对应设计分片 d109 §15 UC-D109-28/29/33/34；需求 FR-29 N3/N5/N6。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from src.api.platform_object_ddl import PLATFORM_OBJECT_DDL
from src.kernel.persistence.repositories_platform_object import (
    ChannelRepoLinkRepo,
    UserRepoPlatformLinkRepo,
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    raw = engine.raw_connection()
    raw.executescript(PLATFORM_OBJECT_DDL)
    raw.commit()
    s = Session(engine)
    s.execute(text(
        "INSERT INTO grape_t_repo_platforms (id,provider,name,enabled) VALUES "
        "('p1','gitee','Gitee',1),('p2','gitcode','GitCode',1),('p3','codearts','CodeArts',1)"
    ))
    s.execute(text(
        "INSERT INTO grape_t_channel_repo_link (id,oauth_provider,repo_provider,source) VALUES "
        "('l1','gitee','gitee','derived'),"
        "('l2','gitcode','gitcode','derived')"
    ))
    s.commit()
    yield s
    s.close()
    engine.dispose()


def test_relation_table_has_no_state_columns(session):
    """模型硬约束：关系表只有关系与时间（无 via_bits / status / 来源位）。"""
    cols = {r[1] for r in session.execute(text("PRAGMA table_info(grape_t_user_repo_platform_link)"))}
    assert {"user_id", "repo_provider", "linked_at", "revoked_at", "revoked_reason"} <= cols
    assert "via_bits" not in cols and "status" not in cols


def test_link_expands_targets_by_channel_repo_link(session, monkeypatch):
    """UC-28：登录/绑定按 link 展开（gitee→gitee 一行；纯登录渠道不建幽灵行）。"""
    from src.domains.platform import hooks

    # 绕过 uow/engine，直接在同一 session 上执行钩子内部逻辑
    monkeypatch.setattr("src.kernel.persistence.uow.uow", lambda: _Passthrough(session))
    hooks.on_oauth_login("u1", "gitee")
    assert UserRepoPlatformLinkRepo.get(session, "u1", "gitee") is not None
    hooks.on_oauth_login("u1", "huawei")  # 无 link 且非平台 → 不建行
    assert UserRepoPlatformLinkRepo.get(session, "u1", "huawei") is None


def test_pat_saved_links_without_channel(session, monkeypatch):
    """UC-29：只配 PAT（从未登录）也建关系行。"""
    from src.domains.platform import hooks

    monkeypatch.setattr("src.kernel.persistence.uow.uow", lambda: _Passthrough(session))
    hooks.on_pat_saved("u9", "gitcode")
    row = UserRepoPlatformLinkRepo.get(session, "u9", "gitcode")
    assert row is not None and row.revoked_at == ""


def test_platform_disabled_writes_reason(session, monkeypatch):
    """UC-34：用户禁用 → revoked(disabled_by_user)，行保留。"""
    from src.domains.platform import hooks

    monkeypatch.setattr("src.kernel.persistence.uow.uow", lambda: _Passthrough(session))
    UserRepoPlatformLinkRepo.link(session, "u1", "gitee")
    hooks.on_platform_disabled("u1", "gitee")
    row = UserRepoPlatformLinkRepo.get(session, "u1", "gitee")
    assert row.revoked_at and row.revoked_reason == "disabled_by_user"


def test_link_merge_keeps_manual_rows_and_cleans_config_rows(session):
    """UC-33：导入合并 —— manual 行保留且优先；config/derived 行按配置全量替换。"""
    # 现有一条来自配置的 gitcode→gitcode，一条 manual 的 gitee→codearts
    ChannelRepoLinkRepo.merge_for_oauth(session, "gitee", [
        {"repo_provider": "gitee", "source": "config"},
    ], "h1")
    session.execute(text(
        "INSERT INTO grape_t_channel_repo_link (id,oauth_provider,repo_provider,source) "
        "VALUES ('m1','gitee','codearts','manual')"
    ))
    session.commit()

    # 再导入一次：配置只声明 gitee（codearts 不在配置里）→ manual 行必须保留
    ChannelRepoLinkRepo.merge_for_oauth(session, "gitee", [
        {"repo_provider": "gitee", "source": "config"},
    ], "h2")
    rows = {r.repo_provider: r.source for r in ChannelRepoLinkRepo.list_by_oauth(session, "gitee")}
    assert rows.get("codearts") == "manual", "manual 行被配置覆盖/删除"
    assert rows.get("gitee") == "config"

    # 配置改成只声明 codearts（与 manual 同 pair）→ 不得产生重复行
    n = ChannelRepoLinkRepo.merge_for_oauth(session, "gitee", [
        {"repo_provider": "codearts", "source": "config"},
    ], "h3")
    rows = [r for r in ChannelRepoLinkRepo.list_by_oauth(session, "gitee")]
    assert n == 0 and len(rows) == 1 and rows[0].source == "manual"


def test_bind_path_writes_relation_hook():
    """UC-28 源码守卫：绑定回调必须调 _hook_platform_link（此前只在登录路径写）。"""
    src = Path(__file__).resolve().parents[2] / "src" / "domains" / "mgmt" / "routes" / "grape_auth.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_handle_oauth_bind")
    calls = {c.func.id for c in ast.walk(fn)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert "_hook_platform_link" in calls, "绑定路径未写关系（此前只在登录路径写）"


class _Passthrough:
    """把 uow() 换成"不提交的直通 session"（钩子内部 with uow() as s: 用）。"""

    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, *a):
        self.session.flush()
        return False

def test_link_pair_is_the_only_criterion(session, monkeypatch):
    """配了配对就建行：relation 档位 / drivers 都不再参与判定。

    channel-repo-links.json 里写了一条 (c → p) 就是"c 的令牌可驱动 p"（2026-11-14 用户裁定
    「只要配置关系即可」）。因此：①关系表已无 relation 列（档位退役），建行只看配对；
    ②目标平台 drivers 没声明 oauth_identity 也不影响建行；③跨名同样成立。
    """
    from src.domains.platform import hooks

    monkeypatch.setattr("src.kernel.persistence.uow.uow", lambda: _Passthrough(session))
    session.execute(text(
        "INSERT INTO grape_t_channel_repo_link (id,oauth_provider,repo_provider,source) VALUES "
        "('x1','huawei','codearts','config'),"
        "('x2','gitlab','gitee','config'),"
        "('x3','github','codearts','config')"
    ))
    session.commit()
    hooks.on_oauth_login("u2", "huawei")
    assert UserRepoPlatformLinkRepo.get(session, "u2", "codearts") is not None, "配了配对就建行（不看 relation 档位）"
    hooks.on_oauth_login("u3", "gitlab")
    assert UserRepoPlatformLinkRepo.get(session, "u3", "gitee") is not None, "跨名配对同样建行"
    hooks.on_oauth_login("u5", "github")
    assert UserRepoPlatformLinkRepo.get(session, "u5", "codearts") is not None, "建行不看 drivers 声明"


def test_self_domain_is_implicit_but_cross_name_needs_a_pair(session, monkeypatch):
    """同域隐式（渠道自己即平台，无需配对）；跨名/纯登录渠道没配对就不建行。

    2026-11-14 定稿：只有跨名配对才是显式授权；同一渠道同一平台不需要 c→c 行。
    """
    from src.domains.platform import hooks

    monkeypatch.setattr("src.kernel.persistence.uow.uow", lambda: _Passthrough(session))
    session.execute(text("DELETE FROM grape_t_channel_repo_link"))
    session.commit()
    # 同域：gitcode 渠道 + gitcode 平台（夹具里在册）→ 天然建自己的关系
    hooks.on_oauth_login("u6", "gitcode")
    assert UserRepoPlatformLinkRepo.get(session, "u6", "gitcode") is not None, "同域应天然建行（无需配对）"
    # 纯登录渠道（huawei 不是仓库平台）→ 无配对则不建行
    hooks.on_oauth_login("u7", "huawei")
    assert UserRepoPlatformLinkRepo.get(session, "u7", "huawei") is None, "纯登录渠道无配对不建行"


def test_clear_pat_cascade_syncs_relation(monkeypatch):
    """清空 PAT 的级联路径必须同步关系表（不经 delete_credential，别漏 hook）。

    反证：删掉 settings._cascade_pat_clear 里的 on_pat_deleted 调用即红 ——
    实弹现象是「PAT 清空、无 OAuth，仍在项目页进不了引导页」。
    """
    import inspect

    from src.domains.mgmt.routes import settings as st

    src = inspect.getsource(st._cascade_pat_clear)
    assert "on_pat_deleted" in src, "清 PAT 级联没有同步关系表（关系行残留 → has_any 仍为 true）"


def test_login_kicks_projects_refresh(monkeypatch):
    """闭环规则：登录/绑定成功必须触发项目列表异步刷新。"""
    from src.domains.platform import hooks

    calls = []
    monkeypatch.setattr(hooks, "_kick_user_projects_refresh", lambda uid: calls.append(uid))
    monkeypatch.setattr(hooks, "_targets", lambda s, p: [])
    monkeypatch.setattr("src.kernel.persistence.uow.uow", lambda: _Passthrough(None))
    hooks.on_oauth_login("u4", "gitee")
    hooks.on_pat_saved("u4", "gitee")
    assert calls == ["u4", "u4"]


def test_link_is_single_statement_idempotent(session):
    """P2-3：link() 用方言 upsert 单语句完成——重复调用只留一行，撤销后可复活。

    原实现 UPDATE 0 行再 INSERT，并发窗口里唯一键冲突 → 异常被 hooks 吞掉、
    uow 一并回滚（关系行与同事务的撤销动作一起丢）。
    """
    from sqlalchemy import text as _t

    uid = "01a1da93-25f3-7050-b873-85070cf15f5e"  # uuid7 形态
    UserRepoPlatformLinkRepo.link(session, uid, "gitee")
    UserRepoPlatformLinkRepo.link(session, uid, "gitee")
    session.commit()

    rows = session.execute(_t(
        "SELECT COUNT(*) FROM grape_t_user_repo_platform_link WHERE user_id=:u"
    ), {"u": uid}).scalar_one()
    assert rows == 1, f"重复 link 产生了多行：{rows}"

    UserRepoPlatformLinkRepo.revoke(session, uid, "gitee", "unbound")
    session.commit()
    row = UserRepoPlatformLinkRepo.get(session, uid, "gitee")
    assert row.revoked_at, "撤销后应带 revoked_at"

    UserRepoPlatformLinkRepo.link(session, uid, "gitee")
    session.commit()
    row = UserRepoPlatformLinkRepo.get(session, uid, "gitee")
    assert row.revoked_at == "" and row.revoked_reason == "", "重新 link 应复活关系行"
