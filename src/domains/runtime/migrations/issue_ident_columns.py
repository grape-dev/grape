"""Issue #3055 B 项：ident 单号的列宽度 + `(repo_key, issue_number)` 键空间收敛。

单：Gitee 字符串 ident（`IKW28R`）单号导致 A2A 交办整族失效，其中 B 项是「列类型
仍是 INT → 静默截断成 0」。口径来源
`docs/weekly-11/issue-3055-issue-ident-hardening.md` §2.3 / §3 / §4-B：

* **MySQL**：`issue.issue_number` / `issue.parent_issue_number` /
  `chat_conversations.issue_number` 从 INT 族 `MODIFY VARCHAR(191)`——非严格
  `sql_mode` 下 INT 列写 ident 会静默截断成 0（同一实体两套口径的直接成因）。
* **SQLite**：**不改列类型**。`INTEGER` 亲和是有意契约（#995/#2072：数字编号落盘即
  int、读侧恒 int；str 参数经亲和无损命中）；非数字 ident 由 SQLite 动态类型原样存
  text，本就没有截断，改 TEXT 亲和反而推翻 #2072 契约。
* **存量 0 号行**：`issue.issue_number = 0`（PR 卡片/无单号占位）改写 `pr:<主键 id>`
  再建唯一索引；否则同仓多行 0 撞 1062，历史代码 `log.warning` 吞异常 → 唯一索引
  静默缺失。`chat_conversations.issue_number = 0` 是「通用会话」合法哨兵，**不动**。
* **失败即 raise**：执行器约定「抛错 = 不盖章，下次启动重试」。这里不做
  「log.warning 后正常返回」的静默降级。

三库边界（`docs/weekly-11/table-owner.tsv`）：两张表都归 **runtime**。非 owner 域
（表不在本库，或在本库是跨库只读视图）抛 `_NonOwnerObject`，执行器跳过且**不盖章**，
等 runtime 的 worker 进程执行——绝不能 log 后正常返回，那会把版本号盖死在 mgmt /
insight 库上，真 owner 库永不执行。
"""
from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import List, Tuple

from src.kernel.config import log

#: 注册迁移版本号（与 src/api/db_setup.py 的 register_migration 字面量必须一致，
#: 由 tests/kernel/test_issue3055_ident_schema_migration.py 锁）。
MIGRATION_VERSION = "202611172020"
MIGRATION_NAME = "issue3055-issue-ident-schema"

ISSUE_TABLE = "issue"
CHAT_TABLE = "chat_conversations"
ISSUE_COLS: Tuple[str, ...] = ("issue_number", "parent_issue_number")
CHAT_COLS: Tuple[str, ...] = ("issue_number",)
UNIQUE_INDEX = "idx_tasks_repo_issue"
UNIQUE_COLS: Tuple[str, ...] = ("repo_key", "issue_number")
#: 选 VARCHAR(191) 而非 TEXT：两列都在索引内，TEXT 无前缀长度不能直接做索引键
#: （MODIFY 报 1170），191 也是 utf8mb4 下不带前缀长度的安全上限。
VARCHAR_LEN = 191
MYSQL_INT_TYPES = ("int", "bigint", "smallint", "tinyint", "mediumint")
MYSQL_TEXT_TYPES = ("text", "tinytext", "mediumtext", "longtext")


@dataclass
class IdentSchemaReport:
    """一轮迁移的执行结果（日志/测试断言用，不参与盖章判定）。"""

    backend: str = ""
    rewritten: int = 0
    widened: List[str] = field(default_factory=list)
    index: str = "skipped"

    def as_dict(self) -> dict:
        return {
            "backend": self.backend,
            "rewritten": self.rewritten,
            "widened": list(self.widened),
            "index": self.index,
        }


def _db_setup():
    """延迟取 db_setup（避免启动期循环导入：db_setup 在文件末尾才 import 本模块）。"""
    from src.api import db_setup

    return db_setup


class IssueIdentColumnsMigration:
    """迁移 202611172020：四步收敛 ident 单号的列宽度与键空间。

    步骤方法（每步都可单独测）：
      ``_rewrite_zero_rows``  存量 0 号行 → ``pr:<id>``
      ``_widen_columns``      MySQL 三列 INT 族 → VARCHAR(191)（SQLite 跳过）
      ``_ensure_unique_index``  `(repo_key, issue_number)` 唯一索引确保存在
      ``_postcheck``          事后复查（不达标 raise，不盖章）

    ``conn`` 可注入（测试用临时 sqlite 连接）；缺省走全局 ``db``。
    """

    def __init__(self, conn=None):
        self._conn = conn

    # ── 入口 ───────────────────────────────────────────────────────────────
    def run(self) -> dict:
        ds = _db_setup()
        conn = self._conn if self._conn is not None else ds.db
        if conn is None:
            raise RuntimeError(
                "[Issue #3055] db 未初始化，无法收敛 issue ident 键空间"
                "（不盖章，下次启动重试）")
        backend = self._backend()
        report = IdentSchemaReport(backend=backend)
        with (ds.db_lock if conn is ds.db else nullcontext()):
            cursor = conn.cursor()
            self._assert_runtime_owner(cursor)
            if backend == "sqlite":
                self._log_sqlite_affinity_contract()
                report.rewritten = self._rewrite_zero_rows(conn)
                report.index = self._ensure_unique_index(conn, backend)
                conn.commit()
                self._postcheck(conn, report)
            else:
                report.widened = self._widen_columns(cursor)
                conn.commit()
                report.rewritten = self._rewrite_zero_rows(conn)
                report.index = self._ensure_unique_index(conn, backend)
                conn.commit()
                self._postcheck(conn, report)
        log.info(
            f"[Issue #3055 {MIGRATION_VERSION}] backend={report.backend} "
            f"widened={report.widened} zero_rows→pr:<id>={report.rewritten} "
            f"{UNIQUE_INDEX}={report.index} 后验通过")
        return report.as_dict()

    # ── 环境 ───────────────────────────────────────────────────────────────
    @staticmethod
    def _backend() -> str:
        from src.kernel.mysql_compat import get_db_backend

        return get_db_backend()

    def _assert_runtime_owner(self, cursor) -> None:
        """三库边界守卫：issue / chat_conversations 必须在本库是**基表**。

        口径同 ``_fn_chat_conversations_status_index``（db_setup.py）：先
        ``_table_present`` + ``_is_base_table``，缺失或视图 → ``_NonOwnerObject``，
        执行器「跳过不盖章」，由 runtime owner 库执行。SQLite 单库恒为基表。
        """
        ds = _db_setup()
        for tbl in (ISSUE_TABLE, CHAT_TABLE):
            present = ds._table_present(cursor, tbl)
            if not present:
                raise ds._NonOwnerObject(
                    f"[Issue #3055] {tbl} 不在本库（非 owner 域），"
                    f"ident 列宽度/键空间收敛由 runtime 库执行")
            if not ds._is_base_table(cursor, tbl):
                raise ds._NonOwnerObject(
                    f"[Issue #3055] {tbl} 在本库是视图（非 owner 域），"
                    f"ident 列宽度/键空间收敛由 runtime 库执行")

    @staticmethod
    def _log_sqlite_affinity_contract() -> None:
        log.info(
            "[Issue #3055] SQLite: 列类型保持 INTEGER 亲和（有意契约：#995/#2072 "
            "数字编号读侧恒 int，非数字 ident 由动态类型原样存 text → 无截断），"
            "本迁移只收敛 0 号占位行与 (repo_key, issue_number) 唯一索引")

    # ── 步骤 1：存量 0 号行 → pr:<id> ──────────────────────────────────────
    @staticmethod
    def pr_ident(row_id) -> str:
        """占位 ident ``pr_<主键 id>``。

        非数字前缀 → 所有 ident 谓词天然不当成「真单号」；按主键派生 → 同仓多行
        互不冲突，且同一行重跑得到同值（幂等）。

        前缀用下划线 ``pr_``（Issue #3055 QA）：全仓 PR 伪 id 判定统一是
        `startswith("pr_")`（issues_center / pr_merged_sre_retry / events / patrol），
        早期迁移写成 ``pr:``（冒号）与之不一致 → 改写后的 0 号行会被当成真单号继续流转。
        """
        return f"pr_{row_id}"

    def _normalize_pr_prefix(self, conn) -> int:
        """把历史 ``pr:<id>`` 归一为 ``pr_<id>``（幂等；兼容早期迁移产物）。"""
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT id, issue_number FROM {ISSUE_TABLE} WHERE issue_number LIKE 'pr:%'")
        rows = cursor.fetchall()
        fixed = 0
        for row_id, value in rows:
            if str(value or "") == self.pr_ident(row_id):
                continue
            cursor.execute(
                f"UPDATE {ISSUE_TABLE} SET issue_number = ? WHERE id = ?",
                (self.pr_ident(row_id), row_id))
            fixed += 1
        if fixed:
            conn.commit()
            log.info(f"[Issue #3055] issue 表 {fixed} 行 pr:<id> 归一为 pr_<id>")
        return fixed

    def _rewrite_zero_rows(self, conn) -> int:
        """``issue.issue_number = 0`` 的行改写 ``pr:<id>``；已是 pr: 前缀的跳过。

        `chat_conversations` 不在处理范围（0 是通用会话合法哨兵）。

        谓词只写 ``issue_number = '0'``，**不能**加 ``OR issue_number = 0``：
        MySQL 的 VARCHAR 列上 `= 0` 会把字符串按前导数字转成 0（'IKW28R' → 0）→
        误匹配真实 ident 行并改写。SQLite 的 INTEGER 亲和则保证字面量 '0' 与 int 0
        等值命中（列亲和把 '0' 转成 0），两后端一条条件都成立。
        """
        self._normalize_pr_prefix(conn)
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT id, issue_number FROM {ISSUE_TABLE} WHERE issue_number = '0'")
        rows = cursor.fetchall()
        # 审计：0 号行在迁移前可能是「INT 列把真实 ident 截断成 0」的根因数据。
        # 唯一索引 (repo_key, issue_number) 要求同仓 0 号行互不相同，**不改名建不起
        # 索引**（1062 被历史 log.warning 吞掉 → 索引静默缺失），所以保留改写，
        # 但把受影响的 id 全量留痕 + 给出回溯 SQL，供人工确认根因后还原。
        if rows:
            ids = [str(r[0]) for r in rows]
            log.warning(json.dumps({
                "event": "issue_zero_rows_rewritten",
                "count": len(ids),
                "ids": ids[:200],
                "ids_truncated": len(ids) > 200,
                "restore_sql": "UPDATE issue SET issue_number=0 WHERE id IN (<上面的 id>)",
                "reason": "unique index (repo_key, issue_number) requires distinct 0-rows",
            }, ensure_ascii=False))
        rewritten = 0
        for row in rows:
            row_id, value = row[0], row[1]
            if value is not None and str(value).startswith(("pr_", "pr:")):
                continue
            cursor.execute(
                f"UPDATE {ISSUE_TABLE} SET issue_number = ? WHERE id = ?",
                (self.pr_ident(row_id), row_id))
            rewritten += 1
        if rewritten:
            conn.commit()
            log.info(f"[Issue #3055] issue 表 {rewritten} 行 issue_number=0 → pr_<主键 id>")
        return rewritten

    # ── 步骤 2：MySQL 列放宽 ───────────────────────────────────────────────
    def _widen_columns(self, cursor) -> List[str]:
        """MySQL 三列 INT 族 → VARCHAR(191)；SQLite 分支不进入。

        保留 NOT NULL / DEFAULT 语义（`MODIFY` 不带默认值会把原默认清掉——
        `chat_conversations.issue_number` 的 `DEFAULT 0` 必须保留）。视图走基表。
        """
        widened: List[str] = []
        for tbl, cols in ((ISSUE_TABLE, ISSUE_COLS), (CHAT_TABLE, CHAT_COLS)):
            resolved = self._resolve_mysql_table(cursor, tbl)
            schema, table, qual = resolved
            for col in cols:
                if self._widen_column(cursor, schema, table, qual, col):
                    widened.append(f"{table}.{col}")
        return widened

    @staticmethod
    def _current_db(cursor) -> str:
        """当前连接的库名（information_schema 查询用 schema）。"""
        cursor.execute("SELECT DATABASE()")
        row = cursor.fetchone()
        return (row[0] if row else "") or ""

    @classmethod
    def _resolve_mysql_table(cls, cursor, table: str) -> Tuple[str, str, str]:
        """解析 table 的真实落点，返回 (schema, table, 反引号限定名)。

        跨 schema DEFINER 视图上直接 ALTER / CREATE INDEX 报 1347，沿用 db_setup
        的 ``_mysql_view_base_table`` 解析口径落到基表。
        """
        ds = _db_setup()
        base = ds._mysql_view_base_table(cursor, table)
        if base:
            base_schema, base_table = base
            schema = base_schema or cls._current_db(cursor)
            qual = f"`{base_schema}`.`{base_table}`" if base_schema else f"`{base_table}`"
            log.warning(
                f"[Issue #3055] MySQL: '{table}' 是视图 → 落到基表 {qual} 执行 DDL"
                f"（对视图名 ALTER/CREATE INDEX 报 1347）")
            return schema, base_table, qual
        return cls._current_db(cursor), table, f"`{table}`"

    @staticmethod
    def _default_clause(default) -> str:
        """information_schema.COLUMN_DEFAULT → VARCHAR 列的 DEFAULT 子句。"""
        value = ("" if default is None else str(default)).strip()
        if not value or value.upper() == "NULL" or "(" in value:
            return ""
        return " DEFAULT '" + value.replace("'", "''") + "'"

    def _widen_column(self, cursor, schema, table, qual, col) -> bool:
        """单列 INT 族 → VARCHAR(191)；已非 INT 族直接跳过（幂等，重跑零 DDL）。"""
        cursor.execute(
            "SELECT IS_NULLABLE, COLUMN_DEFAULT FROM information_schema.columns "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s "
            "AND DATA_TYPE IN ('int','bigint','smallint','tinyint','mediumint')",
            (schema, table, col))
        row = cursor.fetchone()
        if not row:
            return False
        suffix = " NOT NULL" if (row[0] or "").upper() == "NO" else ""
        suffix += self._default_clause(row[1] if len(row) > 1 else None)
        # 为什么不能写 ALGORITHM=INPLACE, LOCK=NONE（评审建议过一次，实测不可行）：
        # 本语句是 INT → VARCHAR(191) 的**数据类型转换**，MySQL 8.0 只支持 COPY 算法，
        # 显式声明 INPLACE 会直接报 ER_ALTER_OPERATION_NOT_SUPPORTED(1846) 让迁移失败；
        # 能 INPLACE 的只是纯加长/加索引。这里显式写 COPY + LOCK=SHARED，把「会重建表、
        # 期间允许并发读、写要等」这件事摆在字面上，避免运维误以为是瞬时 DDL。
        cursor.execute(
            f"ALTER TABLE {qual} MODIFY `{col}` VARCHAR({VARCHAR_LEN}){suffix} "
            f"ALGORITHM=COPY, LOCK=SHARED")
        log.info(f"[Issue #3055] MySQL: {table}.{col} INT → VARCHAR({VARCHAR_LEN})")
        return True

    # ── 步骤 3：唯一索引 ──────────────────────────────────────────────────
    def _ensure_unique_index(self, conn, backend: str) -> str:
        return (self._ensure_unique_index_mysql(conn) if backend != "sqlite"
                else self._ensure_unique_index_sqlite(conn))

    def _ensure_unique_index_sqlite(self, conn) -> str:
        """SQLite：同名普通索引/缺失 → 重建 UNIQUE；已 UNIQUE 零改动。

        SQLite 无 `CREATE UNIQUE INDEX IF NOT EXISTS` 语义，先 DROP 再建（DROP 只在
        索引非 UNIQUE 时发生）。创建失败（重复键未清理）向上抛 → 不盖章、下次重试。
        """
        cursor = conn.cursor()
        sql = self._sqlite_index_sql(cursor)
        if sql and "UNIQUE" in sql.upper():
            return "exists"
        cursor.execute(f"DROP INDEX IF EXISTS {UNIQUE_INDEX}")
        try:
            cursor.execute(
                f"CREATE UNIQUE INDEX {UNIQUE_INDEX} ON {ISSUE_TABLE}"
                f"({', '.join(UNIQUE_COLS)})")
        except Exception as e:
            raise RuntimeError(
                f"[Issue #3055] SQLite: 重建 UNIQUE INDEX {UNIQUE_INDEX}"
                f"({', '.join(UNIQUE_COLS)}) 失败（存在重复键，迁移不盖章、"
                f"下次启动重试）: {e}") from e
        return "created"

    def _ensure_unique_index_mysql(self, conn) -> str:
        """MySQL：显式 DROP 同名普通索引再建 UNIQUE，失败向上抛。

        历史缺口：代码里写过 `CREATE UNIQUE INDEX IF NOT EXISTS`，但兼容层见同名
        索引已存在就跳过（含同名**普通**索引），1062 又被 `log.warning` 吞 → 唯一性
        静默缺失。repo_key 在 `_ISSUE_DDL` 里是 TEXT，唯一键需前缀长度（取 191，
        与 mysql_compat 同口径）；issue_number 已放宽 VARCHAR(191)，无需前缀。
        """
        cursor = conn.cursor()
        schema, table, qual = self._resolve_mysql_table(cursor, ISSUE_TABLE)
        cursor.execute(
            "SELECT NON_UNIQUE FROM information_schema.statistics "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND INDEX_NAME=%s LIMIT 1",
            (schema, table, UNIQUE_INDEX))
        row = cursor.fetchone()
        if row and int(row[0]) == 0:
            return "exists"
        if row:
            cursor.execute(f"DROP INDEX {UNIQUE_INDEX} ON {qual}")
        specs = []
        for col in UNIQUE_COLS:
            cursor.execute(
                "SELECT DATA_TYPE FROM information_schema.columns "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s",
                (schema, table, col))
            info = cursor.fetchone()
            data_type = ((info[0] if info else "") or "").lower()
            specs.append(f"`{col}`({VARCHAR_LEN})" if data_type in MYSQL_TEXT_TYPES
                         else f"`{col}`")
        cursor.execute(
            f"CREATE UNIQUE INDEX {UNIQUE_INDEX} ON {qual} ({', '.join(specs)})")
        return "created"

    @staticmethod
    def _sqlite_index_sql(cursor) -> str:
        row = cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (UNIQUE_INDEX,)).fetchone()
        return (row[0] or "") if row else ""

    # ── 步骤 4：事后复查（不达标 raise，不盖章） ──────────────────────────
    def _postcheck(self, conn, report: IdentSchemaReport) -> None:
        if report.backend == "sqlite":
            self._postcheck_sqlite(conn)
        else:
            self._postcheck_mysql(conn)

    def _postcheck_sqlite(self, conn) -> None:
        cursor = conn.cursor()
        dups = cursor.execute(
            f"SELECT repo_key, issue_number, COUNT(*) FROM {ISSUE_TABLE} "
            "GROUP BY repo_key, issue_number HAVING COUNT(*) > 1 LIMIT 5").fetchall()
        if dups:
            raise RuntimeError(
                f"[Issue #3055] SQLite 后验失败：issue 表仍有重复键 {dups[:5]}"
                f"（唯一索引语义未生效，不盖章）")
        sql = self._sqlite_index_sql(cursor)
        if not (sql and "UNIQUE" in sql.upper()):
            raise RuntimeError(
                f"[Issue #3055] SQLite 后验失败：{UNIQUE_INDEX} 不是唯一索引或缺失"
                f"（不盖章，下次启动重试）")

    def _postcheck_mysql(self, conn) -> None:
        """MySQL 后验：三列 DATA_TYPE 非 INT 族 + 唯一索引在位。"""
        cursor = conn.cursor()
        for tbl, cols in ((ISSUE_TABLE, ISSUE_COLS), (CHAT_TABLE, CHAT_COLS)):
            schema, table, _ = self._resolve_mysql_table(cursor, tbl)
            for col in cols:
                cursor.execute(
                    "SELECT DATA_TYPE FROM information_schema.columns "
                    "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s",
                    (schema, table, col))
                row = cursor.fetchone()
                if not row:
                    raise RuntimeError(
                        f"[Issue #3055] MySQL 后验失败：{table}.{col} 不存在（不盖章）")
                data_type = (row[0] or "").lower()
                if data_type in MYSQL_INT_TYPES:
                    raise RuntimeError(
                        f"[Issue #3055] MySQL 后验失败：{table}.{col} 仍为 {data_type}"
                        f"（MODIFY VARCHAR 未生效/无 ALTER 权限，不盖章、下次启动重试）")
        schema, table, _ = self._resolve_mysql_table(cursor, ISSUE_TABLE)
        cursor.execute(
            "SELECT NON_UNIQUE FROM information_schema.statistics "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND INDEX_NAME=%s LIMIT 1",
            (schema, table, UNIQUE_INDEX))
        row = cursor.fetchone()
        if not row or int(row[0]) != 0:
            raise RuntimeError(
                f"[Issue #3055] MySQL 后验失败：{UNIQUE_INDEX} 在 {table} 上不是唯一"
                f"索引或缺失（不盖章、下次启动重试）")