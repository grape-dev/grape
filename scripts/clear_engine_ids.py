#!/usr/bin/env python3
"""Clear pool_engine_ids for remaining queued tasks."""
import pymysql

conn = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD',
                       database='grape-dev-db', charset='utf8mb4', autocommit=False)
cur = conn.cursor()

# Clear pool_engine_ids so any matching-tag runtime can claim
cur.execute("UPDATE agent_tasks SET pool_engine_ids = '' WHERE status = 'queued' AND pool_engine_ids != '' AND pool_engine_ids IS NOT NULL")
print(f"Rows updated: {cur.rowcount}")

# Check runtime status
cur.execute("SELECT id, name, status, scope, last_seen_at, worker_version FROM agent_runtimes WHERE id IN (76, 160, 156)")
for r in cur.fetchall():
    print(r)

conn.commit()
conn.close()
