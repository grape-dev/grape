"""DDL 安全带（kernel 端口）：幂等补列 + 跨方言探列。

为什么下沉到 kernel（架构守护 3.0 PR #2009 I1）：
域模块（如 `src/domains/mgmt/migrations/org_repo_create_options.py`）需要补列原语，
但不应 `from src.api.db_setup import _ensure_column` —— 那是 domains→api 的反向依赖，
会让域代码绑死 API 装配层。这里提供一份自包含、可注入的安全带；`db_setup` 只做
兼容包装（注入自己的域守卫、人工处理清单和 MDL 等待异常），既有调用点语义不变。

I2：`table_info_rows / table_columns / table_exists` 是探列的单一入口。
SQLite 走 PRAGMA；mysql_compat 游标由 `src/kernel/mysql_compat.py` 翻译成
information_schema；裸 MySQL 游标（不经 compat）PRAGMA 会 1064，自动回退
information_schema。
"""
from __future__ import annotations

import re
from typing import Callable, List, Optional

from src.kernel.config import log


class NonOwnerObject(Exception):
    """目标表在当前库中是视图（非 owner 域库）——跳过该条迁移且**不盖章**。

    库按域拆分后，同一张表只有在 owner 库才是基表；其它域库拿到的是跨库只读视图。
    视图既不能 ALTER、列又是快照，所以补列只能由 owner 库进程执行。抛这个异常
    让执行器「跳过不盖章」，等 owner 库补齐后其它库自然收敛，而不是把版本号盖死。
    """


class MdlWaitTimeout(Exception):
    """MySQL MDL 等待超时（错误码 1205 / Lock wait timeout）——跳过该条迁移不盖章。"""


#: `_ensure_column` 两级降级都失败时的人工处理清单（启动结束打 ERROR 汇总）。
DDL_MANUAL_REVIEW: List[str] = []

#: legacy-stamp 首跑中被「min_code_version 门槛拦截 / MDL 超时」而未执行的 DDL 清单。
LEGACY_BLOCKED_DDL: List[str] = []


def strip_null_default(ddl: str) -> str:
    """仅剥离尾部 `DEFAULT NULL`（与可空列的隐式默认完全等价）；其它显式默认值原样保留。"""
    return re.sub(r"\s+DEFAULT\s+NULL\s*$", "", (ddl or "").strip(), flags=re.IGNORECASE)


def strip_text_default(ddl: str) -> str:
    """剥离 TEXT 族的显式 DEFAULT（如 `TEXT DEFAULT ''`）。

    MySQL 对带 DEFAULT 的 TEXT 列 INSTANT/INPLACE 均不支持（1845），且接受语法后
    会静默丢弃默认值 —— 语义上本就等价于无默认。剥离后即可用 INSTANT 补列。
    """
    d = (ddl or "").strip()
    if not re.search(r"\b(?:TINY|MEDIUM|LONG)?TEXT\b", d, re.IGNORECASE):
        return ddl
    return re.sub(r"\s+DEFAULT\s+.*$", "", d, flags=re.IGNORECASE)


def is_mdl_timeout(err: Exception) -> bool:
    s = str(err).lower()
    return ("lock wait timeout" in s or "1205" in s
            or "metadata lock" in s or "metadata_locks" in s)


def default_mysql_backend() -> bool:
    """是否 MySQL 后端（ALGORITHM / SET SESSION 仅 MySQL 下发；SQLite 路径不变）。"""
    try:
        from src.kernel.mysql_compat import get_db_backend
        return get_db_backend() == "mysql"
    except Exception:
        return False


def default_guard(table: str, what: str = "") -> bool:
    """表归属守卫：别家域的表不建/不改（与 db_setup._owned_skip 同口径）。"""
    try:
        from src.kernel import table_ownership as _own
        owner = _own.foreign_owner(table)
        if owner:
            log.info(f"[DDL_SKIP] {what} `{table}`: owner={owner} current={_own.current_domain()}")
            return True
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[DDL_SKIP] ownership guard error on `{table}`: {exc}")
    return False


def default_is_base_table(c, tbl: str) -> bool:
    """当前库中 tbl 是否为基表（MySQL 查 information_schema；SQLite 恒 True）。"""
    if not default_mysql_backend():
        return True
    try:
        row = c.execute(
            "SELECT table_type FROM information_schema.tables "
            "WHERE table_schema=DATABASE() AND table_name=?",
            (tbl,),
        ).fetchone()
        if not row:
            return True   # 查不到（mock/临时表）→ 按基表处理，保持既有行为
        return str(row[0]).upper() == "BASE TABLE"
    except Exception:
        return True   # 查询失败按基表处理，保持既有行为


def table_info_rows(c, table: str) -> list:
    """表的 PRAGMA table_info 行（列序：cid, name, type, notnull, dflt_value, pk）。

    先走 PRAGMA（SQLite 原生 / mysql_compat 翻译）；裸 MySQL 游标 PRAGMA 会 1064，
    回退 information_schema（列序与 sqlite 对齐）。查询失败返回空列表 —— 调用方
    按「表/列缺失」处理，不得把「探不到」当成「已具备」。
    """
    try:
        rows = c.execute(f"PRAGMA table_info({table})").fetchall() or []
        if rows:
            return list(rows)
    except Exception:  # noqa: BLE001 - 裸 MySQL 不支持 PRAGMA，走下面的回退
        pass
    try:
        rows = c.execute(
            "SELECT ORDINAL_POSITION-1, COLUMN_NAME, COLUMN_TYPE, "
            "IF(IS_NULLABLE='NO',1,0), COLUMN_DEFAULT, IF(COLUMN_KEY='PRI',1,0) "
            "FROM information_schema.columns WHERE TABLE_SCHEMA=DATABASE() "
            "AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
            (table,),
        ).fetchall() or []
        return list(rows)
    except Exception:  # noqa: BLE001 - SQLite 没有 information_schema
        return []


def table_columns(c, table: str) -> list:
    """表的列名清单（探列单一入口）。"""
    return [str(r[1]) for r in table_info_rows(c, table)]


def table_exists(c, table: str) -> bool:
    return bool(table_info_rows(c, table))


def ensure_column(
    c,
    tbl: str,
    col: str,
    ddl: str,
    *,
    allow_copy: bool = False,
    strict: bool = False,
    force: bool = False,
    raise_on_guard: bool = False,
    guard: Optional[Callable[[str, str], bool]] = None,
    is_base_table: Optional[Callable] = None,
    mysql_backend: Optional[Callable[[], bool]] = None,
    manual_review: Optional[List[str]] = None,
    blocked_ddl: Optional[List[str]] = None,
    non_owner_exc: Optional[type] = None,
    mdl_exc: Optional[type] = None,
    is_mdl_timeout_fn: Optional[Callable] = None,
):
    """幂等补列：列不存在则 ALTER（INSTANT → INPLACE → 人工清单/COPY）。

    参数语义与 `src.api.db_setup._ensure_column` 完全一致；可注入参数让 db_setup
    保留自己的守卫/清单/异常类，同时域模块可直接用默认实现（无 domains→api 依赖）。
    """
    guard = guard or default_guard
    is_base_table = is_base_table or default_is_base_table
    mysql_backend = mysql_backend or default_mysql_backend
    manual_review = DDL_MANUAL_REVIEW if manual_review is None else manual_review
    blocked_ddl = LEGACY_BLOCKED_DDL if blocked_ddl is None else blocked_ddl
    non_owner_exc = non_owner_exc or NonOwnerObject
    mdl_exc = mdl_exc or MdlWaitTimeout
    is_mdl_timeout_fn = is_mdl_timeout_fn or is_mdl_timeout

    if not force and guard(tbl, "ensure column"):
        # 2026-11-18 事故（category_dict 迁移 202611171846）：进程域判定不出/不是属主时
        # 这里静默 return False，调用方 fn 正常返回 → 迁移照样盖章 → 列永不创建也不重试。
        # ⚠️ 默认保持静默：存量 strict=True 调用点里有属主域与注册域不一致的迁移，
        # 全局改成抛错会把它们一起引爆。需要「跳过即失败」的迁移显式传 raise_on_guard=True。
        if raise_on_guard:
            raise RuntimeError(
                f"_ensure_column({tbl}.{col}) 被域归属守卫跳过（本进程非该表属主），"
                f"raise_on_guard=True 拒绝静默盖章——迁移应在属主域执行或改用 force=True")
        return False
    cols = table_columns(c, tbl)
    if col in cols:
        return
    if not is_base_table(c, tbl):
        # 非 owner 库的视图：ALTER 必然 1347，且视图列是建视图时快照 —— 必须由
        # owner 库补列 + 重建视图。**跳过且不盖章**（抛给迁移执行器，下次启动重试）。
        raise non_owner_exc(
            f"{tbl}.{col}: 当前库中是视图（非 owner），需 owner 库补列并重建视图后重试")
    base_sql = f"ALTER TABLE {tbl} ADD COLUMN `{col}` {ddl}"
    if not mysql_backend():
        c.execute(base_sql)
        return
    # MySQL：INSTANT → INPLACE,LOCK=NONE → 人工清单（绝不静默 COPY）
    try:
        c.execute(f"{base_sql}, ALGORITHM=INSTANT")
        return
    except Exception as instant_err:  # noqa: BLE001
        if is_mdl_timeout_fn(instant_err):
            # MDL 超时是「被阻塞」而非「不支持」，不降级重试——直接跳过不盖章。
            blocked_ddl.append(f"{tbl}.{col} ADD COLUMN（MDL 超时）")
            raise mdl_exc(str(instant_err))
        log.warning(f"_ensure_column({tbl}.{col}) ALGORITHM=INSTANT failed: {instant_err}，"
                    f"降级重试 ALGORITHM=INPLACE, LOCK=NONE")
    try:
        c.execute(f"{base_sql}, ALGORITHM=INPLACE, LOCK=NONE")
        return
    except Exception as inplace_err:  # noqa: BLE001
        if is_mdl_timeout_fn(inplace_err):
            blocked_ddl.append(f"{tbl}.{col} ADD COLUMN（MDL 超时）")
            raise mdl_exc(str(inplace_err))
        # Issue #1546：带 DEFAULT NULL 的 TEXT 列 INSTANT/INPLACE 均不支持，剥默认值重试。
        ddl_nd = strip_null_default(ddl)
        if ddl_nd != ddl:
            base_nd = f"ALTER TABLE {tbl} ADD COLUMN `{col}` {ddl_nd}"
            for algo_nd in ("ALGORITHM=INSTANT", "ALGORITHM=INPLACE, LOCK=NONE"):
                try:
                    c.execute(f"{base_nd}, {algo_nd}")
                    log.info(f"_ensure_column({tbl}.{col}) 剥离 DEFAULT NULL 后补列成功（{algo_nd}）")
                    return
                except Exception as nd_err:  # noqa: BLE001
                    if is_mdl_timeout_fn(nd_err):
                        blocked_ddl.append(f"{tbl}.{col} ADD COLUMN（MDL 超时）")
                        raise mdl_exc(str(nd_err))
                    inplace_err = nd_err
        # TEXT 族的显式 DEFAULT（如 "TEXT DEFAULT ''"）在 MySQL 上 INSTANT/INPLACE
        # 均不支持，且 MySQL 会静默丢弃该默认值——剥离后重试即可补列。
        ddl_td = strip_text_default(ddl)
        if ddl_td != ddl and ddl_td != ddl_nd:
            base_td = f"ALTER TABLE {tbl} ADD COLUMN `{col}` {ddl_td}"
            for algo_td in ("ALGORITHM=INSTANT", "ALGORITHM=INPLACE, LOCK=NONE"):
                try:
                    c.execute(f"{base_td}, {algo_td}")
                    log.info(f"_ensure_column({tbl}.{col}) 剥离 TEXT 默认值后补列成功（{algo_td}）")
                    return
                except Exception as td_err:  # noqa: BLE001
                    if is_mdl_timeout_fn(td_err):
                        blocked_ddl.append(f"{tbl}.{col} ADD COLUMN（MDL 超时）")
                        raise mdl_exc(str(td_err))
                    inplace_err = td_err
        if allow_copy:
            copy_sql = base_sql.replace(ddl, ddl_td) if ddl_td != ddl else base_sql
            try:
                c.execute(copy_sql)      # 默认算法：MySQL 按需选 COPY 重建（小表安全）
                log.info(f"_ensure_column({tbl}.{col}) INPLACE 不支持 → 已按默认算法(COPY)补列")
                return
            except Exception as copy_err:  # noqa: BLE001
                entry = (f"{tbl}.{col}: INSTANT/INPLACE/默认算法(COPY) 均失败 "
                         f"({copy_err})——需人工处理")
                manual_review.append(entry)
                log.error(f"_ensure_column MANUAL REVIEW: {entry}")
                if strict:
                    raise RuntimeError(f"_ensure_column 补列失败（strict）: {entry}")
                return
        entry = (f"{tbl}.{col}: INSTANT 与 INPLACE,LOCK=NONE 均失败 "
                 f"(INPLACE err={inplace_err})——需人工处理，未静默 COPY")
        manual_review.append(entry)
        log.error(f"_ensure_column MANUAL REVIEW: {entry}")
        if strict:
            raise RuntimeError(f"_ensure_column 补列失败（strict）: {entry}")