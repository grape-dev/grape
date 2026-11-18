#!/usr/bin/env bash
# Grape Docker 容器启动入口
# 职责：在 uvicorn 启动前自动完成 DB 层的"开机种子"——
#   1) seed agent_runtimes.codex（host_name 必须 = socket.gethostname()，
#      否则 resolve_local_engine 的 host_name 过滤会挡住所有本机引擎）
#   2) DB 层的 OAuth providers / meta 同步在 main.py 的 migrate_oauth_providers()
#      里已自动完成（读 /app/configs/oauth-providers.json），这里不重复。
#
# 设计原则：idempotent（幂等）——容器重启多次也不会产生脏数据。
set -euo pipefail

echo "[entrypoint] host=$(hostname)  pre-seeding DB..."

# agent_runtimes codex 种子
# 必须在 Python 侧做——SQLite + socket.gethostname()
python3 - <<'PYSEED'
import sys, os, socket, sqlite3

DB_PATH = "/app/data/sessions.db"
HN = socket.gethostname()

# 等 DB 文件出现（mount 后第一次启动时可能还没建）
import time
for _ in range(30):
    if os.path.exists(DB_PATH):
        break
    time.sleep(0.2)
else:
    print("[entrypoint] DB not ready after 6s, skip seed (will retry on next start)")
    sys.exit(0)

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# 确保 agent_runtimes 表存在（极端情况：首次启动迁移尚未完成）
try:
    c.execute("SELECT COUNT(*) FROM agent_runtimes")
except sqlite3.OperationalError:
    print("[entrypoint] agent_runtimes table not yet created, skip seed (migrate will do it later)")
    conn.close()
    sys.exit(0)

# 幂等 seed：只确保有且仅有一条 host_name=当前 hostname 的 codex 行
c.execute("SELECT id FROM agent_runtimes WHERE name='codex' AND host_name=?", (HN,))
existing = c.fetchone()
if existing:
    _rid = existing[0]
    c.execute("UPDATE agent_runtimes SET installed=1, healthy=1, status='online', host_name=? WHERE id=?", (HN, _rid))
    print(f"[entrypoint] codex runtime (id={_rid}) reset to online, host_name={HN}")
else:
    # 先清掉所有旧 codex 行（换了容器 hostname 后旧行已成孤儿）
    c.execute("DELETE FROM agent_runtimes WHERE name='codex'")
    print(f"[entrypoint] cleaned {c.rowcount} orphan codex row(s)")
    c.execute("""
        INSERT INTO agent_runtimes
          (credential_id, name, display_name, installed, healthy, scope, status,
           host_name, pip_package, max_concurrent, agent_tags, scene_tags)
        VALUES (0, 'codex', 'Codex Agent', 1, 1, 'global', 'online',
                ?, 'openai-codex', 10, '[]', '["default"]')
    """, (HN,))
    print(f"[entrypoint] codex runtime seeded, host_name={HN}")

conn.commit()
conn.close()
PYSEED

echo "[entrypoint] DB seed done, starting uvicorn..."
exec uvicorn src.api.main:app --host 0.0.0.0 --port 3000 --proxy-headers --forwarded-allow-ips='*'
