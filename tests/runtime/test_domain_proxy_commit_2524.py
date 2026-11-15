# -*- coding: utf-8 -*-
"""Issue #2524 现场回归（二）：域代理的 commit/rollback 必须与 cursor() 的取连接分支一致。

runtime 域进程里 `cursor()` 走 `session_manager.db`（autocommit=False 的线程级连接），
但 `commit()` 先判 `uses_own_dsn("runtime")`，命中就 `return None`。DSN 存在时分库部署
三个代理全部变成“假提交”：enqueue 写入的任务行一直悬在事务里，UI 看到“排队中”
却在 `agent_tasks` 查不到任何行，随后的任何 rollback 还会把它直接回滚掉。
"""
import importlib
import pytest


_PROXIES = [
    ("src.domains.runtime.engine.service", "_DomainDbProxy"),
    ("src.domains.runtime.issues_center", "_DomainDbProxy"),
    ("src.domains.runtime.pipeline.event_pipeline", "_DomainDbProxy"),
    ("src.domains.runtime.routes.issues", "_DomainDbProxy"),
]


class _RecordingMain:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, *a, **kw):
        return self

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


@pytest.mark.parametrize("module_path,cls_name", _PROXIES)
def test_commit_reaches_thread_local_connection_in_runtime_process(monkeypatch, module_path, cls_name):
    mod = importlib.import_module(module_path)
    from src.kernel import db_registry, session_manager

    rec = _RecordingMain()
    monkeypatch.setattr(db_registry, "current_domain", lambda: "runtime")
    # DSN 存在（分库部署的常态）：旧实现会在这一步 return None
    monkeypatch.setattr(db_registry, "uses_own_dsn", lambda _d: True)
    monkeypatch.setattr(session_manager, "db", rec)

    proxy = getattr(mod, cls_name)()
    proxy.commit()
    assert rec.commits == 1, f"{module_path}: 假提交会导致 enqueue 的行不可见"
    proxy.rollback()
    assert rec.rollbacks == 1