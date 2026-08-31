#!/usr/bin/env python3
"""Check #59 pipeline status."""
import pymysql

conn = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD',
                       database='grape-dev-db', charset='utf8mb4')
cur = conn.cursor(pymysql.cursors.DictCursor)

cur.execute("SELECT seq, event_type, agent_type, status, created_at FROM events WHERE issue_id = '#59' ORDER BY seq")
print("=== #59 EVENTS ===")
for r in cur.fetchall():
    print(f"  seq={r['seq']} {r['event_type']} {r['agent_type']} {r['status']} {r['created_at']}")

cur.execute("SELECT stage, owner_agent, a2a_seq, updated_at FROM issue_state WHERE issue_id = '#59'")
print("\n=== #59 STATE ===")
for r in cur.fetchall():
    print(f"  stage={r['stage']} owner={r['owner_agent']} a2a_seq={r['a2a_seq']}")

cur.execute("SELECT id, agent_type, status, runtime_id, started_at FROM agent_tasks WHERE issue_id = '#59' ORDER BY created_at")
print("\n=== #59 TASKS ===")
for r in cur.fetchall():
    print(f"  {r['id']} {r['agent_type']} {r['status']} rt={r['runtime_id']} started={str(r['started_at'])[:19]}")

conn.close()
