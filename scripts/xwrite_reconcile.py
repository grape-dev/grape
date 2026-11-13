#!/usr/bin/env python3
"""P3-6 跨域写对账与意图表归档。

三个域库各有一份 `grape_t_xwrite_log`（mgmt / runtime / insight，有意为之）。
本脚本**一次连三个域**做三件事：

  report    按域汇总 pending/applied/failed/dead 计数、最老 pending 年龄、死信明细
  archive   删除「applied 且最后更新早于 N 天」的行（分批，默认 dry-run）
  requeue   把指定死信重置为 failed/attempts=0（人工定位原因后手动重试）

为什么归档必须有上限：`cross_write.unique_key()` 之后每次真实调用都会新增一行
（幂等改由目标域原语负责），applied 行只增不减 → 三个月就是几十万行，
拖慢 drain 的 `status IN ('pending','failed')` 扫描。

用法：
  python scripts/xwrite_reconcile.py report
  python scripts/xwrite_reconcile.py archive --days 7            # 预演
  python scripts/xwrite_reconcile.py archive --days 7 --apply    # 执行
  python scripts/xwrite_reconcile.py requeue --domain runtime --id 12345
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

LOG_TABLE = "grape_t_xwrite_log"
DOMAINS = ("mgmt", "runtime", "insight")
STATUSES = ("pending", "applied", "failed", "dead")


def _dsn(domain: str) -> str:
    return os.environ.get(f"GRAPE_DSN_{domain.upper()}", "").strip()


def _connect(domain: str):
    """直连该域库（不做域权限拦截：对账是运维动作，只读+归档）。"""
    import pymysql
    from urllib.parse import urlparse, unquote

    dsn = _dsn(domain)
    if not dsn:
        return None
    u = urlparse(dsn)
    return pymysql.connect(host=u.hostname or "127.0.0.1", port=u.port or 3306,
                           user=unquote(u.username or ""), password=unquote(u.password or ""),
                           database=(u.path or "/").lstrip("/") or None,
                           charset="utf8mb4", connect_timeout=8, autocommit=True)


def collect(domain: str, now: int | None = None) -> dict[str, Any] | None:
    """汇总一个域的意图表；表不存在/连不上返回 None。"""
    now = int(now or time.time())
    try:
        conn = _connect(domain)
    except Exception as exc:  # noqa: BLE001
        return {"domain": domain, "error": f"connect failed: {exc}"}
    if conn is None:
        return {"domain": domain, "error": "no DSN"}
    try:
        c = conn.cursor()
        c.execute(f"SELECT status, COUNT(*) FROM {LOG_TABLE} GROUP BY status")
        counts = {str(r[0]): int(r[1]) for r in c.fetchall()}
        out: dict[str, Any] = {
            "domain": domain,
            "counts": {s: counts.get(s, 0) for s in STATUSES},
            "total": sum(counts.values()),
        }
        c.execute(f"SELECT COUNT(*), MIN(created_at) FROM {LOG_TABLE} "
                  f"WHERE status IN ('pending','failed')")
        _row = c.fetchone() or (0, None)
        n_open, oldest = _row[0], _row[1]
        out["open"] = int(n_open or 0)
        out["oldest_open_age_s"] = (now - int(oldest)) if oldest else None
        c.execute(f"SELECT action, COUNT(*) FROM {LOG_TABLE} WHERE status='applied' "
                  f"GROUP BY action ORDER BY 2 DESC LIMIT 5")
        out["top_applied"] = [{"action": r[0], "n": int(r[1])} for r in c.fetchall()]
        c.execute(f"SELECT id, action, attempts, idempotency_key, last_error FROM {LOG_TABLE} "
                  f"WHERE status='dead' ORDER BY id DESC LIMIT 10")
        out["dead_sample"] = [
            {"id": int(r[0]), "action": r[1], "attempts": int(r[2]),
             "key": r[3], "last_error": (r[4] or "")[:160]} for r in c.fetchall()]
        c.close()
        return out
    except Exception as exc:  # noqa: BLE001 - 表可能还没建
        return {"domain": domain, "error": f"query failed: {exc}"}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def archive(domain: str, days: int, apply: bool = False, batch: int = 500,
            max_batches: int = 200, now: float | None = None) -> dict[str, Any]:
    """删除 applied 且 updated_at 早于 N 天的行。默认预演，不动数据。"""
    now = time.time() if now is None else float(now)
    cutoff = int(now - days * 86400)
    res: dict[str, Any] = {"domain": domain, "cutoff": cutoff, "deleted": 0, "batches": 0}
    try:
        conn = _connect(domain)
    except Exception as exc:  # noqa: BLE001
        return {**res, "error": f"connect failed: {exc}"}
    if conn is None:
        return {**res, "error": "no DSN"}
    try:
        c = conn.cursor()
        if not apply:
            c.execute(f"SELECT COUNT(*) FROM {LOG_TABLE} WHERE status='applied' AND updated_at < %s",
                      (cutoff,))
            _cnt = c.fetchone()
            res["would_delete"] = int(_cnt[0]) if _cnt else 0
            c.close()
            return res
        for _ in range(max_batches):
            c.execute(f"DELETE FROM {LOG_TABLE} WHERE status='applied' AND updated_at < %s "
                      f"LIMIT {int(batch)}", (cutoff,))
            n = int(c.rowcount or 0)
            res["deleted"] += n
            res["batches"] += 1
            if n < batch:
                break
            time.sleep(0.05)
        c.close()
        return res
    except Exception as exc:  # noqa: BLE001
        return {**res, "error": f"archive failed: {exc}"}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def requeue(domain: str, intent_id: int) -> dict[str, Any]:
    """把一条死信重置为待重试（人工确认原因后使用）。"""
    try:
        conn = _connect(domain)
    except Exception as exc:  # noqa: BLE001
        return {"domain": domain, "error": f"connect failed: {exc}"}
    if conn is None:
        return {"domain": domain, "error": "no DSN"}
    try:
        c = conn.cursor()
        c.execute(f"UPDATE {LOG_TABLE} SET status='failed', attempts=0, next_retry_at=0, "
                  f"updated_at=? WHERE id=? AND status='dead'", (int(time.time()), int(intent_id)))
        n = int(c.rowcount or 0)
        c.close()
        return {"domain": domain, "id": intent_id, "requeued": n}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="跨域写对账 / 意图表归档")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("report")
    a = sub.add_parser("archive")
    a.add_argument("--days", type=int, default=7)
    a.add_argument("--apply", action="store_true", help="真的删除（默认只预演）")
    r = sub.add_parser("requeue")
    r.add_argument("--domain", required=True, choices=DOMAINS)
    r.add_argument("--id", type=int, required=True)
    sub.add_parser("daily")  # 定时作业：归档 + 汇总，有死信则非零退出
    args = ap.parse_args(argv)

    if args.cmd == "daily":
        days = int(os.environ.get("XWRITE_ARCHIVE_DAYS", "7") or 7)
        for d in DOMAINS:
            print(json.dumps(archive(d, days, apply=True), ensure_ascii=False))
        bad = 0
        for d in DOMAINS:
            rep = collect(d)
            print(json.dumps(rep, ensure_ascii=False))
            if rep and (rep.get("error") or rep.get("counts", {}).get("dead")):
                bad += 1
        return 1 if bad else 0
    if args.cmd == "archive":
        for d in DOMAINS:
            print(json.dumps(archive(d, args.days, apply=args.apply), ensure_ascii=False))
        return 0
    if args.cmd == "requeue":
        print(json.dumps(requeue(args.domain, args.id), ensure_ascii=False))
        return 0

    bad = 0
    for d in DOMAINS:
        rep = collect(d)
        print(json.dumps(rep, ensure_ascii=False))
        if rep and (rep.get("error") or rep.get("counts", {}).get("dead")):
            bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())