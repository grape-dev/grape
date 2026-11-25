"""Unit of Work（D53 评审采纳：Gaia session-per-use-case 形态的本地化）。

- 用例级 session：with uow() 包裹多步操作，正常退出 commit、异常 rollback；
- Repository 方法接受外部 session，内部只 flush 不 commit（拿主键用
  flush，禁止中途 commit 再继续写——唯一提交点收敛在 UoW 出口）；
- 后台线程各自独立 UoW，禁止跨线程共享 session（多进程陈旧读防线）；
- **域路由（Issue #3897 / #3732 同口径）**：`uow(domain="mgmt")` 绑该域引擎
  （`get_domain_engine`），而不是进程主库。三库拓扑下 worker/event 进程主库是
  runtime 库，identity 表（owner=mgmt）在那边只是跨域视图——固定绑主库会让每次
  身份解析 1146（`Table 'grape_runtime.external_identities' doesn't exist`）。
  跨域表（mgmt/runtime/insight，归属见 configs/table_ownership.json）的 ORM 事务
  一律传 owner 域；`uow()` 无参语义不变（单库/本域进程零回归）。
- Issue #3732 与 #3897 收敛到同一处实现：本模块只保留 `uow(domain)`（含
  `session.info["domain"]` 归属标记），不再另设域工厂包装。
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy.orm import Session, sessionmaker

from src.kernel.persistence.engine import get_domain_engine, get_engine


@contextmanager
def uow(domain: str | None = None) -> "Iterator[Session]":
    """用例级事务边界：yield session，出口统一 commit/rollback 后 close。

    domain：该 UoW 归属的域（None=进程主库，既有行为零变化）。跨域 ORM 读写**必须**
    传 owner 域：`get_domain_engine` 在单库/本域进程回落主库，三库拓扑缺 DSN 时显式抛
    `DomainUnavailable`（绝不静默打到错误的库）。
    """
    bind = get_domain_engine(domain) if domain else get_engine()
    factory = sessionmaker(bind=bind, expire_on_commit=True, future=True)
    session = factory()
    # 域归属标记（Issue #3732）：日志/审计要能报出「打的是哪个域」。取 getattr
    # 而非直取 session.info——用例以伪造 session 作 seam（tests/mgmt/
    # test_issue_3897_identity_relation.py），无 info 时按无标记跳过。
    _info = getattr(session, "info", None)
    if domain and _info is not None:
        _info["domain"] = (domain or "").strip().lower() or None
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
