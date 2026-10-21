#!/usr/bin/env python3
"""chat_agent_session 配置行重复/错位 —— 只读诊断脚本（第一步：迁移前基线）

背景
----
`chat_agent_session` 同时承载两类行：
  * 用户级 Chat 配置行（`repo_key=''`）—— runtime_id / skill / im_* 都在这行上；
  * repo 级会话行（`repo_key='<repo>'`）—— 飞书 Chat 会话用。
用户级配置行的 id 规则是 `sess-cfg-{user_id}`（写路径 `session_manager._write_chat_config_row`），
但读路径是 `SELECT ... WHERE user_id=? AND (repo_key='' OR repo_key IS NULL) LIMIT 1`——
**没有 ORDER BY，且该表历史上的 UNIQUE(repo_key) 约束已被移除**，于是当同一用户存在多行时，
"读到哪一行"由存储/主键顺序决定，而写入只落 `sess-cfg-{user_id}`：
对"老行 id 排在 sess-cfg-{user_id} 之前"的用户（字符串序，如 'sess-cfg-112' < 'sess-cfg-3'），
界面改配置**确定性地不生效**（写新行、读老行）。

本脚本
------
严格只读（仅 SELECT/SHOW/DESC，且尽力开启会话只读），输出：
  1) 总览计数
  2) 每个用户的配置行明细（含"预计被读到的那行"与"改配置是否生效"判定）
  3) 冲突统计：引擎冲突 / IM 冲突 / 幽灵引擎 / id 不合规 / repo_key NULL 与空串
  4) 可选交叉验证：若有 obs_llm_usage，比对 chat 用量行的 engine_id 与"会读到的行"

用法
----
直连（本机 .venv 有 pymysql）：

    GRAPE_DB_HOST=127.0.0.1 GRAPE_DB_PORT=3306 GRAPE_DB_USER=... \
    GRAPE_DB_PASSWORD=... GRAPE_DB_NAME=grape_test_db \
    .venv/bin/python scripts/diag/chat_config_rows_audit.py

远端（目标机只有 mysql 客户端，如云端 master）——先出 SQL 再管道执行：

    .venv/bin/python scripts/diag/chat_config_rows_audit.py --emit-sql > /tmp/audit.sql
    ssh <host> "mysql -h<dbhost> -u<user> -p<pass> -t <db>" < /tmp/audit.sql

凭据一律通过环境变量/命令行传入，脚本内不存任何口令。
"""
from __future__ import annotations

import argparse
import os
import sys

# 让 `--with-current-read` 能 import 主服务代码（脚本在 scripts/diag/ 下，需把仓库根加进 sys.path）
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CANONICAL = "sess-cfg-"

# ---------------------------------------------------------------- SQL 定义
# 注意：全部为只读语句；不含字面量 '%'（避免 pymysql 参数绑定时的 Python 格式化问题）。
Q_OVERVIEW = [
    ("配置行总数（repo_key 为空/NULL 的用户级配置行）",
     "SELECT COUNT(*) AS v FROM chat_agent_session WHERE repo_key='' OR repo_key IS NULL"),
    ("表内总行数（含 repo 级会话行）",
     "SELECT COUNT(*) AS v FROM chat_agent_session"),
    ("有配置行的用户数",
     "SELECT COUNT(DISTINCT user_id) AS v FROM chat_agent_session WHERE repo_key='' OR repo_key IS NULL"),
    ("有 >=2 条配置行的用户数",
     "SELECT COUNT(*) AS v FROM (SELECT user_id FROM chat_agent_session"
     " WHERE repo_key='' OR repo_key IS NULL GROUP BY user_id HAVING COUNT(*)>1) t"),
    ("其中：跨行引擎设置冲突的用户数",
     "SELECT COUNT(*) AS v FROM (SELECT user_id FROM chat_agent_session"
     " WHERE repo_key='' OR repo_key IS NULL GROUP BY user_id"
     " HAVING COUNT(*)>1 AND COUNT(DISTINCT runtime_id)>1) t"),
    ("其中：跨行 im_app_id 冲突的用户数",
     "SELECT COUNT(*) AS v FROM (SELECT user_id FROM chat_agent_session"
     " WHERE repo_key='' OR repo_key IS NULL GROUP BY user_id"
     " HAVING COUNT(*)>1 AND COUNT(DISTINCT im_app_id)>1) t"),
    ("配置行里 id 不符合 sess-cfg-{user_id} 的行数",
     "SELECT COUNT(*) AS v FROM chat_agent_session WHERE (repo_key='' OR repo_key IS NULL)"
     " AND id <> CONCAT('sess-cfg-', user_id)"),
    ("全表（含 repo 级会话行）id 不符合 sess-cfg-{user_id} 的行数",
     "SELECT COUNT(*) AS v FROM chat_agent_session WHERE id <> CONCAT('sess-cfg-', user_id)"),
    ("写读错位用户数（写目标 sess-cfg-{user_id} 不是当前会被读到的那行）",
     "SELECT COUNT(*) AS v FROM (SELECT c.user_id FROM chat_agent_session c"
     " WHERE c.repo_key='' OR c.repo_key IS NULL GROUP BY c.user_id"
     " HAVING CONCAT('sess-cfg-', c.user_id) <> MIN(c.id)) t"),
    ("其中：永久不生效（写目标排在被读行之后，字符串序）→ 改配置永远读不回来",
     "SELECT COUNT(*) AS v FROM (SELECT c.user_id FROM chat_agent_session c"
     " WHERE c.repo_key='' OR c.repo_key IS NULL GROUP BY c.user_id"
     " HAVING CONCAT('sess-cfg-', c.user_id) > MIN(c.id)) t"),
    ("其中：下次写入会顶替（写目标排在前面）→ 改配置会生效，但历史行变僵尸",
     "SELECT COUNT(*) AS v FROM (SELECT c.user_id FROM chat_agent_session c"
     " WHERE c.repo_key='' OR c.repo_key IS NULL GROUP BY c.user_id"
     " HAVING CONCAT('sess-cfg-', c.user_id) < MIN(c.id)) t"),
    ("其中：既写读错位、跨行引擎又冲突的用户数（最坏：引擎改不动且在漂）",
     "SELECT COUNT(*) AS v FROM (SELECT c.user_id FROM chat_agent_session c"
     " WHERE c.repo_key='' OR c.repo_key IS NULL GROUP BY c.user_id"
     " HAVING CONCAT('sess-cfg-', c.user_id) <> MIN(c.id) AND COUNT(DISTINCT c.runtime_id)>1) t"),
    ("repo_key 为 NULL 的行数（应为空串，需归一）",
     "SELECT COUNT(*) AS v FROM chat_agent_session WHERE repo_key IS NULL"),
    ("runtime_id 指向不存在引擎（幽灵引擎）的配置行数",
     "SELECT COUNT(*) AS v FROM chat_agent_session c WHERE (c.repo_key='' OR c.repo_key IS NULL)"
     " AND c.runtime_id>0 AND NOT EXISTS (SELECT 1 FROM agent_runtimes ar WHERE ar.id=c.runtime_id)"),
    # 自检：明细表的过滤条件是否真的生效（防 "A OR B AND C" 优先级静默失效这类坑）
    ("自检：明细过滤是否生效（ok=生效）",
     "SELECT CASE WHEN"
     "  (SELECT COUNT(DISTINCT c.user_id) FROM chat_agent_session c"
     "    WHERE (c.repo_key='' OR c.repo_key IS NULL)"
     "      AND c.user_id IN (SELECT user_id FROM chat_agent_session"
     "                         WHERE repo_key='' OR repo_key IS NULL"
     "                         GROUP BY user_id HAVING COUNT(*)>1))"
     "  = (SELECT COUNT(*) FROM (SELECT user_id FROM chat_agent_session"
     "       WHERE repo_key='' OR repo_key IS NULL GROUP BY user_id HAVING COUNT(*)>1) t2)"
     " THEN 'ok' ELSE 'FILTER_MISMATCH(明细过滤未生效，查括号/优先级)' END AS v"),
]

Q_INDEXES = "SHOW INDEX FROM chat_agent_session"

# 每个用户的配置行明细 + 读/写路径判定
#   predict_read_id  : 读路径返回的行（模拟：与 InnoDB 全表扫描主键序一致，取 id 最小者，用库内排序规则计算）
#   write_target_id  : 写路径会写/替换的行
#   change_effective : 写进去的行是否就是会被读到的那行（否则"改配置不生效"）
Q_PER_USER = """
SELECT
    c.user_id,
    c.id,
    c.runtime_id,
    COALESCE(ar.name, '(引擎行不存在)') AS engine_name,
    IFNULL(c.im_app_id, '') AS im_app_id,
    IFNULL(c.skill, '') AS skill,
    IFNULL(c.skill_id, 0) AS skill_id,
    IFNULL(c.last_active_at, '') AS last_active_at,
    (SELECT MIN(c2.id) FROM chat_agent_session c2
      WHERE c2.user_id = c.user_id AND (c2.repo_key='' OR c2.repo_key IS NULL)) AS predict_read_id,
    CONCAT('sess-cfg-', c.user_id) AS write_target_id,
    CASE WHEN c.id = (SELECT MIN(c2.id) FROM chat_agent_session c2
                       WHERE c2.user_id = c.user_id AND (c2.repo_key='' OR c2.repo_key IS NULL))
         THEN 'yes' ELSE 'no' END AS is_read_row,
    CASE WHEN CONCAT('sess-cfg-', c.user_id) <= (SELECT MIN(c2.id) FROM chat_agent_session c2
                       WHERE c2.user_id = c.user_id AND (c2.repo_key='' OR c2.repo_key IS NULL))
         THEN 'yes' ELSE 'no' END AS write_would_take_effect
  FROM chat_agent_session c
  LEFT JOIN agent_runtimes ar ON ar.id = c.runtime_id
 WHERE (c.repo_key='' OR c.repo_key IS NULL)
   AND c.user_id IN (
        SELECT user_id FROM chat_agent_session
         WHERE repo_key='' OR repo_key IS NULL
         GROUP BY user_id HAVING COUNT(*)>1)
 ORDER BY c.user_id, c.id
"""

Q_MISMATCH_IDS = """
SELECT id, user_id, runtime_id, IFNULL(im_app_id,'') AS im_app_id
  FROM chat_agent_session
 WHERE id <> CONCAT('sess-cfg-', user_id)
 ORDER BY user_id, id
"""

Q_GHOST = """
SELECT c.id, c.user_id, c.runtime_id
  FROM chat_agent_session c
 WHERE (c.repo_key='' OR c.repo_key IS NULL)
   AND c.runtime_id > 0
   AND NOT EXISTS (SELECT 1 FROM agent_runtimes ar WHERE ar.id = c.runtime_id)
 ORDER BY c.user_id
"""

# 身份对照：配置行的 user_id 可能处在**旧 id 空间**（账号体系迁移/事故留下的错位）。
# 旧空间靠 unified_user_mapping 认人，新空间靠 user_accounts；两边都缺时标"(无账号/无映射)"。
# 注意：不同环境的这两张表可能不存在（本地测试库就没有 mapping），查询失败可忽略。
Q_IDENTITY = """
SELECT c.user_id,
       COUNT(*) AS 配置行数,
       CASE WHEN c.user_id < (SELECT MIN(id) FROM user_accounts) THEN '旧 id 空间' ELSE '新 id 空间' END AS id空间,
       IFNULL(m.gitcode_user, '') AS gitcode_user,
       IFNULL(m.gitcode_nickname, '') AS gitcode昵称,
       IFNULL(m.feishu_name, '') AS 飞书名,
       IFNULL(m.app_id, '') AS im_app_id,
       IFNULL(ua.id, 0) AS 账号表id,
       IFNULL(ua.username, '') AS 账号username
  FROM chat_agent_session c
  LEFT JOIN unified_user_mapping m ON m.user_id = c.user_id
  LEFT JOIN user_accounts ua ON ua.gitcode_id = m.gitcode_id AND ua.gitcode_id <> ''
 WHERE c.repo_key='' OR c.repo_key IS NULL
 GROUP BY c.user_id, m.gitcode_user, m.gitcode_nickname, m.feishu_name, m.app_id, ua.id, ua.username
 ORDER BY c.user_id
"""

# 交叉验证（可选，库里有 obs_llm_usage 才有意义）：chat 用量行的 engine_id
# 是否等于该用户"会被读到的那行"的 runtime_id —— 不等即说明运行用的引擎与配置读到的不是同一行。
# 注意：这里的 predict_read_* 是**修复前**口径（按主键字符串序取 MIN(id)）。
# 修复后的口径请用 direct 模式（末尾会调真实的 `_pick_chat_config_row` 补打"当前读法"）。
Q_CROSSCHECK = """
SELECT u.user_id,
       COUNT(*) AS chat_rows,
       MAX(u.engine_id) AS engine_id_used,
       (SELECT MIN(c2.id) FROM chat_agent_session c2
         WHERE c2.user_id = u.user_id AND (c2.repo_key='' OR c2.repo_key IS NULL)) AS predict_read_id,
       (SELECT MAX(c3.runtime_id) FROM chat_agent_session c3
         WHERE c3.id = (SELECT MIN(c2.id) FROM chat_agent_session c2
                         WHERE c2.user_id = u.user_id
                           AND (c2.repo_key='' OR c2.repo_key IS NULL))) AS predict_read_runtime_id
  FROM obs_llm_usage u
 WHERE u.run_kind = 'chat' AND u.user_id > 0
 GROUP BY u.user_id
 ORDER BY u.user_id
"""


def _assert_read_only(sql: str) -> str:
    head = sql.strip().lstrip("(").split(None, 1)[0].upper() if sql.strip() else ""
    if head not in {"SELECT", "SHOW", "DESC", "DESCRIBE", "EXPLAIN"}:
        raise SystemExit(f"拒绝执行非只读语句：{sql[:60]}...")
    return sql


def emit_sql(dbname: str) -> None:
    """输出可直接喂给 mysql 客户端的只读 SQL（每条前有注释标题，便于人工核对）。"""
    print(f"-- chat_agent_session 配置行只读诊断 @ {dbname}")
    print("-- 严格只读：仅 SELECT/SHOW；不修改任何数据\n")
    print("SET SESSION TRANSACTION READ ONLY;\n")
    for title, sql in Q_OVERVIEW:
        print(f"-- {title}")
        print(_assert_read_only(sql).rstrip(";") + ";\n")
    print("-- 表索引（判断读路径顺序是否依赖主键序）")
    print(_assert_read_only(Q_INDEXES) + ";\n")
    print("-- 每个用户的配置行明细 + 读/写路径判定（user_id 只看有重复行者）")
    print(_assert_read_only(Q_PER_USER).strip() + ";\n")
    print("-- id 不符合 sess-cfg-{user_id} 的行")
    print(_assert_read_only(Q_MISMATCH_IDS).strip() + ";\n")
    print("-- 幽灵引擎（runtime_id 指向已不存在的引擎行）")
    print(_assert_read_only(Q_GHOST).strip() + ";\n")
    print("-- 身份对照（配置行 user_id → 旧 id 空间映射 / 新 id 空间账号；表不存在时报错可忽略）")
    print(_assert_read_only(Q_IDENTITY).strip() + ";\n")
    # 放在**最后**：mysql 客户端遇到错误会中止后续语句，而这张表在部分库不存在
    print("-- 交叉验证：chat 用量 engine_id vs 会读到的行（无 obs_llm_usage 时本语句报错，"
          "已是最后一条，不影响前面的输出；或给 mysql 加 --force）")
    print(_assert_read_only(Q_CROSSCHECK).strip() + ";\n")


def run_direct(args) -> int:
    try:
        import pymysql  # noqa: WPS433
    except ImportError:
        print("缺少 pymysql：请用 .venv/bin/python 运行，或改用 --emit-sql 模式。", file=sys.stderr)
        return 2
    conn = pymysql.connect(host=args.host, port=args.port, user=args.user,
                           password=args.password, database=args.db, charset="utf8mb4",
                           cursorclass=pymysql.cursors.Cursor)
    print(f"== chat_agent_session 配置行只读诊断 ==\n   目标：{args.user}@{args.host}:{args.port}/{args.db}\n")
    with conn.cursor() as c:
        # 只读会话（尽力而为；不支持时忽略）
        try:
            c.execute("SET SESSION TRANSACTION READ ONLY")
        except Exception:
            pass
        print("--- 总览 ---")
        for title, sql in Q_OVERVIEW:
            try:
                c.execute(_assert_read_only(sql))
                row = c.fetchone()
                print(f"  {title}: {row[0] if row else 'n/a'}")
            except Exception as e:
                print(f"  {title}: 查询失败（{str(e)[:80]}）")
        print("\n--- 表索引 ---")
        try:
            c.execute(Q_INDEXES)
            cols = [d[0] for d in c.description]
            for r in c.fetchall():
                d = dict(zip(cols, r))
                print(f"  {d.get('Key_name')} col={d.get('Column_name')} unique={'是' if d.get('Non_unique') == 0 else '否'}")
        except Exception as e:
            print(f"  查询失败（{str(e)[:80]}）")
        for label, sql in (("每个用户的配置行明细 + 读/写判定", Q_PER_USER),
                           ("id 不合规的行", Q_MISMATCH_IDS),
                           ("幽灵引擎行", Q_GHOST),
                           ("身份对照（旧 id 空间映射 / 新 id 空间账号）", Q_IDENTITY),
                           ("交叉验证（chat 用量 vs 会读到的行）", Q_CROSSCHECK)):
            print(f"\n--- {label} ---")
            try:
                c.execute(_assert_read_only(sql))
                cols = [d[0] for d in c.description]
                print("  " + " | ".join(cols))
                for r in c.fetchall():
                    print("  " + " | ".join("" if v is None else str(v) for v in r))
            except Exception as e:
                print(f"  查询失败（{str(e)[:100]}）—— 若为表不存在可忽略")
        # 修复后口径：直接调**真实函数**（同一份逻辑，避免口径漂移）补打"当前读法"
        # 默认关闭：import 主服务模块会触发其 import 期自检（例如"清理废弃列"），而本脚本承诺只读。
        if not args.with_current_read:
            print("\n--- 当前读法（修复后口径）--- 需加 --with-current-read 才会用真实函数计算（见脚本说明）")
            conn.close()
            return 0
        print("\n--- 当前读法（修复后：优先写路径行 + 跳过引擎不可用的行；真实函数，非预测）---")
        print("    注意：本项会 import 主服务代码，可能触发其 import 期自检（如清理废弃列）")
        try:
            # 用**主服务自己的 DB 句柄**（pymysql 直连不认 `?` 占位符，必须走 compat 层）
            from src.session_manager import db as appdb, db_lock, _pick_chat_config_row
            print(f"      （用主服务配置的库计算：{getattr(appdb, 'backend', '') or 'configs/db-backend.json'}）")
            with db_lock:
                ac = appdb.cursor()
                ac.execute("SELECT DISTINCT user_id FROM chat_agent_session"
                           " WHERE repo_key='' OR repo_key IS NULL ORDER BY user_id")
                seen = [r[0] for r in ac.fetchall() if r[0]]
                if seen:
                    print(f"  {'user_id':>8} | {'当前会读到的行':<15} | 引擎")
                    for uid in seen:
                        picked_id, row, backend = _pick_chat_config_row(ac, uid)
                        print(f"  {uid:>8} | {picked_id or '(无配置行)':<15} | {backend or '—'}")
        except Exception as e:
            print(f"  需要主服务代码可导入（在仓库根用 .venv/bin/python 运行）：{str(e)[:120]}")
    conn.close()
    print("\n（只读诊断结束，未修改任何数据）")
    return 0


def db_cursor_engines(c, runtime_id):
    """取引擎名（小工具，供上面的"当前读法"打印用）。"""
    if not runtime_id:
        return ""
    try:
        c.execute("SELECT name FROM agent_runtimes WHERE id=?", (runtime_id,))
        r = c.fetchone()
        return (r[0] if r else "")
    except Exception:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="chat_agent_session 配置行只读诊断（迁移前基线）")
    ap.add_argument("--host", default=os.environ.get("GRAPE_DB_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("GRAPE_DB_PORT", "3306")))
    ap.add_argument("--user", default=os.environ.get("GRAPE_DB_USER", "root"))
    ap.add_argument("--password", default=os.environ.get("GRAPE_DB_PASSWORD", ""))
    ap.add_argument("--db", default=os.environ.get("GRAPE_DB_NAME", "grape_test_db"))
    ap.add_argument("--emit-sql", action="store_true",
                    help="只打印只读 SQL（给远端 mysql 客户端管道执行），不连库")
    ap.add_argument("--with-current-read", action="store_true",
                    help="额外用主服务真实函数算一遍\"修复后会读到哪行\"（会 import 主服务代码，"
                         "可能触发其 import 期自检；需要仓库可导入）")
    args = ap.parse_args()
    if args.emit_sql:
        emit_sql(args.db)
        return 0
    if not args.password:
        print("缺少密码：设置 GRAPE_DB_PASSWORD 或 --password（脚本不内置任何凭据）", file=sys.stderr)
        return 2
    return run_direct(args)


if __name__ == "__main__":
    raise SystemExit(main())