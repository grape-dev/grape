#!/usr/bin/env python3
"""Issue #431：从 bak_chat_cfg_migrate_* 备份表恢复被静默清空的飞书 IM 凭证。

背景：PUT /api/chat/config 普通保存（不带 im 字段）曾把 chat_agent_session 的
im_app_id / im_app_secret / im_provider 清空（缺陷已修复）。scripts/diag/chat_config_rows_migrate.py
在执行迁移前会建 `bak_chat_cfg_migrate_<date>` 整表快照，本脚本从最新（或 --bak 指定）备份表
把「当前行为空、备份中非空」的 IM 三字段恢复回去。

安全设计
--------
* 默认 dry-run：只读，打印恢复清单，不写任何数据
* `--apply --yes` 才写；写前先把当前行整行快照进 `bak_chat_cfg_im_restore_<date>`
* 恢复条件（全部满足才恢复）：
  1. 当前 canonical 行（sess-cfg-{uid}，repo_key=''）三字段任一为空/与备份不一致
  2. 备份表中该用户存在 repo_key='' 行且 im_app_id 非空
  3. 该 im_app_id 未被其他用户当前占用（避免破坏 app_id 全局唯一约束）
* `--user` 可只处理指定 user_id（如先恢复 user_id=1）

用法
----
    # 只读预演
    .venv/bin/python scripts/diag/chat_config_im_restore.py \
        --host <dbhost> --port 3306 --user <u> --password <p> --db <db>

    # 只预演 user_id=1
    ... --user 1

    # 真跑（会先备份）
    ... --apply --yes
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

IM_COLS = ("im_app_id", "im_app_secret", "im_provider")


class CompatCursor:
    """把 `?` 占位符翻成 `%s` 的游标包装（与 chat_config_rows_migrate.py 同款）。"""

    def __init__(self, cur):
        self._c = cur

    def execute(self, sql, params=None):
        q = sql.replace("?", "%s") if params else sql
        if params:
            return self._c.execute(q, params)
        return self._c.execute(q)

    def fetchone(self):
        return self._c.fetchone()

    def fetchall(self):
        return self._c.fetchall()

    @property
    def rowcount(self):
        return self._c.rowcount


def find_backup(c, bak=None):
    """选备份表：--bak 指定 > 按表名时间戳取最新 bak_chat_cfg_migrate_*。"""
    if bak:
        c.execute("SHOW TABLES LIKE %s", (bak,))
        if not c.fetchone():
            print(f"备份表 {bak} 不存在", file=sys.stderr)
            return None
        return bak
    c.execute("SHOW TABLES LIKE 'bak_chat_cfg_migrate_%'")
    rows = [r[0] for r in c.fetchall()]
    if not rows:
        print("未找到 bak_chat_cfg_migrate_* 备份表", file=sys.stderr)
        return None
    return sorted(rows)[-1]


def plan_restore(c, bak, only_uid=None):
    """生成恢复清单：[(uid, canon_id, {col: backup_value})]。"""
    c.execute(f"SELECT user_id, id, im_app_id, im_app_secret, im_provider FROM {bak} "
              "WHERE (repo_key='' OR repo_key IS NULL) AND im_app_id!=''")
    bak_rows = {}
    for uid, rid, aid, sec, prov in c.fetchall():
        if only_uid and uid != only_uid:
            continue
        # 同用户多行时优先 canonical 行，其次 app_id 非空的最后一行
        if uid not in bak_rows or rid.startswith("sess-cfg-"):
            bak_rows[uid] = (rid, aid, sec, prov)
    actions = []
    for uid, (brid, aid, sec, prov) in sorted(bak_rows.items()):
        canon = f"sess-cfg-{uid}"
        c.execute("SELECT id, im_app_id, im_app_secret, im_provider FROM chat_agent_session "
                  "WHERE user_id=? AND (repo_key='' OR repo_key IS NULL) ORDER BY (id=?) DESC, last_active_at DESC LIMIT 1",
                  (uid, canon))
        cur = c.fetchone()
        if not cur:
            print(f"  ? user {uid}: 当前无配置行（备份 {brid} 有 {aid[:20]}），跳过（需人工）")
            continue
        cur_id, cur_aid, cur_sec, cur_prov = cur
        if cur_aid == aid and cur_sec == sec and cur_prov == prov:
            continue  # 完全一致，无需恢复
        if cur_aid and cur_aid != aid:
            print(f"  ? user {uid}: 当前已绑 {cur_aid[:20]}（≠备份 {aid[:20]}），跳过（可能是有意换绑）")
            continue
        # app_id 全局唯一：恢复前确认无其他用户占用
        c.execute("SELECT COUNT(*) FROM chat_agent_session WHERE im_app_id=? AND user_id!=? AND im_app_id!=''",
                  (aid, uid))
        if c.fetchone()[0] > 0:
            print(f"  ? user {uid}: 备份凭证 {aid[:20]} 已被其他用户占用，跳过（需人工）")
            continue
        actions.append((uid, cur_id, {"im_app_id": aid, "im_app_secret": sec, "im_provider": prov}))
    return actions


def apply_restore(conn, c, actions):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    rbak = f"bak_chat_cfg_im_restore_{stamp}"
    print(f"\n[1/2] 备份当前整表 → {rbak}")
    c.execute(f"CREATE TABLE {rbak} LIKE chat_agent_session")
    c.execute(f"INSERT INTO {rbak} SELECT * FROM chat_agent_session")
    conn.commit()
    print(f"[2/2] 恢复 {len(actions)} 个用户的 IM 凭证")
    for uid, rid, vals in actions:
        try:
            sets = ", ".join(f"{col}=?" for col in IM_COLS)
            c.execute(f"UPDATE chat_agent_session SET {sets} WHERE id=?", tuple(vals[col] for col in IM_COLS) + (rid,))
            conn.commit()
            print(f"  ✓ user {uid} ({rid}): im_app_id={vals['im_app_id'][:20]}...")
        except Exception as e:
            conn.rollback()
            print(f"  ✗ user {uid} 失败并回滚：{str(e)[:140]}")
    return rbak


def main():
    ap = argparse.ArgumentParser(description="Issue #431：从 bak_chat_cfg_migrate_* 恢复被清空的 IM 凭证（默认 dry-run）")
    ap.add_argument("--host", default=os.environ.get("GRAPE_DB_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("GRAPE_DB_PORT", "3306")))
    ap.add_argument("--user", default=os.environ.get("GRAPE_DB_USER", "root"))
    ap.add_argument("--password", default=os.environ.get("GRAPE_DB_PASSWORD", ""))
    ap.add_argument("--db", default=os.environ.get("GRAPE_DB_NAME", ""))
    ap.add_argument("--bak", default=None, help="指定备份表名（默认自动选最新 bak_chat_cfg_migrate_*）")
    ap.add_argument("--uid", type=int, default=None, help="只处理该 user_id（如 1）")
    ap.add_argument("--apply", action="store_true", help="执行（默认只预演）")
    ap.add_argument("--yes", action="store_true", help="确认执行")
    args = ap.parse_args()
    if not args.password:
        print("缺少密码：设置 GRAPE_DB_PASSWORD 或 --password", file=sys.stderr)
        return 2
    import pymysql
    conn = pymysql.connect(host=args.host, port=args.port, user=args.user, password=args.password,
                           database=args.db, charset="utf8mb4")
    c = CompatCursor(conn.cursor())
    if not args.apply:
        try:
            c.execute("SET SESSION TRANSACTION READ ONLY")
        except Exception:
            pass
    bak = find_backup(c, args.bak)
    if not bak:
        conn.close()
        return 2
    print(f"== Issue #431 IM 凭证恢复 @ {args.db}（备份表: {bak}）==\n")
    actions = plan_restore(c, bak, args.uid)
    if not actions:
        print("无需恢复（当前 IM 字段与备份一致 / 无满足条件的行）")
        conn.close()
        return 0
    for uid, rid, vals in actions:
        print(f"  [恢复] user {uid} ({rid}): im_app_id={vals['im_app_id'][:20]}... "
              f"im_provider={vals['im_provider']}")
    if not args.apply:
        print("\n（预演结束，未修改任何数据；加 --apply --yes 执行）")
        conn.close()
        return 0
    if not args.yes:
        print("\n需要 --yes 确认执行（本次未写）", file=sys.stderr)
        conn.close()
        return 2
    rbak = apply_restore(conn, c, actions)
    print(f"\n完成。回滚备份表 {rbak}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
