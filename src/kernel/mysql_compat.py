"""MySQL 兼容层：模拟 sqlite3.Connection/Cursor 接口，自动把 SQLite 方言翻译为 MySQL 方言。

设计目标：grape 项目 26 个模块 `from src.kernel.session_manager import db, db_lock` 直接使用
sqlite3 风格 API（db.execute / db.cursor / cursor.execute(fetchone|fetchall) / db.commit /
c.lastrowid / c.rowcount / c.description / PRAGMA table_info / sqlite_master 查询）。
本模块提供相同接口的 MySQL 实现，并在 execute 时做 SQL 方言翻译，业务模块零改动。

启用方式（开关）：configs/db-backend.json 的 "backend" 字段（sqlite/mysql），
也可用环境变量 DEVOPS_DB_BACKEND=mysql 覆盖（配合 DEVOPS_DB_HOST/PORT/USER/PASSWORD/NAME，
或直接用 DEVOPS_DB_URL=mysql://user:***@host:port/dbname）。默认 sqlite。

团队规则（写 SQL 前必读，docs/weekly-08/mysql-dialect-compat-requirements.md §四）：
1. SQL 字符串字面量内禁止反斜杠——KWP 关键字保护使字符串字面量绕过翻译层，
   `ESCAPE '\'` 类写法在 MySQL 上是 1064 语法炸弹（SQLite 部署测不出）。
2. 前缀匹配 = `LIKE ?` + 常量前缀参数（如 f"{provider}:%"，provider 须白名单枚举），
   不引入 ESCAPE 子句与转义助手。
3. 确需用户输入进 LIKE 模式时，用 `ESCAPE '|'`（双方言安全）+ 转义函数。
4. DISTINCT + ORDER BY 的排序列必须在 SELECT 列表内（ONLY_FULL_GROUP_BY）。
5. SQL 内时间统一用 datetime('now')（无参形式，本层翻译）；带修饰符变体
   （datetime('now','-60 seconds')）是 SQLite 私有方言，禁止使用——应用层
   预计算 cutoff + `?` 参数（UTC 基线统一见 fix/time-utc-baseline 分支）。
"""
import json
import os
import re
import socket
import threading
import time
import weakref

import pymysql

from src.kernel.config import log

# ── 长连接健活参数（2026-11 生产实测：mgmt 进程全站卡顿 40~100s）─────────────
# 现象：MySQL 侧 processlist 全 Sleep（查询没到过），客户端线程全停在 pymysql
# `_read_bytes`；点「编辑」触发的 /dashboard + /scenes/with-pipeline 一起排队，
# 浏览器 30s 放弃 → 499。原因是跨网段链路上的池化长连接被中间设备静默丢弃
# （对端 ACK 过、包没到 DB），复用时既没有存活校验也没有读超时，只能等内核
# keepalive（默认 7200s）或 NAT 侧 RST 才解锁。
# 修法：连接建立时打开短周期 TCP keepalive；借出前按空闲阈值做**带超时的** ping；
# 读/写加兜底超时。全部可用环境变量调参，便于线上按需回退。
_DB_PING_IDLE_SECONDS = float(os.environ.get("DB_PING_IDLE_SECONDS", "5"))
_DB_PING_TIMEOUT_SECONDS = float(os.environ.get("DB_PING_TIMEOUT_SECONDS", "3"))
_DB_READ_TIMEOUT = int(os.environ.get("DB_READ_TIMEOUT", "120"))
_DB_WRITE_TIMEOUT = int(os.environ.get("DB_WRITE_TIMEOUT", "120"))
_DB_TCP_KEEPALIVE = os.environ.get("DB_TCP_KEEPALIVE", "1").lower() in ("1", "true", "yes")

# ── 连接池僵死防护（2026-11-23 mgmt 进程全站僵死事故）────────────────────────
# 事故形状：连接池上限 16（DB_POOL_MAX_CONNECTIONS）< anyio 工作线程上限 40。
# 慢语句把 16 条连接占满后，dbutils 的 blocking=True 会让**所有**后续借连接
# 的线程（含跑事件循环的主线程）无限期阻塞在 PooledDB._wait_lock 上 ——
# 主线程一卡，uvicorn 连静态文件都不再响应，进程活着但整站静默。
# 修法：① 借连接改成短轮询 + 超时快速失败（带池水位日志，不再永久挂死）；
#      ② 全部实例登记到弱引用表，供请求收尾兜底归还 / 池水位巡检 / 健康检查。
_DB_POOL_WAIT_TIMEOUT = float(os.environ.get("DB_POOL_WAIT_TIMEOUT", "15"))
_DB_POOL_WARN_WAIT = float(os.environ.get("DB_POOL_WARN_WAIT", "3"))
_DB_INSTANCES: "weakref.WeakSet" = weakref.WeakSet()

# 只读语句首关键字：认 CTE(WITH)/EXPLAIN/DESCRIBE，且允许 SQL 以注释开头。
# 原实现只按 lstrip 前缀判 SELECT/SHOW/PRAGMA —— 带注释或 WITH 开头的只读语句
# 不会被 commit，池化连接就一直粘在该线程的 thread-local 上不归还。
_READ_ONLY_LEADING = ("SELECT", "SHOW", "PRAGMA", "DESCRIBE", "DESC", "EXPLAIN", "WITH")


def _strip_sql_leading_noise(sql: str) -> str:
    """剥掉 SQL 开头的空白与注释（-- # /* */），用于判断语句类型。"""
    s = (sql or "").lstrip()
    while True:
        if s.startswith("--") or s.startswith("#"):
            nl = s.find("\n")
            s = "" if nl < 0 else s[nl + 1:].lstrip()
            continue
        if s.startswith("/*"):
            end = s.find("*/")
            s = "" if end < 0 else s[end + 2:].lstrip()
            continue
        return s


def _is_read_only_statement(sql: str) -> bool:
    """只读语句判定（决定执行后是否自动 commit 归还连接）。"""
    head = _strip_sql_leading_noise(sql)
    if not head:
        return False
    tok = re.split(r"[\s(]", head, 1)[0].upper()
    return tok in _READ_ONLY_LEADING


# Issue #4022：issue.status 专用写代判定——只有真正影响快照所含 status 的
# 语句才推进计数，events/agent_tasks 等高频写不会让列表状态快照失效。
# 注意：Cursor.execute 在执行前已经过 `qualify_sql`，schema 模式下 `issue` 会被
# 改写为 `grape_runtime.issue`（MySQL）或 `rt_issue`（SQLite 前缀）。因此判定不能
# 直接匹配裸表名，必须先取 SQL 中的目标表标识并归一为逻辑名再比较。
_WRITE_TARGET_RE = r"(?P<table>[^\s(,;]+)"
_ISSUE_ROW_WRITE_RE = re.compile(
    r"^(?:INSERT\s+(?:IGNORE\s+|OR\s+\w+\s+)?INTO|REPLACE\s+INTO|DELETE\s+FROM)\s+"
    + _WRITE_TARGET_RE,
    re.IGNORECASE)
_ISSUE_UPDATE_RE = re.compile(
    r"^UPDATE\s+" + _WRITE_TARGET_RE, re.IGNORECASE)
_ISSUE_STATUS_SET_RE = re.compile(r"\bstatus\s*=", re.IGNORECASE)
_SCHEMA_TABLE_PREFIXES = ("rt_", "mgmt_", "ins_")


def _logical_table_name(token: str) -> str:
    """把带引号/schema 限定/分 Schema 前缀的表标识归一为逻辑表名。

    `grape_runtime.issue` / `` `grape_runtime`.`issue` `` / `rt_issue` / `issue`
    都归一为 `issue`；`issue_state`、`rt_issue_state` 归一为 `issue_state`，不会
    与 `issue` 混淆（前缀只在词首且整段剥离后精确比较）。
    """
    name = (token or "").strip()
    if not name:
        return ""
    if "." in name:
        name = name.rsplit(".", 1)[-1]
    name = name.strip().strip("`\"[]").strip().lower()
    for _prefix in _SCHEMA_TABLE_PREFIXES:
        if name.startswith(_prefix):
            return name[len(_prefix):]
    return name


def _is_issue_status_write(sql: str) -> bool:
    """判断写语句是否改变 issue 行的 status（含增删行：行存在性也影响快照）。"""
    head = _strip_sql_leading_noise(sql)
    if not head:
        return False
    m = _ISSUE_ROW_WRITE_RE.match(head)
    if m and _logical_table_name(m.group("table")) == "issue":
        return True
    m = _ISSUE_UPDATE_RE.match(head)
    if m and _logical_table_name(m.group("table")) == "issue":
        return bool(_ISSUE_STATUS_SET_RE.search(head))
    return False


def release_all_thread_connections() -> int:
    """把**当前线程**在每个库实例上借的池连接都还回池，返回归还条数。"""
    n = 0
    for inst in list(_DB_INSTANCES):
        try:
            if inst.release_thread_connection():
                n += 1
        except Exception:  # noqa: BLE001 - 兜底清理不得抛
            pass
    return n


def pool_stats_all() -> list:
    """所有已建池实例的水位快照（进程内视角）。"""
    out = []
    for inst in list(_DB_INSTANCES):
        try:
            out.append(inst.pool_stats())
        except Exception:  # noqa: BLE001
            pass
    return out


def _enable_tcp_keepalive(conn) -> None:
    """给已建立连接开短周期 TCP keepalive（默认 60s 起探、10s 间隔、3 次判死）。"""
    if not _DB_TCP_KEEPALIVE:
        return
    sock = getattr(conn, "_sock", None)
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for opt, val in (("TCP_KEEPIDLE", int(os.environ.get("DB_TCP_KEEPIDLE", "60"))),
                         ("TCP_KEEPINTVL", int(os.environ.get("DB_TCP_KEEPINTVL", "10"))),
                         ("TCP_KEEPCNT", int(os.environ.get("DB_TCP_KEEPCNT", "3")))):
            if hasattr(socket, opt):
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
    except Exception as e:  # noqa: BLE001 - keepalive 失败不影响连接可用
        log.debug(f"mysql_compat: enable tcp keepalive failed: {e}")


def _pymysql_connect(**kwargs):
    """PooledDB creator：建连 + 打开 keepalive（池内每条连接都走这里）。"""
    conn = pymysql.connect(**kwargs)
    _enable_tcp_keepalive(conn)
    return conn

# ====== 配置读取 ======

_DB_BACKEND_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "configs", "db-backend.json"
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
# F7/M-11: 参数化判存变体（user/db.py:792 legacy 表清理）——原正则只认无参数形式，
# MySQL 上静默跳过 legacy 表清理。标记后由 Cursor 特判走 information_schema。
_SQLITE_MASTER_NAME_PARAM_RE = re.compile(
    r"^\s*SELECT\s+name\s+FROM\s+sqlite_master\s+WHERE\s+type\s*=\s*'table'\s+AND\s+name\s*=\s*\?\s*$", re.I
)
_SQLITE_MASTER_SQL_RE = re.compile(
    r"^\s*SELECT\s+sql\s+FROM\s+sqlite_master\s+WHERE\s+type\s*=\s*'table'\s+AND\s+name\s*=\s*['\"]([\w]+)['\"]\s*$",
    re.I,
)
_CREATE_UNIQUE_INDEX_RE = re.compile(
    r"^\s*CREATE\s+UNIQUE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+ON\s+(\w+)\s*\((.+)\)\s*$", re.I
)
_CREATE_INDEX_RE = re.compile(
    r"^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+ON\s+(\w+)\s*\((.+?)\)\s*(?:WHERE\s+(.+))?\s*$", re.I
)
_CREATE_INDEX_WHERE_RE = re.compile(r"\bWHERE\b", re.I)
_PRAGMA_IGNORE_RE = re.compile(
    r"^\s*PRAGMA\s+(journal_mode|busy_timeout|synchronous|foreign_keys|locking_mode|cache_size|wal_autocheckpoint|temp_store)",
    re.I,
)
_PRAGMA_INDEX_LIST_RE = re.compile(r"^\s*PRAGMA\s+index_list\(\s*([`\"'\w]+)\s*\)\s*$", re.I)
_PRAGMA_INDEX_INFO_RE = re.compile(r"^\s*PRAGMA\s+index_info\(\s*([`\"'\w]+)\s*\)\s*$", re.I)
# 仅处理 'gitcode:' || col 拼接模式（代码中唯一用法）
_CONCAT_RE = re.compile(r"'gitcode:'\s*\|\|\s*([A-Za-z_][A-Za-z0-9_.]*)")

# 通用字面量拼接：'<lit>' || <col> → CONCAT('<lit>', <col>)
# MySQL 默认 sql_mode 下 `||` 是逻辑 OR：'#'||n 求值为 (0 OR n) → 1，比较恒不相等，
# 不翻译会静默返回错误数据（pipeline.py 的 e.issue_id='#'||i.issue_number 即此类）。
# 只有左操作数是字符串字面量时才翻译，真正的逻辑 OR 写法不受影响。
_LIT_CONCAT_RE = re.compile(r"'((?:[^'\\]|\\.)*)'\s*\|\|\s*([A-Za-z_][A-Za-z0-9_.]*)")

# CREATE TABLE 语句中的列行级翻译
_COL_TEXT_PK_RE = re.compile(r"^(\s*)([`\"\w]+)\s+TEXT\s+PRIMARY\s+KEY\b", re.I)
# TEXT 系列列（TEXT/MEDIUMTEXT/LONGTEXT）DEFAULT 字面量 → 表达式（MySQL 8.0.13 前不允许 DEFAULT 字面量）
_COL_TEXT_DEFAULT_RE = re.compile(
    r"^(\s*)([`\"\w]+)\s+(?:MEDIUMTEXT|LONGTEXT|TEXT)\s+NOT\s+NULL\s+DEFAULT\s+'((?:[^'\\]|\\.)*)'(?=[,\s)]|$)", re.I
)
_COL_TEXT_DEFAULT2_RE = re.compile(
    r"^(\s*)([`\"\w]+)\s+(?:MEDIUMTEXT|LONGTEXT|TEXT)\s+DEFAULT\s+'((?:[^'\\]|\\.)*)'(?=[,\s)]|$)", re.I
)
# D105 UUID 化：user_id 族列 INTEGER→TEXT 后保留数字哨兵 DEFAULT（0=无归属）。
# SQLite 容忍 TEXT DEFAULT <数字>；MySQL 1101 拒绝——转表达式默认值 DEFAULT (<n>)（8.0.13+）。
# 实弹：yaw 四进程 MySQL 8.0.43 冷启动，session_api_tokens/user_feedback/org_member 等
# 8 表建表全败于此形态（catch 吞成 warning → 表静默缺失 → 运行时 1146）。
_COL_TEXT_DEFAULT_NUM_RE = re.compile(
    r"^(\s*)([`\"\w]+)\s+((?:MEDIUMTEXT|LONGTEXT|TEXT)\s+(?:NOT\s+NULL\s+)?DEFAULT\s+)(-?\d+)(?=[,\s)]|$)", re.I
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


def run_virtual_sql(raw_cursor, sql: str, params=None) -> bool:
    """执行「翻译层标记为游标特判」的 SQLite 方言 SQL，返回是否已处理。

    这几种语句无法翻译成等价的 MySQL **文本**（要先读 information_schema 再按
    sqlite 的结果形状重排列），所以 `translate_sql()` 原样返回并打标记，由游标
    自己执行。`mysql_compat.Cursor.execute` 内置了这段逻辑，但三库分域拓扑走的是
    `db_registry._CompatCursor`——那里只调 `translate_sql()` 没有特判，于是
    PRAGMA / sqlite_master 原样透传给 MySQL，直接 1064 语法错误。

    症状（分域三库拓扑 db_registry._CompatCursor 下启动迁移）：
        Migrate managed_repos failed: (1064) ... near 'PRAGMA table_info(managed_repos)'
        Migrate user_repos failed: (1064) ... near 'PRAGMA table_info(user_repos)'
        Migrate events repo_key prefix failed: (1064) ... PRAGMA table_info(events)
    三条迁移全部静默失败（异常被 except 吞成 warning），升级动作等于没做。

    只处理结果形状与 sqlite 一致的四类；`SHOW CREATE TABLE`（需重排为单列 sql）
    与 `CREATE INDEX IF NOT EXISTS`（可直接翻译成带存在性检查的 DDL）不在此处
    改写，保持原行为，避免在兼容游标上引入结果列错位。
    """
    # PRAGMA table_info(X) → information_schema.columns（列序与 sqlite 对齐）
    m = _PRAGMA_TABLE_INFO_RE.match(sql)
    if m:
        raw_cursor.execute(
            "SELECT ORDINAL_POSITION-1, COLUMN_NAME, COLUMN_TYPE, "
            "IF(IS_NULLABLE='NO',1,0), COLUMN_DEFAULT, IF(COLUMN_KEY='PRI',1,0) "
            "FROM information_schema.columns WHERE TABLE_SCHEMA=DATABASE() "
            "AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
            (m.group(2),),
        )
        return True
    # PRAGMA index_list(X) → (seq, name, unique)
    m = _PRAGMA_INDEX_LIST_RE.match(sql)
    if m:
        raw_cursor.execute(
            "SELECT 0, INDEX_NAME, IF(NON_UNIQUE=0,1,0) "
            "FROM information_schema.statistics WHERE TABLE_SCHEMA=DATABASE() "
            "AND TABLE_NAME=%s GROUP BY INDEX_NAME, NON_UNIQUE",
            (m.group(1).strip("`\"'"),),
        )
        return True
    # PRAGMA index_info(X) → (seqno, cid, name)
    m = _PRAGMA_INDEX_INFO_RE.match(sql)
    if m:
        raw_cursor.execute(
            "SELECT SEQ_IN_INDEX-1, 0, COLUMN_NAME FROM information_schema.statistics "
            "WHERE TABLE_SCHEMA=DATABASE() AND INDEX_NAME=%s ORDER BY SEQ_IN_INDEX",
            (m.group(1).strip("`\"'"),),
        )
        return True
    # SELECT name FROM sqlite_master WHERE type='table' → SHOW TABLES（单列同形）
    if _SQLITE_MASTER_NAME_RE.match(sql):
        raw_cursor.execute("SHOW TABLES")
        return True
    # 参数化判存：SELECT name FROM sqlite_master WHERE type='table' AND name=?
    if _SQLITE_MASTER_NAME_PARAM_RE.match(sql) and params:
        tbl = params[0] if isinstance(params, (tuple, list)) else params
        raw_cursor.execute(
            "SELECT TABLE_NAME FROM information_schema.tables "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
            (tbl,),
        )
        return True
    return False


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
        # TEXT NOT NULL DEFAULT 'x' / TEXT DEFAULT 'x' → 剥除字面量 DEFAULT（G4 根修）
        # MySQL TEXT 系列列不接受字面量 DEFAULT（1101），包成表达式 DEFAULT ('')
        # 在部分版本下仍 1064（drill4 实锤）→ 直接剥除，默认 NULL，应用层 ``or ''`` 兜底；
        # 带括号的表达式 DEFAULT 保留（test_text_expression_default_kept）。
        if _COL_TEXT_DEFAULT_RE.search(c) or _COL_TEXT_DEFAULT2_RE.search(c):
            if re.match(r"^[`\"\w]+\s+(?:MEDIUMTEXT|LONGTEXT|TEXT)\b", c, re.I) and "PRIMARY KEY" not in c.upper():
                type_m = re.match(r"^[`\"\w]+\s+(MEDIUMTEXT|LONGTEXT|TEXT)\b", c, re.I)
                col_type = type_m.group(1).upper() if type_m else "TEXT"
                name_m = re.match(r"^(\s*)([`\"\w]+)", c)
                not_null = " NOT NULL" if re.search(r"\bNOT\s+NULL\b", c, re.I) else ""
                c = f"{name_m.group(1)}{name_m.group(2)} {col_type}{not_null}"
        # TEXT [NOT NULL] DEFAULT <数字>（D105 UUID 化哨兵）→ 同上剥除
        # （实弹：yaw 四进程 MySQL 8.0.43 冷启动，session_api_tokens/user_feedback/
        #  org_member 等 8 表建表全败于 TEXT DEFAULT 0 直传 → 表静默缺失 → 运行时 1146）
        if _COL_TEXT_DEFAULT_NUM_RE.search(c) and "PRIMARY KEY" not in c.upper():
            _m = _COL_TEXT_DEFAULT_NUM_RE.search(c)
            # group(3)=「类型 + 可选 NOT NULL + DEFAULT 」——剥 DEFAULT 尾即保留类型与 NOT NULL
            c = f"{_m.group(1)}{_m.group(2)} {_m.group(3).split('DEFAULT')[0].rstrip()}"
        # datetime('now') → CAST(NOW() AS CHAR)（字符串语义与 SQLite 一致，且避免 % 占位冲突）
        c = _COL_DATETIME_DEFAULT_RE.sub("CAST(NOW() AS CHAR)", c)
        out.append(c)
    return head + ", ".join(out) + ")" + trailing


# INSERT ... ON CONFLICT(<pk>) DO UPDATE SET <col>=<expr(excluded.col)> ... → MySQL ON DUPLICATE KEY UPDATE
# 匹配到 DO UPDATE SET 子句末尾（到语句尾；不跨多语句）。
# 列清单可以是**复合**的（如 UNIQUE(resolution, bucket_start, metric, dim_key)）——
# MySQL 的 ON DUPLICATE KEY UPDATE 不需要列清单，翻译时整段丢弃即可。
_CONFLICT_UPSERT_RE = re.compile(
    r"(?is)\bON\s+CONFLICT\s*\(([^)]+)\)\s+DO\s+UPDATE\s+SET\s+"
    r"(.+?)(?=(?:\bON\s+CONFLICT\b)|$)")

def _mysql_upsert_repl(m):
    """SQLite: ON CONFLICT(pk) DO UPDATE SET a=expr(excluded.a) → MySQL: ON DUPLICATE KEY UPDATE a=expr(VALUES(a))"""
    _sets = m.group(2)
    # excluded.`col`（反引号列名）→ VALUES(`col`)；excluded.col（裸列名）→ VALUES(col)
    # 注意不能用 \b 锚定（反引号非词字符会截断列名），分带/不带反引号两类处理
    _sets = re.sub(r"excluded\.`([^`]*)`", r"VALUES(`\1`)", _sets)
    _sets = re.sub(r"excluded\.([A-Za-z0-9_]+)", r"VALUES(\1)", _sets)
    return "ON DUPLICATE KEY UPDATE " + _sets


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
    # sqlite_master 参数化判存（F7/M-11）→ 标记：Cursor.execute 特判
    if _SQLITE_MASTER_NAME_PARAM_RE.match(sql):
        return sql  # 标记：Cursor.execute 特判
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
    # INSERT ... ON CONFLICT(<pk>) DO UPDATE SET <col>=... excluded.col ...（SQLite/PostgreSQL 方言）
    # → ON DUPLICATE KEY UPDATE <col>=... VALUES(col) ...（MySQL 方言，8.0.20 以下用 VALUES()）
    # 关键引用：src/domains/runtime/im/bot_lease.py heartbeat() 的实例心跳 UPSERT，缺失翻译会 1064（cluster-seq-11130 根因）。
    sql = _CONFLICT_UPSERT_RE.sub(_mysql_upsert_repl, sql)
    # datetime('now') → CAST(NOW() AS CHAR)（仅无参形式；UTC 基线统一在
    # fix/time-utc-baseline 分支处理，本分支只修语法层方言）
    sql = _COL_DATETIME_DEFAULT_RE.sub("CAST(NOW() AS CHAR)", sql)
    # 'gitcode:' || col → CONCAT('gitcode:', col)
    sql = _CONCAT_RE.sub(r"CONCAT('gitcode:', \1)", sql)
    # 其余字面量拼接：'<lit>' || col → CONCAT('<lit>', col)（见 _LIT_CONCAT_RE 注释）
    sql = _LIT_CONCAT_RE.sub(r"CONCAT('\1', \2)", sql)
    # CAST(x AS TEXT) → CAST(x AS CHAR)（MySQL 不支持 TEXT 作为 CAST 目标类型，1064 语法错误；
    #   CHAR 在 SQLite 中按 TEXT 亲和性处理，双方言兼容）
    sql = re.sub(r"\bCAST\((.+?)\s+AS\s+TEXT\)", r"CAST(\1 AS CHAR)", sql, flags=re.I)
    # CAST(x AS INTEGER) → CAST(x AS SIGNED)（R-3 修复：MySQL 无 INTEGER 目标类型，
    #   SQLite 中 SIGNED 同义可行，双方言兼容）
    sql = re.sub(r"\bCAST\((.+?)\s+AS\s+INTEGER\)", r"CAST(\1 AS SIGNED)", sql, flags=re.I)
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
        # MODIFY [COLUMN] <col> TEXT|MEDIUMTEXT|LONGTEXT ... DEFAULT '<lit>' → DEFAULT ('<lit>')
        # （obs-skill-storage-design.md §4：skill_files 升 MEDIUMTEXT 走 MODIFY，同样受
        #   MySQL 8.0.13 前 TEXT 系列列不允许 DEFAULT 字面量限制）
        sql = re.sub(
            r"(\bMODIFY\s+(?:COLUMN\s+)?[`\w]+\s+(?:TEXT|MEDIUMTEXT|LONGTEXT)(?:\s+NOT\s+NULL)?\s+DEFAULT\s+)'((?:[^'\\]|\\.)*)'",
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


# 已有的 MySQL 原生占位符（少数模块直接写 %s / %(name)s）——不能逃逸，否则会被双重转义
_NATIVE_PLACEHOLDER_RE = re.compile(r"%(?:\([^)]+\))?s")


def _replace_qmarks(sql: str) -> str:
    """把 SQL 中的 ? 占位符替换为 %s，跳过字符串字面量内的 ?。

    同时把**字面 `%` 逃逸成 `%%`**：pymysql 在带参数执行时会做一次 Python `%` 格式化
    （`query % args`），SQL 里任何字面 `%`（最常见是 `LIKE 'xxx:%'`）都会被当成格式符，
    抛 `unsupported format character` / `not enough arguments for format string`。
    真机事故：用户面统一事件视图的告警来源查询静默失败、事件全丢（见 alerts.list_events）。

    只逃逸"非占位符"的 `%`：先保护已有的 `%s` / `%(name)s`（少数模块直接写 MySQL 原生
    占位符），其余转成 `%%`（pymysql 格式化后会还原成单个 `%`）。
    残余边界：`?` 式语句里若出现字面 `%s`（如 `LIKE '%s%'`）不会被逃逸——用 `substr/instr`
    替代即可，勿在 SQL 里写字面 `%`。
    """
    out, in_str, i = [], None, 0
    while i < len(sql):
        ch = sql[i]
        if in_str:
            # 字符串字面量里的 % 一律逃逸（pymysql 对**整条语句**做格式化；
            # 这里出现的 %s 也是字面量文本，`%%s` 格式化后正好还原成 `%s`）
            out.append("%%" if ch == "%" else ch)
            if ch == in_str and sql[i - 1:i] != "\\":
                in_str = None
        elif ch in "'\"":
            in_str = ch
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        elif ch == "%":
            # 字面量之外的 %s / %(name)s 是原生占位符，必须原样保留
            m = _NATIVE_PLACEHOLDER_RE.match(sql, i)
            if m:
                out.append(m.group(0))
                i += len(m.group(0))
                continue
            out.append("%%")
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
    # ON DUPLICATE KEY 是 MySQL 的 upsert 关键字（由 _CONFLICT_UPSERT_RE 翻译产生），
    # 不能被当成 meta 表的 key 列加反引号 —— 否则得到 `ON DUPLICATE `key` UPDATE` → 1064
    # （2026-11-21 实测：meta upsert 首次上 MySQL 就炸在这里）。
    pat = re.compile(r"\b(?:(?:PRIMARY|UNIQUE|FOREIGN|INDEX)\s+KEY|ON\s+DUPLICATE\s+KEY)\b", re.I)
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

    # sqlite3.Cursor 可直接迭代（`for row in cursor` / `list(cursor)`）；兼容层
    # 缺这两个协议会退化为 TypeError（Issue #3772：no_progress 扫描恒失败）。
    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def execute(self, sql: str, params=None):
        # 分 Schema 灰度（Issue #699 Phase 2）：表名语法位置的注册表表名映射为
        # grape_mgmt./grape_runtime./grape_insight. 限定名（GRAPE_SCHEMA_MODE 未启用时直通）。
        try:
            from src.kernel.db import qualify_sql
            sql = qualify_sql(sql)
        except Exception as e:
            log.warning(f"mysql_compat: qualify_sql failed, falling back to raw SQL: {sql[:80]!r} ({e})")
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
            tbl = m.group(1).strip("`\"'")
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
            idx = m.group(1).strip("`\"'")
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
        # sqlite_master 参数化判存（F7/M-11）→ information_schema 等价
        if _SQLITE_MASTER_NAME_PARAM_RE.match(sql) and params:
            tbl = params[0] if isinstance(params, (tuple, list)) else params
            cur = self._conn._raw_cursor()
            cur.execute(
                "SELECT TABLE_NAME FROM information_schema.tables "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
                (tbl,))
            row = cur.fetchone()
            self._rows = [(tbl,)] if row and row[0] == tbl else []
            self._idx = 0
            self.description = [("name",)]
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
        # CREATE [UNIQUE] INDEX IF NOT EXISTS（含可选 WHERE 子句的部分索引）
        m = _CREATE_INDEX_RE.match(sql)
        if m:
            unique = "UNIQUE " if "UNIQUE" in sql.upper() and not sql.upper().startswith("CREATE INDEX") else ""
            idx, tbl, cols = m.group(1), m.group(2), m.group(3)
            # MySQL 不支持部分索引（WHERE 子句），降级为全列唯一索引。
            # m.group(4) 是 WHERE 子句，MySQL 端丢弃不用。
            cur = self._conn._raw_cursor()
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.statistics "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND INDEX_NAME=%s",
                (tbl, idx),
            )
            exists = cur.fetchone()[0] > 0
            if not exists:
                # TEXT/BLOB 列建索引需前缀长度（MySQL 限制），自动加 (191)
                # F7 附带：调用方已带前缀长度（如 repo_key(255)）时先剥掉，统一走本逻辑
                col_list = [c.strip().strip("`\"") for c in cols.split(",")]
                fixed_cols = []
                for col in col_list:
                    _paren = col.find("(")
                    if _paren > 0:
                        col = col[:_paren].strip()
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
                # FR-20.2: MySQL 不支持部分索引（WHERE 子句）。
                # 有 WHERE 子句时降级为全列唯一索引（MySQL 靠全列约束 + 应用层守卫兜底）。
                # 无 WHERE 子句时正常创建。
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
        # OTel DB span（2026-11-05）：在 execute 调用点取当前 context——连接可能来自
        # 启动期创建的池（早于 pymysql 插桩，连接未被 patch），在 execute 处手动包 span
        # 保证挂到请求 trace 下。OTel 未启用/无 span 时零开销（noop span）。
        _otel_span = None
        try:
            from src.kernel.otel import start_db_span
            # db.name 让三库（mgmt/runtime/insight）在 trace 里分得开；连不上就留空
            _dbn = ""
            try:
                _dbn = (getattr(self._conn, "_cfg", None) or {}).get("database", "")
            except Exception:
                _dbn = ""
            _otel_span = start_db_span(t, db_name=_dbn)
        except Exception:
            _otel_span = None
        try:
            cur.execute(t, params)
        except Exception as _otel_exc:
            # 失败也要把 span 收干净并标成 error（原来走 finally 只 end()，
            # 排障时看着像成功的一跳）
            if _otel_span is not None:
                try:
                    from src.kernel.otel import end_db_span
                    end_db_span(_otel_span, rowcount=None, exc=_otel_exc)
                except Exception:
                    pass
                _otel_span = None
            raise
        else:
            if _otel_span is not None:
                try:
                    from src.kernel.otel import end_db_span
                    end_db_span(_otel_span, rowcount=getattr(cur, "rowcount", None))
                except Exception:
                    _otel_span = None
        # autocommit=False：SELECT 也会隐式开启事务，同线程连接复用时
        # 后续 SELECT 看到旧 MVCC 快照（/repo 返回老列表根因）。
        # 只读语句执行后自动 commit 释放事务，保持读一致性。
        # 判定收口到 _is_read_only_statement：兼容注释开头 / WITH(CTE) / EXPLAIN，
        # 否则这类只读语句不 commit，池化连接会一直粘在该线程上不归还。
        if _is_read_only_statement(t):
            self._conn.commit()
        self._pycur = cur
        self.rowcount = cur.rowcount
        self.lastrowid = cur.lastrowid
        self.description = cur.description
        self._rows = None
        self._idx = 0
        # Issue #4022：sqlite3.Connection.total_changes 等价物——写语句（按行数）累加
        # 连接级写代计数，供读侧「状态快照 + 写代校验」判定快照是否仍新鲜。
        # 计数器在执行点累加（与 sqlite 一致，不依赖 commit），只增不减。
        if not _is_read_only_statement(t):
            try:
                self._conn._bump_total_changes(max(int(getattr(cur, "rowcount", 0) or 0), 1))
            except Exception:
                pass
            # Issue #4022 QA Standards-1：另记 issue.status 专用写代，供列表状态快照
            # 判定是否仍新鲜（events/agent_tasks 等无关写不再误判过期）。
            try:
                if _is_issue_status_write(t):
                    self._conn._bump_issue_status_write()
            except Exception:
                pass
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
        if self._pycur is not None:
            try:
                self._pycur.close()
            except Exception:
                pass
        self._pycur = None


class MySQLCompatDB:
    """模拟 sqlite3.Connection：MySQL 连接池化（DBUtils PooledDB）。

    设计目标（P1-4A 治本，Issue #357）：
    - 用连接池替代每线程裸连接，消灭 770 万次连接 churn / 51.8 万 Aborted_clients。
    - 连接从池借还（dedicated connection per cursor），SQL 级锁内化为连接粒度——
      外部 ``with db_lock`` 调用点接口不变，锁语义内化（锁内不再串行全部 SQL，
      仅保证同一 cursor 的 borrow-execute-return 原子性）。
    - ``DB_POOL_DISABLED=1`` 环境变量一键回滚到旧的每线程裸连接模式（灰度安全网）。
    - 池参数：mincached=2, maxcached=16, maxconnections=32（可环境变量覆盖）。
    """

    def __init__(self, **kwargs):
        self._cfg = kwargs
        self._local = threading.local()
        self._lock = threading.RLock()  # 兼容层内部兜底锁
        self._pool = None  # PooledDB instance (lazy init)
        self._total_changes = 0  # sqlite3 total_changes 等价写代计数（Issue #4022）
        # Issue #4022 QA Standards-1：issue.status 专用写代（只统计影响 issue 行
        # status 的写，events/agent_tasks 高频写不推进），避免列表状态快照被无关写刷爆。
        self._issue_status_write_gen = 0
        self._pool_disabled = os.environ.get("DB_POOL_DISABLED", "").lower() in ("1", "true", "yes")
        _DB_INSTANCES.add(self)

    def _bump_total_changes(self, n: int):
        """累加写代计数（cursor 执行写语句时调用；跨池连接共享同一计数器）。"""
        with self._lock:
            self._total_changes += max(int(n or 0), 0)

    def _bump_issue_status_write(self):
        """推进 issue.status 专用写代（Issue #4022；仅影响 issue 行 status 的写调用）。"""
        with self._lock:
            self._issue_status_write_gen += 1

    @property
    def issue_status_write_generation(self) -> int:
        """issue.status 专用写代（Issue #4022 QA Standards-1/-2）。

        与 `total_changes` 不同：只有 INSERT/REPLACE/DELETE issue 行或 SET status 的
        UPDATE 才推进，events/agent_tasks 等高频写不会让列表状态快照失效。读侧
        （routes/issues.py `_issue_status_by_ids`）优先用它判定快照新鲜度。
        """
        with self._lock:
            return self._issue_status_write_gen

    @property
    def total_changes(self) -> int:
        """sqlite3.Connection.total_changes 兼容属性（Issue #4022）。

        自连接建立以来的累计写行数：任何写语句都会使其变化——读侧状态快照
        （routes/issues.py `_issue_status_by_ids`）用它判定「自上次快照以来是否
        有写入」，写后即读不因 TTL 削弱；语义与 sqlite（执行点计数）一致。
        """
        with self._lock:
            return self._total_changes

    def _ensure_pool(self):
        """Lazy-init the connection pool (called once on first use)."""
        if self._pool is not None or self._pool_disabled:
            return
        try:
            from dbutils.pooled_db import PooledDB
        except ImportError:
            log.warning("DBUtils not installed, falling back to per-thread connection (DB_POOL_DISABLED=1)")
            self._pool_disabled = True
            return
        try:
            self._pool = PooledDB(
                creator=_pymysql_connect,
                mincached=int(os.environ.get("DB_POOL_MIN", "2")),
                maxcached=int(os.environ.get("DB_POOL_MAX_CACHED", "16")),
                maxconnections=int(os.environ.get("DB_POOL_MAX_CONNECTIONS", "32")),
                # blocking=False：池满立刻抛 TooManyConnectionsError，由
                # _borrow_pooled_conn 短轮询 + 超时快速失败。原来的 blocking=True
                # 会让线程（包括事件循环主线程）无限期挂在池锁上，全站静默。
                blocking=False,
                host=self._cfg.get("host", "127.0.0.1"),
                port=self._cfg.get("port", 3306),
                user=self._cfg.get("user", ""),
                password=self._cfg.get("password", ""),
                database=self._cfg.get("database", ""),
                charset=self._cfg.get("charset", "utf8mb4"),
                autocommit=False,
                connect_timeout=10,
                read_timeout=_DB_READ_TIMEOUT,
                write_timeout=_DB_WRITE_TIMEOUT,
                # 借出时 ping(reconnect=True)：DB 重启 / NAT 静默丢流后自动换新连接，
                # 而不是把死连接交给业务查询（否则查询读 socket 挂死几十秒~几分钟）
                ping=1,
                # REPEATABLE READ 下，只读不 commit 的线程会让池化连接的读快照
                # 冻结在首次 SELECT（登录 state: 签发线程 INSERT+commit 落库成功，
                # 校验线程沿用老快照 → 3 秒后仍 NOT FOUND，登录永远 state_invalid）。
                # READ COMMITTED 与代码的显式事务/行锁用法兼容（同 SQLite 语义）。
                init_command="SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED",
            )
            log.info("MySQLCompatDB: connection pool initialized "
                     f"(min={os.environ.get('DB_POOL_MIN', '2')}, "
                     f"max_cached={os.environ.get('DB_POOL_MAX_CACHED', '16')}, "
                     f"max_conn={os.environ.get('DB_POOL_MAX_CONNECTIONS', '32')})")
        except Exception as e:
            log.warning(f"MySQLCompatDB: pool init failed ({e}), falling back to per-thread connection")
            self._pool_disabled = True

    def _conn_alive(self, conn) -> bool:
        """借出前健活校验（带 socket 超时，避免 ping 自己挂死）。

        只在空闲超过 _DB_PING_IDLE_SECONDS 时做一次，正常请求不会多一次往返。
        ping 失败/超时即判死交给调用方丢弃重取 —— 死连接若直接拿去执行查询，
        读会挂在 socket 上几十秒到几分钟（2026-11 生产全站卡顿的机制：
        对端 ACK 过、包没到 DB，MySQL 侧 processlist 只有 Sleep）。
        """
        last = float(getattr(self._local, "conn_alive_at", 0.0) or 0.0)
        if time.time() - last < _DB_PING_IDLE_SECONDS:
            return True
        sock = getattr(conn, "_sock", None)
        prev = None
        try:
            if sock is not None:
                prev = sock.gettimeout()
                sock.settimeout(_DB_PING_TIMEOUT_SECONDS)
            conn.ping(reconnect=True)
            self._local.conn_alive_at = time.time()
            return True
        except Exception as e:  # noqa: BLE001 - 判死即重建
            log.warning(f"MySQLCompatDB: 池化连接失活，丢弃重建: {e}")
            return False
        finally:
            if sock is not None and prev is not None:
                try:
                    sock.settimeout(prev)
                except Exception as e:  # noqa: BLE001 - 恢复超时失败不影响判死结果
                    log.debug(f"mysql_compat: restore socket timeout failed: {e}")

    def _drop_local_conn(self) -> None:
        conn = getattr(self._local, "conn", None)
        self._local.conn = None
        self._local.conn_alive_at = 0.0
        if conn is not None:
            try:
                conn.close()  # 池化包装 close() = 归还；死连接由池在借出时剔除
            except Exception as e:  # noqa: BLE001 - 丢弃的已是死连接，关闭失败无需上抛
                log.debug(f"mysql_compat: close dead pooled conn failed: {e}")

    def _borrow_pooled_conn(self):
        """从池借一条连接：短轮询 + 超时快速失败（替代 dbutils 的无限阻塞）。

        池满（并发语句 > maxconnections）时不再让线程永久挂死：
        - 等待超过 DB_POOL_WARN_WAIT（默认 3s）先打一条 WARNING + 池水位；
        - 超过 DB_POOL_WAIT_TIMEOUT（默认 15s）抛 TimeoutError，让请求快速 500，
          而不是把事件循环和整站一起拖死。
        """
        deadline = time.monotonic() + _DB_POOL_WAIT_TIMEOUT
        started = time.monotonic()
        warned = False
        pool = self._pool
        if pool is None:
            raise RuntimeError("connection pool not initialized")
        while True:
            try:
                return pool.connection()
            except Exception as e:  # noqa: BLE001
                if type(e).__name__ not in ("TooManyConnectionsError", "TooManyConnections"):
                    raise
                waited = time.monotonic() - started
                if waited >= _DB_POOL_WAIT_TIMEOUT:
                    st = self.pool_stats()
                    log.error(
                        f"MySQLCompatDB[{self._cfg.get('database', '')}]: 借连接超时 "
                        f"{_DB_POOL_WAIT_TIMEOUT:.0f}s（池已满 in_use={st.get('in_use')}/"
                        f"{st.get('max')}），本次请求快速失败以免拖死整个进程")
                    raise TimeoutError(
                        f"db pool exhausted ({st.get('in_use')}/{st.get('max')}) "
                        f"after {_DB_POOL_WAIT_TIMEOUT:.0f}s")
                if not warned and waited >= _DB_POOL_WARN_WAIT:
                    warned = True
                    st = self.pool_stats()
                    log.warning(
                        f"MySQLCompatDB[{self._cfg.get('database', '')}]: 等待池连接 "
                        f"{waited:.1f}s（in_use={st.get('in_use')}/{st.get('max')}）")
                time.sleep(0.05)

    def pool_stats(self) -> dict:
        """连接池水位（进程内视角）：in_use / idle / max。"""
        p = self._pool
        base = {"database": self._cfg.get("database", ""), "enabled": p is not None}
        if p is None:
            base.update({"max": 0, "in_use": 0, "idle": 0})
            return base
        try:
            idle = len(getattr(p, "_idle_cache", []) or [])
            total = int(getattr(p, "_connections", 0) or 0)
            mx = int(getattr(p, "_maxconnections", 0) or 0)
            base.update({"max": mx, "in_use": max(total - idle, 0), "idle": idle})
        except Exception:  # noqa: BLE001
            base.update({"max": 0, "in_use": 0, "idle": 0})
        return base

    def release_thread_connection(self) -> bool:
        """把本线程借的池连接还回池（幂等）。请求收尾兜底用。"""
        if self._pool is None:
            return False
        if getattr(self._local, "conn", None) is None:
            return False
        self._drop_local_conn()
        return True

    def _raw_cursor(self):
        """Get a raw pymysql cursor.

        Pool mode: borrow a connection from the pool **once per thread** and
        reuse it for all subsequent cursors until ``commit()``/``rollback()``
        returns it.  This guarantees that every ``execute()`` within a single
        transaction (e.g. ``db.execute(INSERT) → db.execute(UPDATE) → db.commit()``)
        runs on the *same* underlying connection, preserving transaction
        atomicity.  The connection is returned to the pool on commit/rollback
        (see ``commit``/``rollback``/``close``).

        Fallback mode (DB_POOL_DISABLED): per-thread bare connection (legacy behavior).
        """
        if self._pool is None:
            self._ensure_pool()

        if self._pool is not None:
            # Pool mode: reuse thread-local borrowed connection within a transaction.
            # Borrowing once per thread (not per cursor) ensures multi-statement
            # transactions (db.execute×N + db.commit) share one connection.
            conn = getattr(self._local, "conn", None)
            if conn is not None and not self._conn_alive(conn):
                self._drop_local_conn()
                conn = None
            if conn is None:
                conn = self._borrow_pooled_conn()
                self._local.conn = conn
                self._local.conn_alive_at = time.time()
            return conn.cursor()

        # Fallback: per-thread bare connection (legacy)
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = _pymysql_connect(
                host=self._cfg.get("host", "127.0.0.1"),
                port=self._cfg.get("port", 3306),
                user=self._cfg.get("user", ""),
                password=self._cfg.get("password", ""),
                database=self._cfg.get("database", ""),
                charset=self._cfg.get("charset", "utf8mb4"),
                autocommit=False,
                connect_timeout=10,
                read_timeout=_DB_READ_TIMEOUT,
                write_timeout=_DB_WRITE_TIMEOUT,
            )
            self._local.conn = conn
        try:
            conn.ping(reconnect=True)
        except Exception:
            conn = _pymysql_connect(
                host=self._cfg.get("host", "127.0.0.1"),
                port=self._cfg.get("port", 3306),
                user=self._cfg.get("user", ""),
                password=self._cfg.get("password", ""),
                database=self._cfg.get("database", ""),
                charset=self._cfg.get("charset", "utf8mb4"),
                autocommit=False,
                connect_timeout=10,
                read_timeout=_DB_READ_TIMEOUT,
                write_timeout=_DB_WRITE_TIMEOUT,
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
            # Pool mode: return the borrowed connection to the pool after commit.
            # This prevents connection leaks (each cursor borrowed a new connection
            # but never returned it). After commit the transaction is complete,
            # so the connection can be reused by other threads.
            if self._pool is not None:
                try:
                    conn.close()  # PooledDedicatedDBConnection.close() returns to pool
                except Exception:
                    pass
                self._local.conn = None

    def rollback(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.rollback()
            if self._pool is not None:
                try:
                    conn.close()  # return to pool
                except Exception:
                    pass
                self._local.conn = None

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

    def dedicated_connection(self):
        """建立一条**池外**专用连接（Issue #366/#365 修复：迁移锁专用）。

        不从池借出——池借出对象的 close() 语义是"还池"，持锁连接会以 Sleep 驻留池中，
        而 MySQL 的 GET_LOCK 是**连接级**锁：连接不断开锁就不释放 → 下次启动排队等满
        timeout（实测 174/79 每次重启卡住需人工 KILL）。

        调用方用完必须关闭（建议 with）；真断开即释放其上持有的所有连接级状态
        （命名锁、临时表、用户变量）。接口同 sqlite3.Connection 的最小子集。
        """
        return DedicatedConnection(pymysql.connect(**_dedicated_connect_params(self._cfg)))


def _dedicated_connect_params(cfg: dict) -> dict:
    """池外专用连接的参数（与 _raw_cursor 的 per-thread 回退路径保持一致）。"""
    return {
        "host": cfg.get("host", "127.0.0.1"),
        "port": cfg.get("port", 3306),
        "user": cfg.get("user", ""),
        "password": cfg.get("password", ""),
        "database": cfg.get("database", ""),
        "charset": cfg.get("charset", "utf8mb4"),
        "autocommit": False,
        "connect_timeout": 10,
        "read_timeout": _DB_READ_TIMEOUT,
        "write_timeout": _DB_WRITE_TIMEOUT,
    }


class DedicatedConnection:
    """池外专用连接：包装一条独立于连接池的裸 pymysql 连接。

    刻意最小：cursor()/close()/closed + 上下文管理器。SQL **不做方言翻译**，
    调用方必须传 MySQL 原生语句。
    """

    def __init__(self, conn):
        self._conn = conn
        self.closed = False

    def cursor(self):
        if self.closed:
            raise RuntimeError("DedicatedConnection is closed")
        return self._conn.cursor()

    def close(self):
        """真断开（幂等）：连接断开即由 MySQL 释放其上持有的所有命名锁。"""
        if self.closed:
            return
        self.closed = True
        try:
            self._conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False
