"""账号读会话（mgmt 域自持）：契约内部解决“用哪条连接”。

为什么在 mgmt 而不是 kernel：
  · “账号数据归 mgmt”是领域归属，连接选择属于该域的数据访问实现细节；
  · runtime 只调 mgmt.public 的契约函数，不需要（也不应该）知道域名/DSN/引擎。

行为：
  · 本进程域=mgmt 或单库拓扑 → 主库引擎
  · 其它进程 + GRAPE_DSN_MGMT → 域引擎
  · 其它进程 + 缺 DSN → DomainEngineUnavailable（**不回落主库**，由调用方转成可见告警）
"""
from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy.orm import sessionmaker


@contextmanager
def account_session():
    """产出一个只读 ORM 会话（短生命周期，用完即关）。"""
    from src.kernel.persistence.engine import get_domain_engine

    factory = sessionmaker(bind=get_domain_engine("mgmt"), expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.close()
