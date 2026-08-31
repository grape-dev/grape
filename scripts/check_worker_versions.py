#!/usr/bin/env python3
"""Check worker versions."""
import pymysql, json

conn = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD',
                       database='grape-dev-db', charset='utf8mb4')
cur = conn.cursor(pymysql.cursors.DictCursor)

cur.execute("SELECT id, name, device_name, status, last_ip FROM runtime_credentials WHERE status='active' AND device_name LIKE '%worker%' ORDER BY id")
creds = cur.fetchall()
print("=== Worker credentials ===")
for r in creds:
    print(f"  cred_id={r['id']} {r['device_name']} ip={r['last_ip']}")
    cur.execute("SELECT id, name, status, worker_version FROM agent_runtimes WHERE credential_id=%s", (r['id'],))
    for rt in cur.fetchall():
        print(f"    rt={rt['id']} {rt['name']} status={rt['status']} ver={rt['worker_version']}")

# Check server HEAD
import subprocess
head = subprocess.check_output("cd /opt/grape && git rev-parse --short HEAD", shell=True).decode().strip()
print(f"\nServer HEAD: git-{head}")

# Check worker package version
import os
ver_path = "/opt/grape/data/cache/worker-package/VERSION"
if os.path.isfile(ver_path):
    with open(ver_path) as f:
        print(f"Worker package VERSION: {f.read().strip()}")
else:
    print("No worker package built yet")

conn.close()
