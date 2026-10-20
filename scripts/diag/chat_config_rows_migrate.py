#!/usr/bin/env python3
"""chat_agent_session 配置行迁移 —— 「rename 代替搬数据」版（方案 §12.3 路子 1）

只做三步（其余一律跳过、交人工裁决）：

  A 类  用户的 canonical 行（`sess-cfg-{user_id}`）**就是**当前被读到的那行，且还有其他行
        → 把多余行的"独有非空字段"并进 canonical（保住 IM 绑定等），再删多余行
  B1 类 没有 canonical 行、且该用户只有 1 行
        → 直接把这行 **id 改名**成 canonical（零数据搬移）
  D 类  canonical 行存在但**引擎已不可用**（指向已删引擎/未知引擎），且该用户只有另一行且它在被读
        → 先并字段（若有独有非空）→ 删掉死 canonical → 把在读那行改名为 canonical
        （严格校验：死行必须属于本人 user_id；目标行必须存在且引擎可用）
  其余  多行且 canonical 缺失 / 归属存疑
        → **跳过**，打印到"待裁决"清单（配 decisions 文件才处理，本次不实现）

安全设计
--------
* 默认 `--dry-run`：只读，打印动作清单与预计影响，不写任何数据
* `--apply --yes` 才写；先建备份表 `bak_chat_cfg_migrate_<date>`（整表快照），每步一个事务
* 复用**生产同一份逻辑**（`src.session_manager._pick_chat_config_row`），不另写一套判定
* 已核实：`sess-cfg-*` id 不被任何其他表引用（0 处）→ 改名安全；失败即中止，备份可回滚

注意
----
本脚本会 `import src.session_manager` 以复用读取逻辑；该模块有 import 期自检（如清理废弃列），
**作用在主服务配置指向的那个库**（configs/db-backend.json / 环境变量），不是本脚本的 --db 目标。
建议在仓库根用 `.venv/bin/python` 运行，并确认两者指向同一库后再 --apply。

用法
----
    # 只读预演（本地/云端都可）
    .venv/bin/python scripts/diag/chat_config_rows_migrate.py \
        --host <dbhost> --port 3306 --user <u> --password <p> --db <db>

    # 真跑（会先备份）
    .venv/bin/python scripts/diag/chat_config_rows_migrate.py ... --apply --yes
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CANON = "sess-cfg-"
# 需要"并进 canonical"的独有字段（REPLACE/UPDATE 时列全，避免抹掉未列出的列）
MERGE_COLS = ("im_app_id", "im_app_secret", "im_provider", "skill", "skill_id",
              "runtime", "runtime_session_id", "status", "event_history", "source", "repository")


class CompatCursor:
    """把 `?` 占位符翻成 `%s` 的游标包装（主服务内部 SQL 用 SQLite 风格占位符）。"""

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
    def description(self):
        return self._c.description

    @property
    def rowcount(self):
        return self._c.rowcount


def classify(c, user_id):
    """返回 (picked_id, picked_row, backend, canonical_exists)。

    复用主服务真实逻辑：候选序 = canonical 优先 + 遗留行按 last_active DESC。
    """
    from src.session_manager import _pick_chat_config_row
    picked_id, row, backend = _pick_chat_config_row(c, user_id)
    c.execute("SELECT COUNT(*) FROM chat_agent_session WHERE id=? AND (repo_key='' OR repo_key IS NULL)",
              (CANON + str(user_id),))
    canon_exists = (c.fetchone() or [0])[0] > 0
    return picked_id, row, backend, canon_exists


def load_users(c):
    c.execute("SELECT DISTINCT user_id FROM chat_agent_session WHERE repo_key='' OR repo_key IS NULL ORDER BY user_id")
    return [r[0] for r in c.fetchall() if r[0]]


def rows_of(c, user_id):
    c.execute("SELECT id, runtime_id, " + ", ".join(MERGE_COLS) +
              " FROM chat_agent_session WHERE user_id=? AND (repo_key='' OR repo_key IS NULL)"
              " ORDER BY last_active_at DESC, id DESC", (user_id,))
    return c.fetchall()


def plan(c, users):
    """生成动作清单。返回 (actions, skipped)。"""
    actions, skipped = [], []
    for uid in users:
        picked_id, row, backend, canon_exists = classify(c, uid)
        rows = rows_of(c, uid)
        canon_id = CANON + str(uid)
        if not rows:
            continue
        if picked_id == canon_id and len(rows) == 1:
            continue          # 已对齐（单行且就是 canonical）→ 无需动作
        if picked_id == canon_id and len(rows) > 1:
            zoms = [r for r in rows if r[0] != canon_id]
            actions.append(dict(kind="A", uid=uid, canon_id=canon_id, keep=canon_id,
                                delete=[r[0] for r in zoms], rows=rows, backend=backend,
                                merge=[(r[0], {col: r[2 + i] for i, col in enumerate(MERGE_COLS)}) for r in zoms]))
        elif (not canon_exists) and len(rows) == 1:
            actions.append(dict(kind="B1", uid=uid, canon_id=canon_id, keep=rows[0][0],
                                rename=(rows[0][0], canon_id), rows=rows, backend=backend))
        elif canon_exists and picked_id != canon_id and len(rows) == 2:
            # D 类：canonical 是与本人同 user_id 的"死行"（引擎不可用），另一行在读 → 删死行 + 把在读行改名
            from src.session_manager import _chat_config_row_engine
            dead = [r for r in rows if r[0] == canon_id]
            good = [r for r in rows if r[0] != canon_id]
            if len(dead) == 1 and len(good) == 1:
                c.execute("SELECT user_id FROM chat_agent_session WHERE id=?", (canon_id,))
                _owner = (c.fetchone() or [None])[0]
                _rt = dead[0][1]
                _, usable = _chat_config_row_engine(c, _rt)
                if _owner == uid and not usable and picked_id == good[0][0]:
                    actions.append(dict(kind="D", uid=uid, canon_id=canon_id, keep=good[0][0],
                                        rename=(good[0][0], canon_id), delete=[canon_id], rows=rows,
                                        backend=backend,
                                        merge=[(canon_id, {col: dead[0][2 + i] for i, col in enumerate(MERGE_COLS)})]))
                    continue
            skipped.append(dict(uid=uid, picked=picked_id, n=len(rows),
                                why="canonical 与本人不同源/不可判定", rows=[(r[0], r[1], r[2]) for r in rows]))
        else:
            why = ("canonical 名字的行属于别人（跨空间占用）或引擎不可用" if canon_exists and picked_id != canon_id
                   else "多行且 canonical 缺失" if len(rows) > 1 else "身份存疑")
            skipped.append(dict(uid=uid, picked=picked_id, n=len(rows), why=why,
                                rows=[(r[0], r[1], r[2]) for r in rows]))
    return actions, skipped


def print_plan(actions, skipped, dbname):
    print(f"== chat_agent_session 迁移预演（rename 版） @ {dbname} ==\n")
    print(f"待执行用户 {len(actions)} ｜ 跳过（待裁决）{len(skipped)}\n")
    for a in actions:
        if a["kind"] == "A":
            print(f"  [A] user {a['uid']:>4}  保留 {a['keep']}（{a['backend'] or '—'}）"
                  f"  删除 {len(a['delete'])} 行: {', '.join(a['delete'])}")
            for rid, vals in a["merge"]:
                take = {k: v for k, v in vals.items() if v not in ("", None, 0)}
                if take:
                    print(f"        ↳ 先把 {rid} 的独有非空字段并进 canonical: "
                          + ", ".join(f"{k}={'***' if 'secret' in k else v}" for k, v in take.items()))
        elif a["kind"] == "B1":
            print(f"  [B1] user {a['uid']:>4}  改名 {a['rename'][0]} → {a['canon_id']}（零数据搬移）")
        else:
            print(f"  [D]  user {a['uid']:>4}  删死 canonical {a['delete'][0]}（引擎不可用）"
                  f" → 再把 {a['rename'][0]} 改名成 {a['canon_id']}（现在读到的就是它）")
    if skipped:
        print(f"\n-- 跳过（保持原样，等人工裁决）--")
        for s in skipped:
            print(f"  user {s['uid']:>4}  读到={s['picked']}  行数={s['n']}  原因={s['why']}"
                  f"  行: {', '.join(r[0] for r in s['rows'])}")
    return actions


def apply_plan(conn, c, actions, dbname):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = f"bak_chat_cfg_migrate_{stamp}"
    print(f"\n[1/3] 备份整表 → {bak}")
    c.execute(f"CREATE TABLE {bak} LIKE chat_agent_session")
    c.execute(f"INSERT INTO {bak} SELECT * FROM chat_agent_session")
    c.execute("SELECT COUNT(*) FROM chat_agent_session")
    n1 = c.fetchone()[0]
    c.execute(f"SELECT COUNT(*) FROM {bak}")
    n2 = c.fetchone()[0]
    assert n1 == n2, f"备份行数不一致：原表 {n1} vs 备份 {n2}"
    conn.commit()
    print(f"      原表 {n1} 行 = 备份 {n2} 行 ✓")

    print(f"\n[2/3] 执行 {len(actions)} 个用户")
    for a in actions:
        try:
            if a["kind"] == "A":
                for rid, vals in a["merge"]:
                    sets, params = [], []
                    for col in MERGE_COLS:
                        v = vals.get(col)
                        if v not in ("", None, 0):
                            sets.append(f"{col}=?")
                            params.append(v)
                    if sets:
                        c.execute(f"UPDATE chat_agent_session SET {', '.join(sets)} WHERE id=?",
                                  tuple(params) + (a["canon_id"],))
                for rid in a["delete"]:
                    c.execute("DELETE FROM chat_agent_session WHERE id=? AND (repo_key='' OR repo_key IS NULL)", (rid,))
                conn.commit()
                print(f"  ✓ [A] user {a['uid']}: 合并字段 + 删除 {len(a['delete'])} 行")
            elif a["kind"] == "D":
                for rid, vals in a["merge"]:
                    sets, params = [], []
                    for col in MERGE_COLS:
                        v = vals.get(col)
                        if v not in ("", None, 0):
                            sets.append(f"{col}=?")
                            params.append(v)
                    if sets:
                        c.execute(f"UPDATE chat_agent_session SET {', '.join(sets)} WHERE id=?",
                                  tuple(params) + (a["keep"],))
                old, new = a["rename"]
                c.execute("SELECT COUNT(*) FROM chat_agent_session WHERE id=?", (old,))
                assert (c.fetchone() or [0])[0] == 1, f"目标行 {old} 不存在，中止"
                c.execute("DELETE FROM chat_agent_session WHERE id=? AND user_id=?", (a["delete"][0], a["uid"]))
                dr = c.rowcount
                c.execute("UPDATE chat_agent_session SET id=? WHERE id=?", (new, old))
                conn.commit()
                print(f"  ✓ [D] user {a['uid']}: 删死行 {dr} 行 + {old} → {new}")
            else:
                old, new = a["rename"]
                c.execute("SELECT COUNT(*) FROM chat_agent_session WHERE id=?", (new,))
                if c.fetchone()[0]:
                    print(f"  ✗ [B1] user {a['uid']}: 目标 {new} 已存在，跳过（需人工）")
                    continue
                c.execute("UPDATE chat_agent_session SET id=? WHERE id=?", (new, old))
                conn.commit()
                print(f"  ✓ [B1] user {a['uid']}: {old} → {new}")
        except Exception as e:
            conn.rollback()
            print(f"  ✗ user {a['uid']} 失败并回滚该用户：{str(e)[:140]}")

    print(f"\n[3/3] 校验")
    users = load_users(c)
    bad = 0
    for uid in users:
        picked_id, row, backend, canon_exists = classify(c, uid)
        if canon_exists and picked_id != CANON + str(uid):
            print(f"  ⚠ user {uid}: canonical 存在但不是被读到的行（{picked_id}）")
            bad += 1
    c.execute("SELECT COUNT(*) FROM (SELECT user_id FROM chat_agent_session WHERE repo_key='' OR repo_key IS NULL"
              " GROUP BY user_id HAVING COUNT(*)>1) t")
    print(f"  仍有多个配置行的用户：{c.fetchone()[0]}（本次按设计只处理 A/B1 两类，其余待裁决）")
    print(f"  canonical 异常用户：{bad}")
    print(f"  备份表：{bak}（回滚：DELETE 配置行 + 从该表回灌）")
    return bak


def main():
    ap = argparse.ArgumentParser(description="chat_agent_session 配置行迁移（rename 版，默认 dry-run）")
    ap.add_argument("--host", default=os.environ.get("GRAPE_DB_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("GRAPE_DB_PORT", "3306")))
    ap.add_argument("--user", default=os.environ.get("GRAPE_DB_USER", "root"))
    ap.add_argument("--password", default=os.environ.get("GRAPE_DB_PASSWORD", ""))
    ap.add_argument("--db", default=os.environ.get("GRAPE_DB_NAME", "grape_test_db"))
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
        # 预演模式：把会话设成只读，物理上杜绝任何写入（写模式当然不能设，否则 CREATE 备份表都会失败）
        try:
            c.execute("SET SESSION TRANSACTION READ ONLY")
        except Exception:
            pass
    users = load_users(c)
    actions, skipped = plan(c, users)
    print_plan(actions, skipped, args.db)
    if not args.apply:
        print("\n（预演结束，未修改任何数据；加 --apply --yes 执行）")
        conn.close()
        return 0
    if not args.yes:
        print("\n需要 --yes 确认执行（本次未写）", file=sys.stderr)
        conn.close()
        return 2
    print("\n" + "=" * 60 + "\n开始执行\n" + "=" * 60)
    bak = apply_plan(conn, c, actions, args.db)
    print(f"\n完成。备份表 {bak}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
