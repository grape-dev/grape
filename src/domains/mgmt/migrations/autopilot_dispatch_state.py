"""autopilot_tasks「不可派发」状态三列 + 存量一次性幂等自检（Issue #3733）。

实现放在本域（mgmt 是 `autopilot_tasks` 的 owner 域），`src/api/db_setup.py` 只留
`register_migration` 接线，理由两条：

1. 补列必须由 owner 域执行——跨域迁移会被 `_ensure_column` 的域归属守卫跳过，
   而守卫跳过时迁移默认仍会盖章（见 2026-11-18 `category_dict` 事故），列就永不落地；
   本迁移对每条补列传 `raise_on_guard=True`，万一在非属主进程执行则显式抛错、不盖章；
2. 含表名的写语句若留在 api 层，会被 `tests/arch/test_foreign_table_refs.py` 判成
   新增跨域引用（`autopilot_tasks←api`），破棘轮。

契约：见 `docs/issue-3733/plan.md` 与
`tests/kernel/test_issue3733_autopilot_dispatch_state_migration.py`。
"""
from __future__ import annotations

import logging
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# 迁移注册（version / name / 域）只在 `src/api/db_setup.py` 的 register_migration 处维护：
# 仓库静态守卫 `tests/api/test_migration_track_guard.py` 只认模块级字符串字面量，
# 本模块不再导出同名常量（复审 Nit：避免同一条版本号双份维护、漂移后静默撞号）。

TABLE = "autopilot_tasks"

#: （列名, DDL）：三列都是短码 / 时间戳，**VARCHAR(255) 足够，且必须是 VARCHAR 而非 TEXT**:
#: MySQL 8.0.13 之前 TEXT 不支持 DEFAULT，而本仓实测 `TEXT NOT NULL DEFAULT ''` 在
#: 8.0.46 上落地成 `text NOT NULL DEFAULT NULL`（strict 下任何不给值的 INSERT 直接
#: 1364）—— 建单接口就是被这个打成 500 的（Issue #3733，实测见 PR #2157）。
#: 补列时仍传 allow_copy=True：这是对旧版本 MySQL（TEXT 时代补列的 COPY 语义）的
#: 兜底，autopilot_tasks 是小表，即便退化到 COPY 也安全（复审 Optional：原注释仍按
#: TEXT 描述，与现在的 VARCHAR DDL 漂移）。
STATE_COLUMNS = (
    ("dispatch_state", "VARCHAR(255) NOT NULL DEFAULT ''"),
    ("dispatch_state_reason", "VARCHAR(255) NOT NULL DEFAULT ''"),
    ("dispatch_state_at", "VARCHAR(255) NOT NULL DEFAULT ''"),
)

#: VARCHAR(255) 的存宽。收窄 MODIFY 前必须量一次存量长度（PR #2157 复审 Optional）：
#: strict 模式下超长值让 MySQL 直接报 1406 而不是截断，迁移会「每次启动重试、日志只有
#: 一行 1406」；先量一次就能把「哪一列 / 最长多少 / 几行」写进报错，运维按图清理。
MAX_STATE_COLUMN_LEN = 255

#: `COLUMN_DEFAULT` 的三态判据（PR #2157 门禁复审 BLOCKED 的最小修复）。
#:
#: 原判据 `str(current.get(col) or "") == ""` 把 **NULL（= 列无默认值，待修）** 与
#: **''（= 已修好）** 压成同一态：MySQL 存量库（#3733 首版 TEXT 落地成
#: `NOT NULL DEFAULT NULL`）与新装库（CREATE TABLE 走 mysql_compat 的表达式默认值，
#: COLUMN_DEFAULT 读到 `_utf8mb4''` 而非 `''`）上一条 MODIFY 都不发，
#: 迁移 202611240011 空转 0 条 DDL 却照样盖章 —— 假绿，strict 下 1364 照旧。
#:
#: 三态显式化后：
#: - ``OK``      ：列存在且默认值已是 ``''``（**唯一**可以零写的态）；
#: - ``MISSING`` ：列不存在。本迁移只改列定义（MODIFY），改不了缺列，
#:                 必须 raise 不盖章，交补列迁移 202611240007 在属主域重试；
#: - ``NULL``    ：列存在但无默认值（COLUMN_DEFAULT IS NULL）→ **必发 MODIFY**；
#: - ``OTHER``   ：列存在但默认值非 ``''``（如表达式默认 / 历史残留字面量）→ 必发 MODIFY。
DEFAULT_OK = "ok"
DEFAULT_MISSING = "missing"
DEFAULT_NULL = "null"
DEFAULT_OTHER = "other"


def classify_column_default(current: dict, col: str) -> str:
    """把 information_schema 读到的默认值判成三态之一（ensure 与 post_check 共用）。

    共用同一判据是本次修复的要点：两个函数各自判断就会漂移，一处按 NULL 待修、
    另一处按 NULL 已修，假绿就会从 post_check 那一侧复活。
    """
    if col not in current:
        return DEFAULT_MISSING
    value = current[col]
    if value is None:
        return DEFAULT_NULL
    return DEFAULT_OK if str(value) == "" else DEFAULT_OTHER


def backfill_dispatch_state(cursor: Any, now: str = "") -> int:
    """补状态三列 + 把 enabled 且空指令的存量行标记为不可派发（幂等）。

    返回本次**新标记**的行数（0 = 重复执行，零改动）。

    - 只标记 `enabled=1 AND TRIM(instruction)=''`：指令非空 / 已停用的行不动；
    - **不改 `enabled`**：直接停用会掩盖「已启用却在空转」的事实，补齐指令即恢复派发；
    - `dispatch_state<>:blocked` 谓词使重复执行零写（注册式迁移盖章后不再执行，
      未盖章时每次启动重试），且不刷新 `dispatch_state_at`——该时间是状态进入时刻，
      也是跨进程判重的权威。
    """
    from src.api import db_setup as _ds

    from src.domains.mgmt.autopilot_state import (
        AUTOPILOT_EMPTY_INSTRUCTION_REASON,
        AUTOPILOT_STATE_BLOCKED_EMPTY_INSTRUCTION,
    )

    for col, ddl in STATE_COLUMNS:
        # strict + raise_on_guard（复审 Required）：`_ensure_column` 在 INSTANT/INPLACE/COPY
        # 全失败、或被域归属守卫跳过时只记人工清单并**正常返回**（db_setup.py:1239-1250），
        # 而迁移执行器约定「fn 正常返回=盖章」→ 列没建成却把版本号盖死，下次启动不再重试。
        # 后果不止界面空白：`list_enabled_autopilot_tasks_any` 已显式 SELECT 这三列，
        # 缺列即 1054 → 调度器每 tick 抛错被兜底吞成 `Autopilot scheduler loop error`，
        # **全部 autopilot 任务一起停派**。故这里与 db_setup.py:10923 / 11051 同口径：
        # 补列失败必须抛错（不盖章，下次在属主域重试）。
        _ds._ensure_column(cursor, TABLE, col, ddl, allow_copy=True,
                           strict=True, raise_on_guard=True)
    _now = now or datetime.now(timezone.utc).isoformat()
    cur = cursor.execute(
        "UPDATE autopilot_tasks SET dispatch_state=?, dispatch_state_reason=?, "
        "dispatch_state_at=? WHERE enabled=1 AND TRIM(instruction)='' AND dispatch_state<>?",
        (AUTOPILOT_STATE_BLOCKED_EMPTY_INSTRUCTION, AUTOPILOT_EMPTY_INSTRUCTION_REASON,
         _now, AUTOPILOT_STATE_BLOCKED_EMPTY_INSTRUCTION),
    )
    return int(getattr(cur, "rowcount", 0) or 0)


def run(conn: Any = None) -> int:
    """迁移入口：在 owner 连接上补列 + 自检（`conn` 缺省用进程主库）。

    失败一律 raise（不做「log 后正常返回」的静默降级）：执行器约定「抛错 = 不盖章，
    下次启动重试」；吞掉就会「列没建成、版本号却盖死」，线上查询立刻 1054。
    """
    from src.api import db_setup as _ds

    target = _ds.db if conn is None else conn
    with _ds.db_lock:
        cursor = target.cursor()
        try:
            marked = backfill_dispatch_state(cursor)
            target.commit()
        finally:
            cursor.close()
    if marked:
        log.info(f"Autopilot: {marked} enabled task(s) marked as not dispatchable "
                 f"(empty instruction, Issue #3733)")
    return marked


def _state_column_defaults(conn: Any) -> dict:
    """读 information_schema 取三列当前默认值（MySQL）。

    只在 MySQL 后端调用（`ensure_state_column_defaults` /
    `state_column_defaults_converged` 都先判后端）：SQLite 没有 information_schema。
    返回 dict「列名 → COLUMN_DEFAULT」，**缺失的列不出现在 dict 里**——
    缺失是三态判据中的独立一态，调用方不可用 `.get(col)` 的默认值糊过去。
    """
    out = {}
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT COLUMN_NAME, COLUMN_DEFAULT FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=? "
            "AND COLUMN_NAME IN ('dispatch_state','dispatch_state_reason','dispatch_state_at')",
            (TABLE,))
        for row in cursor.fetchall():
            out[str(row[0])] = row[1]
    finally:
        cursor.close()
    return out


def _overlong_state_values(conn: Any, col: str) -> tuple:
    """量一列存量值里超过 `MAX_STATE_COLUMN_LEN` 的行数与最长长度。

    返回 ``(超长行数, 最大长度)``。长度函数按方言挑：MySQL 是 `CHAR_LENGTH`（按字符，
    与 VARCHAR 的 1406 口径一致），SQLite 没有它、退化成 `LENGTH`。两种都探测不出来
    （表还没建 / 权限不足）就 raise，由调用方 `_precheck_state_column_lengths` 记
    WARNING 后按 0 处理 —— 这是收窄 DDL 前的**诊断前置检查**，不是收敛判据：
    量不到不该阻断迁移，真超长时 MODIFY 自己会 1406（只是日志里没有可操作信息）。
    """
    row = None
    with closing(conn.cursor()) as cursor:
        for expr in ("CHAR_LENGTH", "LENGTH"):   # MySQL / SQLite 方言
            try:
                row = cursor.execute(
                    f"SELECT COUNT(*), COALESCE(MAX({expr}({col})), 0) FROM {TABLE} "
                    f"WHERE {expr}({col}) > ?", (MAX_STATE_COLUMN_LEN,)).fetchone()
            except Exception as e:  # noqa: BLE001 - 换方言重试；两种都失败由调用方 WARNING
                log.debug(f"autopilot state length probe {expr} unsupported: {e}")
                row = None
            if row is not None:
                break
    if row is None:
        raise RuntimeError(f"autopilot state column length probe failed: {TABLE}.{col}")
    return int(row[0] or 0), int(row[1] or 0)


def _precheck_state_column_lengths(conn: Any, cols) -> None:
    """发 MODIFY 前统一预检存量长度：任一列超长就 raise（不盖章，零 DDL）。

    strict 模式下把宽列收窄到 `VARCHAR(255)` 遇超长值是 1406 而不是截断，迁移就会
    每次启动重试、日志只剩一行 SQL 错误码；这里把「哪一列 / 最多几行 / 最长多少」
    一次报全。不静默截断业务值——那是改数据，属运维手工动作。
    """
    overlong = []
    for col in cols:
        try:
            rows, longest = _overlong_state_values(conn, col)
        except Exception as e:  # noqa: BLE001 - 探测失败只少一条诊断，不阻断迁移
            log.warning(f"autopilot state column length probe failed for {col}: {e}")
            rows, longest = 0, 0
        if rows:
            overlong.append(f"{TABLE}.{col}: {rows} 行，最长 {longest}")
    if overlong:
        raise RuntimeError(
            "autopilot state column defaults: 存量值超过 VARCHAR("
            f"{MAX_STATE_COLUMN_LEN})，strict 下 MODIFY 必报 1406："
            + "; ".join(overlong)
            + "。先清理/截断这些行再重试（本迁移不盖章，不静默截断业务值）")


def ensure_state_column_defaults(conn: Any = None) -> int:
    """把三列从「TEXT NOT NULL 无默认」修正为 `VARCHAR(255) NOT NULL DEFAULT ''`。

    背景（2026-11-25 部署验证实测）：#3733 首版 DDL 用 `TEXT NOT NULL DEFAULT ''`，
    MySQL 8.0 上落地结果是 `text NOT NULL DEFAULT NULL` —— strict 模式下任何不显式
    给值的 INSERT 直接 1364 `Field 'dispatch_state' doesn't have a default value`，
    建单接口 500。列已存在时 `_ensure_column` 不会改类型/默认值，所以需要本迁移补一刀。

    约定（架构守护 PR #2157 C2/M2）：
    - 危险 DDL（MODIFY COLUMN）走唯一入口 `_destructive_ddl(kind_hint="cleanup")`，
      被 min_code_version 门槛拦截时 **raise 不盖章**（跳过 ≠ 已迁移）；
    - 后端探测失败不再静默 `return 0`（执行器以「fn 正常返回 = 盖章」为约定，
      静默返回会把版本号盖死、1364 永不重试），改为 raise；
    - 幂等：只对**已收敛为空串默认值**（``classify_column_default == DEFAULT_OK``）的列
      零写，其余三态（NULL / 表达式默认 / 缺列）分别 MODIFY 与 raise，返回值 = 实际修正列数
      （PR #2157 门禁复审：只按「默认值非空串」判待修会把 NULL 当成已修，迁移空转却盖章）。
    - 收窄前预检存量长度（`_overlong_state_values`）：有超长值就 raise 并报出「列 / 行数 /
      最长长度」，避免 strict 下的 1406 变成每次启动只有一行 SQL 错误码的黑盒重试
      （PR #2157 复审 Optional；不静默截断业务值）。
    列缺失时 raise：本迁移只改列定义，改不了缺列，交补列迁移 202611240007 重试。
    SQLite（单实例、无蓝绿共存）列已是 TEXT DEFAULT ''，无需修正，返回 0。
    """
    from src.api import db_setup as _ds

    try:
        from src.kernel.session_manager import _is_mysql_backend
        is_mysql = _is_mysql_backend()
    except Exception as e:
        raise RuntimeError(
            f"autopilot state column defaults: backend detection failed: {e}") from e
    if not is_mysql:
        return 0
    target = _ds.db if conn is None else conn
    fixed = 0
    with _ds.db_lock:
        cursor = target.cursor()
        try:
            current = _state_column_defaults(target)
            missing = [col for col, _ddl in STATE_COLUMNS
                       if classify_column_default(current, col) == DEFAULT_MISSING]
            if missing:
                # 缺列不能靠 MODIFY 修（MySQL 1054），也不能盖章跳过：缺失即「未收敛」，
                # 交补列迁移（202611240007，owner 域 + raise_on_guard）下次启动重试。
                raise RuntimeError(
                    "autopilot state column defaults: columns missing "
                    f"{missing} - 补列迁移 202611240007 未落地，本迁移不盖章")
            to_fix = [(col, ddl) for col, ddl in STATE_COLUMNS
                      if classify_column_default(current, col) != DEFAULT_OK]
            # 先把该修的单子拉出来、统一预检长度，再发 DDL：任何一列超长都在动
            # 第一列之前就 raise，不留下「改了一半」的半态。
            if to_fix:
                _precheck_state_column_lengths(target, [col for col, _ddl in to_fix])
            for col, ddl in to_fix:
                if not _ds._destructive_ddl(
                        cursor, f"ALTER TABLE {TABLE} MODIFY COLUMN {col} {ddl}",
                        kind_hint="cleanup"):
                    raise RuntimeError(
                        "autopilot state column defaults: blocked by cleanup gate "
                        "(min_code_version), not stamped")
                fixed += 1
            target.commit()
        finally:
            cursor.close()
    log.info(f"Autopilot: normalized {fixed} dispatch_state column default(s) (Issue #3733)")
    return fixed


def state_column_defaults_converged(conn: Any = None) -> None:
    """post_check：盖章前确认三列默认值已收敛为空串（不收敛则 raise，不盖章）。

    MySQL：`information_schema.COLUMNS.COLUMN_DEFAULT` 必须是 `''`；
    列缺失（补列迁移未落地）与 `NULL`（列无默认值）**都判失败** —— 两者都让
    strict 模式下的无值 INSERT 报 1364，判放过就是门禁本身成了假绿（PR #2157 复审）。
    SQLite：列定义本身带 `DEFAULT ''`（1500 行级小表，NULL 默认值不成立）。
    后端判不出时按失败处理（与 ensure 同口径，不静默通过）。
    """
    from src.api import db_setup as _ds

    try:
        from src.kernel.session_manager import _is_mysql_backend
        is_mysql = _is_mysql_backend()
    except Exception as e:
        raise RuntimeError(
            f"autopilot state column defaults post_check: backend detection failed: {e}") from e
    if not is_mysql:
        return
    target = _ds.db if conn is None else conn
    with _ds.db_lock:
        current = _state_column_defaults(target)
    states = {col: classify_column_default(current, col) for col, _ddl in STATE_COLUMNS}
    missing = [col for col, state in states.items() if state == DEFAULT_MISSING]
    if missing:
        raise RuntimeError(
            f"autopilot state column defaults post_check: columns missing {missing}")
    bad = [(col, current.get(col)) for col, state in states.items()
           if state != DEFAULT_OK]
    if bad:
        raise RuntimeError(
            "autopilot state column defaults post_check: DEFAULT not normalized for "
            f"{bad} (NULL/非空串默认值均未收敛) - not stamped, retry on next start")
