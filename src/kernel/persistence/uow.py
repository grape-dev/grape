"""Unit of Work（D53 评审采纳：Gaia session-per-use-case 形态的本地化）。

- 用例级 session：with uow() 包裹多步操作，正常退出 commit、异常 rollback；
- Repository 方法接受外部 session，内部只 flush 不 commit（拿主键用
  flush，禁止中途 commit 再继续写——唯一提交点收敛在 UoW 出口）；
- 后台线程各自独立 UoW，禁止跨线程共享 session（多进程陈旧读防线）。
"""
from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy.orm import Session, sessionmaker

from src.kernel.persistence.engine import get_engine


@contextmanager
def uow() -> "Session":
    """用例级事务边界：yield session，出口统一 commit/rollback 后 close。"""
    factory = sessionmaker(bind=get_engine(), expire_on_commit=True, future=True)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
