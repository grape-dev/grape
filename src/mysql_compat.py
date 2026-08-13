"""MySQL 兼容层：模拟 sqlite3.Connection/Cursor 接口，自动把 SQLite 方言翻译为 MySQL 方言。

设计目标：grape 项目 26 个模块 `from src.session_manager import db, db_lock` 直接使用
sqlite3 风格 API（db.execute / db.cursor / cursor.execute(fetchone|fetchall) / db.commit /
c.lastrowid / c.rowcount / c.description / PRAGMA table_info / sqlite_master 查询）。
本模块提供相同接口的 MySQL 实现，并在 execute 时做 SQL 方言翻译，业务模块零改动。

启用方式（开关）：configs/db-backend.json 的 "backend" 字段（sqlite/mysql），
也可用环境变量 DEVOPS_DB_BACKEND=mysql 覆盖（配合 DEVOPS_DB_HOST/PORT/USER/PASSWORD/NAME，
或直接用 DEVOPS_DB_URL=mysql://user:pass@host:port/dbname）。默认 sqlite。
"""
import json
import os
import re
import threading

import pymysql

# ====== 配置读取 ======

_DB_BACKEND_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "configs", "db-backend.json"
)


def load_db_backend_config() -> dict:
    """读取 configs/db-backend.json 开关配置（不存在时返回默认 sqlite）。"""
    default = {"backend": "sqlite", "mysql": {}}
    try:
        if os.path.exists(_DB_BACKEND_CONFIG_PATH):
            with open(_DB_BACKEND_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
            return {
                "backend": str(cfg.get("backend", "sqlite")).lower(),
                "mysql": cfg.get("mysql") or {},
            }
    except Exception:
        pass
    return default


def get_db_backend() -> str:
    """当前数据库后端：环境变量 DEVOPS_DB_BACKEND 优先，其次配置文件，默认 sqlite。"""
    env = os.environ.get("DEVOPS_DB_BACKEND", "").lower()
    if env in ("mysql", "rds", "1", "true"):
        return "mysql"
    if env in ("sqlite", "0", "false"):
        return "sqlite"
    return load_db_backend_config().get("backend", "sqlite")


def mysql_config() -> dict:
    """从配置文件读取 MySQL 连接配置（环境变量 DEVOPS_DB_* 可覆盖）。"""
    cfg = load_db_backend_config().get("mysql") or {}
    url = os.environ.get("DEVOPS_DB_URL", "")
    if url:
        # mysql://user:pass@host:port/dbname
        m = re.match(r"mysql://([^:]+):([^@]*)@([^:/]+):?(\d+)?/([^?]+)", url)
        if m:
            user, pwd, host, port, name = m.groups()
            return {
                "host": host,
                "port": int(port or 3306),
                "user": user,
                "password": pwd,
                "database": name,
                "charset": "utf8mb4",
            }
    return {
        "host": os.environ.get("DEVOPS_DB_HOST", cfg.get("host", "127.0.0.1")),
        "port": int(os.environ.get("DEVOPS_DB_PORT", cfg.get("port", 3306))),
        "user": os.environ.get("DEVOPS_DB_USER", cfg.get("user", "grape")),
        "password": os.environ.get("DEVOPS_DB_PASSWORD", cfg.get("password", "")),
        "database": os.environ.get("DEVOPS_DB_NAME", cfg.get("database", "grape_db")),
        "charset": "utf8mb4",
    }


# ====== SQL 方言翻译 ======

_PRAGMA_TABLE_INFO_RE = re.compile(r"^\s*PRAGMA\s+table_info\(\s*([`\"']?)(\w+)\1\s*\)\s*$", re.I)
_SQLITE_MASTER_NAME_RE = re.compile(
    r"^\s*SELECT\s+name\s+FROM\s+sqlite_master\s+WHERE\s+type\s*=\s*'table'\s*$", re.I
)
_SQLITE_MASTER_SQL_RE = re.compile(
    r"^\s*SELECT\s+sql\s+FROM\s+sqlite_master\s+WHERE\s+type\s*=\s*'table'\s+AND\s+name\s*=\s*['\"]([\w]+)['\"]\s*$",
    re.I,
)
_CREATE_UNIQUE_INDEX_RE = re.compile(
    r"^\s*CREATE\s+UNIQUE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+ON\s+(\w+)\s*\((.+)\)\s*$", re.I
)
_CREATE_INDEX_RE = re.compile(
    r"^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+ON\s+(\w+)\s*\((.+)\)\s*$", re.I
)
_PRAGMA_IGNORE_RE = re.compile(
    r"^\s*PRAGMA\s+(journal_mode|busy_timeout|synchronous|foreign_keys|locking_mode|cache_size|wal_autocheckpoint|temp_store)",
    re.I,
)
_PRAGMA_INDEX_LIST_RE = re.compile(r"^\s*PRAGMA\s+index_list\(\s*([`\"\w]+)\s*\)\s*$", re.I)
_PRAGMA_INDEX_INFO_RE = re.compile(r"^\s*PRAGMA\s+index_info\(\s*([`\"\w]+)\s*\)\s*$", re.I)
# 仅处理 'gitcode:' || col 拼接模式（代码中唯一用法）
_CONCAT_RE = re.compile(r"'gitcode:'\s*\|\|\s*([A-Za-z_][A-Za-z0-9_.]*)")

# CREATE TABLE 语句中的列行级翻译
_COL_TEXT_PK_RE = re.compile(r"^(\s*)([`\"\w]+)\s+TEXT\s+PRIMARY\s+KEY\b", re.I)
_COL_TEXT_DEFAULT_RE = re.compile(
    r"^(\s*)([`\"\w]+)\s+TEXT\s+NOT\s+NULL\s+DEFAULT\s+'((?:[^'\\]|\\.)*)'(?=[,\s)]|$)", re.I
)
_COL_TEXT_DEFAULT2_RE = re.compile(
    r"^(\s*)([`\"\w]+)\s+TEXT\s+DEFAULT\s+'((?:[^'\\]|\\.)*)'(?=[,\s)]|$)", re.I
)
_COL_INT_PK_AUTOINC_RE = re.compile(
    r"^(\s*)([`\"\w]+)\s+INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", re.I
)
_COL_DATETIME_DEFAULT_RE = re.compile(
    r"(datetime\('now'\))", re.I
)


def _split_create_columns(body: str) -> list[str]:
    """按逗号分割 CREATE TABLE 列定义（括号深度感知，剥离行尾 -- 注释）。"""
    cols, depth, cur, in_str = [], 0, "", None
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if in_str:
            cur += ch
            if ch == in_str and body[i - 1:i] != "\\":
                in_str = None
            i += 1
            continue
        if ch in "'\"":
            in_str = ch
            cur += ch
        elif ch == "-" and body[i:i + 2] == "--":
            # 行尾注释：跳到行尾（注释内容不影响列结构）
            j = body.find("\n", i)
            if j == -1:
                break
            cur += " "
            i = j
            continue
        elif ch == "(":
            depth += 1
            cur += ch
        elif ch == ")":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            cols.append(cur.strip())
            cur = ""
        else:
            cur += ch
        i += 1
    if cur.strip():
        cols.append(cur.strip())
    return cols


def _translate_create_table(sql: str) -> str:
    """翻译 CREATE TABLE：TEXT 主键/唯一列→VARCHAR(191)、AUTOINCREMENT→AUTO_INCREMENT、
    TEXT DEFAULT 字面量→表达式、datetime('now')→CAST(NOW() AS CHAR)。"""
    m = re.match(r"^\s*(CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"\w]+\s*\()", sql, re.I)
    if not m:
        return sql
    head = m.group(1)
    body = sql[m.end():]
    # 去掉结尾分号与右括号（容忍尾部空白/换行）
    trailing = ""
    stripped = body.rstrip()
    if stripped.endswith(";"):
        trailing = ";"
        stripped = stripped[:-1].rstrip()
    if stripped.endswith(")"):
        stripped = stripped[:-1]
    else:
        return sql  # 无法解析，原样返回（宁可不翻译也不破坏）
    body = stripped
    cols = _split_create_columns(body)

    # 收集索引列（复合 PRIMARY KEY / UNIQUE 中的列名）→ 需要 TEXT→VARCHAR
    index_cols = set()
    for c in cols:
        cm = re.match(r"(?:CONSTRAINT\s+\w+\s+)?(?:PRIMARY\s+KEY|UNIQUE)\s*\((.*)\)\s*$", c, re.I)
        if cm:
            for name in re.findall(r"[`\"\w]+", cm.group(1)):
                if name not in ("PRIMARY", "KEY", "UNIQUE"):
                    index_cols.add(name.strip("`\""))

    out = []
    for c in cols:
        is_constraint = bool(re.match(r"^(?:CONSTRAINT\s+\w+\s+)?(?:PRIMARY\s+KEY|UNIQUE)\s*\(", c, re.I))
        # 复合 PRIMARY KEY / UNIQUE 约束行：原样保留
        if is_constraint:
            out.append(c)
            continue
        # 普通列：提取列名与类型
        name_m = re.match(r"^([`\"\w]+)", c)
        col_name = name_m.group(1).strip("`\"") if name_m else ""
        # 索引列（复合键成员）中的 TEXT → VARCHAR(191)
        if col_name in index_cols:
            c = re.sub(r"^([`\"\w]+\s+)TEXT\b", r"\1VARCHAR(191)", c, count=1, flags=re.I)
        # 行内 UNIQUE 的 TEXT 列 → VARCHAR(191)（如 username TEXT UNIQUE NOT NULL /
        # name TEXT NOT NULL UNIQUE —— TEXT 与 UNIQUE 之间允许列修饰符，否则 MySQL 1170）
        if re.search(r"\bTEXT\b[^,]*\bUNIQUE\b", c, re.I) and not re.search(r"\bUNIQUE\s*\(", c, re.I):
            c = re.sub(r"\bTEXT\b", "VARCHAR(191)", c, count=1, flags=re.I)
        # TEXT PRIMARY KEY（单列主键）
        c = _COL_TEXT_PK_RE.sub(lambda mm: f"{mm.group(1)}{mm.group(2)} VARCHAR(191) PRIMARY KEY", c)
        # INTEGER PRIMARY KEY AUTOINCREMENT → INT AUTO_INCREMENT PRIMARY KEY
        c = _COL_INT_PK_AUTOINC_RE.sub(
            lambda mm: f"{mm.group(1)}{mm.group(2)} INT AUTO_INCREMENT PRIMARY KEY", c
        )
        # TEXT NOT NULL DEFAULT 'x' / TEXT DEFAULT 'x' → 表达式默认值（8.0.13+）
        if _COL_TEXT_DEFAULT_RE.search(c) or _COL_TEXT_DEFAULT2_RE.search(c):
            if re.match(r"^[`\"\w]+\s+TEXT\b", c, re.I) and "PRIMARY KEY" not in c.upper():
                def _expr_default(mm):
                    prefix, name, val = mm.group(1), mm.group(2), mm.group(3)
                    not_null = " NOT NULL" if "NOT NULL" in mm.string[:mm.start()].upper() or "NOT NULL" in c.upper() else ""
                    return f"{prefix}{name} TEXT{not_null} DEFAULT ('{val}')"
                c = _COL_TEXT_DEFAULT_RE.sub(_expr_default, c)
                c = _COL_TEXT_DEFAULT2_RE.sub(_expr_default, c)
        # datetime('now') → CAST(NOW() AS CHAR)（字符串语义与 SQLite 一致，且避免 % 占位冲突）
        c = _COL_DATETIME_DEFAULT_RE.sub("CAST(NOW() AS CHAR)", c)
        out.append(c)
    return head + ", ".join(out) + ")" + trailing


def translate_sql(sql: str) -> str:
    """把 SQLite 方言 SQL 翻译为 MySQL 方言。"""
    if not sql or not sql.strip():
        return sql
    # PRAGMA table_info(X) → information_schema 查询（由 Cursor 特判执行）
    if _PRAGMA_TABLE_INFO_RE.match(sql):
        return sql  # 标记：Cursor.execute 特判
    # PRAGMA index_list / index_info → information_schema 查询（由 Cursor 特判执行）
    if _PRAGMA_INDEX_LIST_RE.match(sql) or _PRAGMA_INDEX_INFO_RE.match(sql):
        return sql  # 标记：Cursor.execute 特判
    # PRAGMA 其余（journal_mode 等）→ no-op
    if _PRAGMA_IGNORE_RE.match(sql):
        return "__NOOP__"
    # sqlite_master 表名列举 → SHOW TABLES
    if _SQLITE_MASTER_NAME_RE.match(sql):
        return "SHOW TABLES"
    # sqlite_master 取建表 SQL → SHOW CREATE TABLE（结果重排由 Cursor 特判）
    if _SQLITE_MASTER_SQL_RE.match(sql):
        return sql  # 标记：Cursor.execute 特判
    # CREATE [UNIQUE] INDEX IF NOT EXISTS → 特判（MySQL 不支持 IF NOT EXISTS）
    if _CREATE_INDEX_RE.match(sql):
        return sql  # 标记：Cursor.execute 特判

    # CREATE TABLE 深度翻译
    if re.match(r"^\s*CREATE\s+TABLE\b", sql, re.I):
        sql = _translate_create_table(sql)

    # INSERT OR REPLACE INTO → REPLACE INTO
    sql = re.sub(r"\bINSERT\s+OR\s+REPLACE\s+INTO\b", "REPLACE INTO", sql, flags=re.I)
    # INSERT OR IGNORE INTO → INSERT IGNORE INTO
    sql = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT IGNORE INTO", sql, flags=re.I)
    # datetime('now') → CAST(NOW() AS CHAR)
    sql = _COL_DATETIME_DEFAULT_RE.sub("CAST(NOW() AS CHAR)", sql)
    # 'gitcode:' || col → CONCAT('gitcode:', col)
    sql = _CONCAT_RE.sub(r"CONCAT('gitcode:', \1)", sql)
    # 参数占位符 ? → %s（pymysql 风格）
    sql = _replace_qmarks(sql)
    # meta 表的 key 列是 MySQL 保留字 → 反引号（仅涉及 meta 表的 SQL；跳过字符串字面量）
    if re.search(r"\bmeta\b", sql, re.I):
        sql = _backtick_key(sql)
    # agent_runtimes.binary 列是 MySQL 保留字 → 反引号（仅 CREATE/ALTER 语句中的列定义）
    if re.match(r"^\s*(CREATE\s+TABLE|ALTER\s+TABLE)", sql, re.I):
        sql = re.sub(r"(?<![`\w])binary(?![`\w])", "`binary`", sql, flags=re.I)
    # ALTER TABLE ... ADD COLUMN <col> TEXT [NOT NULL] DEFAULT '<lit>' → DEFAULT ('<lit>')
    # （MySQL 8.0.13 前 TEXT 列不允许 DEFAULT 字面量，报 1101；CREATE TABLE 已在
    #   _translate_create_table 处理，ADD COLUMN 走不到那里——实测 db_setup _ensure_column
    #   补列 knowledge_files/subscribe_events 时 1101 失败被 except 吞掉，列缺失）
    if re.match(r"^\s*ALTER\s+TABLE", sql, re.I):
        sql = re.sub(
            r"(\bADD\s+COLUMN\s+[`\"\w]+\s+TEXT(?:\s+NOT\s+NULL)?\s+DEFAULT\s+)'((?:[^'\\]|\\.)*)'",
            lambda mm: f"{mm.group(1)}('{mm.group(2)}')", sql, flags=re.I)
    # MySQL 1093：DELETE/UPDATE 目标表不能出现在子查询的 FROM 中 → 用派生表包装
    # 模式：DELETE FROM <tbl> WHERE ... NOT IN (SELECT ... FROM <tbl> ...)
    m = re.match(r"^\s*DELETE\s+FROM\s+([`\"\w]+)\b([\s\S]*)$", sql, re.I)
    if m:
        tbl = m.group(1).strip("`\"")
        rest = m.group(2)
        # 定位 "NOT IN (" 后的子查询，用括号深度扫描找到配对的右括号
        notin = re.search(r"NOT\s+IN\s*\(", rest, re.I)
        if notin:
            start = notin.end() - 1  # '(' 位置
            depth = 0
            i = start
            while i < len(rest):
                if rest[i] == "(":
                    depth += 1
                elif rest[i] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            inner = rest[start + 1:i]
            # 仅当子查询 FROM 引用目标表时包装
            if re.search(r"\bFROM\s+[`\"\w]*\b" + re.escape(tbl) + r"\b", inner, re.I):
                wrapped = "(SELECT * FROM (" + inner + ") AS __derived)"
                rest = rest[:start] + wrapped + rest[i + 1:]
                sql = "DELETE FROM `" + tbl + "`" + rest
    return sql


def _replace_qmarks(sql: str) -> str:
    """把 SQL 中的 ? 占位符替换为 %s，跳过字符串字面量内的 ?。"""
    out, in_str, i = [], None, 0
    while i < len(sql):
        ch = sql[i]
        if in_str:
            out.append(ch)
            if ch == in_str and sql[i - 1:i] != "\\":
                in_str = None
        elif ch in "'\"":
            in_str = ch
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _backtick_key(sql: str) -> str:
    """把 meta 表 SQL 中字符串字面量外的 key 列名加反引号（MySQL 保留字）。

    保护 PRIMARY/UNIQUE/FOREIGN/INDEX KEY 关键字，避免误伤。
    """
    # 先保护关键字中的 KEY
    placeholders = []
    def _protect(mm):
        placeholders.append(mm.group(0))
        return f"__KWP{len(placeholders) - 1}__"
    pat = re.compile(r"\b(?:PRIMARY|UNIQUE|FOREIGN|INDEX)\s+KEY\b", re.I)
    sql2 = pat.sub(_protect, sql)
    out, in_str, i = [], None, 0
    while i < len(sql2):
        ch = sql2[i]
        if in_str:
            out.append(ch)
            if ch == in_str and sql2[i - 1:i] != "\\":
                in_str = None
        elif ch in "'\"":
            in_str = ch
            out.append(ch)
        else:
            # 匹配独立单词 key（前后非单词字符；跳过已反引号包裹的 `key` 与关键字占位）
            if (ch.lower() == "k" and re.match(r"key(?![A-Za-z0-9_])", sql2[i:], re.I)
                    and (i == 0 or not re.match(r"[A-Za-z0-9_]", sql2[i - 1]))
                    and (i == 0 or sql2[i - 1] != "`")):
                out.append("`key`")
                i += 3
                continue
            out.append(ch)
        i += 1
    result = "".join(out)
    for idx, kw in enumerate(placeholders):
        result = result.replace(f"__KWP{idx}__", kw)
    return result


# ====== Cursor / Connection 兼容实现 ======


class MySQLCompatCursor:
    """模拟 sqlite3.Cursor 的最小接口。"""

    def __init__(self, conn: "MySQLCompatDB"):
        self._conn = conn
        self._rows = None
        self._idx = 0
        self._pycur = None
        self.rowcount = -1
        self.lastrowid = None
        self.description = None

    def execute(self, sql: str, params=None):
        if params is not None and not isinstance(params, (tuple, list, dict)):
            params = (params,)
        # PRAGMA table_info(X)
        m = _PRAGMA_TABLE_INFO_RE.match(sql)
        if m:
            tbl = m.group(2)
            q = (
                "SELECT ORDINAL_POSITION-1, COLUMN_NAME, COLUMN_TYPE, "
                "IF(IS_NULLABLE='NO',1,0), COLUMN_DEFAULT, IF(COLUMN_KEY='PRI',1,0) "
                "FROM information_schema.columns WHERE TABLE_SCHEMA=DATABASE() "
                "AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION"
            )
            cur = self._conn._raw_cursor()
            cur.execute(q, (tbl,))
            self._rows = list(cur.fetchall())
            self._idx = 0
            self.description = [("cid",), ("name",), ("type",), ("notnull",), ("dflt_value",), ("pk",)]
            self.rowcount = len(self._rows)
            self._pycur = None
            return self
        # PRAGMA index_list(X) → (seq, name, unique) 兼容 sqlite 结构
        m = _PRAGMA_INDEX_LIST_RE.match(sql)
        if m:
            tbl = m.group(1)
            q = (
                "SELECT 0, INDEX_NAME, IF(NON_UNIQUE=0,1,0) "
                "FROM information_schema.statistics WHERE TABLE_SCHEMA=DATABASE() "
                "AND TABLE_NAME=%s GROUP BY INDEX_NAME, NON_UNIQUE"
            )
            cur = self._conn._raw_cursor()
            cur.execute(q, (tbl,))
            self._rows = list(cur.fetchall())
            self._idx = 0
            self.description = [("seq",), ("name",), ("unique",)]
            self.rowcount = len(self._rows)
            self._pycur = None
            return self
        # PRAGMA index_info(X) → (seqno, cid, name) 兼容 sqlite 结构
        m = _PRAGMA_INDEX_INFO_RE.match(sql)
        if m:
            idx = m.group(1)
            q = (
                "SELECT SEQ_IN_INDEX-1, 0, COLUMN_NAME FROM information_schema.statistics "
                "WHERE TABLE_SCHEMA=DATABASE() AND INDEX_NAME=%s ORDER BY SEQ_IN_INDEX"
            )
            cur = self._conn._raw_cursor()
            cur.execute(q, (idx,))
            self._rows = list(cur.fetchall())
            self._idx = 0
            self.description = [("seqno",), ("cid",), ("name",)]
            self.rowcount = len(self._rows)
            self._pycur = None
            return self
        # SELECT sql FROM sqlite_master ... name='X' → SHOW CREATE TABLE X，取第 2 列作 sql
        m = _SQLITE_MASTER_SQL_RE.match(sql)
        if m:
            tbl = m.group(1)
            cur = self._conn._raw_cursor()
            try:
                cur.execute(f"SHOW CREATE TABLE `{tbl}`")
                row = cur.fetchone()
                if row and row[1]:
                    # 规范化：去掉反引号 + 逗号后补空格（让业务侧 'credential_id, name'、
                    # 'repo_key text unique' 等子串检查命中 MySQL 输出，避免误判触发重建）
                    create_sql = re.sub(r"`([^`]+)`", r"\1", row[1])
                    create_sql = re.sub(r",(?=[^\s])", ", ", create_sql)
                    self._rows = [(create_sql,)]
                else:
                    self._rows = []
            except Exception:
                self._rows = []
            self._idx = 0
            self.description = [("sql",)]
            self.rowcount = len(self._rows)
            self._pycur = None
            return self
        # CREATE [UNIQUE] INDEX IF NOT EXISTS
        m = _CREATE_INDEX_RE.match(sql)
        if m:
            unique = "UNIQUE " if "UNIQUE" in sql.upper() and not sql.upper().startswith("CREATE INDEX") else ""
            idx, tbl, cols = m.group(1), m.group(2), m.group(3)
            cur = self._conn._raw_cursor()
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.statistics "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND INDEX_NAME=%s",
                (tbl, idx),
            )
            exists = cur.fetchone()[0] > 0
            if not exists:
                # TEXT/BLOB 列建索引需前缀长度（MySQL 限制），自动加 (191)
                col_list = [c.strip().strip("`\"") for c in cols.split(",")]
                fixed_cols = []
                for col in col_list:
                    cur.execute(
                        "SELECT DATA_TYPE FROM information_schema.columns "
                        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s",
                        (tbl, col),
                    )
                    row = cur.fetchone()
                    if row and row[0] and row[0].lower() in ("text", "tinytext", "mediumtext", "longtext", "blob", "mediumblob", "longblob"):
                        fixed_cols.append(f"`{col}`(191)")
                    else:
                        fixed_cols.append(f"`{col}`")
                cur.execute(f"CREATE {unique}INDEX `{idx}` ON `{tbl}` ({', '.join(fixed_cols)})")
            self._rows = []
            self.rowcount = 0
            self._pycur = None
            return self
        # no-op PRAGMA
        if sql == "__NOOP__":
            self._rows = []
            self.rowcount = 0
            self._pycur = None
            return self

        t = translate_sql(sql)
        cur = self._conn._raw_cursor()
        cur.execute(t, params)
        self._pycur = cur
        self.rowcount = cur.rowcount
        self.lastrowid = cur.lastrowid
        self.description = cur.description
        self._rows = None
        self._idx = 0
        return self

    def executemany(self, sql: str, seq_of_params):
        for params in seq_of_params:
            self.execute(sql, params)
        return self

    def fetchone(self):
        if self._pycur is not None:
            row = self._pycur.fetchone()
            return row
        if self._rows is None:
            return None
        if self._idx >= len(self._rows):
            return None
        row = self._rows[self._idx]
        self._idx += 1
        return row

    def fetchall(self):
        # sqlite3 返回 list（代码依赖 list 方法如 .reverse()），pymysql 返回 tuple → 统一转 list
        if self._pycur is not None:
            rows = self._pycur.fetchall()
            return list(rows) if rows is not None else []
        if self._rows is None:
            return []
        rows = self._rows[self._idx:]
        self._idx = len(self._rows)
        return list(rows)

    def fetchmany(self, size: int = 1):
        if self._pycur is not None:
            rows = self._pycur.fetchmany(size)
            return list(rows) if rows is not None else []
        if self._rows is None:
            return []
        rows = self._rows[self._idx:self._idx + size]
        self._idx += len(rows)
        return list(rows)

    def close(self):
        self._rows = None
        self._pycur = None


class MySQLCompatDB:
    """模拟 sqlite3.Connection：每线程独立 pymysql 连接（threading.local），
    所有操作需在外部 db_lock 串行化（与 sqlite3 版语义一致）。"""

    def __init__(self, **kwargs):
        self._cfg = kwargs
        self._local = threading.local()
        self._lock = threading.RLock()  # 兼容层内部兜底锁

    def _raw_cursor(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = pymysql.connect(
                host=self._cfg.get("host", "127.0.0.1"),
                port=self._cfg.get("port", 3306),
                user=self._cfg.get("user", ""),
                password=self._cfg.get("password", ""),
                database=self._cfg.get("database", ""),
                charset=self._cfg.get("charset", "utf8mb4"),
                autocommit=False,
                connect_timeout=10,
            )
            self._local.conn = conn
        try:
            conn.ping(reconnect=True)
        except Exception:
            conn = pymysql.connect(
                host=self._cfg.get("host", "127.0.0.1"),
                port=self._cfg.get("port", 3306),
                user=self._cfg.get("user", ""),
                password=self._cfg.get("password", ""),
                database=self._cfg.get("database", ""),
                charset=self._cfg.get("charset", "utf8mb4"),
                autocommit=False,
                connect_timeout=10,
            )
            self._local.conn = conn
        return conn.cursor()

    def cursor(self):
        return MySQLCompatCursor(self)

    def execute(self, sql: str, params=None):
        """sqlite3.Connection.execute 便捷方法：直接执行并返回 cursor。"""
        cur = MySQLCompatCursor(self)
        cur.execute(sql, params)
        return cur

    def executemany(self, sql: str, seq_of_params):
        cur = MySQLCompatCursor(self)
        cur.executemany(sql, seq_of_params)
        return cur

    def commit(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.commit()

    def rollback(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.rollback()

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    def executescript(self, sql: str):
        for stmt in re.split(r";\s*(?:\n|$)", sql):
            stmt = stmt.strip()
            if stmt:
                self.execute(stmt)
