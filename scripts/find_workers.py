#!/usr/bin/env python3
"""Find worker 0006 and 0002 IPs from DB."""
import pymysql, json

conn = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD',
                       database='grape-dev-db', charset='utf8mb4')
cur = conn.cursor(pymysql.cursors.DictCursor)

cur.execute("SELECT id, name, device_name, last_ip, status, last_seen_at FROM runtime_credentials WHERE device_name LIKE '%0006%' OR device_name LIKE '%0002%' OR name LIKE '%0006%' OR name LIKE '%0002%'")
for r in cur.fetchall():
    print(json.dumps(r, default=str, ensure_ascii=False))

print("---")
cur.execute("SELECT id, name, host_name, status, last_seen_at FROM agent_runtimes WHERE host_name LIKE '%0006%' OR host_name LIKE '%0002%'")
for r in cur.fetchall():
    print(json.dumps(r, default=str, ensure_ascii=False))

print("---")
# Also search all credentials to find worker IPs
cur.execute("SELECT id, name, device_name, last_ip, status FROM runtime_credentials WHERE status='active' ORDER BY id")
for r in cur.fetchall():
    print(json.dumps(r, default=str, ensure_ascii=False))

conn.close()
