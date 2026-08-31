#!/usr/bin/env python3
"""Check 0001 worker status."""
import pymysql, json

conn = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD',
                       database='grape-dev-db', charset='utf8mb4')
cur = conn.cursor(pymysql.cursors.DictCursor)

cur.execute("SELECT id, name, device_name, last_ip, status, last_seen_at FROM runtime_credentials WHERE device_name LIKE '%0001%' OR name LIKE '%001%'")
print("=== 0001 credentials ===")
for r in cur.fetchall():
    print(json.dumps(r, default=str, ensure_ascii=False))

cur.execute("SELECT id, name, host_name, status, busy_slots, last_seen_at, worker_version, credential_id FROM agent_runtimes WHERE host_name LIKE '%0001%' OR host_name LIKE '%build-0001%'")
print("\n=== 0001 runtimes ===")
for r in cur.fetchall():
    print(json.dumps(r, default=str, ensure_ascii=False))

cur.execute("SELECT id, name, host_name, status, busy_slots, last_seen_at, worker_version FROM agent_runtimes WHERE credential_id=0")
print("\n=== local engines (cred=0) ===")
for r in cur.fetchall():
    print(json.dumps(r, default=str, ensure_ascii=False))

conn.close()
