#!/usr/bin/env python3
"""Phase 1: Emergency DB fixes — cleanup duplicate tasks, reset busy_slots, fix pool_engine_ids deadlocks."""
import pymysql, json

conn = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='YOUR_DB_PASSWORD',
                       database='grape-dev-db', charset='utf8mb4', autocommit=False)
cur = conn.cursor(pymysql.cursors.DictCursor)

# ========== 1a: Clean duplicate queued tasks ==========
cur.execute("""
SELECT issue_session_id, agent_type, repo_key,
       GROUP_CONCAT(id ORDER BY created_at ASC) as task_ids,
       COUNT(*) as cnt
FROM agent_tasks
WHERE status = 'queued' AND issue_session_id != ''
GROUP BY issue_session_id, agent_type, repo_key
HAVING cnt > 1
""")
dupes = cur.fetchall()
print('=== DUPLICATE QUEUED TASKS ===')
total_deleted = 0
for d in dupes:
    ids = d['task_ids'].split(',')
    keep = ids[0]
    to_cancel = ids[1:]
    print(f'  session={d["issue_session_id"][:12]}... agent={d["agent_type"]} keep={keep} cancel={to_cancel}')
    for tid in to_cancel:
        cur.execute("UPDATE agent_tasks SET status='cancelled', result_text='duplicate cleanup' WHERE id=%s AND status='queued'", (tid,))
        if cur.rowcount:
            total_deleted += 1
            print(f'    cancelled: {tid}')
print(f'Total duplicate tasks cancelled: {total_deleted}')

# ========== 1b: Fix pool_engine_ids pointing to offline runtimes ==========
cur.execute("""
SELECT id, pool_engine_ids, issue_id, agent_type
FROM agent_tasks
WHERE status = 'queued' AND pool_engine_ids != '' AND pool_engine_ids IS NOT NULL
""")
locked = cur.fetchall()
print('\n=== POOL_ENGINE_IDS DEADLOCK FIX ===')
for t in locked:
    try:
        allowed = json.loads(t['pool_engine_ids'])
    except Exception:
        continue
    if not allowed:
        continue
    placeholders = ','.join(['%s'] * len(allowed))
    cur.execute(f"SELECT id, status FROM agent_runtimes WHERE id IN ({placeholders})", tuple(allowed))
    rt_status = {r['id']: r['status'] for r in cur.fetchall()}
    online = [rid for rid in allowed if rt_status.get(rid) == 'online']
    offline = [rid for rid in allowed if rt_status.get(rid) != 'online']

    if len(online) == 0 and len(offline) > 0:
        print(f'  task {t["id"]} ({t["agent_type"]} #{t["issue_id"]}): ALL offline {offline} -> clearing pool_engine_ids')
        cur.execute("UPDATE agent_tasks SET pool_engine_ids='' WHERE id=%s", (t['id'],))
    elif len(offline) > 0:
        print(f'  task {t["id"]} ({t["agent_type"]} #{t["issue_id"]}): removing offline {offline}, keeping online {online}')
        cur.execute("UPDATE agent_tasks SET pool_engine_ids=%s WHERE id=%s", (json.dumps(online), t['id']))

# ========== 1c: Reset busy_slots leaks ==========
cur.execute("""
SELECT r.id, r.name, r.busy_slots, r.max_concurrent,
       (SELECT COUNT(*) FROM agent_tasks t WHERE t.runtime_id = r.id AND t.status IN ('dispatched','running')) as actual_active
FROM agent_runtimes r
WHERE r.status = 'online' AND r.busy_slots > 0
""")
leaks = cur.fetchall()
print('\n=== BUSY_SLOTS RESET ===')
for r in leaks:
    actual = r['actual_active']
    busy = r['busy_slots']
    if busy > actual:
        print(f'  runtime {r["id"]} ({r["name"]}): busy_slots={busy} actual={actual} -> reset to {actual}')
        cur.execute("UPDATE agent_runtimes SET busy_slots=%s WHERE id=%s", (actual, r['id']))

# ========== 1d: Unpin tasks from stale runtimes ==========
cur.execute("SELECT id, runtime_id, issue_id, agent_type FROM agent_tasks WHERE status = 'queued' AND runtime_id > 0")
pinned = cur.fetchall()
print('\n=== PINNED TASK UNPIN ===')
for t in pinned:
    cur.execute("SELECT status, last_seen_at, worker_version FROM agent_runtimes WHERE id=%s", (t['runtime_id'],))
    rt = cur.fetchone()
    if rt and (rt['status'] != 'online' or not rt['worker_version']):
        print(f'  task {t["id"]} ({t["agent_type"]} #{t["issue_id"]}): runtime={t["runtime_id"]} stale -> unpin')
        cur.execute("UPDATE agent_tasks SET runtime_id=0 WHERE id=%s", (t['id'],))

conn.commit()
print('\n=== PHASE 1 COMPLETE ===')

# Verify
cur.execute("SELECT status, COUNT(*) as cnt FROM agent_tasks WHERE status IN ('queued','cancelled') GROUP BY status")
for r in cur.fetchall():
    print(f'  {r["status"]}: {r["cnt"]}')

cur.execute("SELECT id, busy_slots FROM agent_runtimes WHERE busy_slots > 0 AND status='online'")
rows = cur.fetchall()
if rows:
    for r in rows:
        print(f'  runtime {r["id"]}: busy_slots={r["busy_slots"]}')
else:
    print('  all online runtimes: busy_slots=0')

conn.close()
