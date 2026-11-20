"""D105 主键 UUID 化通用迁移器（幂等 / 启动期一次性 / SQLite+MySQL 双后端）。

抽象动机：第一批（external_identities）、第二批（grape_accounts + 29 张引用表）、
第三批（organization/org_member，待做）是同一套迁移形态——主键整型→字符串化保留、
引用列 INTEGER→VARCHAR(36) 放宽。收敛为一个声明式对象，每批只声明"哪张表、
哪些引用列、新 DDL/拷贝列"，执行与幂等判定全部复用，不再每批手写一遍双后端分支。

幂等判据（三态，二次跑零写入）：
- fresh 库（表不存在）→ 直接返回，走新 DDL
- 已是目标形态（SQLite 无 AUTOINCREMENT / MySQL id 已 varchar）→ 直接返回
- 存量形态 → 执行重建/放宽

耗时说明（MySQL）：MODIFY int→varchar 走 ALGORITHM=COPY 整表重建+重建索引，
阻塞写；grape 各表行数在万级以下，单表亚秒级，批内串行合计秒级~几十秒，
建议低峰执行；单表到百万行级再考虑 pt-online-schema-change。
注意：本模块禁止模块级 `from src.kernel.session_manager import db`——那会把连接对象在
首次 import 时固化，测试 reload/多库切换场景下会写到错误文件（实锤踩过）。
一律在方法内调用期取当前 sm.db。
"""
from src.kernel.config import log


class UuidPkMigration:
    """声明式 UUID 主键迁移：一张主键表 + 可选的 user_id 族引用列放宽。"""

    def __init__(self, pk_table: str, sqlite_create_sql: str = None,
                 sqlite_copy_sql: str = None,
                 uid_widen_cols: tuple = (), all_uid_cols: tuple = (),
                 sqlite_uid_pk_cols: tuple = ()):
        """
        pk_table:          主键表名（如 grape_accounts / external_identities）
        sqlite_create_sql: SQLite 重建表用的目标形态 CREATE TABLE（TEXT PRIMARY KEY）；
                           **可省略**——缺省时自省生成（sqlite_master DDL 原文改造 +
                           PRAGMA 列清单，见 _auto_sqlite_plan）
        sqlite_copy_sql:   SQLite 拷贝 INSERT...SELECT（id 用 CAST(id AS TEXT) 保值）；
                           同样可省略，与 create 一起自省生成
        uid_widen_cols:    本批要放宽的 user_id 族列名（MySQL 侧 information_schema 扫描目标；
                           MySQL 侧本就全自省，此参数仅圈定扫描范围）
        all_uid_cols:      引用列全族（仅用于注释/日志，不参与判定）
        sqlite_uid_pk_cols: SQLite 侧「引用列即 INTEGER PRIMARY KEY（rowid 别名）」的列名
                           （如 unified_user_mapping.user_id）。rowid 别名列强类型，插 UUID
                           字符串报 datatype mismatch，普通 INTEGER 列的亲和规则不适用——
                           必须整表重建为 TEXT PRIMARY KEY。此类表是纯引用表（无独立 id 主键），
                           与 pk_table 的主键改造互斥；仅支持自省模式（勿与声明式 DDL 混用）
        """
        self.pk_table = pk_table
        self.sqlite_create_sql = sqlite_create_sql
        self.sqlite_copy_sql = sqlite_copy_sql
        self.uid_widen_cols = tuple(uid_widen_cols)
        self.all_uid_cols = tuple(all_uid_cols)
        self.sqlite_uid_pk_cols = tuple(sqlite_uid_pk_cols)

    # ---- 后端探测 -------------------------------------------------------
    @staticmethod
    def _is_mysql() -> bool:
        try:
            from src.kernel.session_manager import _is_mysql_backend
            return _is_mysql_backend()
        except Exception:
            return False

    # ---- 入口 -----------------------------------------------------------
    def run(self) -> None:
        from src.kernel.session_manager import db_lock        # 调用期取用（勿模块级固化）
        with db_lock:
            if self._is_mysql():
                self._run_mysql()
            else:
                self._run_sqlite()

    # ---- MySQL：ALTER MODIFY（COPY 重建，逐列幂等）-----------------------
    def _run_mysql(self) -> None:
        from src.kernel.session_manager import db             # 调用期取用
        c = db.cursor()
        # ① 主键列型——判定"字符串且长度>=36"：varchar 但长度不足会静默截断 UUID
        #（非严格模式截 0/截断比报错更危险），必须视为未迁移形态重跑 MODIFY。
        # 纯引用表（sqlite_uid_pk_cols 模式，如 unified_user_mapping）无独立 id 列，跳过；
        # 其 user_id 族列已由 0002 的 information_schema 扫描覆盖（INTEGER→VARCHAR(36)）
        if self.sqlite_uid_pk_cols:
            c.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='%s' AND COLUMN_NAME='id'" % self.pk_table)
            if not c.fetchone()[0]:
                return                  # 纯引用表：MySQL 侧无需动作
        c.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='%s'"
            " AND COLUMN_NAME='id' AND DATA_TYPE='varchar'"
            " AND CHARACTER_MAXIMUM_LENGTH>=36" % self.pk_table)
        if not c.fetchone()[0]:
            c.execute(f"ALTER TABLE {self.pk_table} MODIFY id VARCHAR(36) NOT NULL")
            db.commit()
            log.info(f"uuid-pk migration: {self.pk_table}.id -> VARCHAR(36) (MySQL)")
        # ② 引用列放宽（逐列判定，跳过已 varchar>=36——滚动升级期半迁移状态幂等；
        # COPY 阻塞该表写入，预检打行数日志，行数异常大的表提前暴露、低峰分批）
        if self.uid_widen_cols:
            in_list = ",".join("'%s'" % col for col in self.uid_widen_cols)
            c.execute(
                "SELECT TABLE_NAME, COLUMN_NAME, IS_NULLABLE FROM information_schema.columns "
                f"WHERE TABLE_SCHEMA=DATABASE() AND COLUMN_NAME IN ({in_list})"
                " AND DATA_TYPE IN ('int','bigint','smallint','tinyint','mediumint')")
            targets = c.fetchall()
            if targets:
                try:
                    from src.kernel.session_manager import _db_path, _backup_db
                    bak = _backup_db(_db_path, tag="pre-uuid-uid-widen")
                    log.info(f"uuid-pk migration: pre-widen backup -> {bak}")
                except Exception as e:
                    log.warning(f"uuid-pk migration: pre-widen backup failed (继续迁移): {e}")
            for tbl, col, nullable in targets:
                try:
                    c.execute(f"SELECT COUNT(*) FROM {tbl}")
                    log.info(f"uuid-pk migration pre-check: {tbl}.{col} rows={c.fetchone()[0]}"
                             " (COPY 将阻塞该表写入)")
                except Exception as _e:
                    log.warning(f"uuid-pk migration pre-check: {tbl} count failed: {_e}")
                suffix = " NOT NULL" if nullable == "NO" else ""
                c.execute(f"ALTER TABLE {tbl} MODIFY {col} VARCHAR(36){suffix}")
            if targets:
                db.commit()
                log.info(f"uuid-pk migration: widened {len(targets)} user-id cols -> VARCHAR(36) (MySQL)")

    # ---- SQLite：重建表（CAST 保值；引用列不动，INTEGER 亲和可存 TEXT）----
    def _run_sqlite(self) -> None:
        from src.kernel.session_manager import db             # 调用期取用
        c = db.cursor()
        c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (self.pk_table,))
        row = c.fetchone()
        create_sql = (row[0] or "") if row else ""
        if self.pk_table not in create_sql:
            return                      # fresh 库走新 DDL
        if self.sqlite_uid_pk_cols:
            # 纯引用表模式：幂等判定不看 AUTOINCREMENT（此表本就没有），
            # 看 PRAGMA 里 uid pk 列是否已全部 TEXT 化
            info = {r[1]: (r[2] or "").lower()
                    for r in c.execute(f"PRAGMA table_info({self.pk_table})").fetchall()}
            if all(info.get(col) == "text" for col in self.sqlite_uid_pk_cols):
                return                  # 已是目标形态
            if self.sqlite_create_sql is not None or self.sqlite_copy_sql is not None:
                raise ValueError(f"{self.pk_table}: sqlite_uid_pk_cols 模式仅支持自省，"
                                 "勿声明 DDL/拷贝 SQL（uid pk 列与 id 主键改造互斥）")
        elif "AUTOINCREMENT" not in create_sql:
            return                      # 已是目标形态（勿用去空格比对，原文含空格会恒不匹配）
        # 部署全自动守则：判定过幂等（必有改动）后先自动备份再重建；备份失败不阻断迁移
        try:
            from src.kernel.session_manager import _db_path, _backup_db
            bak = _backup_db(_db_path, tag=f"pre-uuid-{self.pk_table}")
            log.info(f"uuid-pk migration: pre-rebuild backup -> {bak}")
        except Exception as e:
            log.warning(f"uuid-pk migration: pre-rebuild backup failed (继续迁移): {e}")
        old = f"{self.pk_table}_uuidmig_old"
        # 自省模式：未显式声明 DDL 时从 sqlite_master 原文 + PRAGMA 自动生成——
        # DDL 原文改造保证约束/默认值/UNIQUE 零丢失；显式索引（CREATE INDEX）
        # 挂在被 RENAME 的旧表上，DROP 前必须捞出原文、拷贝完成后重建
        create_stmt = self.sqlite_create_sql
        copy_stmt = self.sqlite_copy_sql
        index_sqls = []
        if create_stmt is None or copy_stmt is None:
            gen_create, gen_copy, index_sqls = self._auto_sqlite_plan(
                c, create_sql, uid_pk_cols=self.sqlite_uid_pk_cols)
            create_stmt = create_stmt or gen_create
            copy_stmt = copy_stmt or gen_copy
        else:
            copy_stmt = copy_stmt.replace("__OLD__", old)   # 声明式：占位符替换
        c.execute(f"ALTER TABLE {self.pk_table} RENAME TO {old}")
        c.execute(create_stmt)
        c.execute(copy_stmt)
        from src.api.db_setup import _destructive_ddl  # 调用期取用（db_setup 反向调用本模块，勿模块级固化）
        _destructive_ddl(c, f"DROP TABLE {old}", kind_hint="cleanup")
        for isql in index_sqls:
            # RENAME 会把索引 SQL 里的表名改写为旧表名，重建前替换回新表名
            isql = isql.replace(f'"{old}"', f'"{self.pk_table}"').replace(f"'{old}'", f"'{self.pk_table}'")
            try:
                c.execute(isql)
            except Exception as e:
                log.warning(f"uuid-pk migration: index rebuild failed on {self.pk_table}: {e}")
        db.commit()
        log.info(f"uuid-pk migration: {self.pk_table} rebuilt with TEXT PK, "
                 f"legacy ids stringified (SQLite, {'auto' if self.sqlite_create_sql is None else 'declared'})")

    def _auto_sqlite_plan(self, c, create_sql: str, uid_pk_cols: tuple = ()) -> tuple[str, str, list[str]]:
        """自省生成重建三件套：CREATE（DDL 原文改主键形态）/ 拷贝 SELECT（PRAGMA 列清单，
        仅 id 套 CAST 保值）/ 显式索引原文列表。

        uid_pk_cols 非空时为「纯引用表」模式：DDL 中「<col> INTEGER PRIMARY KEY」形态的
        rowid 别名列改为 TEXT PRIMARY KEY（一个表只能有一个 PK，此类表无独立 id 主键，
        AUTOINCREMENT 改造不适用）；拷贝 SELECT 对这些列同样套 CAST 保字符串化。
        """
        import re as _re
        # ① DDL 原文改造（保留全部约束/默认值/UNIQUE——比手抄/PRAGMA 还原精度高）
        new_create = create_sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT",
                                        "TEXT PRIMARY KEY", 1)
        for col in uid_pk_cols:
            new_create = new_create.replace(f"{col} INTEGER PRIMARY KEY",
                                            f"{col} TEXT PRIMARY KEY", 1)
        if new_create == create_sql:
            raise ValueError(f"{self.pk_table}: DDL 无 INTEGER PRIMARY KEY（AUTOINCREMENT 或 "
                             f"uid pk 列 {uid_pk_cols}），自省改造失败（形态与幂等判定不一致）")
        # ② 列清单（PRAGMA 顺序即物理列序）——id 与 uid pk 列 CAST 保字符串化
        cast_cols = {"id", *uid_pk_cols}
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({self.pk_table})").fetchall()]
        if not cols:
            raise ValueError(f"{self.pk_table}: PRAGMA table_info 空列清单")
        sel = ", ".join(f"CAST({col} AS TEXT)" if col in cast_cols else col for col in cols)
        copy_sql = (f"INSERT INTO {self.pk_table} ({', '.join(cols)}) "
                    f"SELECT {sel} FROM {self.pk_table}_uuidmig_old")
        # ③ 显式索引原文（sqlite_autoindex 开头的是 UNIQUE 约束自带索引，DDL 已含，跳过）
        index_sqls = [r[0] for r in c.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? "
            "AND sql IS NOT NULL AND name NOT LIKE 'sqlite_autoindex%'", (self.pk_table,)).fetchall()]
        return new_create, copy_sql, index_sqls
