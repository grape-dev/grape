"""按域取连接的统一入口：owner 域代码在自己的库上跑 SQL。

三库独立后，"域模块 + 进程库"的组合会失效：runtime/insight 的代码也挂在 mgmt 进程里，
用 `session_manager.db` 会打到 mgmt 库（跨域视图已删 → 1146）。本模块给出一个不改变
SQL 文本、只切换连接的入口：

    from src.kernel.domain_db import cursor
    with cursor("runtime") as c:
        c.execute("SELECT ... FROM <运行域表> ...")  # SQL 文本原样保留，仅换连接

行为：
- 该域配了自有 DSN（`GRAPE_DSN_<D>` + `GRAPE_DOMAIN_LIB=1`）→ 用 `db_registry.cursor(domain)`；
- 否则（单库/视图期）→ 回落到进程主库 `session_manager.db`（原行为）。

只读查询直接替换即可；**写操作**需自己提交（owner 连接为 autocommit 语义，
进程库需要 `db.commit()`），因此写路径仍应优先走 `public` 写函数而不是本入口。
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator


@contextmanager
def cursor(domain: str) -> Iterator[object]:
    """取域连接游标（own DSN 优先，回落进程库）。"""
    if domain not in ("mgmt", "runtime", "insight"):
        raise ValueError(f"unknown domain: {domain}")
    from src.kernel import db_registry

    if db_registry.uses_own_dsn(domain):
        with db_registry.cursor(domain) as c:
            yield c
        return

    from src.kernel.session_manager import db, db_lock

    with db_lock:
        yield db.cursor()