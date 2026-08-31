#!/usr/bin/env python3
"""Verify fix results after restart."""
import pymysql

conn = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD',
                       database='grape-dev-db', charset='utf8mb4')
cur = conn.cursor(pymysql.cursors.DictCursor)

cur.execute("SELECT status, COUNT(*) as cnt FROM agent_tasks WHERE created_at >= CURDATE() GROUP BY status ORDER BY cnt DESC")
print("=== TASK STATUS AFTER FIX ===")
for r in cur.fetchall():
    print(f"  {r['status']}: {r['cnt']}")

cur.execute("SELECT id, issue_id, agent_type, runtime_id, pool_engine_ids, pool_tag FROM agent_tasks WHERE status = 'queued' ORDER BY created_at")
print("\n=== REMAINING QUEUED ===")
for r in cur.fetchall():
    print(f"  {r['id']} {r['agent_type']} #{r['issue_id']} rt={r['runtime_id']} tag={r['pool_tag']} engine_ids={r['pool_engine_ids']}")

cur.execute("""SELECT t.id, t.issue_id, t.agent_type, t.status, t.runtime_id, t.started_at, r.name as rt_name
FROM agent_tasks t LEFT JOIN agent_runtimes r ON t.runtime_id = r.id
WHERE t.status IN ('running','dispatched') ORDER BY t.started_at DESC""")
print("\n=== RUNNING/DISPATCHED ===")
for r in cur.fetchall():
    print(f"  {r['id']} {r['agent_type']} #{r['issue_id']} status={r['status']} rt={r['rt_name']}({r['runtime_id']}) started={str(r['started_at'])[:19]}")

cur.execute("SELECT id, name, busy_slots, max_concurrent FROM agent_runtimes WHERE status='online' AND busy_slots > 0 ORDER BY busy_slots DESC")
print("\n=== BUSY SLOTS (>0) ===")
for r in cur.fetchall():
    print(f"  {r['id']} ({r['name']}): busy={r['busy_slots']}/{r['max_concurrent']}")

# Check recent logs for claim activity
cur.execute("""SELECT seq, event_type, agent_type, issue_id, status, created_at
FROM events WHERE created_at >= CURDATE() + INTERVAL 23 HOUR ORDER BY created_at DESC LIMIT 10""")
print("\n=== RECENT EVENTS (last hour) ===")
for r in cur.fetchall():
    print(f"  seq={r['seq']} {r['event_type']} {r['agent_type']} #{r['issue_id']} {r['status']} {r['created_at']}")

conn.close()
