#!/usr/bin/env python3
"""Issue #990 MySQL 命名锁探针（只读，不改任何 schema）。

用途：在真实 MySQL/RDS 上验证 ``sync_task_session_token`` 依赖的 GET_LOCK 语义与
「专用锁连接跨次复用」的收益。生产代码路径：``src/runtime/service.py``
（``_acquire_task_token_lock`` / ``_locked_connection``）。

测什么：
1. 连接开销：每次新建连接的 GET_LOCK 周期 vs 复用一条专用连接（复用是本仓现行实现）；
2. 同名字锁互斥：N 条独立连接并发抢同一把锁，检查临界区是否真的串行；
3. 拿不到锁的超时行为：应返回 0（调用方据此 fail-closed，不轮换 token）；
4. 断链自动释放：持锁连接关闭后，另一连接应能立刻拿到锁。

用法：
    GRAPE_MYSQL_PASSWORD=... python scripts/mysql_lock_probe.py --host 127.0.0.1 --user root
    # 可选 --port 3306 --iterations 50 --contenders 8 --hold-ms 50

不传 --password 时只读环境变量，避免密码进入 shell history / 日志。
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import threading
import time

try:
    import pymysql
except ImportError:  # pragma: no cover - 提示而非 traceback
    print("pymysql is required (pip install pymysql)", file=sys.stderr)
    raise SystemExit(2)


def _connect(cfg: dict):
    return pymysql.connect(connect_timeout=8, autocommit=True, charset="utf8mb4", **cfg)


def _lock_cycle(conn, name: str, timeout: int = 2) -> float:
    t0 = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute("SELECT GET_LOCK(%s, %s)", (name, timeout))
        cur.fetchone()
        cur.execute("SELECT RELEASE_LOCK(%s)", (name,))
        cur.fetchone()
    return (time.perf_counter() - t0) * 1000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("GRAPE_MYSQL_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("GRAPE_MYSQL_PORT", "3306")))
    ap.add_argument("--user", default=os.environ.get("GRAPE_MYSQL_USER", "root"))
    ap.add_argument("--password", default=os.environ.get("GRAPE_MYSQL_PASSWORD", ""))
    ap.add_argument("--iterations", type=int, default=50)
    ap.add_argument("--contenders", type=int, default=8)
    ap.add_argument("--hold-ms", type=int, default=50)
    args = ap.parse_args()

    cfg = dict(host=args.host, port=args.port, user=args.user,
               password=args.password or os.environ.get("GRAPE_MYSQL_PASSWORD", ""))
    probe = f"grape_probe_{os.getpid()}"

    t0 = time.perf_counter()
    conn = _connect(cfg)
    connect_ms = (time.perf_counter() - t0) * 1000
    with conn.cursor() as cur:
        cur.execute("SELECT VERSION(), @@max_connections")
        version, max_conn = cur.fetchone()
    print(f"INFO version={version} max_connections={max_conn} connect_ms={connect_ms:.1f}")

    # 1) 每次新建连接
    fresh_lat = []
    for i in range(args.iterations):
        t0 = time.perf_counter()
        c = _connect(cfg)
        with c.cursor() as cur:
            cur.execute("SELECT GET_LOCK(%s, 2)", (f"{probe}_fresh_{i}",))
            cur.fetchone()
            cur.execute("SELECT RELEASE_LOCK(%s)", (f"{probe}_fresh_{i}",))
            cur.fetchone()
        c.close()
        fresh_lat.append((time.perf_counter() - t0) * 1000)

    # 2) 复用一条连接（现行实现）
    reused = _connect(cfg)
    reuse_lat = []
    for i in range(args.iterations):
        reuse_lat.append(_lock_cycle(reused, f"{probe}_reuse_{i}"))
    reused.close()

    def _stats(v):
        return (statistics.median(v), sorted(v)[int(len(v) * 0.95)], sum(v))

    f_p50, f_p95, f_total = _stats(fresh_lat)
    r_p50, r_p95, r_total = _stats(reuse_lat)
    print(f"LOAD new_conn_per_cycle n={args.iterations} p50={f_p50:.2f}ms p95={f_p95:.2f}ms total={f_total:.0f}ms")
    print(f"LOAD reused_conn       n={args.iterations} p50={r_p50:.2f}ms p95={r_p95:.2f}ms total={r_total:.0f}ms")
    saving = (1 - r_total / f_total) * 100 if f_total else 0
    print(f"RESULT reuse_saves={f_total - r_total:.0f}ms ({saving:.0f}%)")

    # 3) 同名字锁互斥
    name = f"{probe}_contend"
    spans = []
    spans_lock = threading.Lock()

    def contender(wid: int) -> None:
        c = _connect(cfg)
        with c.cursor() as cur:
            cur.execute("SELECT GET_LOCK(%s, 30)", (name,))
            got = cur.fetchone()[0]
            start = time.perf_counter()
            if got == 1:
                time.sleep(args.hold_ms / 1000)
            end = time.perf_counter()
            with spans_lock:
                spans.append((wid, got, start, end))
            if got == 1:
                cur.execute("SELECT RELEASE_LOCK(%s)", (name,))
                cur.fetchone()
        c.close()

    ts = [threading.Thread(target=contender, args=(i,)) for i in range(args.contenders)]
    t0 = time.perf_counter()
    [t.start() for t in ts]
    [t.join() for t in ts]
    wall = (time.perf_counter() - t0) * 1000
    ok = [s for s in spans if s[1] == 1]
    ordered = sorted(ok, key=lambda s: s[2])
    overlap = any(ordered[i][3] > ordered[i + 1][2] for i in range(len(ordered) - 1))
    expected = args.contenders * args.hold_ms
    print(f"LOAD same_name n={args.contenders} hold={args.hold_ms}ms wall={wall:.0f}ms "
          f"expected_serial>={expected}ms overlap={overlap} acquired={len(ok)}")
    if overlap or len(ok) != args.contenders:
        print("RESULT mutual_exclusion=FAIL")

    # 4) 超时行为（拿不到锁）
    holder, waiter = _connect(cfg), _connect(cfg)
    with holder.cursor() as cur:
        cur.execute("SELECT GET_LOCK(%s, 2)", (f"{probe}_to",))
        cur.fetchone()
    t0 = time.perf_counter()
    with waiter.cursor() as cur:
        cur.execute("SELECT GET_LOCK(%s, 1)", (f"{probe}_to",))
        got = cur.fetchone()[0]
    wait_ms = (time.perf_counter() - t0) * 1000
    with holder.cursor() as cur:
        cur.execute("SELECT RELEASE_LOCK(%s)", (f"{probe}_to",))
        cur.fetchone()
    holder.close()
    waiter.close()
    print(f"LOAD timeout got={got} wait_ms={wait_ms:.0f} (0 = fail-closed path)")

    # 5) 断链释放
    d1 = _connect(cfg)
    with d1.cursor() as cur:
        cur.execute("SELECT GET_LOCK(%s, 2)", (f"{probe}_crash",))
        cur.fetchone()
    d1.close()
    d2 = _connect(cfg)
    with d2.cursor() as cur:
        cur.execute("SELECT GET_LOCK(%s, 2)", (f"{probe}_crash",))
        reacquired = cur.fetchone()[0]
        cur.execute("SELECT RELEASE_LOCK(%s)", (f"{probe}_crash",))
        cur.fetchone()
    d2.close()
    print(f"INFO crash_release reacquired={reacquired} (1 = 断链已释放)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())