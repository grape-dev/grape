"""端口未绑定时 token_service 必须降级返回空，不能 NameError。

回归背景：worker 进程没有装配层，_provider_or_none 的唯一走法就是 PortNotBound
分支；该分支曾直接引用未导入的 log，等于"优雅降级"变任务失败。
"""
from __future__ import annotations

import json

import pytest

from src.kernel import token_service as ts


@pytest.fixture()
def unbound(monkeypatch):
    """让 ports.vcs_provider 像未装配的 worker 一样抛 PortNotBound。"""
    import src.kernel.ports.vcs_provider as vp
    from src.kernel.ports._base import PortNotBound

    def _raise(*a, **k):
        raise PortNotBound("ports not bound in worker")

    monkeypatch.setattr(vp, "create_provider_from_repo_key", _raise)
    monkeypatch.setattr(ts, "_PortNotBound", PortNotBound, raising=False)
    monkeypatch.setattr(ts, "_PORT_UNBOUND_WARNED", False, raising=False)
    return ts


def test_provider_or_none_degrades_without_nameerror(unbound):
    assert unbound._provider_or_none("gitcode:org-a/repo-a") is None


def test_warning_is_logged_once_per_process(unbound, caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        assert unbound._provider_or_none("gitcode:org-a/repo-a") is None
    first = [r for r in caplog.records if "端口未绑定" in r.getMessage()]
    assert len(first) == 1

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert unbound._provider_or_none("gitcode:org-b/repo-b") is None
    again = [r for r in caplog.records if "端口未绑定" in r.getMessage()]
    assert again == []  # 只警告一次，不刷日志


def test_bot_token_reads_empty_when_unbound(unbound):
    assert unbound.get_repo_bot_token("gitcode:org-a/repo-a") == ""
    assert unbound.get_bot_identity_token("gitcode:org-a/repo-a") == ""