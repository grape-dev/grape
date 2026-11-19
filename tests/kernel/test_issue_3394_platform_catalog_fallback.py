"""Issue #3394-B 回归：平台前缀目录兜底 + insight 角色端口绑定。

三层缺陷（缺一层都不复现，beta 实测）：
  1. `_catalog_providers_from_dir` 用 `dirname(dirname(abspath(__file__)))` 得
     `<repo>/src`，去找 `<repo>/src/configs/platforms`（不存在）→ 恒空集且被
     except 静默吞掉；
  2. `_catalog_providers` 只在端口**抛异常**时兜底，端口返回空集时不兜底；
  3. insight 进程在三库模式（GRAPE_DOMAIN_LIB=1）跳过 init_database，因而漏掉
     contracts_wiring.bind_all → vcs_cards 端口未绑定。
结果：repo_key_candidates 只剩 base → check_repo_access 恒判非默认前缀仓库 403。

架构约束：kernel 只经 `src.kernel.ports.*` 触达域实现，端口绑定由装配层
（src/api/contracts_wiring）在各进程启动时执行，本文件按同一口径验证。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "test_sessions.db")

FILE_PLATFORMS = {"gitcode", "github", "gitee", "gitlab"}


def _port_unbound(monkeypatch):
    """模拟 4 进程之一未执行 contracts_wiring.bind_all（PortNotBound）。"""
    from src.kernel.ports import vcs_cards

    def _boom():
        raise RuntimeError("kernel port 'vcs_cards' is not bound")

    monkeypatch.setattr(vcs_cards, "list_vcs_platforms", _boom)


def test_catalog_from_dir_resolves_repo_root():
    """路径推导落到仓库根：configs/platforms/*.json 全部可见。"""
    from src.kernel import repo_key

    assert repo_key._catalog_providers_from_dir() >= FILE_PLATFORMS


def test_catalog_falls_back_when_port_raises(monkeypatch):
    """端口未绑定（抛异常）→ 目录兜底非空，且候选集覆盖全部平台前缀。"""
    from src.kernel import repo_key

    _port_unbound(monkeypatch)
    assert repo_key._catalog_providers() >= FILE_PLATFORMS
    cands = repo_key.repo_key_candidates("gitcode:o/r")
    assert {"gitee:o/r", "github:o/r", "gitlab:o/r"} <= set(cands)


def test_catalog_falls_back_when_port_returns_empty(monkeypatch):
    """端口可用但返回空集 → 空集不等于「没有平台」，同样兜底。"""
    from src.kernel import repo_key
    from src.kernel.ports import vcs_cards

    monkeypatch.setattr(vcs_cards, "list_vcs_platforms", lambda: [])
    assert repo_key._catalog_providers() >= FILE_PLATFORMS
    assert "gitee:o/r" in repo_key.repo_key_candidates("gitcode:o/r")


def test_check_repo_access_accepts_non_default_prefix(monkeypatch):
    """端口未绑定的进程里，gitee/github 托管仓库也不得恒 403。"""
    from fastapi import HTTPException

    from src.api import deps

    _port_unbound(monkeypatch)
    monkeypatch.setattr(deps.user_db, "get_managed_repos",
                        lambda uid: [{"repo_key": "gitee:o/r"}])
    user = {"id": "it3394-user", "auth_type": "jwt"}

    deps.check_repo_access(user, "o/r")          # 命中 gitee: 关系 → 放行
    with pytest.raises(HTTPException):
        deps.check_repo_access(user, "other/repo")  # 无关系 → 仍 403


def test_three_db_bootstrap_binds_kernel_ports(monkeypatch):
    """三库模式（GRAPE_DOMAIN_LIB=1）启动同样执行装配层端口绑定。"""
    from src.api import contracts_wiring, server_common

    bind_calls = []
    monkeypatch.setenv("GRAPE_DOMAIN_LIB", "1")
    monkeypatch.setattr(contracts_wiring, "bind_all",
                        lambda **kw: bind_calls.append(kw) or [])
    monkeypatch.setattr("src.api.db_setup.run_domain_migrations", lambda: None)

    server_common.init_process_db()

    assert bind_calls, "三库模式进程启动必须绑定 kernel 端口（装配层职责）"