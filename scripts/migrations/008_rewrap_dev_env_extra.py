"""迁移 008_rewrap_dev_env_extra（2026-11-21）——agent_tasks 存量 dev 信封重加密。

背景
----
security_config._load() 并发首读会短暂把空 dict 发布成模块缓存，
get_credential_keys() 于是回落到内置 ``dev`` 常量（= base64 的 32 个 '0'，
公开常量），落库信封变成 ``enc:v1:dev:<nonce>:<ct>``。执行侧（配了
credential_keys=cred1 的 worker）解不开 → 按空 env 跑 → 场景 work_branch
丢失 → 回落到不存在的 main 比基线 → 任务 1~3 秒 failed，报
``workspace baseline mismatch ... reason=head_unreachable``。

代码侧已修（security_config._load 原子发布 + credential_crypto._load_keys
不 latch/生产拒 dev）。本脚本修存量：把 dev 信封解开，用当前密钥重新封。

口径
----
* 只处理 ``env_extra LIKE 'enc:v1:dev:%'``（其它 kid 一律不动）；
* 解开的是当时的完整 env（明文 JSON），重封用 credential_crypto.encrypt，
  即当前 kid（正常为 cred1）；解不开或重封失败的行原样保留并计入 failed；
* 幂等：跑完第二次 dry-run 应为 0 行；
* 先整批备份到 ``agent_tasks_env_extra_bak_<UTC>``（id, env_extra），再改。

三库拓扑（必读）
----------------
agent_tasks 在 **runtime 库**，脚本连的是 DEVOPS_DB_* 指的库；只给
GRAPE_DSN_* 会回落本地 SQLite（连错库且不报错）。生产执行（占位符按环境替换）：

    DEVOPS_DB_BACKEND=mysql DEVOPS_DB_HOST=<mysql-host> DEVOPS_DB_PORT=3306 \\
    DEVOPS_DB_USER=<runtime-db-user> DEVOPS_DB_PASSWORD=<password> \\
    DEVOPS_DB_NAME=<runtime-db> \\
    .venv/bin/python scripts/migrations/008_rewrap_dev_env_extra.py [--apply]

用法
----
    python3 scripts/migrations/008_rewrap_dev_env_extra.py          # dry-run
    python3 scripts/migrations/008_rewrap_dev_env_extra.py --apply  # 备份+改写
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time

_MIGRATION_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_MIGRATION_DIR))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

TABLE = "agent_tasks"
ENVELOPE_PREFIX = "enc:v1:"
DEV_KID = "dev"
# kid=dev 的内置密钥（公开常量，等同明文）——只用于把存量旧信封解开
_DEV_KEY = b"0" * 32


def _dev_decrypt(raw: str) -> str:
    """解开 enc:v1:dev:<nonce>:<ct> → 明文（AES-256-GCM，内置常量密钥）。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    body = raw[len(ENVELOPE_PREFIX):]
    kid, nonce_b64, ct_b64 = body.split(":", 2)
    if kid != DEV_KID:
        raise ValueError(f"不是 dev 信封（kid={kid}）")
    plain = AESGCM(_DEV_KEY).decrypt(base64.b64decode(nonce_b64),
                                     base64.b64decode(ct_b64), None)
    return plain.decode()


def _rows(conn, dev_only: bool = True):
    where = f"{TABLE}.env_extra LIKE ?" if dev_only else "1=1"
    params = ("enc:v1:dev:%",) if dev_only else ()
    cur = conn.execute(
        f"SELECT id, env_extra, created_at, status FROM {TABLE} WHERE {where} "
        f"ORDER BY created_at", params)
    return cur.fetchall() or []


def _plan(rows):
    """分类：可重封 / 解不开 / 重封失败。纯函数，dry-run 与 apply 共用。"""
    from src.domains.mgmt.auth.credential_crypto import encrypt

    ready, undecodable, rewrite_failed, samples = [], [], [], []
    by_status = {}
    for rid, raw, created_at, status in rows:
        by_status[str(status or "")] = by_status.get(str(status or ""), 0) + 1
        try:
            plain = _dev_decrypt(raw)
            env = json.loads(plain)
        except Exception:
            undecodable.append(rid)
            continue
        try:
            sealed = encrypt(json.dumps(env, ensure_ascii=False))
            new_kid = sealed.split(":", 1)[0]
        except Exception:
            rewrite_failed.append(rid)
            continue
        if new_kid == DEV_KID:
            # 密钥环仍坏（当前进程也只会写 dev）→ 拒绝落库，先修进程
            rewrite_failed.append(rid)
            continue
        ready.append((rid, ENVELOPE_PREFIX + sealed, len(env)))
        if len(samples) < 5:
            samples.append((rid, new_kid, sorted(env)[:6], bool(env.get("GRAPE_WORK_BRANCH"))))
    return {"rows": len(rows), "ready": ready, "undecodable": undecodable,
            "rewrite_failed": rewrite_failed, "by_status": by_status,
            "samples": samples}


def _backup(conn, plan) -> str:
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    bak = f"{TABLE}_env_extra_bak_{ts}"
    conn.execute(f"CREATE TABLE {bak} (id VARCHAR(191) PRIMARY KEY, env_extra MEDIUMTEXT, "
                 f"backed_up_at VARCHAR(32))")
    conn.commit()
    n = 0
    for rid, _new, _size in plan["ready"]:
        cur = conn.execute(f"SELECT env_extra FROM {TABLE} WHERE id = ?", (rid,))
        row = cur.fetchone()
        conn.execute(f"INSERT INTO {bak} (id, env_extra, backed_up_at) VALUES (?,?,?)",
                     (rid, row[0] if row else None,
                      time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
        n += 1
    conn.commit()
    print(f"[008] 已备份 {n} 行 → {bak}")
    return bak


def _apply(conn, plan):
    changed = 0
    for rid, new_val, _size in plan["ready"]:
        cur = conn.execute(f"UPDATE {TABLE} SET env_extra = ? WHERE id = ? "
                           f"AND env_extra LIKE ?", (new_val, rid, "enc:v1:dev:%"))
        changed += int(getattr(cur, "rowcount", 0) or 0)
    conn.commit()
    return changed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="2026-11-21：agent_tasks 存量 enc:v1:dev 信封重加密（默认 dry-run）")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不改数据（默认）")
    parser.add_argument("--apply", action="store_true", help="备份后改写")
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        print("[008] --dry-run 与 --apply 不能同时给", file=sys.stderr)
        return 2

    from src.kernel import session_manager
    conn = session_manager.db

    backend = os.environ.get("DEVOPS_DB_BACKEND", "").lower()
    target = (f"mysql {os.environ.get('DEVOPS_DB_HOST', '?')}:"
              f"{os.environ.get('DEVOPS_DB_PORT', '3306')}/"
              f"{os.environ.get('DEVOPS_DB_NAME', '?')}") if backend == "mysql" else \
             os.environ.get("DEVOPS_DB_PATH") or "<session default>"
    print(f"[008] 目标库: {target}")

    rows = _rows(conn)
    plan = _plan(rows)
    print(f"[008] 扫描 dev 信封 {plan['rows']} 行；可重封 {len(plan['ready'])}；"
          f"解不开 {len(plan['undecodable'])}；重封失败 {len(plan['rewrite_failed'])}")
    print(f"[008] 按状态分布: {plan['by_status']}")
    for rid, kid, keys, has_branch in plan["samples"]:
        print(f"     sample {rid} → kid={kid} env_keys={keys} work_branch={has_branch}")

    if plan["undecodable"]:
        print(f"[008] 解不开（非 dev 密文/损坏，保持原样）: {plan['undecodable'][:10]}",
              file=sys.stderr)
    if not args.apply:
        print("[008] dry-run 结束（未改数据）。加 --apply 执行。")
        return 0
    if not plan["ready"]:
        print("[008] 无需变更")
        return 0

    _backup(conn, plan)
    n = _apply(conn, plan)
    left = _rows(conn)
    print(f"[008] 已重封 {n} 行；剩余 dev 信封 {len(left)} 行")
    if left:
        print(f"[008] 警告：仍有 {len(left)} 行 dev 信封（多为解不开/当前进程只会写 dev）",
              file=sys.stderr)
        return 1
    print("[008] 自检通过：dev 信封归零")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())